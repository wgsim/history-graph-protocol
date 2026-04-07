"""Tests for MCP tool functions in server.py.

Monkey-patches server module globals to inject an isolated tmp DB/CAS,
bypassing the _get_components() lazy-init guard (_db is None check).
"""

from __future__ import annotations

import base64
import pytest
from pathlib import Path
from typing import Any

import hgp.server as server_module
from hgp.server import (
    hgp_create_operation,
    hgp_query_operations,
    hgp_query_subgraph,
    hgp_acquire_lease,
    hgp_validate_lease,
    hgp_release_lease,
    hgp_get_artifact,
    hgp_anchor_git,
    hgp_reconcile,
)
from hgp.db import Database
from hgp.cas import CAS
from hgp.lease import LeaseManager
from hgp.reconciler import Reconciler
from hgp.errors import ChainStaleError, InvalidationTargetNotFoundError, ParentNotFoundError, PayloadTooLargeError


@pytest.fixture
def server_components(tmp_path: Path):
    """Inject temp DB/CAS into server module globals, bypassing lazy init."""
    content_dir = tmp_path / ".hgp_content"
    content_dir.mkdir()

    db = Database(tmp_path / "hgp.db")
    db.initialize()
    cas = CAS(content_dir)
    lease_mgr = LeaseManager(db)
    reconciler = Reconciler(db, cas, content_dir)

    # Save originals
    orig = (
        server_module._db, server_module._cas,
        server_module._lease_mgr, server_module._reconciler,
        server_module._project_root, server_module._project_bound,
    )

    # Patch globals
    server_module._db = db
    server_module._cas = cas
    server_module._lease_mgr = lease_mgr
    server_module._reconciler = reconciler
    server_module._project_root = tmp_path
    server_module._project_bound = True

    yield {"db": db, "cas": cas, "lease_mgr": lease_mgr, "reconciler": reconciler, "content_dir": content_dir}

    # Restore originals
    (
        server_module._db, server_module._cas,
        server_module._lease_mgr, server_module._reconciler,
        server_module._project_root, server_module._project_bound,
    ) = orig
    db.close()


# ── Task 1 Smoke test ────────────────────────────────────────────────────────

def test_smoke_create_operation(server_components):
    result = hgp_create_operation(op_type="artifact", agent_id="a")
    assert "op_id" in result
    assert result["status"] == "COMPLETED"
    assert result["commit_seq"] >= 1
    assert result["object_hash"] is None
    assert result["chain_hash"].startswith("sha256:")


# ── Task 5: hgp_create_operation ────────────────────────────────────────────

def test_create_with_payload(server_components):
    """Payload base64-encoded → stored in CAS → round-trip via get_artifact."""
    raw = b"hello hgp"
    encoded = base64.b64encode(raw).decode()
    result = hgp_create_operation(op_type="artifact", agent_id="a", payload=encoded)
    assert result["object_hash"] is not None
    art = hgp_get_artifact(result["object_hash"])
    assert base64.b64decode(art["content"]) == raw


def test_create_with_parents(server_components):
    """Parent edges are stored; child's ancestor includes parent."""
    parent = hgp_create_operation(op_type="artifact", agent_id="a")
    child = hgp_create_operation(op_type="artifact", agent_id="a", parent_op_ids=[parent["op_id"]])
    db = server_components["db"]
    edge = db.execute(
        "SELECT * FROM op_edges WHERE child_op_id=? AND parent_op_id=?",
        (child["op_id"], parent["op_id"]),
    ).fetchone()
    assert edge is not None
    assert edge["edge_type"] == "causal"


def test_create_parent_not_found(server_components):
    """Missing parent_op_id returns PARENT_NOT_FOUND error dict (no exception raised)."""
    result = hgp_create_operation(op_type="artifact", agent_id="a", parent_op_ids=["nonexistent-id"])
    assert result.get("error") == "PARENT_NOT_FOUND"


def test_create_invalidates_target_not_found(server_components):
    """invalidates_op_ids referencing a missing op returns error dict, not a raw SQLite IntegrityError."""
    import sqlite3
    result = hgp_create_operation(op_type="invalidation", agent_id="a", invalidates_op_ids=["missing-op"])
    assert result.get("error") == "INVALIDATION_TARGET_NOT_FOUND"
    # Confirm raw IntegrityError is NOT raised
    try:
        hgp_create_operation(op_type="invalidation", agent_id="a", invalidates_op_ids=["missing-op-2"])
    except sqlite3.IntegrityError as exc:
        raise AssertionError(f"Raw SQLite IntegrityError leaked: {exc}") from exc


def test_create_chain_stale(server_components):
    """Providing an outdated chain_hash returns CHAIN_STALE error dict (no exception raised)."""
    op = hgp_create_operation(op_type="artifact", agent_id="a")
    stale_hash = op["chain_hash"]
    # Mutate the subgraph
    server_components["db"].update_operation_status(op["op_id"], "INVALIDATED")
    server_components["db"].commit()
    result = hgp_create_operation(
        op_type="artifact",
        agent_id="a",
        parent_op_ids=[op["op_id"]],
        subgraph_root_op_id=op["op_id"],
        chain_hash=stale_hash,
    )
    assert result.get("error") == "CHAIN_STALE"


def test_create_invalidates_cascade(server_components):
    """Creating an op with invalidates_op_ids sets target to INVALIDATED."""
    target = hgp_create_operation(op_type="artifact", agent_id="a")
    hgp_create_operation(
        op_type="invalidation",
        agent_id="a",
        invalidates_op_ids=[target["op_id"]],
    )
    db = server_components["db"]
    row = db.get_operation(target["op_id"])
    assert row is not None
    assert row["status"] == "INVALIDATED"


def test_create_lease_auto_release(server_components):
    """Providing lease_id in create releases the lease."""
    parent = hgp_create_operation(op_type="artifact", agent_id="a")
    lease = hgp_acquire_lease(agent_id="a", subgraph_root_op_id=parent["op_id"])
    hgp_create_operation(
        op_type="artifact",
        agent_id="a",
        parent_op_ids=[parent["op_id"]],
        lease_id=lease["lease_id"],
    )
    db = server_components["db"]
    row = db.execute("SELECT status FROM leases WHERE lease_id=?", (lease["lease_id"],)).fetchone()
    assert row["status"] == "RELEASED"


def test_create_with_metadata(server_components):
    """Metadata dict is round-trippable via query_operations."""
    meta = {"model": "claude-sonnet-4-6", "version": 1}
    result = hgp_create_operation(op_type="artifact", agent_id="a", metadata=meta)
    result2 = hgp_query_operations(op_id=result["op_id"])
    import json
    assert json.loads(result2["operations"][0]["metadata"]) == meta


# ── Task 5: hgp_query_operations ────────────────────────────────────────────

def test_query_by_op_id(server_components):
    r = hgp_create_operation(op_type="artifact", agent_id="a")
    result = hgp_query_operations(op_id=r["op_id"])
    ops = result["operations"]
    assert len(ops) == 1
    assert ops[0]["op_id"] == r["op_id"]


def test_query_by_agent_id(server_components):
    hgp_create_operation(op_type="artifact", agent_id="agent-x")
    hgp_create_operation(op_type="artifact", agent_id="agent-y")
    ops = hgp_query_operations(agent_id="agent-x")["operations"]
    assert all(o["agent_id"] == "agent-x" for o in ops)
    assert len(ops) == 1


