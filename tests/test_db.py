from __future__ import annotations

import pytest
from pathlib import Path
from hgp.db import Database
from hgp.models import OpType, OpStatus


def test_schema_creation(hgp_dirs: dict):
    db = Database(hgp_dirs["db_path"])
    db.initialize()
    # Verify tables exist
    tables = db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    table_names = {row[0] for row in tables}
    assert "operations" in table_names
    assert "op_edges" in table_names
    assert "objects" in table_names
    assert "leases" in table_names
    assert "commit_counter" in table_names
    assert "git_anchors" in table_names
    assert "op_evidence" in table_names


def test_op_evidence_indexes_exist(hgp_dirs: dict):
    db = Database(hgp_dirs["db_path"])
    db.initialize()
    indexes = {row[1] for row in db.execute(
        "SELECT * FROM sqlite_master WHERE type='index'"
    ).fetchall()}
    assert "idx_evidence_citing" in indexes
    assert "idx_evidence_cited" in indexes


def test_insert_and_query_operation(hgp_dirs: dict):
    db = Database(hgp_dirs["db_path"])
    db.initialize()
    op_id = "test-op-001"
    db.insert_operation(
        op_id=op_id,
        op_type=OpType.ARTIFACT,
        agent_id="agent-1",
        commit_seq=1,
        chain_hash="sha256:abc",
    )
    op = db.get_operation(op_id)
    assert op is not None
    assert op["status"] == OpStatus.COMPLETED
    assert op["commit_seq"] == 1


def test_commit_counter_increments(hgp_dirs: dict):
    db = Database(hgp_dirs["db_path"])
    db.initialize()
    seq1 = db.next_commit_seq()
    seq2 = db.next_commit_seq()
    assert seq2 == seq1 + 1


def test_wal_mode_enabled(hgp_dirs: dict):
    db = Database(hgp_dirs["db_path"])
    db.initialize()
    row = db.execute("PRAGMA journal_mode").fetchone()
    assert row[0] == "wal"


def test_expire_leases(hgp_dirs: dict):
    """expire_leases() marks past-expiry ACTIVE leases as EXPIRED and returns count."""
    from datetime import datetime, timezone, timedelta
    db = Database(hgp_dirs["db_path"])
    db.initialize()
    db.begin_immediate()
    db.insert_operation("root", "artifact", "agent-1", 1, "sha256:r")
    db.commit()

    past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    future = (datetime.now(timezone.utc) + timedelta(seconds=300)).isoformat()
    now_ts = datetime.now(timezone.utc).isoformat()

    db.execute(
        "INSERT INTO leases (lease_id, agent_id, subgraph_root_op_id, chain_hash, issued_at, expires_at, status) "
        "VALUES ('lease-old', 'a', 'root', 'sha256:h', ?, ?, 'ACTIVE')",
        (now_ts, past),
    )
    db.execute(
        "INSERT INTO leases (lease_id, agent_id, subgraph_root_op_id, chain_hash, issued_at, expires_at, status) "
        "VALUES ('lease-new', 'a', 'root', 'sha256:h', ?, ?, 'ACTIVE')",
        (now_ts, future),
    )
    db.commit()

    count = db.expire_leases()
    db.commit()
    assert count == 1

    old_row = db.execute("SELECT status FROM leases WHERE lease_id='lease-old'").fetchone()
    new_row = db.execute("SELECT status FROM leases WHERE lease_id='lease-new'").fetchone()
    assert old_row["status"] == "EXPIRED"
    assert new_row["status"] == "ACTIVE"


