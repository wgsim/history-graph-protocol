from __future__ import annotations

import os
import uuid
from pathlib import Path
from datetime import datetime, timedelta, timezone
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
    """Stale UUID4 .tmp files older than grace period are removed."""
    staging = hgp_dirs["content_dir"] / ".staging"
    old_tmp = staging / f"{uuid.uuid4()}.tmp"
    old_tmp.write_bytes(b"leftover")
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


def test_reconcile_checks_inactive_ops_for_missing_blob(hgp_dirs: dict):
    """Fix 1: inactive COMPLETED ops must still be checked for blob integrity."""
    missing_hash = "sha256:" + "b" * 64
    db, cas, rec = _setup(hgp_dirs)
    db.begin_immediate()
    db.insert_operation("inactive-op", "artifact", "agent-1", 1, "sha256:placeholder",
                        object_hash=missing_hash)
    db.commit()
    db.execute("UPDATE operations SET memory_tier = 'inactive' WHERE op_id = 'inactive-op'")
    db.commit()
    report = rec.reconcile()
    assert missing_hash in report.missing_blobs


# ── Security: M-3 staging glob too broad ─────────────────────────────────────

def test_staging_non_uuid_tmp_not_deleted(hgp_dirs: dict):
    """Reconciler must only delete UUID4-named .tmp files, not arbitrary .tmp names."""
    staging = hgp_dirs["content_dir"] / ".staging"
    # Non-UUID4 filename that should NOT be deleted even if stale
    non_uuid_tmp = staging / "exploit.tmp"
    non_uuid_tmp.write_bytes(b"should survive")
    old_time = (datetime.now() - timedelta(hours=1)).timestamp()
    os.utime(non_uuid_tmp, (old_time, old_time))

    _, cas, rec = _setup(hgp_dirs)
    rec.reconcile()
    assert non_uuid_tmp.exists(), "Non-UUID4 .tmp file must not be deleted by reconciler"


# ── Rule 5: PENDING op recovery ───────────────────────────────────────────────

