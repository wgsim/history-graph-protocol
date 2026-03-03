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