def test_expire_leases_demotes_short_term_with_no_active_lease(hgp_dirs: dict):
    """expire_leases() demotes short_term root to long_term when its only active lease expires."""
    from datetime import datetime, timezone, timedelta
    db = Database(hgp_dirs["db_path"])
    db.initialize()
    db.begin_immediate()
    db.insert_operation("root-a", "artifact", "agent-1", 1, "sha256:a")
    db.insert_operation("root-b", "artifact", "agent-1", 2, "sha256:b")
    db.commit()

    past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    future = (datetime.now(timezone.utc) + timedelta(seconds=300)).isoformat()
    now_ts = datetime.now(timezone.utc).isoformat()

    # root-a: one expired lease (no active leases remain → should be demoted)
    db.execute(
        "INSERT INTO leases (lease_id, agent_id, subgraph_root_op_id, chain_hash, issued_at, expires_at, status) "
        "VALUES ('lease-a', 'agent-1', 'root-a', 'sha256:h', ?, ?, 'ACTIVE')",
        (now_ts, past),
    )
    # root-b: one expired lease + one still-active lease (should stay short_term)
    db.execute(
        "INSERT INTO leases (lease_id, agent_id, subgraph_root_op_id, chain_hash, issued_at, expires_at, status) "
        "VALUES ('lease-b1', 'agent-1', 'root-b', 'sha256:h', ?, ?, 'ACTIVE')",
        (now_ts, past),
    )
    db.execute(
        "INSERT INTO leases (lease_id, agent_id, subgraph_root_op_id, chain_hash, issued_at, expires_at, status) "
        "VALUES ('lease-b2', 'agent-1', 'root-b', 'sha256:h', ?, ?, 'ACTIVE')",
        (now_ts, future),
    )
    db.set_memory_tier("root-a", "short_term")
    db.set_memory_tier("root-b", "short_term")
    db.commit()

    db.expire_leases()
    db.commit()

    assert db.get_operation("root-a")["memory_tier"] == "long_term"  # demoted: no active lease
    assert db.get_operation("root-b")["memory_tier"] == "short_term"  # kept: still has active lease


# ── V2 Memory Tier Tests ─────────────────────────────────────


def test_memory_tier_columns_exist(hgp_dirs: dict):
    db = Database(hgp_dirs["db_path"])
    db.initialize()
    cols = {row[1] for row in db.execute("PRAGMA table_info(operations)").fetchall()}
    assert "memory_tier" in cols
    assert "access_count" in cols
    assert "last_accessed" in cols


def test_access_count_is_real(hgp_dirs: dict):
    """access_count must support fractional decay values."""
    db = Database(hgp_dirs["db_path"])
    db.initialize()
    db.begin_immediate()
    db.insert_operation("op-1", "artifact", "agent-1", 1, "sha256:x")
    db.commit()
    db.record_access("op-1", weight=0.7)
    db.commit()
    op = db.get_operation("op-1")
    assert op["access_count"] == pytest.approx(0.7)


def test_new_operation_default_tier(hgp_dirs: dict):
    db = Database(hgp_dirs["db_path"])
    db.initialize()
    db.begin_immediate()
    db.insert_operation("op-1", "artifact", "agent-1", 1, "sha256:x")
    db.commit()
    op = db.get_operation("op-1")
    assert op["memory_tier"] == "long_term"
    assert op["access_count"] == 0
    assert op["last_accessed"] is None


def test_record_access_full_weight(hgp_dirs: dict):
    db = Database(hgp_dirs["db_path"])
    db.initialize()
    db.begin_immediate()
    db.insert_operation("op-1", "artifact", "agent-1", 1, "sha256:x")
    db.commit()
    db.record_access("op-1")
    db.commit()
    op = db.get_operation("op-1")
    assert op["access_count"] == pytest.approx(1.0)
    assert op["last_accessed"] is not None


def test_record_access_low_weight_no_last_accessed(hgp_dirs: dict):
    """Depth 3+ (weight=0.1) must NOT update last_accessed."""
    db = Database(hgp_dirs["db_path"])
    db.initialize()
    db.begin_immediate()
    db.insert_operation("op-1", "artifact", "agent-1", 1, "sha256:x")
    db.commit()
    db.record_access("op-1", weight=0.1)
    db.commit()
    op = db.get_operation("op-1")
    assert op["access_count"] == pytest.approx(0.1)
    assert op["last_accessed"] is None  # NOT updated


def test_record_access_promotes_inactive(hgp_dirs: dict):
    db = Database(hgp_dirs["db_path"])
    db.initialize()
    db.begin_immediate()
    db.insert_operation("op-2", "artifact", "agent-1", 1, "sha256:x")
    db.commit()
    db.execute("UPDATE operations SET memory_tier = 'inactive' WHERE op_id = 'op-2'")
    db.commit()
    db.record_access("op-2")
    db.commit()
    assert db.get_operation("op-2")["memory_tier"] == "long_term"


