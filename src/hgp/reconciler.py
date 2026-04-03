"""Crash recovery reconciler — 5-rule deterministic consistency check."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

_STAGING_FILE_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\.tmp$"
)

from hgp.cas import CAS
from hgp.db import Database
from hgp.models import ReconcileReport

ORPHAN_GRACE_PERIOD = timedelta(minutes=15)
PENDING_GRACE_PERIOD = timedelta(minutes=5)


def _file_matches_hash(file_path: str, expected_hash: str) -> bool:
    """Return True if file_path exists and its SHA-256 matches expected_hash."""
    try:
        data = Path(file_path).read_bytes()
        computed = f"sha256:{hashlib.sha256(data).hexdigest()}"
        return computed == expected_hash
    except OSError:
        return False


class Reconciler:
    def __init__(self, db: Database, cas: CAS, content_dir: Path) -> None:
        self._db = db
        self._cas = cas
        self._staging_dir = content_dir / ".staging"

    def reconcile(self, dry_run: bool = False) -> ReconcileReport:
        report = ReconcileReport()
        now = datetime.now(timezone.utc)

        # Rules 1 & 2: COMPLETED op with missing blob → MISSING_BLOB
        # Only object_hash references a CAS blob; chain_hash is a computed digest, not a stored blob.
        completed_ops = self._db.query_operations(status="COMPLETED", include_inactive=True)
        for op in completed_ops:
            candidate = op.get("object_hash")
            if candidate and not self._cas.exists(candidate):
                report.missing_blobs.append(candidate)
                if not dry_run:
                    self._db.update_operation_status(op["op_id"], "MISSING_BLOB")

        # Rule 3: Blob with no DB reference + older than grace → ORPHAN_CANDIDATE
        for obj_hash, mtime in self._cas.list_all_blobs_with_mtime():
            if not self._db.object_referenced(obj_hash):
                if now - mtime > ORPHAN_GRACE_PERIOD:
                    report.orphan_candidates.append(obj_hash)
                    if not dry_run:
                        self._db.upsert_object_status(obj_hash, "ORPHAN_CANDIDATE")
                else:
                    report.skipped_young_blobs += 1

        # Clean stale staging files older than grace period
        if self._staging_dir.exists():
            for tmp_file in self._staging_dir.glob("*.tmp"):
                if not _STAGING_FILE_RE.fullmatch(tmp_file.name):
                    continue
                try:
                    mtime = datetime.fromtimestamp(tmp_file.stat().st_mtime, tz=timezone.utc)
                    if now - mtime > ORPHAN_GRACE_PERIOD:
                        if not dry_run:
                            tmp_file.unlink()
                        report.staging_cleaned += 1
                except FileNotFoundError:
                    pass  # Concurrent cleanup — expected, ignore
                except OSError as exc:
                    report.errors.append(f"staging cleanup error for {tmp_file.name}: {exc}")

        # Rule 5: PENDING op recovery — finalize or triage stuck PENDING ops
        pending_ops = self._db.query_operations(status="PENDING")
        for op in pending_ops:
            created_at_str = op.get("created_at", "")
            try:
                created_at = datetime.fromisoformat(
                    created_at_str.replace("Z", "+00:00")
                )
            except (ValueError, AttributeError):
                continue
            age = now - created_at
            if age <= PENDING_GRACE_PERIOD:
                report.pending_skipped_young += 1
                continue
            file_path = op.get("file_path")
            if not file_path:
                # Not a file-tracking op — skip
                continue
            op_type = op.get("op_type")
            op_id = op["op_id"]
            if op_type == "artifact":
                obj_hash = op.get("object_hash") or ""
                recovered = (
                    bool(obj_hash)
                    and self._cas.exists(obj_hash)
                    and _file_matches_hash(file_path, obj_hash)
                )
                if recovered:
                    if not dry_run:
                        self._db.finalize_operation(op_id)
                    report.pending_recovered += 1
                else:
                    if not dry_run:
                        self._db.update_operation_status(op_id, "STALE_PENDING")
                    report.pending_stale += 1
            elif op_type == "invalidation":
                file_gone = not Path(file_path).exists()
                if file_gone:
                    if not dry_run:
                        targets = self._db.get_invalidated_targets(op_id)
                        self._db.begin_immediate()
                        try:
                            self._db.finalize_operation(op_id)
                            for target_id in targets:
                                self._db.update_operation_status(target_id, "INVALIDATED")
                            self._db.commit()
                        except Exception as exc:
                            self._db.rollback()
                            report.errors.append(
                                f"invalidation recovery failed for {op_id}: {exc}"
                            )
                            continue
                    report.pending_recovered += 1
                else:
                    if not dry_run:
                        self._db.update_operation_status(op_id, "STALE_PENDING")
                    report.pending_stale += 1

        # Rule 4: Tier demotion — ops not accessed within threshold become inactive
        if not dry_run:
            report.demoted_to_inactive = self._db.demote_inactive(threshold_days=30)
            self._db.commit()
        else:
            pulse_row = self._db.execute(
                "SELECT MAX(COALESCE(last_accessed, created_at)) FROM operations"
            ).fetchone()
            if pulse_row and pulse_row[0]:
                row = self._db.execute(
                    """SELECT COUNT(*) FROM operations
                       WHERE memory_tier = 'long_term'
                         AND (julianday(?) - julianday(COALESCE(last_accessed, created_at))) > 30""",
                    (pulse_row[0],),
                ).fetchone()
                report.demoted_to_inactive = row[0] if row else 0

        return report