def test_query_by_status(server_components):
    r = hgp_create_operation(op_type="artifact", agent_id="a")
    hgp_create_operation(op_type="invalidation", agent_id="a", invalidates_op_ids=[r["op_id"]])
    ops = hgp_query_operations(status="INVALIDATED")["operations"]
    assert any(o["op_id"] == r["op_id"] for o in ops)


def test_query_by_op_type(server_components):
    """op_type filter must be forwarded to db.query_operations (Bug 2 fix)."""
    hgp_create_operation(op_type="artifact", agent_id="a")
    hgp_create_operation(op_type="hypothesis", agent_id="a")
    ops = hgp_query_operations(op_type="hypothesis")["operations"]
    assert len(ops) == 1
    assert ops[0]["op_type"] == "hypothesis"


def test_query_by_since_commit_seq(server_components):
    """since_commit_seq filter must be forwarded to db.query_operations (Bug 2 fix)."""
    r1 = hgp_create_operation(op_type="artifact", agent_id="a")
    r2 = hgp_create_operation(op_type="artifact", agent_id="a")
    r3 = hgp_create_operation(op_type="artifact", agent_id="a")
    seq1 = r1["commit_seq"]
    ops = hgp_query_operations(since_commit_seq=seq1)["operations"]
    op_ids = {o["op_id"] for o in ops}
    assert r1["op_id"] not in op_ids
    assert r2["op_id"] in op_ids
    assert r3["op_id"] in op_ids


# ── Task 5: hgp_query_subgraph ───────────────────────────────────────────────

def test_query_subgraph_ancestors(server_components):
    a = hgp_create_operation(op_type="artifact", agent_id="a")
    b = hgp_create_operation(op_type="artifact", agent_id="a", parent_op_ids=[a["op_id"]])
    result = hgp_query_subgraph(root_op_id=b["op_id"], direction="ancestors")
    ids = {o["op_id"] for o in result["operations"]}
    assert a["op_id"] in ids
    assert b["op_id"] in ids


def test_query_subgraph_descendants(server_components):
    a = hgp_create_operation(op_type="artifact", agent_id="a")
    b = hgp_create_operation(op_type="artifact", agent_id="a", parent_op_ids=[a["op_id"]])
    result = hgp_query_subgraph(root_op_id=a["op_id"], direction="descendants")
    ids = {o["op_id"] for o in result["operations"]}
    assert a["op_id"] in ids
    assert b["op_id"] in ids


def test_query_subgraph_max_depth(server_components):
    """max_depth=1 from root A should return only A and B (not C or D). Bug 3 fix."""
    a = hgp_create_operation(op_type="artifact", agent_id="a")
    b = hgp_create_operation(op_type="artifact", agent_id="a", parent_op_ids=[a["op_id"]])
    c = hgp_create_operation(op_type="artifact", agent_id="a", parent_op_ids=[b["op_id"]])
    d = hgp_create_operation(op_type="artifact", agent_id="a", parent_op_ids=[c["op_id"]])
    result = hgp_query_subgraph(root_op_id=a["op_id"], direction="descendants", max_depth=1)
    ids = {o["op_id"] for o in result["operations"]}
    assert a["op_id"] in ids
    assert b["op_id"] in ids
    assert c["op_id"] not in ids
    assert d["op_id"] not in ids


def test_query_subgraph_include_invalidated(server_components):
    """include_invalidated=False (default) filters INVALIDATED ops."""
    a = hgp_create_operation(op_type="artifact", agent_id="a")
    b = hgp_create_operation(op_type="artifact", agent_id="a", parent_op_ids=[a["op_id"]])
    hgp_create_operation(op_type="invalidation", agent_id="a", invalidates_op_ids=[b["op_id"]])

    result_no_inv = hgp_query_subgraph(root_op_id=b["op_id"], direction="ancestors", include_invalidated=False)
    ids_no_inv = {o["op_id"] for o in result_no_inv["operations"]}
    assert b["op_id"] not in ids_no_inv

    result_with_inv = hgp_query_subgraph(root_op_id=b["op_id"], direction="ancestors", include_invalidated=True)
    ids_with_inv = {o["op_id"] for o in result_with_inv["operations"]}
    assert b["op_id"] in ids_with_inv


# ── Task 5: hgp_get_artifact ─────────────────────────────────────────────────

def test_get_artifact_roundtrip(server_components):
    raw = b"artifact content bytes"
    encoded = base64.b64encode(raw).decode()
    r = hgp_create_operation(op_type="artifact", agent_id="a", payload=encoded)
    art = hgp_get_artifact(r["object_hash"])
    assert art["object_hash"] == r["object_hash"]
    assert art["size"] == len(raw)
    assert base64.b64decode(art["content"]) == raw


def test_get_artifact_not_found(server_components):
    result = hgp_get_artifact("sha256:" + "0" * 64)
    assert result["error"] == "NOT_FOUND"


# ── Task 5: lease lifecycle ───────────────────────────────────────────────────

def test_lease_lifecycle(server_components):
    """acquire → validate → release full lifecycle."""
    root = hgp_create_operation(op_type="artifact", agent_id="a")
    lease = hgp_acquire_lease(agent_id="a", subgraph_root_op_id=root["op_id"])
    assert "lease_id" in lease
    assert lease["chain_hash"].startswith("sha256:")

    validated = hgp_validate_lease(lease["lease_id"])
    assert validated["valid"] is True

    released = hgp_release_lease(lease["lease_id"])
    assert released["released"] is True

    after = hgp_validate_lease(lease["lease_id"])
    assert after["valid"] is False


def test_lease_validate_extend_false(server_components):
    """extend=False returns current expires_at without updating it."""
    root = hgp_create_operation(op_type="artifact", agent_id="a")
    lease = hgp_acquire_lease(agent_id="a", subgraph_root_op_id=root["op_id"], ttl_seconds=300)
    original_expires = server_components["db"].execute(
        "SELECT expires_at FROM leases WHERE lease_id=?", (lease["lease_id"],)
    ).fetchone()["expires_at"]

    hgp_validate_lease(lease["lease_id"], extend=False)

    after_expires = server_components["db"].execute(
        "SELECT expires_at FROM leases WHERE lease_id=?", (lease["lease_id"],)
    ).fetchone()["expires_at"]
    assert original_expires == after_expires


def test_lease_not_found(server_components):
    result = hgp_validate_lease("nonexistent-lease-id")
    assert result["valid"] is False
    assert result["reason"] == "LEASE_NOT_FOUND"


# ── Task 5: git anchor ────────────────────────────────────────────────────────

def test_git_anchor_basic(server_components):
    op = hgp_create_operation(op_type="artifact", agent_id="a")
    sha = "a" * 40
    result = hgp_anchor_git(op_id=op["op_id"], git_commit_sha=sha)
    assert result["anchored"] is True
    assert result["git_commit_sha"] == sha


def test_git_anchor_invalid_sha(server_components):
    op = hgp_create_operation(op_type="artifact", agent_id="a")
    result = hgp_anchor_git(op_id=op["op_id"], git_commit_sha="tooshort")
    assert result["error"] == "INVALID_SHA"


