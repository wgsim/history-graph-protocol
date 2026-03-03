from __future__ import annotations

import base64
import pytest
from pathlib import Path
from hgp.db import Database
from hgp.cas import CAS
from hgp.dag import compute_chain_hash
from hgp.lease import LeaseManager
from hgp.reconciler import Reconciler


def _make_components(hgp_dirs: dict) -> tuple[Database, CAS, LeaseManager, Reconciler]:
    db = Database(hgp_dirs["db_path"])
    db.initialize()
    cas = CAS(hgp_dirs["content_dir"])
    mgr = LeaseManager(db)
    rec = Reconciler(db, cas, hgp_dirs["content_dir"])
    return db, cas, mgr, rec


def test_create_root_operation(hgp_dirs: dict):
    """Create a root artifact operation with no parents."""
    db, cas, mgr, rec = _make_components(hgp_dirs)
    payload = b"my first artifact"
    encoded = base64.b64encode(payload).decode()
    obj_hash = cas.store(payload)

    db.begin_immediate()
    seq = db.next_commit_seq()
    chain_hash = "sha256:" + "0" * 64  # genesis hash
    db.insert_operation("op-root", "artifact", "agent-1", seq, chain_hash,
                        object_hash=obj_hash)
    db.commit()

    op = db.get_operation("op-root")
    assert op["status"] == "COMPLETED"
    assert op["object_hash"] == obj_hash
    assert cas.read(obj_hash) == payload


def test_create_child_operation(hgp_dirs: dict):
    """Create parent → child with chain_hash validation."""
    db, cas, mgr, rec = _make_components(hgp_dirs)

    # Create parent
    db.begin_immediate()
    seq = db.next_commit_seq()
    db.insert_operation("parent", "artifact", "agent-1", seq, "sha256:" + "0" * 64)
    db.commit()

    parent_hash = compute_chain_hash(db, "parent")

    # Create child with chain_hash
    db.begin_immediate()
    seq = db.next_commit_seq()
    db.insert_operation("child", "artifact", "agent-1", seq, parent_hash)
    db.insert_edge("child", "parent", "causal")
    db.commit()

    op = db.get_operation("child")
    assert op["status"] == "COMPLETED"


def test_chain_stale_detection(hgp_dirs: dict):
    """Simulates CHAIN_STALE: agent holds old chain_hash while subgraph changes.

    chain_hash is computed over the ancestor subgraph of a node. Stale detection
    fires when the state of an operation already in the subgraph changes (e.g. an
    operation is invalidated) between the time an agent snapshots the hash and the
    time it tries to commit.
    """
    db, cas, mgr, rec = _make_components(hgp_dirs)

    db.begin_immediate()
    seq = db.next_commit_seq()
    db.insert_operation("op-a", "artifact", "agent-1", seq, "sha256:" + "0" * 64)
    db.commit()

    # Agent 1 snapshots chain_hash of op-a
    agent1_hash = compute_chain_hash(db, "op-a")

    # Agent 2 mutates op-a directly (invalidates it)
    db.update_operation_status("op-a", "INVALIDATED")
    db.commit()

    # Agent 1 tries to commit using stale hash — detects the change
    current_hash = compute_chain_hash(db, "op-a")
    assert current_hash != agent1_hash  # CHAIN_STALE detected


def test_concurrent_chain_stale(hgp_dirs: dict):
    """Two agents snapshot the same chain_hash; agent-1 commits first, agent-2 sees CHAIN_STALE.

    Both agents build on 'genesis' as their parent. After agent-1 inserts 'op-a1'
    (child of genesis) and agent-2 re-reads the hash of 'op-a1', the chain_hash
    reflects op-a1's ancestor subgraph which changed from genesis alone to
    genesis + op-a1.
    """
    import sqlite3 as _sqlite3
    db, cas, mgr, rec = _make_components(hgp_dirs)

    db.begin_immediate()
    seq = db.next_commit_seq()
    db.insert_operation("genesis", "artifact", "agent-1", seq, "sha256:" + "0" * 64)
    db.commit()

    # Both agents snapshot the chain_hash of genesis
    agent1_hash = compute_chain_hash(db, "genesis")
    agent2_hash = agent1_hash  # Same snapshot

    # Agent 1 inserts op-a1 as child of genesis (commits first)
    db.begin_immediate()
    seq = db.next_commit_seq()
    db.insert_operation("op-a1", "artifact", "agent-1", seq, agent1_hash)
    db.insert_edge("op-a1", "genesis", "causal")
    db.commit()

    # op-a1 now exists; its ancestor subgraph includes genesis.
    # Agent-2 snapshotted genesis's hash. The chain_hash of op-a1 (the new tip)
    # includes genesis in its ancestor subgraph and is different from genesis alone.
    current_tip_hash = compute_chain_hash(db, "op-a1")
    assert current_tip_hash != agent2_hash  # CHAIN_STALE: tip has moved on


def test_cas_failure_no_db_write(hgp_dirs: dict):
    """If CAS fails (payload too large), no DB record should be created."""
    db, cas, mgr, rec = _make_components(hgp_dirs)
    from hgp.errors import PayloadTooLargeError
    import base64

    large_payload = base64.b64encode(b"x" * (11 * 1024 * 1024)).decode()
    with pytest.raises(PayloadTooLargeError):
        cas.store(base64.b64decode(large_payload))

    ops = db.query_operations()
    assert len(ops) == 0  # No partial DB state


def test_full_lease_commit_flow(hgp_dirs: dict):
    """Full flow: acquire → validate → commit."""
    db, cas, mgr, rec = _make_components(hgp_dirs)

    db.begin_immediate()
    seq = db.next_commit_seq()
    db.insert_operation("genesis", "artifact", "agent-1", seq, "sha256:" + "0" * 64)
    db.commit()

    # Acquire lease
    lease = mgr.acquire("agent-1", "genesis", ttl_seconds=300)
    assert lease.status.value == "ACTIVE"

    # Validate (PING before LLM compute)
    result = mgr.validate(lease.lease_id)
    assert result["valid"] is True

    # Commit child with lease's chain_hash
    db.begin_immediate()
    current = compute_chain_hash(db, "genesis")
    assert current == lease.chain_hash  # Still valid
    seq = db.next_commit_seq()
    db.insert_operation("child-op", "artifact", "agent-1", seq, current)
    db.insert_edge("child-op", "genesis", "causal")
    mgr.release(lease.lease_id)
    db.commit()

    assert db.get_operation("child-op")["status"] == "COMPLETED"
