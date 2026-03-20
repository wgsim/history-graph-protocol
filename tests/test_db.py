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
