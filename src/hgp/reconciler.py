"""Crash recovery reconciler — 3-rule deterministic consistency check."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from hgp.cas import CAS
from hgp.db import Database
from hgp.models import ReconcileReport

ORPHAN_GRACE_PERIOD = timedelta(minutes=15)


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
                try:
                    mtime = datetime.fromtimestamp(tmp_file.stat().st_mtime, tz=timezone.utc)
                    if now - mtime > ORPHAN_GRACE_PERIOD:
                        if not dry_run:
                            tmp_file.unlink()
                        report.staging_cleaned += 1
                except FileNotFoundError:
                    pass  # Concurrent cleanup

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
