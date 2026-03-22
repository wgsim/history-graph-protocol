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


def test_get_descendants(hgp_dirs: dict):
    """get_descendants returns all children/grandchildren including root."""
    db = _make_db(hgp_dirs)
    db.begin_immediate()
    db.insert_operation("root", "artifact", "agent-1", 1, "sha256:r")
    db.insert_operation("child", "artifact", "agent-1", 2, "sha256:c")
    db.insert_operation("grandchild", "artifact", "agent-1", 3, "sha256:g")
    db.insert_edge("child", "root")
    db.insert_edge("grandchild", "child")
    db.commit()
    descs = get_descendants(db, "root")
    assert {d["op_id"] for d in descs} == {"root", "child", "grandchild"}


def test_get_ancestors_max_depth(hgp_dirs: dict):
    """max_depth=1 limits traversal to root + its direct parent only."""
    db = _make_db(hgp_dirs)
    db.begin_immediate()
    db.insert_operation("a", "artifact", "agent-1", 1, "sha256:a")
    db.insert_operation("b", "artifact", "agent-1", 2, "sha256:b")
    db.insert_operation("c", "artifact", "agent-1", 3, "sha256:c")
    db.insert_edge("b", "a")
    db.insert_edge("c", "b")
    db.commit()
    # max_depth=1 from "c": should return c (depth 0) and b (depth 1), not a (depth 2)
    ancestors = get_ancestors(db, "c", max_depth=1)
    ids = {r["op_id"] for r in ancestors}
    assert "c" in ids
    assert "b" in ids
    assert "a" not in ids


def test_get_descendants_max_depth(hgp_dirs: dict):
    """max_depth=1 limits traversal to root + its direct children only."""
    db = _make_db(hgp_dirs)
    db.begin_immediate()
    db.insert_operation("a", "artifact", "agent-1", 1, "sha256:a")
    db.insert_operation("b", "artifact", "agent-1", 2, "sha256:b")
    db.insert_operation("c", "artifact", "agent-1", 3, "sha256:c")
    db.insert_edge("b", "a")
    db.insert_edge("c", "b")
    db.commit()
    descs = get_descendants(db, "a", max_depth=1)
    ids = {r["op_id"] for r in descs}
    assert "a" in ids
    assert "b" in ids
    assert "c" not in ids


# ── Security: C-2 DoS depth cap ─────────────────────────────────────────────

def test_compute_chain_hash_depth_cap_limits_traversal(hgp_dirs: dict):
    """compute_chain_hash must respect _max_depth to prevent DoS on deep chains.

    With a small cap, distant ancestors are excluded, producing a different hash
    than full traversal — proving the depth limit is active.
    """
    db = _make_db(hgp_dirs)
    db.begin_immediate()
    db.insert_operation("op-0", "artifact", "agent-1", 1, "sha256:" + "0" * 64)
    db.insert_operation("op-1", "artifact", "agent-1", 2, "sha256:" + "1" * 64)
    db.insert_operation("op-2", "artifact", "agent-1", 3, "sha256:" + "2" * 64)
    db.insert_operation("op-3", "artifact", "agent-1", 4, "sha256:" + "3" * 64)
    db.insert_edge("op-1", "op-0")
    db.insert_edge("op-2", "op-1")
    db.insert_edge("op-3", "op-2")
    db.commit()

    h_full = compute_chain_hash(db, "op-3", _max_depth=10)   # all 4 nodes
    h_capped = compute_chain_hash(db, "op-3", _max_depth=1)  # op-3 + op-2 only
    assert h_full != h_capped


def test_compute_chain_hash_max_depth_constant_exists(hgp_dirs: dict):
    """MAX_CHAIN_HASH_DEPTH must be exported from hgp.dag and be a reasonable bound."""
    from hgp.dag import MAX_CHAIN_HASH_DEPTH
    assert 100 <= MAX_CHAIN_HASH_DEPTH <= 2000
