from __future__ import annotations

import pytest
from pathlib import Path
from hgp.db import Database
from hgp.dag import compute_chain_hash, get_ancestors, get_descendants


def _make_db(hgp_dirs: dict) -> Database:
    db = Database(hgp_dirs["db_path"])
    db.initialize()
    return db


def test_chain_hash_single_node(hgp_dirs: dict):
    db = _make_db(hgp_dirs)
    db.begin_immediate()
    seq = db.next_commit_seq()
    db.insert_operation("op-1", "artifact", "agent-1", seq, "sha256:placeholder")
    db.commit()
    h = compute_chain_hash(db, "op-1")
    assert h.startswith("sha256:")


def test_chain_hash_includes_edges(hgp_dirs: dict):
    """Two DAGs with same nodes but different edges must have different hashes."""
    db = _make_db(hgp_dirs)
    db.begin_immediate()
    db.insert_operation("op-a", "artifact", "agent-1", 1, "sha256:aa")
    db.insert_operation("op-b", "artifact", "agent-1", 2, "sha256:bb")
    db.commit()

    # DAG 1: op-b is child of op-a (op-a → op-b)
    db.begin_immediate()
    db.insert_edge("op-b", "op-a", "causal")
    db.commit()
    hash_dag1 = compute_chain_hash(db, "op-b")

    # Remove edge and create reverse
    db.execute("DELETE FROM op_edges WHERE child_op_id='op-b' AND parent_op_id='op-a'")
    db.commit()
    db.begin_immediate()
    db.insert_edge("op-a", "op-b", "causal")
    db.commit()
    hash_dag2 = compute_chain_hash(db, "op-a")

    assert hash_dag1 != hash_dag2


def test_chain_hash_changes_on_status_change(hgp_dirs: dict):
    db = _make_db(hgp_dirs)
    db.begin_immediate()
    db.insert_operation("op-x", "artifact", "agent-1", 1, "sha256:xx")
    db.commit()
    h1 = compute_chain_hash(db, "op-x")
    db.update_operation_status("op-x", "INVALIDATED")
    db.commit()
    h2 = compute_chain_hash(db, "op-x")
    assert h1 != h2


def test_chain_hash_deterministic(hgp_dirs: dict):
    db = _make_db(hgp_dirs)
    db.begin_immediate()
    db.insert_operation("op-d", "artifact", "agent-1", 1, "sha256:dd")
    db.commit()
    h1 = compute_chain_hash(db, "op-d")
    h2 = compute_chain_hash(db, "op-d")
    assert h1 == h2


def test_chain_hash_merge_two_parents(hgp_dirs: dict):
    """Merge op: chain_hash must reflect BOTH parent branches."""
    db = _make_db(hgp_dirs)
    db.begin_immediate()
    db.insert_operation("branch-a", "artifact", "agent-1", 1, "sha256:aa")
    db.insert_operation("branch-b", "artifact", "agent-2", 2, "sha256:bb")
    db.insert_operation("merge", "merge", "agent-1", 3, "sha256:mm")
    db.insert_edge("merge", "branch-a", "causal")
    db.insert_edge("merge", "branch-b", "causal")
    db.commit()

    # Mutating branch-a should change merge's chain_hash
    h_before = compute_chain_hash(db, "merge")
    db.update_operation_status("branch-a", "INVALIDATED")
    db.commit()
    h_after = compute_chain_hash(db, "merge")
    assert h_before != h_after


def test_get_ancestors(hgp_dirs: dict):
    db = _make_db(hgp_dirs)
    db.begin_immediate()
    db.insert_operation("root", "artifact", "agent-1", 1, "sha256:r")
    db.insert_operation("mid", "artifact", "agent-1", 2, "sha256:m")
    db.insert_operation("leaf", "artifact", "agent-1", 3, "sha256:l")
    db.insert_edge("mid", "root")
    db.insert_edge("leaf", "mid")
    db.commit()
    ancestors = get_ancestors(db, "leaf")
    assert {a["op_id"] for a in ancestors} == {"leaf", "mid", "root"}
