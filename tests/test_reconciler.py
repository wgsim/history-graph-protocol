from __future__ import annotations

import time
from pathlib import Path
from datetime import datetime, timedelta
from hgp.db import Database
from hgp.cas import CAS
from hgp.reconciler import Reconciler


def _setup(hgp_dirs: dict) -> tuple[Database, CAS, Reconciler]:
    db = Database(hgp_dirs["db_path"])
    db.initialize()
    cas = CAS(hgp_dirs["content_dir"])
    reconciler = Reconciler(db, cas, hgp_dirs["content_dir"])
    return db, cas, reconciler


def test_rule1_2_completed_with_missing_blob(hgp_dirs: dict):
    """DB says COMPLETED but blob missing → MISSING_BLOB."""
    db, cas, rec = _setup(hgp_dirs)
    missing_hash = "sha256:" + "a" * 64
    db.begin_immediate()
    db.insert_operation("op-1", "artifact", "agent-1", 1, "sha256:placeholder", object_hash=missing_hash)
    db.commit()
    report = rec.reconcile()
    assert missing_hash in report.missing_blobs


def test_chain_hash_not_treated_as_blob(hgp_dirs: dict):
    """chain_hash is a computed SHA-256 digest, NOT a CAS blob — must not trigger MISSING_BLOB."""
    db, cas, rec = _setup(hgp_dirs)
    db.begin_immediate()
    # Insert op with only chain_hash set (no object_hash) — chain_hash must not be CAS-checked
    db.insert_operation("op-2", "artifact", "agent-1", 1, "sha256:" + "b" * 64)
    db.commit()
    report = rec.reconcile()
    assert report.missing_blobs == []


def test_rule3_orphan_blob_old(hgp_dirs: dict):
    """Blob exists with no DB ref and mtime > grace → ORPHAN_CANDIDATE."""
    db, cas, rec = _setup(hgp_dirs)
    payload = b"orphan content"
    obj_hash = cas.store(payload)
    # Mock mtime to be old
    hex_hash = obj_hash.removeprefix("sha256:")
    blob_path = hgp_dirs["content_dir"] / hex_hash[:2] / hex_hash[2:]
    old_time = (datetime.now() - timedelta(hours=1)).timestamp()
    import os; os.utime(blob_path, (old_time, old_time))

    report = rec.reconcile()
    assert obj_hash in report.orphan_candidates


def test_rule3_orphan_blob_young_skipped(hgp_dirs: dict):
    """Blob within grace period is NOT classified as orphan."""
    db, cas, rec = _setup(hgp_dirs)
    payload = b"young content"
    obj_hash = cas.store(payload)
    report = rec.reconcile()
    assert obj_hash not in report.orphan_candidates
    assert report.skipped_young_blobs >= 1


def test_staging_cleanup(hgp_dirs: dict):
    """Stale .tmp files older than grace period are removed."""
    staging = hgp_dirs["content_dir"] / ".staging"
    old_tmp = staging / "stale.tmp"
    old_tmp.write_bytes(b"leftover")
    import os
    old_time = (datetime.now() - timedelta(hours=1)).timestamp()
    os.utime(old_tmp, (old_time, old_time))

    _, cas, rec = _setup(hgp_dirs)
    report = rec.reconcile()
    assert not old_tmp.exists()
    assert report.staging_cleaned >= 1


# ── V2 Reconciler Demotion Tests ─────────────────────────────


def test_reconcile_demotes_inactive_ops(hgp_dirs: dict):
    from datetime import datetime, timezone, timedelta
    db, cas, rec = _setup(hgp_dirs)
    db.begin_immediate()
    db.insert_operation("old-op", "artifact", "agent-1", 1, "sha256:x")
    db.insert_operation("new-op", "artifact", "agent-1", 2, "sha256:y")
    db.commit()
    old_ts = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    now_ts = datetime.now(timezone.utc).isoformat()
    db.execute("UPDATE operations SET last_accessed = ? WHERE op_id = 'old-op'", (old_ts,))
    db.execute("UPDATE operations SET last_accessed = ? WHERE op_id = 'new-op'", (now_ts,))
    db.commit()
    report = rec.reconcile()
    assert report.demoted_to_inactive >= 1
    assert db.get_operation("old-op")["memory_tier"] == "inactive"
    assert db.get_operation("new-op")["memory_tier"] == "long_term"


def test_reconcile_demote_dry_run(hgp_dirs: dict):
    from datetime import datetime, timezone, timedelta
    db, cas, rec = _setup(hgp_dirs)
    db.begin_immediate()
    db.insert_operation("old-op", "artifact", "agent-1", 1, "sha256:x")
    db.insert_operation("new-op", "artifact", "agent-1", 2, "sha256:y")
    db.commit()
    old_ts = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    now_ts = datetime.now(timezone.utc).isoformat()
    db.execute("UPDATE operations SET last_accessed = ? WHERE op_id = 'old-op'", (old_ts,))
    db.execute("UPDATE operations SET last_accessed = ? WHERE op_id = 'new-op'", (now_ts,))
    db.commit()
    report = rec.reconcile(dry_run=True)
    assert report.demoted_to_inactive >= 1
    assert db.get_operation("old-op")["memory_tier"] == "long_term"  # not mutated