def test_demote_inactive_relative_baseline(hgp_dirs: dict):
    """Ops not accessed for > threshold relative to project_pulse are demoted."""
    from datetime import datetime, timezone, timedelta
    db = Database(hgp_dirs["db_path"])
    db.initialize()
    db.begin_immediate()
    db.insert_operation("old-op", "artifact", "agent-1", 1, "sha256:x")
    db.insert_operation("new-op", "artifact", "agent-1", 2, "sha256:y")
    db.commit()
    # old-op: last_accessed 40 days ago; new-op: accessed now
    old_ts = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    now_ts = datetime.now(timezone.utc).isoformat()
    db.execute("UPDATE operations SET last_accessed = ? WHERE op_id = 'old-op'", (old_ts,))
    db.execute("UPDATE operations SET last_accessed = ? WHERE op_id = 'new-op'", (now_ts,))
    db.commit()
    count = db.demote_inactive(threshold_days=30)
    db.commit()
    assert count == 1
    assert db.get_operation("old-op")["memory_tier"] == "inactive"
    assert db.get_operation("new-op")["memory_tier"] == "long_term"


def test_demote_inactive_hibernated_project(hgp_dirs: dict):
    """If all ops are old (hibernated project), none should be demoted."""
    from datetime import datetime, timezone, timedelta
    db = Database(hgp_dirs["db_path"])
    db.initialize()
    db.begin_immediate()
    db.insert_operation("op-a", "artifact", "agent-1", 1, "sha256:x")
    db.insert_operation("op-b", "artifact", "agent-1", 2, "sha256:y")
    db.commit()
    # Both ops accessed 60 days ago — hibernated project
    old_ts = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    db.execute("UPDATE operations SET last_accessed = ? WHERE op_id IN ('op-a', 'op-b')", (old_ts,))
    db.commit()
    count = db.demote_inactive(threshold_days=30)
    # project_pulse = 60 days ago; both ops accessed at pulse → gap = 0 → not demoted
    assert count == 0


def test_query_excludes_inactive_by_default(hgp_dirs: dict):
    db = Database(hgp_dirs["db_path"])
    db.initialize()
    db.begin_immediate()
    db.insert_operation("active-op", "artifact", "agent-1", 1, "sha256:x")
    db.insert_operation("inactive-op", "artifact", "agent-1", 2, "sha256:y")
    db.commit()
    db.execute("UPDATE operations SET memory_tier = 'inactive' WHERE op_id = 'inactive-op'")
    db.commit()
    results = db.query_operations()
    ids = {r["op_id"] for r in results}
    assert "active-op" in ids
    assert "inactive-op" not in ids


def test_query_includes_inactive_when_requested(hgp_dirs: dict):
    db = Database(hgp_dirs["db_path"])
    db.initialize()
    db.begin_immediate()
    db.insert_operation("inactive-op", "artifact", "agent-1", 1, "sha256:x")
    db.commit()
    db.execute("UPDATE operations SET memory_tier = 'inactive' WHERE op_id = 'inactive-op'")
    db.commit()
    results = db.query_operations(include_inactive=True)
    ids = {r["op_id"] for r in results}
    assert "inactive-op" in ids


def test_query_tier_ordering(hgp_dirs: dict):
    """short_term ops appear before long_term in results."""
    db = Database(hgp_dirs["db_path"])
    db.initialize()
    db.begin_immediate()
    db.insert_operation("long-op", "artifact", "agent-1", 1, "sha256:x")
    db.insert_operation("short-op", "artifact", "agent-1", 2, "sha256:y")
    db.commit()
    db.set_memory_tier("short-op", "short_term")
    db.commit()
    results = db.query_operations()
    ids = [r["op_id"] for r in results]
    assert ids.index("short-op") < ids.index("long-op")


# ── V3 Evidence Trail DB Tests ────────────────────────────────

def test_insert_evidence_and_get_evidence(hgp_dirs: dict):
    from hgp.models import EvidenceRef, EvidenceRelation
    db = Database(hgp_dirs["db_path"])
    db.initialize()
    db.begin_immediate()
    db.insert_operation("citing", "artifact", "agent-1", 1, "sha256:a")
    db.insert_operation("cited", "artifact", "agent-1", 2, "sha256:b")
    db.commit()

    db.begin_immediate()
    db.insert_evidence("citing", [
        EvidenceRef(op_id="cited", relation=EvidenceRelation.SUPPORTS, inference="it works"),
    ])
    db.commit()

    rows = db.get_evidence("citing")
    assert len(rows) == 1
    assert rows[0]["cited_op_id"] == "cited"
    assert rows[0]["relation"] == "supports"
    assert rows[0]["inference"] == "it works"