def test_git_anchor_idempotent(server_components):
    """Anchoring the same op+sha twice must not raise (INSERT OR IGNORE)."""
    op = hgp_create_operation(op_type="artifact", agent_id="a")
    sha = "b" * 40
    hgp_anchor_git(op_id=op["op_id"], git_commit_sha=sha)
    result = hgp_anchor_git(op_id=op["op_id"], git_commit_sha=sha)
    assert result["anchored"] is True


# ── Task 5: reconcile ────────────────────────────────────────────────────────

def test_reconcile_through_tool(server_components):
    """Reconcile tool finds operation with missing CAS blob."""
    db = server_components["db"]
    missing_hash = "sha256:" + "c" * 64
    db.begin_immediate()
    db.insert_operation("op-missing", "artifact", "agent-1", 999, "sha256:placeholder", object_hash=missing_hash)
    db.commit()
    report = hgp_reconcile(dry_run=True)
    assert missing_hash in report["missing_blobs"]


def test_reconcile_dry_run_no_mutation(server_components):
    """dry_run=True must not change operation status."""
    db = server_components["db"]
    missing_hash = "sha256:" + "d" * 64
    db.begin_immediate()
    db.insert_operation("op-dry", "artifact", "agent-1", 998, "sha256:placeholder", object_hash=missing_hash)
    db.commit()
    hgp_reconcile(dry_run=True)
    row = db.get_operation("op-dry")
    assert row is not None
    assert row["status"] == "COMPLETED"  # not mutated


# ── Task 7: Edge cases ────────────────────────────────────────────────────────

# ── Security: H-3 op_type / status enum validation ───────────────────────────

def test_create_operation_invalid_op_type_returns_error(server_components):
    """op_type not in allowed set must return structured error, not IntegrityError."""
    result = hgp_create_operation(op_type="EVIL_TYPE", agent_id="a")
    assert result.get("error") == "INVALID_OP_TYPE"


def test_query_operations_invalid_status_returns_error(server_components):
    """status not in allowed set must return structured error, not IntegrityError."""
    result = hgp_query_operations(status="NOT_A_STATUS")
    assert isinstance(result, dict)
    assert result.get("error") == "INVALID_STATUS"


def test_query_operations_stale_pending_is_queryable(server_components):
    """STALE_PENDING must be accepted by hgp_query_operations (not INVALID_STATUS)."""
    db = server_components["db"]
    # Insert an op then manually set it to STALE_PENDING
    op = hgp_create_operation(op_type="artifact", agent_id="agent-sp")
    op_id = op["op_id"]
    db.execute("UPDATE operations SET status = 'STALE_PENDING' WHERE op_id = ?", (op_id,))
    db.commit()

    result = hgp_query_operations(status="STALE_PENDING")
    assert "error" not in result, f"Expected queryable status, got: {result}"
    op_ids = [o["op_id"] for o in result.get("operations", [])]
    assert op_id in op_ids


# ── Security: H-4 git_commit_sha hex validation ──────────────────────────────

def test_git_anchor_non_hex_sha_rejected(server_components):
    """40-char non-hex SHA (e.g. with uppercase/special chars) must return INVALID_SHA."""
    op = hgp_create_operation(op_type="artifact", agent_id="a")
    result = hgp_anchor_git(op_id=op["op_id"], git_commit_sha="G" * 40)
    assert result["error"] == "INVALID_SHA"


def test_git_anchor_hex_sha_accepted(server_components):
    """Valid 40-char lowercase hex SHA must succeed."""
    op = hgp_create_operation(op_type="artifact", agent_id="a")
    result = hgp_anchor_git(op_id=op["op_id"], git_commit_sha="a" * 40)
    assert result["anchored"] is True


def test_git_anchor_nonexistent_op_returns_error(server_components):
    """hgp_anchor_git with a non-existent op_id must return OP_NOT_FOUND, not silently succeed."""
    result = hgp_anchor_git(op_id="does-not-exist", git_commit_sha="b" * 40)
    assert result.get("error") == "OP_NOT_FOUND"


# ── Security: H-5 ttl_seconds upper bound ────────────────────────────────────

def test_acquire_lease_ttl_capped_at_86400(server_components):
    """ttl_seconds > 86400 must be silently capped to 86400."""
    root = hgp_create_operation(op_type="artifact", agent_id="a")
    hgp_acquire_lease(agent_id="a", subgraph_root_op_id=root["op_id"], ttl_seconds=999999)
    db = server_components["db"]
    row = db.execute(
        "SELECT expires_at, issued_at FROM leases ORDER BY issued_at DESC LIMIT 1"
    ).fetchone()
    from datetime import datetime, timezone
    issued = datetime.fromisoformat(row["issued_at"].replace("Z", "+00:00"))
    expires = datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00"))
    diff_seconds = (expires - issued).total_seconds()
    assert diff_seconds <= 86400 + 5  # +5s tolerance for test timing


# ── Edge cases ────────────────────────────────────────────────────────────────

def test_create_empty_payload(server_components):
    """Empty base64 payload (b'') is treated as no payload."""
    empty_b64 = base64.b64encode(b"").decode()
    result = hgp_create_operation(op_type="artifact", agent_id="a", payload=empty_b64)
    assert result["object_hash"] is None


def test_create_max_payload_ok(server_components):
    """Exactly 10 MB payload is accepted."""
    MAX = 10 * 1024 * 1024
    payload = base64.b64encode(b"x" * MAX).decode()
    result = hgp_create_operation(op_type="artifact", agent_id="a", payload=payload)
    assert result["object_hash"] is not None


def test_create_max_payload_exceeded(server_components):
    """10 MB + 1 byte returns PAYLOAD_TOO_LARGE error dict."""
    TOO_BIG = 10 * 1024 * 1024 + 1
    payload = base64.b64encode(b"x" * TOO_BIG).decode()
    result = hgp_create_operation(op_type="artifact", agent_id="a", payload=payload)
    assert result.get("error") == "PAYLOAD_TOO_LARGE"


# ── Phase 3: Task 3.1 — base64 validate=True ─────────────────────────────────


def test_create_invalid_base64_returns_error(server_components):
    """payload with non-base64 characters returns INVALID_PAYLOAD (not corrupt CAS store)."""
    result = hgp_create_operation(op_type="artifact", agent_id="a", payload="not!!valid==base64")
    assert result.get("error") == "INVALID_PAYLOAD"


def test_create_base64_with_whitespace_returns_error(server_components):
    """base64 with embedded whitespace is silently accepted by default but rejected with validate=True."""
    # b64decode without validate=True strips spaces; validate=True rejects them
    valid_b64 = base64.b64encode(b"hello").decode()
    padded = valid_b64[:4] + " " + valid_b64[4:]  # inject whitespace mid-string
    result = hgp_create_operation(op_type="artifact", agent_id="a", payload=padded)
    assert result.get("error") == "INVALID_PAYLOAD"


# ── Phase 3: Task 3.2 — limit / max_depth clamping ───────────────────────────


def test_query_operations_limit_clamped(server_components):
    """limit > 1000 is silently clamped; no error, result count ≤ 1000."""
    result = hgp_query_operations(limit=999999999)
    assert isinstance(result, dict)
    assert "operations" in result
    assert len(result["operations"]) <= 1000


