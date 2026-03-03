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