def test_get_citing_ops(hgp_dirs: dict):
    from hgp.models import EvidenceRef, EvidenceRelation
    db = Database(hgp_dirs["db_path"])
    db.initialize()
    db.begin_immediate()
    db.insert_operation("citing", "artifact", "agent-1", 1, "sha256:a")
    db.insert_operation("cited", "artifact", "agent-1", 2, "sha256:b")
    db.commit()

    db.begin_immediate()
    db.insert_evidence("citing", [
        EvidenceRef(op_id="cited", relation=EvidenceRelation.CONTEXT),
    ])
    db.commit()

    rows = db.get_citing_ops("cited")
    assert len(rows) == 1
    assert rows[0]["citing_op_id"] == "citing"
    assert rows[0]["relation"] == "context"


def test_insert_evidence_self_reference_raises(hgp_dirs: dict):
    from hgp.models import EvidenceRef, EvidenceRelation
    db = Database(hgp_dirs["db_path"])
    db.initialize()
    db.begin_immediate()
    db.insert_operation("op-1", "artifact", "agent-1", 1, "sha256:a")
    db.commit()

    db.begin_immediate()
    with pytest.raises(ValueError, match="self"):
        db.insert_evidence("op-1", [
            EvidenceRef(op_id="op-1", relation=EvidenceRelation.SUPPORTS),
        ])
    db.rollback()


def test_insert_evidence_nonexistent_cited_op_raises(hgp_dirs: dict):
    from hgp.models import EvidenceRef, EvidenceRelation
    db = Database(hgp_dirs["db_path"])
    db.initialize()
    db.begin_immediate()
    db.insert_operation("citing", "artifact", "agent-1", 1, "sha256:a")
    db.commit()

    db.begin_immediate()
    with pytest.raises(ValueError, match="not found"):
        db.insert_evidence("citing", [
            EvidenceRef(op_id="ghost-op", relation=EvidenceRelation.SUPPORTS),
        ])
    db.rollback()


def test_insert_evidence_duplicate_raises(hgp_dirs: dict):
    import sqlite3
    from hgp.models import EvidenceRef, EvidenceRelation
    db = Database(hgp_dirs["db_path"])
    db.initialize()
    db.begin_immediate()
    db.insert_operation("citing", "artifact", "agent-1", 1, "sha256:a")
    db.insert_operation("cited", "artifact", "agent-1", 2, "sha256:b")
    db.commit()

    db.begin_immediate()
    db.insert_evidence("citing", [EvidenceRef(op_id="cited", relation=EvidenceRelation.SUPPORTS)])
    db.commit()

    db.begin_immediate()
    with pytest.raises(sqlite3.IntegrityError):
        db.insert_evidence("citing", [EvidenceRef(op_id="cited", relation=EvidenceRelation.REFUTES)])
    db.rollback()


def test_get_evidence_promotes_inactive_cited(hgp_dirs: dict):
    """get_evidence() with weight=0.7 on cited op promotes inactive → long_term."""
    from hgp.models import EvidenceRef, EvidenceRelation
    db = Database(hgp_dirs["db_path"])
    db.initialize()
    db.begin_immediate()
    db.insert_operation("citing", "artifact", "agent-1", 1, "sha256:a")
    db.insert_operation("cited", "artifact", "agent-1", 2, "sha256:b")
    db.commit()

    # Demote cited to inactive via the proper API
    db.set_memory_tier("cited", "inactive")
    db.commit()

    db.begin_immediate()
    db.insert_evidence("citing", [EvidenceRef(op_id="cited", relation=EvidenceRelation.SOURCE)])
    db.commit()

    db.get_evidence("citing")
    assert db.get_operation("cited")["memory_tier"] == "long_term"


def test_rollback_without_active_transaction_does_not_raise(hgp_dirs: dict):
    """rollback() in autocommit mode must not raise (mirrors commit() behavior)."""
    db = Database(hgp_dirs["db_path"])
    db.initialize()
    # No transaction opened — should be safe to call
    db.rollback()  # must not raise sqlite3.OperationalError


