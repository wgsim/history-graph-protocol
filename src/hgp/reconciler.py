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
        # Check both object_hash and chain_hash as potential CAS blob references.
        completed_ops = self._db.query_operations(status="COMPLETED")
        for op in completed_ops:
            for field in ("object_hash", "chain_hash"):
                candidate = op.get(field)
                if candidate and not self._cas.exists(candidate):
                    report.missing_blobs.append(candidate)
                    if not dry_run:
                        self._db.update_operation_status(op["op_id"], "MISSING_BLOB")
                    break  # One report per operation

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

        if not dry_run:
            self._db.commit()

        return report
