from __future__ import annotations

import pytest
import time
from datetime import datetime, timedelta
from hgp.db import Database
from hgp.dag import compute_chain_hash
from hgp.lease import LeaseManager
from hgp.errors import ChainStaleError, LeaseExpiredError


def _setup(hgp_dirs: dict) -> tuple[Database, LeaseManager]:
    db = Database(hgp_dirs["db_path"])
    db.initialize()
    db.begin_immediate()
    db.insert_operation("root-op", "artifact", "agent-1", 1, "sha256:abc")
    db.commit()
    mgr = LeaseManager(db)
    return db, mgr


def test_acquire_lease(hgp_dirs: dict):
    db, mgr = _setup(hgp_dirs)
    lease = mgr.acquire(agent_id="agent-1", subgraph_root_op_id="root-op", ttl_seconds=300)
    assert lease.status.value == "ACTIVE"
    assert lease.subgraph_root_op_id == "root-op"
    assert lease.chain_hash.startswith("sha256:")


def test_validate_valid_lease(hgp_dirs: dict):
    db, mgr = _setup(hgp_dirs)
    lease = mgr.acquire("agent-1", "root-op", ttl_seconds=300)
    result = mgr.validate(lease.lease_id)
    assert result["valid"] is True


def test_validate_stale_chain_hash(hgp_dirs: dict):
    db, mgr = _setup(hgp_dirs)
    lease = mgr.acquire("agent-1", "root-op", ttl_seconds=300)
    # Mutate the subgraph
    db.update_operation_status("root-op", "INVALIDATED")
    db.commit()
    result = mgr.validate(lease.lease_id)
    assert result["valid"] is False
    assert result["reason"] == "CHAIN_STALE"


def test_release_lease(hgp_dirs: dict):
    db, mgr = _setup(hgp_dirs)
    lease = mgr.acquire("agent-1", "root-op", ttl_seconds=300)
    mgr.release(lease.lease_id)
    row = db.execute("SELECT status FROM leases WHERE lease_id=?", (lease.lease_id,)).fetchone()
    assert row["status"] == "RELEASED"