def test_evidence_persists_after_db_reopen(hgp_dirs: dict):
    """Evidence rows survive db.close() + new Database() at same path."""
    from hgp.models import EvidenceRef, EvidenceRelation
    db = Database(hgp_dirs["db_path"])
    db.initialize()
    db.begin_immediate()
    db.insert_operation("citing", "artifact", "agent-1", 1, "sha256:a")
    db.insert_operation("cited",  "artifact", "agent-1", 2, "sha256:b")
    db.commit()
    db.begin_immediate()
    db.insert_evidence("citing", [
        EvidenceRef(op_id="cited", relation=EvidenceRelation.METHOD, scope="s1", inference="i1"),
    ])
    db.commit()
    db.close()

    db2 = Database(hgp_dirs["db_path"])
    db2.initialize()
    rows = db2.get_evidence("citing")
    assert len(rows) == 1
    assert rows[0]["cited_op_id"] == "cited"
    assert rows[0]["scope"] == "s1"
    assert rows[0]["inference"] == "i1"


# ── V3 Third Audit Fix Tests ──────────────────────────────────

def test_insert_evidence_duplicate_cited_op_raises_clean_valueerror(hgp_dirs: dict):
    """Duplicate cited_op_id within refs list → clean ValueError before any SQL."""
    from hgp.models import EvidenceRef, EvidenceRelation
    db = Database(hgp_dirs["db_path"])
    db.initialize()
    db.begin_immediate()
    db.insert_operation("citing", "artifact", "agent-1", 1, "sha256:a")
    db.insert_operation("cited",  "artifact", "agent-1", 2, "sha256:b")
    db.commit()

    db.begin_immediate()
    with pytest.raises(ValueError, match="duplicate"):
        db.insert_evidence("citing", [
            EvidenceRef(op_id="cited", relation=EvidenceRelation.SUPPORTS),
            EvidenceRef(op_id="cited", relation=EvidenceRelation.REFUTES),
        ])
    db.rollback()


def test_get_evidence_respects_max_results(hgp_dirs: dict):
    """get_evidence(max_results=N) returns at most N rows even if more exist."""
    from hgp.models import EvidenceRef, EvidenceRelation
    db = Database(hgp_dirs["db_path"])
    db.initialize()
    db.begin_immediate()
    db.insert_operation("citing", "artifact", "agent-1", 1, "sha256:a")
    for i in range(5):
        db.insert_operation(f"cited-{i}", "artifact", "agent-1", i + 2, f"sha256:{i}")
    db.commit()
    db.begin_immediate()
    db.insert_evidence("citing", [
        EvidenceRef(op_id=f"cited-{i}", relation=EvidenceRelation.CONTEXT)
        for i in range(5)
    ])
    db.commit()

    rows = db.get_evidence("citing", max_results=3)
    assert len(rows) == 3


def test_get_citing_ops_respects_max_results(hgp_dirs: dict):
    """get_citing_ops(max_results=N) returns at most N rows even if more exist."""
    from hgp.models import EvidenceRef, EvidenceRelation
    db = Database(hgp_dirs["db_path"])
    db.initialize()
    db.begin_immediate()
    db.insert_operation("cited", "artifact", "agent-1", 1, "sha256:a")
    for i in range(5):
        db.insert_operation(f"citing-{i}", "artifact", "agent-1", i + 2, f"sha256:{i}")
    db.commit()
    for i in range(5):
        db.begin_immediate()
        db.insert_evidence(f"citing-{i}", [
            EvidenceRef(op_id="cited", relation=EvidenceRelation.SOURCE)
        ])
        db.commit()

    rows = db.get_citing_ops("cited", max_results=2)
    assert len(rows) == 2


def test_get_evidence_zero_max_results_clamped_to_one(hgp_dirs: dict):
    """max_results=0 must be clamped to 1 — LIMIT 0 returns nothing, which is wrong."""
    from hgp.models import EvidenceRef, EvidenceRelation
    db = Database(hgp_dirs["db_path"])
    db.initialize()
    db.begin_immediate()
    db.insert_operation("citing", "artifact", "agent-1", 1, "sha256:a")
    db.insert_operation("cited",  "artifact", "agent-1", 2, "sha256:b")
    db.commit()
    db.begin_immediate()
    db.insert_evidence("citing", [EvidenceRef(op_id="cited", relation=EvidenceRelation.CONTEXT)])
    db.commit()
    # max_results=0 → LIMIT 0 returns zero rows without clamping (wrong)
    # after clamping to max(1, 0) = 1 → returns the 1 existing row
    rows = db.get_evidence("citing", max_results=0)
    assert len(rows) == 1