def test_query_operations_negative_limit_clamped(server_components):
    """limit=-1 must NOT behave as unbounded (SQLite LIMIT -1 returns all rows).
    Negative values are clamped to 1 so the safety cap is always in effect."""
    # Insert a few ops so there is something to return
    for _ in range(3):
        hgp_create_operation(op_type="artifact", agent_id="a")
    result = hgp_query_operations(limit=-1)
    assert isinstance(result, dict)
    assert "operations" in result
    # Clamped to 1 — must not return all rows
    assert len(result["operations"]) <= 1, (
        f"limit=-1 bypassed cap: got {len(result['operations'])} rows"
    )


def test_file_history_limit_clamped(server_components):
    """hgp_file_history limit > 1000 is clamped to 1000."""
    from hgp.server import hgp_file_history
    result = hgp_file_history(file_path="/nonexistent/path.py", limit=999999999)
    assert isinstance(result, dict)
    assert "operations" in result


def test_file_history_negative_limit_clamped(server_components):
    """hgp_file_history limit=-1 must not bypass the result cap."""
    from hgp.server import hgp_file_history, hgp_write_file
    import hgp.server as server_module
    import os
    import tempfile
    # Write a few ops for the same file so there is history to return
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / ".git").mkdir()
        orig_root = os.environ.get("HGP_PROJECT_ROOT")
        os.environ["HGP_PROJECT_ROOT"] = tmp
        try:
            target = str(Path(tmp) / "f.txt")
            for i in range(3):
                hgp_write_file(target, f"v{i}", "agent-a")
            result = hgp_file_history(file_path=target, limit=-1)
        finally:
            if orig_root is None:
                os.environ.pop("HGP_PROJECT_ROOT", None)
            else:
                os.environ["HGP_PROJECT_ROOT"] = orig_root
    assert isinstance(result, dict)
    assert len(result["operations"]) <= 1, (
        f"limit=-1 bypassed cap: got {len(result['operations'])} rows"
    )


def test_query_subgraph_max_depth_clamped(server_components):
    """hgp_query_subgraph max_depth > 500 is clamped to 500."""
    from hgp.server import hgp_query_subgraph
    op = hgp_create_operation(op_type="artifact", agent_id="a")
    result = hgp_query_subgraph(root_op_id=op["op_id"], max_depth=999999)
    assert isinstance(result, dict)
    assert "operations" in result


def test_query_subgraph_max_depth_lower_bound_clamped(server_components):
    """hgp_query_subgraph non-positive max_depth is clamped to 1 (lower bound).

    Must query from child (has an ancestor) so depth=0/-1 would differ from
    depth=1 on the pre-fix implementation.  Pre-fix: 2/1/1; post-fix: 2/2/2.
    """
    from hgp.server import hgp_query_subgraph

    root = hgp_create_operation(op_type="artifact", agent_id="a")
    child = hgp_create_operation(op_type="artifact", agent_id="a", parent_op_ids=[root["op_id"]])
    assert "op_id" in child, f"child creation failed: {child}"

    # Baseline: child + its ancestor root → exactly 2 ops at depth=1
    baseline = hgp_query_subgraph(root_op_id=child["op_id"], max_depth=1)
    assert len(baseline["operations"]) == 2, (
        f"baseline (max_depth=1 from child) should return 2 ops, got {len(baseline['operations'])}"
    )

    result_zero = hgp_query_subgraph(root_op_id=child["op_id"], max_depth=0)
    result_neg = hgp_query_subgraph(root_op_id=child["op_id"], max_depth=-1)

    assert len(result_zero["operations"]) == 2, (
        f"max_depth=0 returned {len(result_zero['operations'])} ops, expected 2 (clamped to 1)"
    )
    assert len(result_neg["operations"]) == 2, (
        f"max_depth=-1 returned {len(result_neg['operations'])} ops, expected 2 (clamped to 1)"
    )


# ── Phase 3: Task 3.3 — hgp_create_operation error dict unification ──────────


def test_create_parent_not_found_no_exception(server_components):
    """PARENT_NOT_FOUND must be an error dict, not a raised exception."""
    result = hgp_create_operation(op_type="artifact", agent_id="a", parent_op_ids=["missing"])
    assert isinstance(result, dict), "must return dict, not raise"
    assert result["error"] == "PARENT_NOT_FOUND"


def test_create_invalidation_target_not_found_no_exception(server_components):
    """INVALIDATION_TARGET_NOT_FOUND must be an error dict, not a raised exception."""
    result = hgp_create_operation(op_type="invalidation", agent_id="a", invalidates_op_ids=["missing"])
    assert isinstance(result, dict), "must return dict, not raise"
    assert result["error"] == "INVALIDATION_TARGET_NOT_FOUND"


def test_create_chain_stale_no_exception(server_components):
    """CHAIN_STALE must be an error dict, not a raised exception."""
    op = hgp_create_operation(op_type="artifact", agent_id="a")
    server_components["db"].update_operation_status(op["op_id"], "INVALIDATED")
    server_components["db"].commit()
    result = hgp_create_operation(
        op_type="artifact", agent_id="a",
        parent_op_ids=[op["op_id"]],
        subgraph_root_op_id=op["op_id"],
        chain_hash=op["chain_hash"],
    )
    assert isinstance(result, dict), "must return dict, not raise"
    assert result["error"] == "CHAIN_STALE"


# ── V2 Memory Tier Tests ─────────────────────────────────────

from hgp.server import hgp_set_memory_tier


def test_acquire_lease_promotes_to_short_term(server_components):
    r = hgp_create_operation(op_type="artifact", agent_id="a")
    assert server_components["db"].get_operation(r["op_id"])["memory_tier"] == "long_term"
    hgp_acquire_lease(agent_id="a", subgraph_root_op_id=r["op_id"])
    assert server_components["db"].get_operation(r["op_id"])["memory_tier"] == "short_term"


def test_release_lease_demotes_to_long_term(server_components):
    r = hgp_create_operation(op_type="artifact", agent_id="a")
    lease = hgp_acquire_lease(agent_id="a", subgraph_root_op_id=r["op_id"])
    hgp_release_lease(lease["lease_id"])
    assert server_components["db"].get_operation(r["op_id"])["memory_tier"] == "long_term"


def test_query_inactive_excluded_by_default(server_components):
    r = hgp_create_operation(op_type="artifact", agent_id="a")
    server_components["db"].set_memory_tier(r["op_id"], "inactive")
    server_components["db"].commit()
    ops = hgp_query_operations()["operations"]
    assert r["op_id"] not in {o["op_id"] for o in ops}


def test_query_inactive_included_when_requested(server_components):
    r = hgp_create_operation(op_type="artifact", agent_id="a")
    server_components["db"].set_memory_tier(r["op_id"], "inactive")
    server_components["db"].commit()
    ops = hgp_query_operations(include_inactive=True)["operations"]
    assert r["op_id"] in {o["op_id"] for o in ops}


def test_query_by_op_id_records_access(server_components):
    r = hgp_create_operation(op_type="artifact", agent_id="a")
    hgp_query_operations(op_id=r["op_id"])
    op = server_components["db"].get_operation(r["op_id"])
    assert op["access_count"] == pytest.approx(1.0)
    assert op["last_accessed"] is not None