def _insert_stale_pending_artifact(db, cas, file_path: str, content: bytes) -> str:
    """Helper: insert a PENDING artifact op backdated past the grace period."""
    obj_hash = cas.store(content)
    db.begin_immediate()
    seq = db.next_commit_seq()
    op_id = f"pending-artifact-{seq}"
    db.insert_operation(
        op_id=op_id, op_type="artifact", agent_id="agent-test",
        commit_seq=seq, chain_hash="sha256:" + "a" * 64,
        object_hash=obj_hash, file_path=file_path, status="PENDING",
    )
    # Backdate to exceed grace period
    old_ts = (datetime.now(timezone.utc) - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    db.execute("UPDATE operations SET created_at = ? WHERE op_id = ?", (old_ts, op_id))
    db.commit()
    return op_id


def _insert_stale_pending_invalidation(db, file_path: str) -> str:
    """Helper: insert a PENDING invalidation op backdated past the grace period."""
    db.begin_immediate()
    seq = db.next_commit_seq()
    op_id = f"pending-inv-{seq}"
    db.insert_operation(
        op_id=op_id, op_type="invalidation", agent_id="agent-test",
        commit_seq=seq, chain_hash="sha256:" + "b" * 64,
        file_path=file_path, status="PENDING",
    )
    old_ts = (datetime.now(timezone.utc) - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    db.execute("UPDATE operations SET created_at = ? WHERE op_id = ?", (old_ts, op_id))
    db.commit()
    return op_id


def test_rule5_pending_artifact_recovered(hgp_dirs: dict):
    """PENDING artifact where CAS blob exists and file matches hash → COMPLETED."""
    db, cas, rec = _setup(hgp_dirs)
    content = b"hello world"
    target = hgp_dirs["root"] / "recovered.txt"
    target.write_bytes(content)
    op_id = _insert_stale_pending_artifact(db, cas, str(target), content)

    report = rec.reconcile()

    assert report.pending_recovered == 1
    assert report.pending_stale == 0
    assert db.get_operation(op_id)["status"] == "COMPLETED"


def test_rule5_pending_artifact_stale_no_file(hgp_dirs: dict):
    """PENDING artifact where file does not exist on disk → STALE_PENDING."""
    db, cas, rec = _setup(hgp_dirs)
    content = b"missing"
    target = hgp_dirs["root"] / "ghost.txt"
    # Do NOT create the file on disk
    op_id = _insert_stale_pending_artifact(db, cas, str(target), content)

    report = rec.reconcile()

    assert report.pending_stale == 1
    assert report.pending_recovered == 0
    assert db.get_operation(op_id)["status"] == "STALE_PENDING"


def test_rule5_pending_artifact_stale_hash_mismatch(hgp_dirs: dict):
    """PENDING artifact where file exists but content differs from blob → STALE_PENDING."""
    db, cas, rec = _setup(hgp_dirs)
    content = b"original"
    target = hgp_dirs["root"] / "mutated.txt"
    target.write_bytes(b"different content")  # file exists but hash won't match
    op_id = _insert_stale_pending_artifact(db, cas, str(target), content)

    report = rec.reconcile()

    assert report.pending_stale == 1
    assert db.get_operation(op_id)["status"] == "STALE_PENDING"


def test_rule5_pending_delete_recovered(hgp_dirs: dict):
    """PENDING invalidation where file is gone → COMPLETED."""
    db, cas, rec = _setup(hgp_dirs)
    target = hgp_dirs["root"] / "deleted.txt"
    # File does NOT exist (already deleted)
    op_id = _insert_stale_pending_invalidation(db, str(target))

    report = rec.reconcile()

    assert report.pending_recovered == 1
    assert report.pending_stale == 0
    assert db.get_operation(op_id)["status"] == "COMPLETED"


def test_rule5_pending_delete_stale_file_exists(hgp_dirs: dict):
    """PENDING invalidation where file still exists → STALE_PENDING."""
    db, cas, rec = _setup(hgp_dirs)
    target = hgp_dirs["root"] / "still_there.txt"
    target.write_text("not deleted")
    op_id = _insert_stale_pending_invalidation(db, str(target))

    report = rec.reconcile()

    assert report.pending_stale == 1
    assert report.pending_recovered == 0
    assert db.get_operation(op_id)["status"] == "STALE_PENDING"


def test_rule5_pending_within_grace_skipped(hgp_dirs: dict):
    """PENDING op created recently (within grace period) must not be touched."""
    db, cas, rec = _setup(hgp_dirs)
    content = b"fresh"
    target = hgp_dirs["root"] / "fresh.txt"
    obj_hash = cas.store(content)
    db.begin_immediate()
    seq = db.next_commit_seq()
    op_id = f"fresh-{seq}"
    db.insert_operation(
        op_id=op_id, op_type="artifact", agent_id="agent-test",
        commit_seq=seq, chain_hash="sha256:" + "c" * 64,
        object_hash=obj_hash, file_path=str(target), status="PENDING",
    )
    # Do NOT backdate — leave created_at as now
    db.commit()

    report = rec.reconcile()

    assert report.pending_skipped_young == 1
    assert report.pending_recovered == 0
    assert report.pending_stale == 0
    assert db.get_operation(op_id)["status"] == "PENDING"


def test_rule5_pending_dry_run(hgp_dirs: dict):
    """dry_run=True must count pending ops but not change their status."""
    db, cas, rec = _setup(hgp_dirs)
    content = b"dry"
    target = hgp_dirs["root"] / "dry.txt"
    # File missing → would be STALE_PENDING if not dry_run
    op_id = _insert_stale_pending_artifact(db, cas, str(target), content)

    report = rec.reconcile(dry_run=True)

    assert report.pending_stale == 1
    assert db.get_operation(op_id)["status"] == "PENDING"  # NOT changed


def test_rule5_pending_no_file_path_skipped(hgp_dirs: dict):
    """PENDING op with file_path=NULL (non-file-tracking op) must be skipped."""
    db, cas, rec = _setup(hgp_dirs)
    db.begin_immediate()
    seq = db.next_commit_seq()
    op_id = f"no-fp-{seq}"
    db.insert_operation(
        op_id=op_id, op_type="artifact", agent_id="agent-test",
        commit_seq=seq, chain_hash="sha256:" + "d" * 64,
        status="PENDING",
        # file_path is None by default
    )
    old_ts = (datetime.now(timezone.utc) - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    db.execute("UPDATE operations SET created_at = ? WHERE op_id = ?", (old_ts, op_id))
    db.commit()

    report = rec.reconcile()

    assert report.pending_recovered == 0
    assert report.pending_stale == 0
    # Op stays PENDING — no file_path, not touched
    assert db.get_operation(op_id)["status"] == "PENDING"


def test_rule5_pending_move_pair_both_recovered(hgp_dirs: dict):
    """Move succeeded: PENDING invalidation (old path gone) + PENDING artifact (new path matches) → both COMPLETED."""
    db, cas, rec = _setup(hgp_dirs)
    content = b"moved content"
    old_path = hgp_dirs["root"] / "src.txt"
    new_path = hgp_dirs["root"] / "dst.txt"
    new_path.write_bytes(content)  # file now at new path
    # old_path does not exist (move succeeded)
    inv_id = _insert_stale_pending_invalidation(db, str(old_path))
    art_id = _insert_stale_pending_artifact(db, cas, str(new_path), content)

    report = rec.reconcile()

    assert report.pending_recovered == 2
    assert report.pending_stale == 0
    assert db.get_operation(inv_id)["status"] == "COMPLETED"
    assert db.get_operation(art_id)["status"] == "COMPLETED"


def _insert_completed_artifact(db, cas, file_path: str, content: bytes) -> str:
    """Helper: insert a COMPLETED artifact op (simulates a prior successful write)."""
    obj_hash = cas.store(content)
    db.begin_immediate()
    seq = db.next_commit_seq()
    op_id = f"completed-artifact-{seq}"
    db.insert_operation(
        op_id=op_id, op_type="artifact", agent_id="agent-test",
        commit_seq=seq, chain_hash="sha256:" + "e" * 64,
        object_hash=obj_hash, file_path=file_path,
    )
    db.commit()
    return op_id


def _insert_stale_pending_invalidation_linked(db, file_path: str, prior_op_id: str) -> str:
    """Helper: insert a PENDING invalidation with an invalidates edge to prior_op_id."""
    db.begin_immediate()
    seq = db.next_commit_seq()
    op_id = f"pending-inv-linked-{seq}"
    db.insert_operation(
        op_id=op_id, op_type="invalidation", agent_id="agent-test",
        commit_seq=seq, chain_hash="sha256:" + "f" * 64,
        file_path=file_path, status="PENDING",
    )
    db.insert_edge(op_id, prior_op_id, "invalidates")
    old_ts = (datetime.now(timezone.utc) - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    db.execute("UPDATE operations SET created_at = ? WHERE op_id = ?", (old_ts, op_id))
    db.commit()
    return op_id


# ── Rule 5: invalidation recovery propagates INVALIDATED to prior artifact ────


def test_rule5_delete_recovery_invalidates_prior_artifact(hgp_dirs: dict):
    """Recovered PENDING invalidation (delete) must also mark the prior artifact INVALIDATED."""
    db, cas, rec = _setup(hgp_dirs)
    content = b"to be deleted"
    target = hgp_dirs["root"] / "victim.txt"
    target.write_bytes(content)

    prior_id = _insert_completed_artifact(db, cas, str(target), content)
    # Simulate delete having succeeded on disk but DB_FINALIZE_ERROR on the invalidation
    target.unlink()
    inv_id = _insert_stale_pending_invalidation_linked(db, str(target), prior_id)

    report = rec.reconcile()

    assert report.pending_recovered == 1
    assert db.get_operation(inv_id)["status"] == "COMPLETED"
    assert db.get_operation(prior_id)["status"] == "INVALIDATED", (
        "Prior artifact must be INVALIDATED when its delete invalidation is recovered"
    )


def test_rule5_move_recovery_invalidates_prior_artifact(hgp_dirs: dict):
    """Recovered PENDING move (invalidation+artifact pair) must mark the prior old-path artifact INVALIDATED."""
    db, cas, rec = _setup(hgp_dirs)
    content = b"moving content"
    old_path = hgp_dirs["root"] / "move_src.txt"
    new_path = hgp_dirs["root"] / "move_dst.txt"
    new_path.write_bytes(content)  # move succeeded on disk
    # old_path does not exist

    prior_id = _insert_completed_artifact(db, cas, str(old_path), content)
    inv_id = _insert_stale_pending_invalidation_linked(db, str(old_path), prior_id)
    art_id = _insert_stale_pending_artifact(db, cas, str(new_path), content)

    report = rec.reconcile()

    assert report.pending_recovered == 2
    assert db.get_operation(inv_id)["status"] == "COMPLETED"
    assert db.get_operation(art_id)["status"] == "COMPLETED"
    assert db.get_operation(prior_id)["status"] == "INVALIDATED", (
        "Prior old-path artifact must be INVALIDATED after move recovery"
    )


# ── Rule 5: atomicity — partial DB failure must not leave contradictory state ─


def test_rule5_invalidation_recovery_atomic_on_target_update_failure(hgp_dirs: dict, monkeypatch):
    """If target INVALIDATED update fails after invalidation is finalized, the whole
    recovery must be rolled back so no COMPLETED/COMPLETED contradiction remains."""
    db, cas, rec = _setup(hgp_dirs)
    content = b"atomic test"
    target = hgp_dirs["root"] / "atomic_victim.txt"
    target.write_bytes(content)

    prior_id = _insert_completed_artifact(db, cas, str(target), content)
    target.unlink()  # file gone — would normally recover
    inv_id = _insert_stale_pending_invalidation_linked(db, str(target), prior_id)

    original_update = db.update_operation_status

    def _fail_on_invalidated(op_id: str, status: str) -> None:
        if status == "INVALIDATED":
            raise RuntimeError("simulated target-invalidation failure")
        original_update(op_id, status)

    monkeypatch.setattr(db, "update_operation_status", _fail_on_invalidated)

    # reconcile must not crash, but must not leave contradictory state
    rec.reconcile()

    inv_status = db.get_operation(inv_id)["status"]
    prior_status = db.get_operation(prior_id)["status"]
    assert not (inv_status == "COMPLETED" and prior_status == "COMPLETED"), (
        f"Contradiction: invalidation={inv_status}, prior={prior_status}; "
        "recovery must be atomic — either both succeed or both remain unchanged"
    )


def test_rule5_pending_move_pair_both_stale(hgp_dirs: dict):
    """Move failed: file still at old path, nothing at new path → both STALE_PENDING."""
    db, cas, rec = _setup(hgp_dirs)
    content = b"unmoved content"
    old_path = hgp_dirs["root"] / "still_src.txt"
    old_path.write_bytes(content)  # file still at old path (move failed)
    new_path = hgp_dirs["root"] / "never_dst.txt"
    # new_path does not exist
    inv_id = _insert_stale_pending_invalidation(db, str(old_path))
    art_id = _insert_stale_pending_artifact(db, cas, str(new_path), content)

    report = rec.reconcile()

    assert report.pending_stale == 2
    assert report.pending_recovered == 0
    assert db.get_operation(inv_id)["status"] == "STALE_PENDING"
    assert db.get_operation(art_id)["status"] == "STALE_PENDING"