def test_get_evidence_default_cap_enforced(hgp_dirs: dict):
    """Calling get_evidence with no max_results arg caps at _MAX_EVIDENCE_RESULTS."""
    from hgp.models import EvidenceRef, EvidenceRelation
    from hgp.db import _MAX_EVIDENCE_RESULTS
    db = Database(hgp_dirs["db_path"])
    db.initialize()
    n = _MAX_EVIDENCE_RESULTS + 1
    db.begin_immediate()
    db.insert_operation("citing", "artifact", "agent-1", 1, "sha256:a")
    for i in range(n):
        db.insert_operation(f"c{i}", "artifact", "agent-1", i + 2, f"sha256:{i}")
    db.commit()
    db.begin_immediate()
    db.insert_evidence("citing", [
        EvidenceRef(op_id=f"c{i}", relation=EvidenceRelation.CONTEXT) for i in range(n)
    ])
    db.commit()
    rows = db.get_evidence("citing")  # no max_results — uses default
    assert len(rows) == _MAX_EVIDENCE_RESULTS


def test_commit_without_active_transaction_does_not_raise(hgp_dirs: dict):
    """commit() in autocommit mode must not raise (symmetric with rollback test)."""
    db = Database(hgp_dirs["db_path"])
    db.initialize()
    db.commit()  # must not raise sqlite3.OperationalError


def test_op_evidence_schema_check_scope_length(hgp_dirs: dict):
    """DB-level CHECK(length(scope)<=1024) rejects scope > 1024 chars on fresh install."""
    import sqlite3
    db = Database(hgp_dirs["db_path"])
    db.initialize()
    db.begin_immediate()
    db.insert_operation("citing", "artifact", "agent-1", 1, "sha256:a")
    db.insert_operation("cited", "artifact", "agent-1", 2, "sha256:b")
    db.commit()

    db.begin_immediate()
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO op_evidence (citing_op_id, cited_op_id, relation, scope) VALUES (?, ?, ?, ?)",
            ("citing", "cited", "supports", "x" * 1025),
        )
    db.rollback()


def test_op_evidence_schema_check_inference_length(hgp_dirs: dict):
    """DB-level CHECK(length(inference)<=4096) rejects inference > 4096 chars on fresh install."""
    import sqlite3
    db = Database(hgp_dirs["db_path"])
    db.initialize()
    db.begin_immediate()
    db.insert_operation("citing", "artifact", "agent-1", 1, "sha256:a")
    db.insert_operation("cited", "artifact", "agent-1", 2, "sha256:b")
    db.commit()

    db.begin_immediate()
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO op_evidence (citing_op_id, cited_op_id, relation, inference) VALUES (?, ?, ?, ?)",
            ("citing", "cited", "supports", "y" * 4097),
        )
    db.rollback()


# ── V4 File Tracking Tests ────────────────────────────────────

def test_file_path_column_exists(tmp_path):
    """file_path column must exist on operations table."""
    from hgp.db import Database
    db = Database(tmp_path / "test.db")
    db.initialize()
    # LIMIT 0 query succeeds only if column exists
    db.execute("SELECT file_path FROM operations LIMIT 0").fetchone()
    db.close()


def test_insert_operation_with_file_path(tmp_path):
    from hgp.db import Database
    db = Database(tmp_path / "test.db")
    db.initialize()
    db.begin_immediate()
    seq = db.next_commit_seq()
    db.insert_operation(
        op_id="op-fp-1",
        op_type="artifact",
        agent_id="agent-1",
        commit_seq=seq,
        chain_hash="sha256:abc",
        file_path="src/main.py",
    )
    db.commit()
    row = db.execute(
        "SELECT file_path FROM operations WHERE op_id = ?", ("op-fp-1",)
    ).fetchone()
    assert row["file_path"] == "src/main.py"
    db.close()


def test_get_ops_by_file_path(tmp_path):
    from hgp.db import Database
    db = Database(tmp_path / "test.db")
    db.initialize()
    db.begin_immediate()
    for i, fp in enumerate(["a.py", "b.py", "a.py"]):
        seq = db.next_commit_seq()
        db.insert_operation(
            op_id=f"op-{i}", op_type="artifact", agent_id="agent-1",
            commit_seq=seq, chain_hash=f"sha256:{i}", file_path=fp,
        )
    db.commit()
    rows = db.get_ops_by_file_path("a.py")
    assert len(rows) == 2
    assert all(r["file_path"] == "a.py" for r in rows)
    db.close()