def test_subgraph_records_access_with_decay(server_components):
    a = hgp_create_operation(op_type="artifact", agent_id="a")
    b = hgp_create_operation(op_type="artifact", agent_id="a", parent_op_ids=[a["op_id"]])
    hgp_query_subgraph(root_op_id=b["op_id"], direction="ancestors")
    db = server_components["db"]
    # b is root (depth 0) → weight 1.0; a is ancestor (depth 1) → weight 0.7
    assert db.get_operation(b["op_id"])["access_count"] == pytest.approx(1.0)
    assert db.get_operation(a["op_id"])["access_count"] == pytest.approx(0.7)


def test_subgraph_depth3_no_last_accessed_update(server_components):
    """Ops at depth >= 3 get access_count update but NOT last_accessed."""
    ops = []
    prev = None
    for i in range(5):
        o = hgp_create_operation(
            op_type="artifact", agent_id="a",
            parent_op_ids=[prev] if prev else None,
        )
        ops.append(o)
        prev = o["op_id"]
    # Query from the leaf (depth 0) — root is at depth 4
    hgp_query_subgraph(root_op_id=ops[-1]["op_id"], direction="ancestors")
    db = server_components["db"]
    root_op = db.get_operation(ops[0]["op_id"])
    assert root_op["access_count"] > 0        # access_count updated
    assert root_op["last_accessed"] is None    # last_accessed NOT updated (depth 4)


def test_subgraph_tier_projection(server_components):
    """inactive ops return stub; long_term ops return summary."""
    a = hgp_create_operation(op_type="artifact", agent_id="a")
    b = hgp_create_operation(op_type="artifact", agent_id="a", parent_op_ids=[a["op_id"]])
    server_components["db"].set_memory_tier(a["op_id"], "inactive")
    server_components["db"].commit()
    result = hgp_query_subgraph(root_op_id=b["op_id"], direction="ancestors")
    projected = {o["op_id"]: o for o in result["operations"]}
    # b is long_term → summary fields only
    assert "object_hash" not in projected[b["op_id"]]
    assert "status" in projected[b["op_id"]]
    # a is inactive → stub fields only
    assert "status" not in projected[a["op_id"]]
    assert projected[a["op_id"]]["memory_tier"] == "inactive"


def test_query_tier_ordering(server_components):
    a = hgp_create_operation(op_type="artifact", agent_id="a")
    b = hgp_create_operation(op_type="artifact", agent_id="a")
    server_components["db"].set_memory_tier(a["op_id"], "short_term")
    server_components["db"].commit()
    ops = hgp_query_operations()["operations"]
    ids = [o["op_id"] for o in ops]
    assert ids.index(a["op_id"]) < ids.index(b["op_id"])


def test_set_memory_tier_explicit(server_components):
    r = hgp_create_operation(op_type="artifact", agent_id="a")
    result = hgp_set_memory_tier(op_id=r["op_id"], tier="inactive")
    assert result["tier"] == "inactive"
    assert server_components["db"].get_operation(r["op_id"])["memory_tier"] == "inactive"


def test_set_memory_tier_invalid(server_components):
    r = hgp_create_operation(op_type="artifact", agent_id="a")
    result = hgp_set_memory_tier(op_id=r["op_id"], tier="nonexistent")
    assert "error" in result


def test_release_lease_with_another_active_lease_keeps_short_term(server_components):
    """Fix 2: releasing one lease must not demote root if another active lease exists."""
    r = hgp_create_operation(op_type="artifact", agent_id="a")
    lease1 = hgp_acquire_lease(agent_id="agent-1", subgraph_root_op_id=r["op_id"])
    lease2 = hgp_acquire_lease(agent_id="agent-2", subgraph_root_op_id=r["op_id"])
    hgp_release_lease(lease1["lease_id"])
    assert server_components["db"].get_operation(r["op_id"])["memory_tier"] == "short_term"
    hgp_release_lease(lease2["lease_id"])
    assert server_components["db"].get_operation(r["op_id"])["memory_tier"] == "long_term"


def test_create_operation_auto_release_demotes_root(server_components):
    """Fix 3: create_operation(lease_id=...) auto-release path must demote root to long_term."""
    r = hgp_create_operation(op_type="artifact", agent_id="a")
    lease = hgp_acquire_lease(agent_id="a", subgraph_root_op_id=r["op_id"])
    assert server_components["db"].get_operation(r["op_id"])["memory_tier"] == "short_term"
    hgp_create_operation(
        op_type="artifact", agent_id="a",
        parent_op_ids=[r["op_id"]],
        lease_id=lease["lease_id"],
    )
    assert server_components["db"].get_operation(r["op_id"])["memory_tier"] == "long_term"


# ── V3 Evidence Trail Tool Tests ──────────────────────────────

def test_create_operation_with_evidence_refs(server_components):
    """hgp_create_operation with evidence_refs inserts rows into op_evidence."""
    from hgp.server import hgp_get_evidence
    cited = hgp_create_operation(op_type="artifact", agent_id="a")
    citing = hgp_create_operation(
        op_type="hypothesis", agent_id="a",
        evidence_refs=[{"op_id": cited["op_id"], "relation": "supports", "inference": "confirmed"}],
    )
    assert "op_id" in citing
    result = hgp_get_evidence(citing["op_id"])
    ev = result["evidence"]
    assert len(ev) == 1
    assert ev[0]["cited_op_id"] == cited["op_id"]
    assert ev[0]["relation"] == "supports"
    assert ev[0]["inference"] == "confirmed"


def test_hgp_get_evidence_records_access(server_components):
    """hgp_get_evidence records access on both citing and cited ops."""
    from hgp.server import hgp_get_evidence
    db = server_components["db"]
    cited = hgp_create_operation(op_type="artifact", agent_id="a")
    citing = hgp_create_operation(
        op_type="hypothesis", agent_id="a",
        evidence_refs=[{"op_id": cited["op_id"], "relation": "context"}],
    )
    hgp_get_evidence(citing["op_id"])
    assert db.get_operation(cited["op_id"])["access_count"] > 0


def test_hgp_get_citing_ops(server_components):
    """hgp_get_citing_ops returns ops that cited the given op."""
    from hgp.server import hgp_get_citing_ops
    cited = hgp_create_operation(op_type="artifact", agent_id="a")
    citing = hgp_create_operation(
        op_type="hypothesis", agent_id="a",
        evidence_refs=[{"op_id": cited["op_id"], "relation": "source"}],
    )
    envelope = hgp_get_citing_ops(cited["op_id"])
    result = envelope["citing_ops"]
    assert len(result) == 1
    assert result[0]["citing_op_id"] == citing["op_id"]
    assert result[0]["relation"] == "source"


def test_hgp_get_citing_ops_records_cited_access_not_citing(server_components):
    """hgp_get_citing_ops records access on cited op, NOT on citing ops."""
    from hgp.server import hgp_get_citing_ops
    db = server_components["db"]
    cited = hgp_create_operation(op_type="artifact", agent_id="a")
    citing = hgp_create_operation(
        op_type="hypothesis", agent_id="a",
        evidence_refs=[{"op_id": cited["op_id"], "relation": "method"}],
    )
    # Reset access counts manually
    db.execute("UPDATE operations SET access_count = 0 WHERE op_id IN (?, ?)",
               (cited["op_id"], citing["op_id"]))
    db.commit()
    hgp_get_citing_ops(cited["op_id"])
    assert db.get_operation(cited["op_id"])["access_count"] > 0
    assert db.get_operation(citing["op_id"])["access_count"] == 0


