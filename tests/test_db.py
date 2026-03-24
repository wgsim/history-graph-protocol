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
        db.commit()
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