def test_migration_idempotent(tmp_path):
    """Calling initialize() twice must not error."""
    from hgp.db import Database
    db = Database(tmp_path / "test.db")
    db.initialize()
    db.close()
    db2 = Database(tmp_path / "test.db")
    db2.initialize()  # second open — migration already applied
    db2.execute("SELECT file_path FROM operations LIMIT 0")
    db2.close()


def test_migration_from_pre_v4_schema(tmp_path):
    """Database() must add file_path column (and its indexes) when opening a pre-V4 DB
    that has an operations table without that column. Existing rows must remain queryable."""
    import sqlite3
    db_path = tmp_path / "pre_v4.db"

    # Build a minimal pre-V4 schema by hand — operations table without file_path.
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("""
        CREATE TABLE objects (
            hash TEXT PRIMARY KEY, size INTEGER NOT NULL,
            mime_type TEXT, status TEXT NOT NULL DEFAULT 'VALID',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            gc_marked_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE operations (
            op_id        TEXT PRIMARY KEY,
            op_type      TEXT NOT NULL,
            status       TEXT NOT NULL DEFAULT 'COMPLETED',
            commit_seq   INTEGER UNIQUE,
            agent_id     TEXT NOT NULL,
            object_hash  TEXT,
            chain_hash   TEXT,
            metadata     TEXT,
            created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            completed_at TEXT,
            access_count REAL NOT NULL DEFAULT 0.0,
            last_accessed TEXT,
            memory_tier  TEXT NOT NULL DEFAULT 'long_term'
        )
    """)
    conn.execute("""
        CREATE TABLE op_edges (
            child_op_id TEXT NOT NULL, parent_op_id TEXT NOT NULL,
            edge_type TEXT NOT NULL DEFAULT 'causal',
            PRIMARY KEY (child_op_id, parent_op_id)
        )
    """)
    conn.execute("""
        CREATE TABLE leases (
            lease_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL,
            subgraph_root_op_id TEXT NOT NULL, chain_hash TEXT NOT NULL,
            issued_at TEXT NOT NULL, expires_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'ACTIVE'
        )
    """)
    conn.execute("""
        CREATE TABLE commit_counter (
            id INTEGER PRIMARY KEY CHECK (id = 1), next_seq INTEGER NOT NULL DEFAULT 1
        )
    """)
    conn.execute("INSERT OR IGNORE INTO commit_counter (id, next_seq) VALUES (1, 1)")
    conn.execute("""
        CREATE TABLE git_anchors (
            op_id TEXT NOT NULL, git_commit_sha TEXT NOT NULL,
            repository TEXT, created_at TEXT NOT NULL,
            PRIMARY KEY (op_id, git_commit_sha)
        )
    """)
    conn.execute("""
        CREATE TABLE op_evidence (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            citing_op_id TEXT NOT NULL, cited_op_id TEXT NOT NULL,
            relation TEXT NOT NULL, scope TEXT, inference TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            UNIQUE (citing_op_id, cited_op_id)
        )
    """)
    conn.execute(
        "INSERT INTO operations (op_id, op_type, agent_id, commit_seq, chain_hash) "
        "VALUES ('legacy-op-1', 'artifact', 'agent-old', 1, 'sha256:legacy')"
    )
    conn.commit()
    conn.close()

    # Verify file_path is absent before migration
    conn2 = sqlite3.connect(str(db_path))
    pre_cols = {row[1] for row in conn2.execute("PRAGMA table_info(operations)").fetchall()}
    assert "file_path" not in pre_cols, "sanity: file_path should not exist before migration"
    conn2.close()

    # Now open via Database — migration must run automatically
    from hgp.db import Database
    db = Database(db_path)
    db.initialize()

    # file_path column must now exist
    cols = {row[1] for row in db.execute("PRAGMA table_info(operations)").fetchall()}
    assert "file_path" in cols, "file_path column must exist after migration"

    # Both indexes must exist
    indexes = {row[1] for row in db.execute(
        "SELECT * FROM sqlite_master WHERE type='index'"
    ).fetchall()}
    assert "idx_operations_file_path" in indexes
    assert "idx_operations_file_path_seq" in indexes

    # Old row must still be queryable
    row = db.get_operation("legacy-op-1")
    assert row is not None
    assert row["agent_id"] == "agent-old"
    assert row["file_path"] is None  # NULL for migrated rows

    db.close()