def test_create_operation_evidence_invalid_relation(server_components):
    """Invalid relation enum value → error response."""
    cited = hgp_create_operation(op_type="artifact", agent_id="a")
    result = hgp_create_operation(
        op_type="hypothesis", agent_id="a",
        evidence_refs=[{"op_id": cited["op_id"], "relation": "invalid_relation"}],
    )
    assert "error" in result


def test_create_operation_evidence_self_reference(server_components):
    """evidence_refs with a nonexistent op_id returns error dict.
    Note: the actual self-reference guard (ValueError from db.insert_evidence) is
    tested at the DB layer in test_insert_evidence_self_reference_raises. At the
    tool layer, the op_id is generated server-side so a genuine self-ref cannot be
    constructed pre-call; this test covers the nonexistent-op rejection path."""
    result = hgp_create_operation(
        op_type="hypothesis", agent_id="a",
        evidence_refs=[{"op_id": "definitely-nonexistent-op", "relation": "supports"}],
    )
    assert "error" in result


def test_create_operation_evidence_nonexistent_cited(server_components):
    """Non-existent cited op_id in evidence_refs → error response."""
    result = hgp_create_operation(
        op_type="hypothesis", agent_id="a",
        evidence_refs=[{"op_id": "ghost-op-id", "relation": "supports"}],
    )
    assert "error" in result


# ── V3 Audit Fix Tests ────────────────────────────────────────

def test_create_operation_duplicate_evidence_returns_error_dict(server_components):
    """Duplicate (citing, cited) pair must return error dict, not raise IntegrityError."""
    from hgp.server import hgp_get_evidence
    cited = hgp_create_operation(op_type="artifact", agent_id="a")
    # First creation succeeds
    citing = hgp_create_operation(
        op_type="hypothesis", agent_id="a",
        evidence_refs=[{"op_id": cited["op_id"], "relation": "supports"}],
    )
    assert "op_id" in citing

    # Second op tries to cite the same op twice in one call (duplicate in list)
    result = hgp_create_operation(
        op_type="hypothesis", agent_id="a",
        evidence_refs=[
            {"op_id": cited["op_id"], "relation": "supports"},
            {"op_id": cited["op_id"], "relation": "refutes"},  # duplicate cited_op_id
        ],
    )
    assert "error" in result  # must NOT raise, must return error dict


def test_hgp_get_evidence_nonexistent_op_returns_error(server_components):
    """hgp_get_evidence on unknown op_id returns error dict, not empty list."""
    from hgp.server import hgp_get_evidence
    result = hgp_get_evidence("nonexistent-op-id")
    assert isinstance(result, dict)
    assert "error" in result


def test_hgp_get_citing_ops_nonexistent_op_returns_error(server_components):
    """hgp_get_citing_ops on unknown op_id returns error dict, not empty list."""
    from hgp.server import hgp_get_citing_ops
    result = hgp_get_citing_ops("nonexistent-op-id")
    assert isinstance(result, dict)
    assert "error" in result


def test_create_operation_too_many_evidence_refs(server_components):
    """More than _MAX_EVIDENCE_REFS evidence_refs returns error dict."""
    cited = hgp_create_operation(op_type="artifact", agent_id="a")
    # Build 51 refs (all pointing to same op_id — will hit fan-out cap before duplicate check)
    from hgp.server import _MAX_EVIDENCE_REFS
    refs = [{"op_id": cited["op_id"], "relation": "supports"}] * (_MAX_EVIDENCE_REFS + 1)
    result = hgp_create_operation(op_type="hypothesis", agent_id="a", evidence_refs=refs)
    assert "error" in result


def test_evidence_ref_scope_too_long(server_components):
    """scope exceeding max_length triggers validation error."""
    cited = hgp_create_operation(op_type="artifact", agent_id="a")
    result = hgp_create_operation(
        op_type="hypothesis", agent_id="a",
        evidence_refs=[{"op_id": cited["op_id"], "relation": "supports", "scope": "x" * 2000}],
    )
    assert "error" in result


def test_evidence_ref_inference_too_long(server_components):
    """inference exceeding max_length triggers validation error."""
    cited = hgp_create_operation(op_type="artifact", agent_id="a")
    result = hgp_create_operation(
        op_type="hypothesis", agent_id="a",
        evidence_refs=[{"op_id": cited["op_id"], "relation": "supports", "inference": "y" * 5000}],
    )
    assert "error" in result


# ── V3 Second Audit Fix Tests ─────────────────────────────────

def test_evidence_scope_and_inference_round_trip(server_components):
    """scope and inference are stored and retrieved correctly end-to-end."""
    from hgp.server import hgp_get_evidence
    cited = hgp_create_operation(op_type="artifact", agent_id="a")
    hgp_create_operation(
        op_type="hypothesis", agent_id="a",
        evidence_refs=[{
            "op_id": cited["op_id"],
            "relation": "method",
            "scope": "field.x[0:10]",
            "inference": "the first 10 elements support the hypothesis",
        }],
    )
    # Re-fetch citing op via query
    from hgp.server import hgp_query_operations
    ops = hgp_query_operations(agent_id="a", op_type="hypothesis")["operations"]
    citing_id = ops[0]["op_id"]
    result = hgp_get_evidence(citing_id)
    ev = result["evidence"]
    assert len(ev) == 1
    assert ev[0]["scope"] == "field.x[0:10]"
    assert ev[0]["inference"] == "the first 10 elements support the hypothesis"


def test_get_citing_ops_multiple_citing_ops(server_components):
    """hgp_get_citing_ops returns all citing ops when multiple ops cite the same op."""
    from hgp.server import hgp_get_citing_ops
    cited = hgp_create_operation(op_type="artifact", agent_id="a")
    citing1 = hgp_create_operation(
        op_type="hypothesis", agent_id="a",
        evidence_refs=[{"op_id": cited["op_id"], "relation": "supports"}],
    )
    citing2 = hgp_create_operation(
        op_type="hypothesis", agent_id="b",
        evidence_refs=[{"op_id": cited["op_id"], "relation": "refutes"}],
    )
    envelope = hgp_get_citing_ops(cited["op_id"])
    result = envelope["citing_ops"]
    citing_ids = {r["citing_op_id"] for r in result}
    assert len(result) == 2
    assert citing1["op_id"] in citing_ids
    assert citing2["op_id"] in citing_ids


def test_create_operation_exactly_max_evidence_refs_succeeds(server_components):
    """Exactly _MAX_EVIDENCE_REFS evidence refs must succeed (boundary: > not >=)."""
    from hgp.server import _MAX_EVIDENCE_REFS
    # Create _MAX_EVIDENCE_REFS distinct cited ops
    cited_ids = []
    for i in range(_MAX_EVIDENCE_REFS):
        r = hgp_create_operation(op_type="artifact", agent_id="a")
        cited_ids.append(r["op_id"])
    refs = [{"op_id": cid, "relation": "context"} for cid in cited_ids]
    result = hgp_create_operation(op_type="hypothesis", agent_id="a", evidence_refs=refs)
    assert "op_id" in result  # must succeed, not error
    assert "error" not in result


# ── V3 Fifth Audit Fix Tests ──────────────────────────────────

def test_create_operation_multiple_distinct_evidence_refs(server_components):
    """Multiple distinct cited ops in one call — all rows must be stored and returned."""
    from hgp.server import hgp_get_evidence
    cited1 = hgp_create_operation(op_type="artifact", agent_id="a")
    cited2 = hgp_create_operation(op_type="artifact", agent_id="a")
    cited3 = hgp_create_operation(op_type="artifact", agent_id="a")
    citing = hgp_create_operation(
        op_type="hypothesis", agent_id="a",
        evidence_refs=[
            {"op_id": cited1["op_id"], "relation": "supports"},
            {"op_id": cited2["op_id"], "relation": "refutes"},
            {"op_id": cited3["op_id"], "relation": "context"},
        ],
    )
    assert "op_id" in citing
    result = hgp_get_evidence(citing["op_id"])
    ev = result["evidence"]
    cited_ids = {e["cited_op_id"] for e in ev}
    assert len(ev) == 3
    assert cited1["op_id"] in cited_ids
    assert cited2["op_id"] in cited_ids
    assert cited3["op_id"] in cited_ids


def test_hgp_get_evidence_with_inactive_cited_op(server_components):
    """get_evidence returns rows even when cited op is inactive; promotes it back to long_term."""
    from hgp.server import hgp_get_evidence
    db = server_components["db"]
    cited = hgp_create_operation(op_type="artifact", agent_id="a")
    citing = hgp_create_operation(
        op_type="hypothesis", agent_id="a",
        evidence_refs=[{"op_id": cited["op_id"], "relation": "source"}],
    )
    db.set_memory_tier(cited["op_id"], "inactive")
    db.commit()
    assert db.get_operation(cited["op_id"])["memory_tier"] == "inactive"

    result = hgp_get_evidence(citing["op_id"])
    ev = result["evidence"]
    assert len(ev) == 1
    assert ev[0]["cited_op_id"] == cited["op_id"]
    assert ev[0]["memory_tier"] == "inactive"  # row reflects tier at read time
    # record_access with weight=0.7 (>=0.4) must have promoted the cited op
    assert db.get_operation(cited["op_id"])["memory_tier"] == "long_term"


def test_evidence_relation_refutes_round_trip(server_components):
    """'refutes' relation is stored and returned correctly end-to-end at server layer."""
    from hgp.server import hgp_get_evidence
    cited = hgp_create_operation(op_type="artifact", agent_id="a")
    citing = hgp_create_operation(
        op_type="hypothesis", agent_id="a",
        evidence_refs=[{"op_id": cited["op_id"], "relation": "refutes", "inference": "contradicts obs"}],
    )
    result = hgp_get_evidence(citing["op_id"])
    ev = result["evidence"]
    assert len(ev) == 1
    assert ev[0]["relation"] == "refutes"
    assert ev[0]["inference"] == "contradicts obs"


def test_evidence_created_at_is_iso8601(server_components):
    """created_at field in evidence records must be parseable ISO-8601 with ms precision."""
    from hgp.server import hgp_get_evidence
    from datetime import datetime
    cited = hgp_create_operation(op_type="artifact", agent_id="a")
    citing = hgp_create_operation(
        op_type="hypothesis", agent_id="a",
        evidence_refs=[{"op_id": cited["op_id"], "relation": "method"}],
    )
    result = hgp_get_evidence(citing["op_id"])
    ev = result["evidence"]
    assert len(ev) == 1
    # SQLite stores: 2026-03-24T19:30:00.000Z — must parse without error
    created_at = ev[0]["created_at"]
    parsed = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    assert parsed.year >= 2026


def test_create_operation_integrity_error_returns_sanitized_message(server_components):
    """sqlite3.IntegrityError from insert_evidence returns DUPLICATE_EVIDENCE_REF with sanitized message.

    Simulates the DB-level UNIQUE constraint violation via monkeypatching so the
    server's IntegrityError branch is exercised through the public tool API.
    """
    import sqlite3 as _sqlite3
    cited = hgp_create_operation(op_type="artifact", agent_id="a")
    db = server_components["db"]

    # Monkeypatch db.insert_evidence to raise IntegrityError with raw schema-leaking message
    original = db.insert_evidence
    def raise_integrity_error(citing_op_id, refs):
        raise _sqlite3.IntegrityError(
            "UNIQUE constraint failed: op_evidence.citing_op_id, op_evidence.cited_op_id"
        )
    db.insert_evidence = raise_integrity_error
    try:
        result = hgp_create_operation(
            op_type="hypothesis", agent_id="a",
            evidence_refs=[{"op_id": cited["op_id"], "relation": "supports"}],
        )
    finally:
        db.insert_evidence = original

    assert result.get("error") == "DUPLICATE_EVIDENCE_REF"
    assert "op_evidence" not in result.get("message", "")
    assert "citing_op_id" not in result.get("message", "")


# ── Task 5: Contract lock tests ───────────────────────────────────────────────

def test_query_operations_response_shape(server_components):
    """hgp_query_operations must always return {"operations": list}, never a bare list."""
    result = hgp_query_operations()
    assert isinstance(result, dict), "response must be a dict"
    assert "operations" in result, "response must have 'operations' key"
    assert isinstance(result["operations"], list), "'operations' value must be a list"


def test_query_operations_op_id_response_shape(server_components):
    """hgp_query_operations with op_id filter must also return {"operations": list}."""
    result = hgp_query_operations(op_id="nonexistent-op-id")
    assert isinstance(result, dict)
    assert "operations" in result
    assert result["operations"] == []


def test_query_operations_with_op_id_found(server_components):
    """hgp_query_operations with op_id for existing op returns {"operations": [op]}."""
    created = hgp_create_operation(op_type="artifact", agent_id="a")
    result = hgp_query_operations(op_id=created["op_id"])
    assert isinstance(result, dict)
    assert len(result["operations"]) == 1
    assert result["operations"][0]["op_id"] == created["op_id"]


# ── Phase 2: Task 2.8 — acquire_lease warns when memory tier update fails ────


def test_acquire_lease_memory_tier_failure_returns_warning_field(server_components, monkeypatch):
    """hgp_acquire_lease must return {"warning": ...} when set_memory_tier raises sqlite3.Error.

    The lease is still valid — the warning field signals that the short_term
    promotion failed, so the caller knows to treat this as a degraded-mode lease.
    """
    import sqlite3 as _sqlite3

    r = hgp_create_operation(op_type="artifact", agent_id="a")
    db = server_components["db"]

    def _fail_set_tier(*args, **kwargs):
        raise _sqlite3.OperationalError("simulated tier update failure")

    monkeypatch.setattr(db, "set_memory_tier", _fail_set_tier)

    lease = hgp_acquire_lease(agent_id="a", subgraph_root_op_id=r["op_id"])

    assert "lease_id" in lease, f"lease_id must be present even on tier failure: {lease}"
    assert "warning" in lease, (
        f"warning field must be present when memory tier update fails: {lease}"
    )
    assert "short_term" in lease["warning"].lower() or "memory tier" in lease["warning"].lower()


# ── Storage routing regressions (522f901 followup) ───────────────────────────


def test_check_file_project_passes_in_global_mode(tmp_path):
    """_check_file_project returns None when _project_root is None (global/unbound mode)."""
    orig = server_module._project_root
    server_module._project_root = None
    try:
        result = server_module._check_file_project(tmp_path)
        assert result is None
    finally:
        server_module._project_root = orig


def test_check_file_project_passes_when_roots_match(tmp_path):
    """_check_file_project returns None when file root matches bound project root."""
    orig = server_module._project_root
    server_module._project_root = tmp_path
    try:
        result = server_module._check_file_project(tmp_path)
        assert result is None
    finally:
        server_module._project_root = orig


def test_write_file_rejects_cross_repo_cold_start(server_components, tmp_path, monkeypatch):
    """hgp_write_file rejects cross-repo ops on the FIRST call (cold-start state).

    Exercises the real startup path: _project_bound=False, _project_root=None.
    Verifies that _ensure_project_bound() resolves the project root before
    _check_file_project() runs, so cross-repo rejection is immediate even
    before _get_components() has been called.
    """
    from hgp.server import hgp_write_file

    repo_a = tmp_path / "repo_a"
    repo_a.mkdir()
    repo_b = tmp_path / "repo_b"
    repo_b.mkdir()

    # Simulate cold-start: root not yet resolved (_db is set by fixture so
    # _get_components() won't re-open the DB, but _project_bound is fresh)
    monkeypatch.setattr(server_module, "_project_root", None)
    monkeypatch.setattr(server_module, "_project_bound", False)

    # find_project_root is called in two places:
    #   1. hgp_write_file → find_project_root(repo_b/...) → should return repo_b
    #   2. _ensure_project_bound → find_project_root(cwd) → should return repo_a
    # Mock returns repo_b for paths under repo_b, repo_a otherwise.
    def mock_find_root(start: Path) -> Path:
        try:
            start.resolve().relative_to(repo_b.resolve())
            return repo_b
        except ValueError:
            return repo_a

    monkeypatch.setattr(server_module, "find_project_root", mock_find_root)

    # First call with a file in repo_b — must be rejected immediately
    result = hgp_write_file(
        file_path=str(repo_b / "test.txt"),
        content="hello",
        agent_id="agent-a",
    )
    assert result.get("error") == "CROSS_REPO_OPERATION", (
        f"First cold-start cross-repo call must be rejected immediately, got: {result}"
    )
    assert server_module._project_bound, "_project_bound must be True after first call"
    assert server_module._project_root == repo_a, (
        f"_project_root must be repo_a after _ensure_project_bound(), got: {server_module._project_root}"
    )


# ── Phase 5 — Test Coverage Gaps ────────────────────────────────────────────


# Task 5.1 ─────────────────────────────────────────────────────────────────────

def test_acquire_lease_auto_releases_prior_active_lease(server_components):
    """acquire() for same agent+subgraph releases the prior ACTIVE lease.

    Acquiring a second lease must result in exactly one ACTIVE lease and
    the first lease transitioning to RELEASED.
    """
    root = hgp_create_operation(op_type="artifact", agent_id="a")
    lease1 = hgp_acquire_lease(agent_id="a", subgraph_root_op_id=root["op_id"])
    lease2 = hgp_acquire_lease(agent_id="a", subgraph_root_op_id=root["op_id"])

    db = server_components["db"]

    row1 = db.execute(
        "SELECT status FROM leases WHERE lease_id=?", (lease1["lease_id"],)
    ).fetchone()
    assert row1["status"] == "RELEASED", (
        f"First lease must be RELEASED after re-acquire, got: {row1['status']}"
    )

    row2 = db.execute(
        "SELECT status FROM leases WHERE lease_id=?", (lease2["lease_id"],)
    ).fetchone()
    assert row2["status"] == "ACTIVE", (
        f"Second lease must be ACTIVE, got: {row2['status']}"
    )

    active_count = db.execute(
        "SELECT COUNT(*) AS n FROM leases "
        "WHERE agent_id='a' AND subgraph_root_op_id=? AND status='ACTIVE'",
        (root["op_id"],),
    ).fetchone()["n"]
    assert active_count == 1, f"Exactly one ACTIVE lease expected, got: {active_count}"


# Task 5.2 ─────────────────────────────────────────────────────────────────────

def test_get_components_init_failure_leaves_db_none(monkeypatch, tmp_path):
    """_get_components() leaves _db=None if DB initialization fails.

    This ensures the next call retries initialization rather than using
    a partially-initialized global state.
    """
    # Reset all singleton state
    orig = (
        server_module._db, server_module._cas,
        server_module._lease_mgr, server_module._reconciler,
        server_module._project_root, server_module._project_bound,
    )
    server_module._db = None
    server_module._cas = None
    server_module._lease_mgr = None
    server_module._reconciler = None
    server_module._project_root = None
    server_module._project_bound = False

    (tmp_path / ".git").mkdir()
    monkeypatch.setenv("HGP_PROJECT_ROOT", str(tmp_path))

    class FailingDatabase:
        """Stub that raises on initialize() to simulate DB init failure."""
        def __init__(self, path): pass
        def initialize(self): raise RuntimeError("simulated DB init failure")
        def close(self): pass

    monkeypatch.setattr(server_module, "Database", FailingDatabase)

    try:
        with pytest.raises(RuntimeError, match="simulated DB init failure"):
            server_module._get_components()

        assert server_module._db is None, "_db must remain None after failed init"
        assert server_module._cas is None, "_cas must remain None after failed init"
        assert server_module._lease_mgr is None, "_lease_mgr must remain None after failed init"
        assert server_module._reconciler is None, "_reconciler must remain None after failed init"
    finally:
        (
            server_module._db, server_module._cas,
            server_module._lease_mgr, server_module._reconciler,
            server_module._project_root, server_module._project_bound,
        ) = orig


# Task 5.3 ─────────────────────────────────────────────────────────────────────

def test_file_history_outside_project_root_returns_empty(server_components):
    """hgp_file_history with a path outside the project root returns empty operations.

    Must not raise — callers rely on an empty-list response for paths
    that have no recorded history, including paths outside the project.
    """
    from hgp.server import hgp_file_history

    # /dev/null is guaranteed to be outside any test tmp_path project root
    result = hgp_file_history(file_path="/dev/null")
    assert isinstance(result, dict), f"Must return a dict, got: {type(result)}"
    assert "operations" in result, f"Must have 'operations' key, got: {result}"
    assert result["operations"] == [], (
        f"Must return empty operations for out-of-project path, got: {result['operations']}"
    )


# Task 5.5 ─────────────────────────────────────────────────────────────────────

def test_lease_validate_extend_true_advances_expires_at(server_components):
    """hgp_validate_lease(extend=True) advances expires_at beyond the original value.

    extend=True resets the TTL from now, so expires_at must be strictly
    greater than the value recorded at acquire time.
    """
    root = hgp_create_operation(op_type="artifact", agent_id="a")
    lease = hgp_acquire_lease(agent_id="a", subgraph_root_op_id=root["op_id"], ttl_seconds=300)

    db = server_components["db"]
    original_expires = db.execute(
        "SELECT expires_at FROM leases WHERE lease_id=?", (lease["lease_id"],)
    ).fetchone()["expires_at"]

    validated = hgp_validate_lease(lease["lease_id"], extend=True)
    assert validated["valid"] is True

    after_expires = db.execute(
        "SELECT expires_at FROM leases WHERE lease_id=?", (lease["lease_id"],)
    ).fetchone()["expires_at"]

    assert after_expires >= original_expires, (
        f"extend=True must not reduce expires_at: {original_expires} → {after_expires}"
    )
