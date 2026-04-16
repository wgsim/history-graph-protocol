"""HGP MCP Server — FastMCP entry point."""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import ValidationError

from hgp.cas import CAS
from hgp.dag import compute_chain_hash, get_ancestors, get_descendants
from hgp.db import Database
from hgp.errors import (
    BlobWriteError,
    PayloadTooLargeError,
)
from hgp.lease import LeaseManager
from hgp.models import EvidenceRef
from hgp.project import (
    PathOutsideRootError,
    ProjectRootError,
    canonical_file_path,
    find_project_root,
)
from hgp.reconciler import Reconciler

_log = logging.getLogger(__name__)

_VALID_OP_TYPES = frozenset({"artifact", "hypothesis", "merge", "invalidation"})
_VALID_STATUSES = frozenset({"PENDING", "COMPLETED", "INVALIDATED", "MISSING_BLOB", "STALE_PENDING"})
_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_MAX_TTL_SECONDS = 86400
# Limits evidence refs per operation to cap O(N) existence checks inside BEGIN IMMEDIATE.
_MAX_EVIDENCE_REFS = 50
_MAX_QUERY_LIMIT = 1000
_MAX_SUBGRAPH_DEPTH = 500

# ── Server initialization ───────────────────────────────────

mcp = FastMCP("hgp")


@dataclass
class HGPContext:
    """Fully-initialized server context: project binding + all components.

    _ctx is None until every component is successfully initialized.
    A failed init leaves _ctx=None so the next call retries cleanly.
    """
    db: Database
    cas: CAS
    lease_mgr: LeaseManager
    reconciler: Reconciler
    project_root: Path | None  # None = global mode (~/.hgp/)


_ctx: HGPContext | None = None


def _get_context() -> HGPContext:
    """Return (and lazily create) the server context singleton.

    Resolves project root and initializes all components in one step.
    """
    global _ctx
    if _ctx is None:
        if os.environ.get("HGP_GLOBAL_MODE"):
            project_root = None
        else:
            try:
                project_root = find_project_root(Path.cwd())
            except ProjectRootError:
                project_root = Path.cwd()
                _log.warning(
                    "No .git repository found from cwd; using cwd-local store %s/.hgp/. "
                    "Run from inside a git repository for repo-local storage, or set "
                    "HGP_PROJECT_ROOT to specify a root explicitly.",
                    project_root,
                )
        hgp_dir = (project_root / ".hgp") if project_root else (Path.home() / ".hgp")
        hgp_content_dir = hgp_dir / ".hgp_content"
        db = Database(hgp_dir / "hgp.db")
        try:
            hgp_dir.mkdir(parents=True, exist_ok=True)
            hgp_content_dir.mkdir(exist_ok=True)
            db.initialize()
            cas = CAS(hgp_content_dir)
            lease_mgr = LeaseManager(db)
            reconciler = Reconciler(db, cas, hgp_content_dir)
            db.expire_leases()
            db.commit()
            startup_report = reconciler.reconcile()
            if startup_report.errors:
                _log.warning("startup reconcile reported errors: %s", startup_report.errors)
        except Exception:
            db.close()
            raise
        _ctx = HGPContext(
            db=db, cas=cas, lease_mgr=lease_mgr, reconciler=reconciler,
            project_root=project_root,
        )
    return _ctx


def _check_file_project(file_root: Path, ctx: HGPContext) -> dict[str, Any] | None:
    """Return an error dict if file_root doesn't match the bound project root.

    Returns None when the check passes (global mode, or roots match).
    """
    if ctx.project_root is not None and file_root.resolve() != ctx.project_root.resolve():
        return {
            "error": "CROSS_REPO_OPERATION",
            "message": (
                f"File belongs to project {file_root} but this server is bound to "
                f"{ctx.project_root}. Start a separate HGP server instance for that project."
            ),
        }
    return None


# ── MCP Tools ───────────────────────────────────────────────

@mcp.tool()
def hgp_create_operation(
    op_type: str,
    agent_id: str,
    parent_op_ids: list[str] | None = None,
    invalidates_op_ids: list[str] | None = None,
    payload: str | None = None,
    mime_type: str | None = None,
    lease_id: str | None = None,
    chain_hash: str | None = None,
    subgraph_root_op_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    evidence_refs: list[dict[str, Any]] | None = None,
    verbose: bool = True,
) -> dict[str, Any]:
    """Create a new operation in the causal history DAG."""
    if (early := _check_mode(mutation=True)) is not None:
        return early
    if op_type not in _VALID_OP_TYPES:
        return {"error": "INVALID_OP_TYPE", "message": f"op_type must be one of {sorted(_VALID_OP_TYPES)}"}

    # Validate evidence_refs early (before any DB work) to fail fast on bad input
    parsed_refs: list[EvidenceRef] = []
    if evidence_refs:
        if len(evidence_refs) > _MAX_EVIDENCE_REFS:
            return {"error": "TOO_MANY_EVIDENCE_REFS", "message": f"max {_MAX_EVIDENCE_REFS} evidence refs per operation"}  # noqa: E501
        try:
            parsed_refs = [EvidenceRef.model_validate(r) for r in evidence_refs]
        except ValidationError as exc:
            return {"error": "INVALID_EVIDENCE_REF", "message": str(exc)}

    ctx = _get_context()
    db = ctx.db
    cas = ctx.cas

    # Validate parents exist
    for pid in (parent_op_ids or []):
        if not db.get_operation(pid):
            return {"error": "PARENT_NOT_FOUND", "message": f"Parent operation not found: {pid}"}

    # Validate invalidation targets exist
    for inv_id in (invalidates_op_ids or []):
        if not db.get_operation(inv_id):
            return {"error": "INVALIDATION_TARGET_NOT_FOUND", "message": f"Invalidation target not found: {inv_id}"}

    root_op_id = subgraph_root_op_id or (parent_op_ids[0] if parent_op_ids else None)

    # Phase 1: Pre-flight chain_hash check (advisory)
    if chain_hash and root_op_id:
        current = compute_chain_hash(db, root_op_id)
        if current != chain_hash:
            return {"error": "CHAIN_STALE", "message": f"CHAIN_STALE: expected {chain_hash}, got {current}"}

    # Phase 2: Write blob to CAS (idempotent, outside transaction)
    object_hash: str | None = None
    if payload:
        try:
            raw = base64.b64decode(payload, validate=True)
        except Exception:
            return {"error": "INVALID_PAYLOAD", "message": "payload is not valid base64"}
        try:
            object_hash = cas.store(raw)
        except PayloadTooLargeError as exc:
            return {"error": "PAYLOAD_TOO_LARGE", "message": str(exc)}
        except BlobWriteError as exc:
            return {"error": "BLOB_WRITE_ERROR", "message": str(exc)}

    # Phase 3: Atomic DB commit (BEGIN IMMEDIATE)
    op_id = str(uuid.uuid4())
    db.begin_immediate()
    try:
        # Re-validate under write lock (closes TOCTOU)
        if chain_hash and root_op_id:
            current = compute_chain_hash(db, root_op_id)
            if current != chain_hash:
                db.rollback()
                return {  # noqa: E501
                    "error": "CHAIN_STALE",
                    "message": f"CHAIN_STALE (under lock): expected {chain_hash}, got {current}",
                }

        seq = db.next_commit_seq()
        db.insert_operation(
            op_id=op_id,
            op_type=op_type,
            agent_id=agent_id,
            commit_seq=seq,
            chain_hash="sha256:pending",
            object_hash=object_hash,
            metadata=json.dumps(metadata) if metadata else None,
            mime_type=mime_type,
        )

        for pid in (parent_op_ids or []):
            db.insert_edge(op_id, pid, "causal")

        for inv_id in (invalidates_op_ids or []):
            db.insert_edge(op_id, inv_id, "invalidates")
            db.update_operation_status(inv_id, "INVALIDATED")

        if parsed_refs:
            try:
                db.insert_evidence(op_id, parsed_refs)
            except ValueError as exc:
                db.rollback()
                return {"error": "INVALID_EVIDENCE_REF", "message": str(exc)}
            except sqlite3.IntegrityError:
                db.rollback()
                # Do not expose column names from the raw IntegrityError message.
                return {"error": "DUPLICATE_EVIDENCE_REF", "message": "Evidence link already exists"}

        # Compute final chain_hash AFTER all edges are inserted
        new_root = subgraph_root_op_id or op_id
        final_chain_hash = compute_chain_hash(db, new_root)
        db.update_chain_hash(op_id, final_chain_hash)

        if lease_id:
            lease_root_id = db.get_active_lease_root(lease_id)
            db.release_active_lease(lease_id)
            if lease_root_id:
                if db.count_active_leases_for_root(lease_root_id) == 0:
                    db.set_memory_tier(lease_root_id, "long_term")

        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception as rb_exc:
            _log.error("ROLLBACK failed after transaction error: %s", rb_exc)
        raise

    result: dict[str, Any] = {
        "op_id": op_id,
        "status": "COMPLETED",
        "commit_seq": seq,
        "object_hash": object_hash,
        "chain_hash": final_chain_hash,
    }
    if not verbose:
        result.pop("object_hash", None)
        result.pop("chain_hash", None)
    return result


_SUMMARY_FIELDS = {"op_id", "op_type", "status", "commit_seq", "agent_id", "memory_tier"}
_STUB_FIELDS = {"op_id", "op_type", "memory_tier"}


def _project(op: dict[str, Any], tier: str) -> dict[str, Any]:
    if tier == "short_term":
        return {k: v for k, v in op.items() if k != "depth"}
    if tier == "long_term":
        return {k: v for k, v in op.items() if k in _SUMMARY_FIELDS}
    return {k: v for k, v in op.items() if k in _STUB_FIELDS}


def _record_access_with_decay(db: Database, ops: list[dict[str, Any]]) -> None:
    """Best-effort depth-based access recording. Uses CTE depth column.

    Called in autocommit mode (no open transaction). record_access() UPDATEs
    auto-commit per-statement; the db.commit() call here is a documented no-op
    retained for symmetry in case the calling context ever opens a transaction.
    db.rollback() is likewise a no-op in autocommit mode but guards the case
    where an explicit transaction is somehow open.
    """
    DECAY = [1.0, 0.7, 0.4, 0.1]
    try:
        for op in ops:
            depth = int(op.get("depth", 0))
            weight = DECAY[min(depth, len(DECAY) - 1)]
            db.record_access(op["op_id"], weight)
        db.commit()
    except sqlite3.Error as exc:
        _log.debug("access recording skipped (lock contention or transient error): %s", exc)
        try:
            db.rollback()
        except Exception as rb_exc:
            _log.debug("rollback in _record_access_with_decay failed: %s", rb_exc)
    except Exception:
        _log.error("Unexpected error in _record_access_with_decay", exc_info=True)
        try:
            db.rollback()
        except Exception:
            pass


@mcp.tool()
def hgp_query_operations(
    op_id: str | None = None,
    agent_id: str | None = None,
    op_type: str | None = None,
    status: str | None = None,
    since_commit_seq: int | None = None,
    include_inactive: bool = False,
    limit: int = 100,
    file_path: str | None = None,
) -> dict[str, Any]:
    """Query operations with optional filters.

    By default excludes inactive-tier ops; pass include_inactive=True to include them.
    """
    if (early := _check_mode(mutation=False)) is not None:
        return early
    if status is not None and status not in _VALID_STATUSES:
        return {"error": "INVALID_STATUS", "message": f"status must be one of {sorted(_VALID_STATUSES)}"}
    limit = max(1, min(limit, _MAX_QUERY_LIMIT))

    db = _get_context().db
    if op_id:
        op = db.get_operation(op_id)
        if op:
            try:
                db.record_access(op_id)
                db.commit()
            except sqlite3.Error as exc:
                _log.debug("access recording skipped in hgp_query_operations op_id=%r: %s", op_id, exc)
                try:
                    db.rollback()
                except Exception:
                    pass
        return {"operations": [op] if op else []}
    # Canonicalize file_path filter so it matches stored canonical paths
    canonical_fp = file_path
    if file_path is not None:
        try:
            root = find_project_root(Path(file_path).parent)
            canonical_fp = canonical_file_path(file_path, root)
        except (ProjectRootError, PathOutsideRootError) as exc:
            _log.debug("hgp_query_operations: file_path canonicalization failed, using raw path: %s", exc)
            # Results may be empty or incomplete if raw path doesn't match stored canonical paths
    ops = db.query_operations(
        status=status, agent_id=agent_id, op_type=op_type,
        since_commit_seq=since_commit_seq,
        include_inactive=include_inactive, limit=limit,
        file_path=canonical_fp,
    )
    return {"operations": ops}


@mcp.tool()
def hgp_file_history(
    file_path: str,
    limit: int = 50,
) -> dict[str, Any]:
    """Return operations recorded for a given file_path, most recent first."""
    if (early := _check_mode(mutation=False)) is not None:
        return early
    limit = max(1, min(limit, _MAX_QUERY_LIMIT))
    db = _get_context().db
    # Canonicalize query path so it matches stored canonical paths
    try:
        root = find_project_root(Path(file_path).parent)
        query_path = canonical_file_path(file_path, root)
    except (ProjectRootError, PathOutsideRootError):
        # Fall back to raw path for queries outside a project root (returns empty)
        query_path = file_path
    rows = db.get_ops_by_file_path(query_path, limit=limit)
    ops = [dict(r) for r in rows]
    # Use depth-based decay: most recent op (index 0) gets full weight, older ops decay.
    _record_access_with_decay(db, [dict(op, depth=i) for i, op in enumerate(ops)])
    return {"file_path": query_path, "operations": ops}


@mcp.tool()
def hgp_query_subgraph(
    root_op_id: str,
    direction: str = "ancestors",
    max_depth: int = 50,
    include_invalidated: bool = False,
) -> dict[str, Any]:
    """Traverse the causal subgraph from root_op_id."""
    if (early := _check_mode(mutation=False)) is not None:
        return early
    max_depth = max(1, min(max_depth, _MAX_SUBGRAPH_DEPTH))
    db = _get_context().db
    # Use a single deferred transaction so chain_hash and ops come from the same snapshot.
    db.begin_deferred()
    try:
        chain_hash = compute_chain_hash(db, root_op_id)
        if direction == "ancestors":
            ops = get_ancestors(db, root_op_id, max_depth=max_depth)
        else:
            ops = get_descendants(db, root_op_id, max_depth=max_depth)
        db.commit()
    except Exception:
        db.rollback()
        raise
    if not include_invalidated:
        ops = [o for o in ops if o["status"] != "INVALIDATED"]
    projected = [_project(op, op.get("memory_tier", "long_term")) for op in ops]
    _record_access_with_decay(db, ops)
    return {"root_op_id": root_op_id, "chain_hash": chain_hash, "operations": projected}


@mcp.tool()
def hgp_acquire_lease(
    agent_id: str,
    subgraph_root_op_id: str,
    ttl_seconds: int = 300,
) -> dict[str, Any]:
    """Acquire a lease on a subgraph for optimistic concurrency."""
    if (early := _check_mode(mutation=True)) is not None:
        return early
    ctx = _get_context()
    db = ctx.db
    lease_mgr = ctx.lease_mgr
    lease = lease_mgr.acquire(agent_id, subgraph_root_op_id, min(ttl_seconds, _MAX_TTL_SECONDS))
    response: dict[str, Any] = {
        "lease_id": lease.lease_id,
        "chain_hash": lease.chain_hash,
        "expires_at": lease.expires_at.isoformat(),
    }
    try:
        db.set_memory_tier(subgraph_root_op_id, "short_term")
        db.commit()
    except sqlite3.Error as exc:
        _log.error(
            "hgp_acquire_lease: memory tier update failed for lease %s: %s",
            lease.lease_id, exc,
        )
        response["warning"] = "memory tier could not be updated to short_term"
    return response


@mcp.tool()
def hgp_validate_lease(lease_id: str, extend: bool = True) -> dict[str, Any]:
    """Validate (PING) a lease token before LLM compute."""
    if (early := _check_mode(mutation=True)) is not None:
        return early
    lease_mgr = _get_context().lease_mgr
    return lease_mgr.validate(lease_id, extend=extend)


@mcp.tool()
def hgp_release_lease(lease_id: str) -> dict[str, Any]:
    """Release a lease token explicitly."""
    if (early := _check_mode(mutation=True)) is not None:
        return early
    ctx = _get_context()
    db = ctx.db
    lease_mgr = ctx.lease_mgr
    root_op_id = db.get_lease_root(lease_id)
    lease_mgr.release(lease_id)
    if root_op_id:
        if db.count_active_leases_for_root(root_op_id) == 0:
            db.set_memory_tier(root_op_id, "long_term")
            db.commit()
    return {"released": True, "lease_id": lease_id}


@mcp.tool()
def hgp_set_memory_tier(op_id: str, tier: str) -> dict[str, Any]:
    """Explicitly set the memory tier of an operation."""
    if (early := _check_mode(mutation=True)) is not None:
        return early
    valid = {"short_term", "long_term", "inactive"}
    if tier not in valid:
        return {"error": "INVALID_TIER", "valid_tiers": sorted(valid)}
    db = _get_context().db
    found = db.set_memory_tier(op_id, tier)
    db.commit()
    if not found:
        return {"error": "OP_NOT_FOUND", "op_id": op_id}
    return {"op_id": op_id, "tier": tier}


@mcp.tool()
def hgp_get_artifact(object_hash: str) -> dict[str, Any]:
    """Retrieve blob content from CAS by hash."""
    if (early := _check_mode(mutation=False)) is not None:
        return early
    cas = _get_context().cas
    data = cas.read(object_hash)
    if data is None:
        return {"error": "NOT_FOUND", "object_hash": object_hash}
    return {
        "object_hash": object_hash,
        "size": len(data),
        "content": base64.b64encode(data).decode(),
    }


@mcp.tool()
def hgp_anchor_git(
    op_id: str,
    git_commit_sha: str,
    repository: str | None = None,
) -> dict[str, Any]:
    """Link an HGP operation to a Git commit SHA."""
    if (early := _check_mode(mutation=True)) is not None:
        return early
    db = _get_context().db
    if not _GIT_SHA_RE.fullmatch(git_commit_sha):
        return {"error": "INVALID_SHA", "message": "git_commit_sha must be 40 lowercase hex chars"}
    if not db.get_operation(op_id):
        return {"error": "OP_NOT_FOUND", "message": f"Operation not found: {op_id!r}"}
    try:
        db.insert_git_anchor(op_id, git_commit_sha, repository)
        db.commit()
    except sqlite3.Error as exc:
        _log.error("DB error in hgp_anchor_git op_id=%r: %s", op_id, exc, exc_info=True)
        return {"error": "DB_ERROR", "message": "Internal database error"}
    return {"anchored": True, "op_id": op_id, "git_commit_sha": git_commit_sha}


_CONTEXT_FILE_TTL_SECONDS = 86400  # 24 hours


def _context_file_path(hgp_dir: Path, session_id: str) -> Path:
    return hgp_dir / f"context-{session_id}.json"


def _hgp_dir_from_ctx(ctx: HGPContext) -> Path:
    if ctx.project_root is not None:
        return ctx.project_root / ".hgp"
    return Path.home() / ".hgp"


@mcp.tool()
def hgp_set_context(root_op_id: str, agent_id: str, session_id: str) -> dict[str, Any]:
    """Store a session root op so SubagentStart hooks can propagate it to subagents.

    Writes .hgp/context-{session_id}.json.  Safe under concurrent sessions —
    each session writes its own file.
    """
    if (early := _check_mode(mutation=True)) is not None:
        return early
    ctx = _get_context()
    if not ctx.db.get_operation(root_op_id):
        return {"error": "OP_NOT_FOUND", "message": f"root_op_id not found: {root_op_id!r}"}
    hgp_dir = _hgp_dir_from_ctx(ctx)
    data = {
        "root_op_id": root_op_id,
        "agent_id": agent_id,
        "session_id": session_id,
        "set_at": time.time(),
    }
    path = _context_file_path(hgp_dir, session_id)
    path.write_text(json.dumps(data), encoding="utf-8")
    return {"status": "ok", "root_op_id": root_op_id, "session_id": session_id}


@mcp.tool()
def hgp_get_context(session_id: str, consume_summaries: bool = True) -> dict[str, Any]:
    """Read the session root op and any pending subagent summaries.

    Returns {root_op_id, agent_id, age_seconds, subagent_summaries} or
    {status: "no_context"}.  When consume_summaries=True (default), summary
    files are deleted after reading so they are not returned twice.
    """
    if (early := _check_mode(mutation=False)) is not None:
        return early
    ctx = _get_context()
    hgp_dir = _hgp_dir_from_ctx(ctx)
    path = _context_file_path(hgp_dir, session_id)
    if not path.exists():
        return {"status": "no_context"}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"error": "READ_ERROR", "message": str(exc)}
    age = time.time() - data.get("set_at", 0)

    summaries: list[dict[str, Any]] = []
    for p in sorted(hgp_dir.glob(f"subagent-summary-{session_id}-*.json")):
        try:
            summaries.append(json.loads(p.read_text(encoding="utf-8")))
            if consume_summaries:
                p.unlink(missing_ok=True)
        except (OSError, json.JSONDecodeError):
            pass

    result: dict[str, Any] = {
        "root_op_id": data["root_op_id"],
        "agent_id": data.get("agent_id", ""),
        "session_id": session_id,
        "age_seconds": int(age),
    }
    if summaries:
        result["subagent_summaries"] = summaries
    return result


@mcp.tool()
def hgp_reconcile(dry_run: bool = False) -> dict[str, Any]:
    """Run crash recovery reconciler; also removes stale session context files (>24 h)."""
    if (early := _check_mode(mutation=True)) is not None:
        return early
    ctx = _get_context()
    reconciler = ctx.reconciler
    report = reconciler.reconcile(dry_run=dry_run)

    # Clean up stale context-{session_id}.json files
    hgp_dir = _hgp_dir_from_ctx(ctx)
    now = time.time()
    removed: list[str] = []
    for p in hgp_dir.glob("context-*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            age = now - data.get("set_at", 0)
            if age > _CONTEXT_FILE_TTL_SECONDS:
                if not dry_run:
                    p.unlink(missing_ok=True)
                removed.append(p.name)
        except (OSError, json.JSONDecodeError):
            pass

    # Clean up stale subagent-summary-*.json files (same TTL)
    for p in hgp_dir.glob("subagent-summary-*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            age = now - data.get("completed_at", 0)
            if age > _CONTEXT_FILE_TTL_SECONDS:
                if not dry_run:
                    p.unlink(missing_ok=True)
                removed.append(p.name)
        except (OSError, json.JSONDecodeError):
            pass

    result = report.model_dump()
    if removed:
        result["stale_context_files_removed"] = removed
    return result


@mcp.tool()
def hgp_get_evidence(op_id: str) -> dict[str, Any]:
    """Return all operations that op_id cited as evidence."""
    if (early := _check_mode(mutation=False)) is not None:
        return early
    db = _get_context().db
    try:
        if not db.get_operation(op_id):
            return {"error": "OP_NOT_FOUND", "message": f"Operation not found: {op_id!r}"}
        return {"op_id": op_id, "evidence": db.get_evidence(op_id)}
    except sqlite3.Error as exc:
        _log.error("DB error in hgp_get_evidence op_id=%r: %s", op_id, exc, exc_info=True)
        return {"error": "DB_ERROR", "message": "Internal database error"}


@mcp.tool()
def hgp_get_citing_ops(op_id: str) -> dict[str, Any]:
    """Return all operations that cited op_id as evidence (reverse direction)."""
    if (early := _check_mode(mutation=False)) is not None:
        return early
    db = _get_context().db
    try:
        if not db.get_operation(op_id):
            return {"error": "OP_NOT_FOUND", "message": f"Operation not found: {op_id!r}"}
        return {"op_id": op_id, "citing_ops": db.get_citing_ops(op_id)}
    except sqlite3.Error as exc:
        _log.error("DB error in hgp_get_citing_ops op_id=%r: %s", op_id, exc, exc_info=True)
        return {"error": "DB_ERROR", "message": "Internal database error"}


def _record_file_op(
    file_path: str,
    content_bytes: bytes,
    agent_id: str,
    reason: str,
    parent_op_ids: list[str] | None,
    evidence_refs: list[dict[str, Any]] | None,
    initial_status: str = "PENDING",
) -> dict[str, Any]:
    """Store content in CAS and insert an artifact operation. Returns {op_id}.

    When initial_status='PENDING' the caller is responsible for calling
    db.finalize_operation(op_id) after the filesystem side effect succeeds.
    """
    parsed_refs: list[EvidenceRef] = []
    if evidence_refs:
        if len(evidence_refs) > _MAX_EVIDENCE_REFS:
            return {"error": "TOO_MANY_EVIDENCE_REFS", "message": f"max {_MAX_EVIDENCE_REFS} evidence refs per operation"}  # noqa: E501
        try:
            parsed_refs = [EvidenceRef.model_validate(r) for r in evidence_refs]
        except ValidationError as exc:
            return {"error": "INVALID_EVIDENCE_REF", "message": str(exc)}

    ctx = _get_context()
    db = ctx.db
    cas = ctx.cas

    try:
        object_hash = cas.store(content_bytes)
    except PayloadTooLargeError as exc:
        return {"error": "PAYLOAD_TOO_LARGE", "message": str(exc)}
    except BlobWriteError as exc:
        return {"error": "BLOB_WRITE_ERROR", "message": str(exc)}
    op_id = str(uuid.uuid4())
    metadata = json.dumps({"reason": reason})

    db.begin_immediate()
    try:
        seq = db.next_commit_seq()
        db.insert_operation(
            op_id=op_id,
            op_type="artifact",
            agent_id=agent_id,
            commit_seq=seq,
            chain_hash="sha256:pending",
            object_hash=object_hash,
            metadata=metadata,
            file_path=file_path,
            status=initial_status,
        )
        for pid in (parent_op_ids or []):
            db.insert_edge(op_id, pid, "causal")
        if parsed_refs:
            try:
                db.insert_evidence(op_id, parsed_refs)
            except ValueError as exc:
                db.rollback()
                return {"error": "INVALID_EVIDENCE_REF", "message": str(exc)}
            except sqlite3.IntegrityError:
                db.rollback()
                return {"error": "DUPLICATE_EVIDENCE_REF", "message": "Evidence link already exists"}
        final_hash = compute_chain_hash(db, op_id)
        db.update_chain_hash(op_id, final_hash)
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception as rb_exc:
            _log.error("_record_file_op: ROLLBACK failed op_id=%s: %s", op_id, rb_exc)
        raise

    return {
        "op_id": op_id,
        "status": initial_status,
        "commit_seq": seq,
        "object_hash": object_hash,
        "chain_hash": final_hash,
    }


@mcp.tool()
def hgp_write_file(
    file_path: str,
    content: str,
    agent_id: str,
    reason: str | None = None,
    parent_op_ids: list[str] | None = None,
    evidence_refs: list[dict[str, Any]] | None = None,
    verbose: bool = True,
) -> dict[str, Any]:
    """Write (create or overwrite) a file and record it as an artifact operation."""
    if (early := _check_mode(mutation=True)) is not None:
        return early
    if err := _check_hgp_dir(file_path):
        return err
    try:
        root = find_project_root(Path(file_path).parent)
        canonical = canonical_file_path(file_path, root)
    except ProjectRootError as e:
        return {"error": "PROJECT_ROOT_NOT_FOUND", "message": str(e)}
    except PathOutsideRootError as e:
        return {"error": "PATH_OUTSIDE_ROOT", "message": str(e)}
    ctx = _get_context()
    if err := _check_file_project(root, ctx):
        return err

    path = Path(file_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    effective_reason = reason or f"CREATE {canonical}"
    result = _record_file_op(
        file_path=canonical,
        content_bytes=content.encode("utf-8"),
        agent_id=agent_id,
        reason=effective_reason,
        parent_op_ids=parent_op_ids,
        evidence_refs=evidence_refs,
        initial_status="PENDING",
    )
    if "error" in result:
        return result
    try:
        path.write_text(content, encoding="utf-8")
    except OSError as exc:
        _log.warning(
            "hgp_write_file filesystem write failed op_id=%s path=%r; "
            "PENDING op will be triaged by reconciler: %s",
            result["op_id"], file_path, exc,
        )
        return {"error": "FILESYSTEM_ERROR", "message": str(exc), "op_id": result["op_id"]}
    try:
        ctx.db.finalize_operation(result["op_id"])
    except Exception as exc:
        return {"error": "DB_FINALIZE_ERROR", "message": str(exc), "op_id": result["op_id"]}
    result["status"] = "COMPLETED"
    if not verbose:
        result.pop("object_hash", None)
        result.pop("chain_hash", None)
    return result


@mcp.tool()
def hgp_append_file(
    file_path: str,
    content: str,
    agent_id: str,
    reason: str | None = None,
    parent_op_ids: list[str] | None = None,
    evidence_refs: list[dict[str, Any]] | None = None,
    verbose: bool = True,
) -> dict[str, Any]:
    """Append content to a file (creates it if absent) and record as artifact."""
    if (early := _check_mode(mutation=True)) is not None:
        return early
    if err := _check_hgp_dir(file_path):
        return err
    try:
        root = find_project_root(Path(file_path).parent)
        canonical = canonical_file_path(file_path, root)
    except ProjectRootError as e:
        return {"error": "PROJECT_ROOT_NOT_FOUND", "message": str(e)}
    except PathOutsideRootError as e:
        return {"error": "PATH_OUTSIDE_ROOT", "message": str(e)}
    ctx = _get_context()
    if err := _check_file_project(root, ctx):
        return err

    path = Path(file_path)
    # Compute post-append content in memory (no filesystem side effect yet)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    combined = existing + content
    effective_reason = reason or f"APPEND {canonical}"
    result = _record_file_op(
        file_path=canonical,
        content_bytes=combined.encode("utf-8"),
        agent_id=agent_id,
        reason=effective_reason,
        parent_op_ids=parent_op_ids,
        evidence_refs=evidence_refs,
        initial_status="PENDING",
    )
    if "error" in result:
        return result
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(content)
    except OSError as exc:
        _log.warning(
            "hgp_append_file filesystem write failed op_id=%s path=%r; "
            "PENDING op will be triaged by reconciler: %s",
            result["op_id"], file_path, exc,
        )
        return {"error": "FILESYSTEM_ERROR", "message": str(exc), "op_id": result["op_id"]}
    try:
        ctx.db.finalize_operation(result["op_id"])
    except Exception as exc:
        return {"error": "DB_FINALIZE_ERROR", "message": str(exc), "op_id": result["op_id"]}
    result["status"] = "COMPLETED"
    if not verbose:
        result.pop("object_hash", None)
        result.pop("chain_hash", None)
    return result


@mcp.tool()
def hgp_edit_file(
    file_path: str,
    old_string: str,
    new_string: str,
    agent_id: str,
    reason: str | None = None,
    parent_op_ids: list[str] | None = None,
    evidence_refs: list[dict[str, Any]] | None = None,
    verbose: bool = True,
) -> dict[str, Any]:
    """Replace the first (and only) occurrence of old_string with new_string."""
    if (early := _check_mode(mutation=True)) is not None:
        return early
    if err := _check_hgp_dir(file_path):
        return err
    try:
        root = find_project_root(Path(file_path).parent)
        canonical = canonical_file_path(file_path, root)
    except ProjectRootError as e:
        return {"error": "PROJECT_ROOT_NOT_FOUND", "message": str(e)}
    except PathOutsideRootError as e:
        return {"error": "PATH_OUTSIDE_ROOT", "message": str(e)}
    ctx = _get_context()
    if err := _check_file_project(root, ctx):
        return err

    path = Path(file_path)
    if not path.exists():
        return {"error": "FILE_NOT_FOUND", "message": f"{file_path} does not exist"}

    original = path.read_text(encoding="utf-8")
    count = original.count(old_string)
    if count == 0:
        return {"error": "STRING_NOT_FOUND", "message": "old_string not found in file"}
    if count > 1:
        return {"error": "AMBIGUOUS_MATCH", "message": f"old_string found {count} times; must be unique"}

    updated = original.replace(old_string, new_string, 1)
    effective_reason = reason or f"MODIFY {canonical}"
    result = _record_file_op(
        file_path=canonical,
        content_bytes=updated.encode("utf-8"),
        agent_id=agent_id,
        reason=effective_reason,
        parent_op_ids=parent_op_ids,
        evidence_refs=evidence_refs,
        initial_status="PENDING",
    )
    if "error" in result:
        return result
    try:
        path.write_text(updated, encoding="utf-8")
    except OSError as exc:
        _log.warning(
            "hgp_edit_file filesystem write failed op_id=%s path=%r; "
            "PENDING op will be triaged by reconciler: %s",
            result["op_id"], file_path, exc,
        )
        return {"error": "FILESYSTEM_ERROR", "message": str(exc), "op_id": result["op_id"]}
    try:
        ctx.db.finalize_operation(result["op_id"])
    except Exception as exc:
        return {"error": "DB_FINALIZE_ERROR", "message": str(exc), "op_id": result["op_id"]}
    result["status"] = "COMPLETED"
    if not verbose:
        result.pop("object_hash", None)
        result.pop("chain_hash", None)
    return result


@mcp.tool()
def hgp_delete_file(
    file_path: str,
    agent_id: str,
    previous_op_id: str | None = None,
    reason: str | None = None,
    verbose: bool = True,
) -> dict[str, Any]:
    """Delete a file and record an invalidation operation."""
    if (early := _check_mode(mutation=True)) is not None:
        return early
    if err := _check_hgp_dir(file_path):
        return err
    try:
        root = find_project_root(Path(file_path).parent)
        canonical = canonical_file_path(file_path, root)
    except ProjectRootError as e:
        return {"error": "PROJECT_ROOT_NOT_FOUND", "message": str(e)}
    except PathOutsideRootError as e:
        return {"error": "PATH_OUTSIDE_ROOT", "message": str(e)}
    ctx = _get_context()
    if err := _check_file_project(root, ctx):
        return err

    path = Path(file_path)
    if path.is_symlink():
        return {"error": "SYMLINK_NOT_SUPPORTED", "message": f"{file_path} is a symlink; HGP does not track symlinks"}
    if not path.exists():
        return {"error": "FILE_NOT_FOUND", "message": f"{file_path} does not exist"}

    db = ctx.db

    # Preflight: validate previous_op_id before any filesystem side effect
    if previous_op_id and not db.get_operation(previous_op_id):
        return {"error": "INVALID_PARENT_OP_ID", "message": f"previous_op_id not found: {previous_op_id}"}

    op_id = str(uuid.uuid4())
    effective_reason = reason or f"DELETE {canonical}"
    metadata = json.dumps({"reason": effective_reason})

    db.begin_immediate()
    try:
        seq = db.next_commit_seq()
        db.insert_operation(
            op_id=op_id, op_type="invalidation", agent_id=agent_id,
            commit_seq=seq, chain_hash="sha256:pending",
            metadata=metadata, file_path=canonical,
            status="PENDING",
        )
        if previous_op_id:
            # Edge records intent; status update deferred until after unlink() succeeds.
            db.insert_edge(op_id, previous_op_id, "invalidates")
        final_hash = compute_chain_hash(db, op_id)
        db.update_chain_hash(op_id, final_hash)
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception as rb_exc:
            _log.error("ROLLBACK failed after transaction error: %s", rb_exc)
        raise

    try:
        path.unlink()
    except OSError as exc:
        return {"error": "FILESYSTEM_ERROR", "message": str(exc), "op_id": op_id}

    # Filesystem unlink succeeded — finalize all DB state in one atomic transaction.
    db.begin_immediate()
    try:
        if previous_op_id:
            db.update_operation_status(previous_op_id, "INVALIDATED")
        db.finalize_operation(op_id)
        db.commit()
    except Exception as exc:
        try:
            db.rollback()
        except Exception as rb_exc:
            _log.error("hgp_delete_file: ROLLBACK failed after finalize error op_id=%s: %s", op_id, rb_exc)
        return {"error": "DB_FINALIZE_ERROR", "message": str(exc), "op_id": op_id}
    result: dict[str, Any] = {
        "op_id": op_id,
        "status": "COMPLETED",
        "commit_seq": seq,
        "chain_hash": final_hash,
    }
    if not verbose:
        result.pop("chain_hash", None)
    return result


@mcp.tool()
def hgp_move_file(
    old_path: str,
    new_path: str,
    agent_id: str,
    previous_op_id: str | None = None,
    reason: str | None = None,
    evidence_refs: list[dict[str, Any]] | None = None,
    verbose: bool = True,
) -> dict[str, Any]:
    """Move/rename a file: invalidates old path op, creates new artifact op."""
    if (early := _check_mode(mutation=True)) is not None:
        return early
    if err := _check_hgp_dir(old_path) or _check_hgp_dir(new_path):
        return err
    try:
        root = find_project_root(Path(old_path).parent)
        canonical_old = canonical_file_path(old_path, root)
        canonical_new = canonical_file_path(new_path, root)
    except ProjectRootError as e:
        return {"error": "PROJECT_ROOT_NOT_FOUND", "message": str(e)}
    except PathOutsideRootError as e:
        return {"error": "PATH_OUTSIDE_ROOT", "message": str(e)}
    ctx = _get_context()
    if err := _check_file_project(root, ctx):
        return err

    src = Path(old_path)
    if src.is_symlink():
        return {"error": "SYMLINK_NOT_SUPPORTED", "message": f"{old_path} is a symlink; HGP does not track symlinks"}
    if not src.exists():
        return {"error": "FILE_NOT_FOUND", "message": f"{old_path} does not exist"}

    effective_reason = reason or f"MOVE {canonical_old} → {canonical_new}"

    # Validate evidence_refs BEFORE any filesystem operation.
    parsed_refs: list[EvidenceRef] = []
    if evidence_refs:
        if len(evidence_refs) > _MAX_EVIDENCE_REFS:
            return {"error": "TOO_MANY_EVIDENCE_REFS", "message": f"max {_MAX_EVIDENCE_REFS} evidence refs per operation"}  # noqa: E501
        try:
            parsed_refs = [EvidenceRef.model_validate(r) for r in evidence_refs]
        except ValidationError as exc:
            return {"error": "INVALID_EVIDENCE_REF", "message": str(exc)}

    db = ctx.db
    cas = ctx.cas

    # Preflight: validate previous_op_id before any filesystem side effect
    if previous_op_id and not db.get_operation(previous_op_id):
        return {"error": "INVALID_PARENT_OP_ID", "message": f"previous_op_id not found: {previous_op_id}"}

    # Auto-resolve previous_op_id from DB if not supplied by caller
    resolved_previous_op_id = previous_op_id
    if resolved_previous_op_id is None:
        latest = db.get_ops_by_file_path(canonical_old, limit=1)
        if latest:
            resolved_previous_op_id = dict(latest[0])["op_id"]

    dst = Path(new_path)
    dst.parent.mkdir(parents=True, exist_ok=True)
    content_bytes = src.read_bytes()
    try:
        object_hash = cas.store(content_bytes)
    except PayloadTooLargeError as exc:
        return {"error": "PAYLOAD_TOO_LARGE", "message": str(exc)}
    except BlobWriteError as exc:
        return {"error": "BLOB_WRITE_ERROR", "message": str(exc)}

    db.begin_immediate()
    try:
        # Insert invalidation op for old_path so hgp_file_history(old_path) records the move.
        inv_op_id = str(uuid.uuid4())
        inv_metadata = json.dumps({"reason": f"MOVE {canonical_old} → {canonical_new}"})
        db.insert_operation(
            op_id=inv_op_id, op_type="invalidation", agent_id=agent_id,
            commit_seq=db.next_commit_seq(), chain_hash="sha256:pending",
            metadata=inv_metadata, file_path=canonical_old,
            status="PENDING",
        )
        if resolved_previous_op_id:
            # Edge records intent; status update deferred until after rename() succeeds.
            db.insert_edge(inv_op_id, resolved_previous_op_id, "invalidates")
        inv_hash = compute_chain_hash(db, inv_op_id)
        db.update_chain_hash(inv_op_id, inv_hash)

        # Insert artifact op for new_path, causally linked to the invalidation op.
        op_id = str(uuid.uuid4())
        metadata = json.dumps({"reason": effective_reason})
        seq = db.next_commit_seq()
        db.insert_operation(
            op_id=op_id, op_type="artifact", agent_id=agent_id,
            commit_seq=seq, chain_hash="sha256:pending",
            object_hash=object_hash, metadata=metadata, file_path=canonical_new,
            status="PENDING",
        )
        db.insert_edge(op_id, inv_op_id, "causal")
        if parsed_refs:
            try:
                db.insert_evidence(op_id, parsed_refs)
            except ValueError as exc:
                db.rollback()
                return {"error": "INVALID_EVIDENCE_REF", "message": str(exc)}
            except sqlite3.IntegrityError:
                db.rollback()
                return {"error": "DUPLICATE_EVIDENCE_REF", "message": "Evidence link already exists"}
        final_hash = compute_chain_hash(db, op_id)
        db.update_chain_hash(op_id, final_hash)
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception as rb_exc:
            _log.error("ROLLBACK failed after transaction error: %s", rb_exc)
        raise

    # Filesystem rename happens AFTER DB commit; finalize both ops only on success.
    try:
        src.rename(dst)
    except OSError as exc:
        return {"error": "FILESYSTEM_ERROR", "message": str(exc), "op_id": op_id}

    # Rename succeeded — finalize all DB state in one atomic transaction.
    db.begin_immediate()
    try:
        if resolved_previous_op_id:
            db.update_operation_status(resolved_previous_op_id, "INVALIDATED")
        db.finalize_operation(inv_op_id)
        db.finalize_operation(op_id)
        db.commit()
    except Exception as exc:
        try:
            db.rollback()
        except Exception as rb_exc:
            _log.error("hgp_move_file: ROLLBACK failed after finalize error op_id=%s: %s", op_id, rb_exc)
        return {"error": "DB_FINALIZE_ERROR", "message": str(exc), "op_id": op_id}
    result = {
        "invalidation_op_id": inv_op_id,
        "op_id": op_id,
        "status": "COMPLETED",
        "commit_seq": seq,
        "object_hash": object_hash,
        "chain_hash": final_hash,
    }
    if not verbose:
        result.pop("object_hash", None)
        result.pop("chain_hash", None)
    return result


_VALID_MODES = {"on", "advisory", "off"}

_MODE_USAGE = (
    "usage: hgp mode [on|advisory|off]\n"
    "\n"
    "  (no args)   show current mode (default: on)\n"
    "  on          normal operation — all tools record to HGP\n"
    "  advisory    mutation tools return HGP_ADVISORY instead of recording\n"
    "  off         all tools return HGP_DISABLED\n"
)


def _read_mode() -> str:
    """Return current HGP mode: 'on', 'advisory', or 'off'. Default: 'on'.

    Reads <project_root>/.hgp/mode. Returns 'on' if absent or no project root.
    """
    try:
        ctx = _get_context()
        if ctx.project_root is None:
            return "on"
        mode_file = ctx.project_root / ".hgp" / "mode"
        if mode_file.exists():
            val = mode_file.read_text().strip()
            return val if val in _VALID_MODES else "on"
    except Exception:
        pass
    return "on"


def _check_mode(mutation: bool = True) -> dict[str, Any] | None:
    """Return an early-exit response if the current mode disables this call.

    mutation=True  → advisory mode blocks (HGP_ADVISORY), off mode blocks (HGP_DISABLED)
    mutation=False → advisory mode passes through, off mode blocks (HGP_DISABLED)

    Returns None if the call should proceed normally.
    """
    mode = _read_mode()
    if mode == "off":
        return {"status": "HGP_DISABLED", "message": "HGP is disabled. Run `hgp mode on` to resume."}
    if mode == "advisory" and mutation:
        return {"status": "HGP_ADVISORY", "message": "HGP is in advisory mode. Recording skipped."}
    return None


def _check_hgp_dir(file_path: str) -> dict[str, Any] | None:
    """Return an error if file_path is inside the .hgp/ internal directory.

    Agents must not write directly into .hgp/ — that directory is reserved for
    HGP internals (database, content store, mode file, etc.).
    """
    try:
        parts = Path(file_path).resolve().parts
    except Exception:
        parts = Path(file_path).parts
    if ".hgp" in parts:
        return {
            "error": "HGP_INTERNAL_PATH",
            "message": (
                f"Writing to the .hgp/ directory is not allowed: {file_path}. "
                "The .hgp/ directory is reserved for HGP internals."
            ),
        }
    return None


def _mode(args: list[str]) -> None:
    """Read or set the HGP mode (on / advisory / off)."""
    import sys

    if len(args) > 1 or (args and args[0] not in _VALID_MODES):
        print(f"hgp mode: invalid argument: {' '.join(args)}", file=sys.stderr)
        print(_MODE_USAGE, file=sys.stderr)
        sys.exit(1)

    try:
        project_root = find_project_root(Path.cwd())
    except ProjectRootError:
        print(
            "hgp mode: no git repository found from current directory.\n"
            "Run this command from inside a git repository.",
            file=sys.stderr,
        )
        sys.exit(1)

    mode_file = project_root / ".hgp" / "mode"

    if not args:
        if mode_file.exists():
            print(mode_file.read_text().strip())
        else:
            print("on")
        return

    new_mode = args[0]
    mode_file.parent.mkdir(parents=True, exist_ok=True)
    mode_file.write_text(new_mode)
    print(f"HGP mode set to: {new_mode}")


_INSTALL_HOOKS_USAGE = (
    "usage: hgp install-hooks [--claude] [--gemini]\n"
    "\n"
    "  (no flags)   install both Claude Code and Gemini CLI hooks\n"
    "  --claude     install Claude Code hooks only\n"
    "  --gemini     install Gemini CLI hooks only\n"
)

_VALID_INSTALL_FLAGS = {"--claude", "--gemini"}

_INSTALL_USAGE = (
    "usage: hgp install [--claude] [--gemini] [--codex] [--local]\n"
    "\n"
    "  (no flags)   install for Claude Code, Gemini CLI, and Codex (global scope)\n"
    "  --claude     Claude Code only\n"
    "  --gemini     Gemini CLI only\n"
    "  --codex      Codex only\n"
    "  --local      project-local scope (default: global/user scope)\n"
)

_VALID_INSTALL_ARGS = {"--claude", "--gemini", "--codex", "--local"}

_HGP_INSTRUCTIONS_BLOCK = """\
<!-- hgp-instructions-start -->
## HGP — History Graph Protocol

HGP is connected as an MCP server. Use HGP tools to record every significant
action and decision.

**Use HGP file tools instead of native file tools:**

| Native tool | HGP equivalent |
|-------------|----------------|
| Write / write_file | `hgp_write_file` |
| Edit / replace | `hgp_edit_file` |
| Bash append | `hgp_append_file` |

Record `hypothesis` operations for decisions, `artifact` for outputs,
`invalidation` when superseding prior work.

## Session Context (Subagent Support)

To propagate HGP context to spawned subagents, register a session root op at
the start of each session:

```
root = hgp_create_operation(op_type="hypothesis", agent_id="claude-code",
                            metadata={"description": "session root"})
hgp_set_context(root_op_id=root["op_id"], agent_id="claude-code",
                session_id="<session_id from SubagentStart/SubagentStop hook event>")
```

SubagentStart hooks will then inject the root op_id into every spawned
subagent's context automatically. Subagents should use
`parent_op_ids=[root_op_id]` for all `hgp_*` calls.
<!-- hgp-instructions-end -->"""


def _install_hooks_files(pkg: str, dest_dir: Path) -> list[str]:
    """Copy hook .py files from hgp.hooks.<pkg> package to dest_dir.

    Returns list of installed file paths (relative to dest_dir parent if possible).
    """
    import importlib.resources
    import shutil

    dest_dir.mkdir(parents=True, exist_ok=True)
    installed: list[str] = []
    pkg_ref = importlib.resources.files(f"hgp.hooks.{pkg}")
    for item in pkg_ref.iterdir():
        if item.name.endswith(".py") and not item.name.startswith("__"):
            dest = dest_dir / item.name
            with importlib.resources.as_file(item) as src:
                if dest.exists():
                    dest.unlink()
                shutil.copy2(src, dest)
            installed.append(str(dest))
    return installed


def _update_hooks_settings(client: str, settings_path: Path, hooks_dir: Path, scope: str) -> None:
    """Merge HGP hook entries into a client settings.json file.

    scope: "global" → absolute paths in hook commands
           "local"  → relative paths (relative to project root, i.e. .claude/hooks/...)
    """
    import sys

    hook_specs: dict[str, list[dict[str, Any]]] = {}
    if client == "claude":
        def _cmd(name: str) -> str:
            p = hooks_dir / name
            return f"python3 {p}" if scope == "global" else f"python3 .claude/hooks/{name}"

        # Claude Code hooks: PreBash/PostBash were removed; Bash lifecycle is now
        # handled via PreToolUse/PostToolUse with matcher="Bash".
        hook_specs = {
            "PreToolUse": [
                {"matcher": "", "hooks": [{"type": "command", "command": _cmd("pre_tool_use_hgp.py")}]},
                {"matcher": "Bash", "hooks": [{"type": "command", "command": _cmd("pre_bash_hgp.py")}]},
            ],
            "PostToolUse": [
                {"matcher": "Bash", "hooks": [{"type": "command", "command": _cmd("post_bash_hgp.py")}]},
            ],
            "SubagentStart": [
                {"matcher": "", "hooks": [{"type": "command", "command": _cmd("subagent_start_hgp.py")}]},
            ],
            "SubagentStop": [
                {"matcher": "", "hooks": [{"type": "command", "command": _cmd("subagent_stop_hgp.py")}]},
            ],
        }
    elif client == "gemini":
        def _gcmd(name: str) -> str:
            p = hooks_dir / name
            return f"python3 {p}" if scope == "global" else f"python3 .gemini/hooks/{name}"

        hook_specs = {
            "BeforeTool": [{"matcher": "", "hooks": [{"type": "command", "command": _gcmd("pre_tool_use_hgp.py")}]}],
            "AfterTool": [{"matcher": "", "hooks": [{"type": "command", "command": _gcmd("post_tool_use_hgp.py")}]}],
            "BeforeShell": [{"matcher": "", "hooks": [{"type": "command", "command": _gcmd("pre_bash_hgp.py")}]}],
            "AfterShell": [{"matcher": "", "hooks": [{"type": "command", "command": _gcmd("post_bash_hgp.py")}]}],
        }
    elif client == "codex":
        def _xdcmd(name: str) -> str:
            p = hooks_dir / name
            return f"python3 {p}" if scope == "global" else f"python3 .codex/hooks/{name}"

        # Codex hooks.json format: list of hook entries per event name.
        # PreToolUse/PostToolUse currently fire for Bash only; apply_patch support
        # is a known Codex bug (github.com/openai/codex/issues/16732).
        hook_specs = {
            "PreToolUse": [{"matcher": "", "hooks": [{"type": "command", "command": _xdcmd("pre_tool_use_hgp.py")}]}],
            "PostToolUse": [{"matcher": "", "hooks": [{"type": "command", "command": _xdcmd("post_tool_use_hgp.py")}]}],
        }

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[str, Any] = {}
    if settings_path.exists():
        try:
            existing = json.loads(settings_path.read_text())
        except json.JSONDecodeError as exc:
            print(f"  ✗ failed to parse {settings_path}: {exc}", file=sys.stderr)
            return

    existing_hooks = existing.get("hooks", {})

    # Remove HGP entries from deprecated event names (Claude Code removed PreBash/PostBash).
    # Warn if non-HGP custom entries remain — those are dead config that the user must migrate.
    _deprecated_event_map = {"PreBash": "PreToolUse", "PostBash": "PostToolUse"}
    if client == "claude":
        for deprecated, replacement in _deprecated_event_map.items():
            if deprecated in existing_hooks:
                cleaned = [
                    e for e in existing_hooks[deprecated]
                    if not any("_hgp.py" in h.get("command", "") for h in e.get("hooks", []))
                ]
                if cleaned:
                    existing_hooks[deprecated] = cleaned
                    custom_cmds = [
                        h.get("command", "")
                        for e in cleaned
                        for h in e.get("hooks", [])
                        if h.get("command")
                    ]
                    cmds_str = ", ".join(custom_cmds) if custom_cmds else f"{len(cleaned)} entry/entries"
                    print(
                        f"  ⚠ deprecated Claude hook event \"{deprecated}\" has {len(cleaned)} custom "
                        f"entry/entries that Claude Code no longer fires — migrate manually to {replacement}:\n"
                        f"    {cmds_str}",
                        file=sys.stderr,
                    )
                else:
                    del existing_hooks[deprecated]

    for event, hgp_entries in hook_specs.items():
        # Preserve non-HGP entries; replace HGP entries in place.
        non_hgp = [
            e for e in existing_hooks.get(event, [])
            if not any("_hgp.py" in h.get("command", "") for h in e.get("hooks", []))
        ]
        existing_hooks[event] = non_hgp + hgp_entries
    existing["hooks"] = existing_hooks
    settings_path.write_text(json.dumps(existing, indent=2) + "\n")


def _inject_instructions(md_path: Path) -> str:
    """Append or update the HGP instructions block in a markdown file.

    Returns "injected", "updated", or "already_current".
    """
    START = "<!-- hgp-instructions-start -->"
    END = "<!-- hgp-instructions-end -->"
    md_path.parent.mkdir(parents=True, exist_ok=True)
    content = md_path.read_text() if md_path.exists() else ""
    if START in content and END in content:
        # replace existing block
        before = content[: content.index(START)]
        after = content[content.index(END) + len(END):]
        new_content = before + _HGP_INSTRUCTIONS_BLOCK + after
        if new_content == content:
            return "already_current"
        md_path.write_text(new_content)
        return "updated"
    # append
    sep = "\n\n" if content and not content.endswith("\n\n") else ""
    md_path.write_text(content + sep + _HGP_INSTRUCTIONS_BLOCK + "\n")
    return "injected"


def _toml_set_key(toml_path: Path, section: str, key: str, value: str) -> str:
    """Ensure `key = value` exists inside `[section]` of a TOML file.

    Creates the file / section if absent. Returns "written", "updated", or
    "already_current".
    """
    toml_path.parent.mkdir(parents=True, exist_ok=True)
    text = toml_path.read_text() if toml_path.exists() else ""
    lines = text.splitlines(keepends=True)

    # Locate existing section
    section_header = f"[{section}]"
    try:
        start_idx = next(i for i, line in enumerate(lines) if line.strip() == section_header)
    except StopIteration:
        start_idx = None

    entry_line = f"{key} = {value}\n"

    if start_idx is None:
        # Append new section + key
        sep = "\n" if text and not text.endswith("\n") else ""
        toml_path.write_text(text + sep + f"\n{section_header}\n{entry_line}")
        return "written"

    # Find end of section
    end_idx = len(lines)
    for i in range(start_idx + 1, len(lines)):
        if lines[i].startswith("[") and not lines[i].startswith("[["):
            end_idx = i
            break

    section_lines = lines[start_idx + 1:end_idx]
    # Check if key already present with correct value
    for i, line in enumerate(section_lines):
        if line.split("=")[0].strip() == key:
            if line.strip() == f"{key} = {value}".strip():
                return "already_current"
            # Update existing key
            section_lines[i] = entry_line
            new_text = "".join(lines[:start_idx + 1]) + "".join(section_lines) + "".join(lines[end_idx:])
            toml_path.write_text(new_text)
            return "updated"

    # Key absent — insert at end of section
    insert_at = end_idx
    new_text = "".join(lines[:insert_at]) + entry_line + "".join(lines[insert_at:])
    toml_path.write_text(new_text)
    return "updated"


def _edit_codex_toml(toml_path: Path, python: str) -> str:
    """Write or update [mcp_servers.hgp] and enable hooks in a Codex config.toml.

    Returns "written", "updated", or "already_current".
    """
    SECTION = "[mcp_servers.hgp]"
    new_lines = [
        SECTION,
        f'command = "{python}"',
        'args = ["-m", "hgp.server"]',
    ]
    new_block = "\n".join(new_lines)

    toml_path.parent.mkdir(parents=True, exist_ok=True)
    if not toml_path.exists():
        toml_path.write_text(new_block + "\n")
        mcp_result = "written"
    else:
        text = toml_path.read_text()
        if SECTION not in text:
            sep = "\n\n" if text and not text.endswith("\n\n") else ""
            toml_path.write_text(text + sep + new_block + "\n")
            mcp_result = "written"
        else:
            # replace the existing section up to the next section header
            lines = text.splitlines(keepends=True)
            start_idx = next(i for i, line in enumerate(lines) if line.strip() == SECTION)
            end_idx = len(lines)
            for i in range(start_idx + 1, len(lines)):
                if lines[i].startswith("[") and not lines[i].startswith("[["):
                    end_idx = i
                    break
            original_block = "".join(lines[start_idx:end_idx]).rstrip()
            if original_block == new_block:
                mcp_result = "already_current"
            else:
                new_text = "".join(lines[:start_idx]) + new_block + "\n" + "".join(lines[end_idx:])
                toml_path.write_text(new_text)
                mcp_result = "updated"

    # Enable Codex lifecycle hooks (experimental feature, off by default)
    hooks_result = _toml_set_key(toml_path, "features", "codex_hooks", "true")

    if mcp_result == "already_current" and hooks_result == "already_current":
        return "already_current"
    return "written" if mcp_result == "written" else "updated"


def _install_mcp(client: str, scope: str, python: str) -> tuple[bool, str]:
    """Register HGP as an MCP server via the client CLI.

    Returns (success, message).
    """
    import shutil
    import subprocess

    if client == "claude":
        cli = shutil.which("claude")
        if not cli:
            return False, "claude CLI not found — skipping MCP registration"
        mcp_scope = "user" if scope == "global" else "local"
        cmd = [cli, "mcp", "add", f"--scope={mcp_scope}", "hgp", "--", python, "-m", "hgp.server"]
    elif client == "gemini":
        cli = shutil.which("gemini")
        if not cli:
            return False, "gemini CLI not found — skipping MCP registration"
        mcp_scope = "user" if scope == "global" else "project"
        cmd = [cli, "mcp", "add", f"--scope={mcp_scope}", "hgp", python, "-m", "hgp.server"]
    elif client == "codex":
        cli = shutil.which("codex")
        if not cli:
            return False, "codex CLI not found — skipping global MCP registration"
        cmd = [cli, "mcp", "add", "hgp", "--", python, "-m", "hgp.server"]
    else:
        return False, f"unknown client: {client}"

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        return True, "registered"
    stderr = result.stderr.strip()
    # many CLI tools print "already exists" or similar on re-registration
    if "already" in stderr.lower() or "exists" in stderr.lower():
        return True, "already registered"
    return False, f"CLI error: {stderr or result.stdout.strip()}"


def _install(args: list[str]) -> None:
    """Unified installer: MCP registration, hooks, and agent instructions."""
    import sys

    unknown = [a for a in args if a not in _VALID_INSTALL_ARGS]
    if unknown:
        print(f"hgp install: unknown flag(s): {' '.join(unknown)}", file=sys.stderr)
        print(_INSTALL_USAGE, file=sys.stderr)
        sys.exit(1)

    scope = "local" if "--local" in args else "global"
    do_claude = not any(a in args for a in ("--claude", "--gemini", "--codex")) or "--claude" in args
    do_gemini = not any(a in args for a in ("--claude", "--gemini", "--codex")) or "--gemini" in args
    do_codex = not any(a in args for a in ("--claude", "--gemini", "--codex")) or "--codex" in args

    python = sys.executable

    # resolve project root for local scope
    project_root: Path | None = None
    if scope == "local" or do_codex:
        try:
            project_root = find_project_root(Path.cwd())
        except ProjectRootError:
            if scope == "local":
                print(
                    "hgp install --local: no git repository found from current directory.\n"
                    "Run this command from inside a git repository.",
                    file=sys.stderr,
                )
                sys.exit(1)

    def _step(label: str, fn: "Callable[[], Any]") -> None:
        try:
            result = fn()
            if isinstance(result, tuple):
                ok_val: Any = result[0]  # type: ignore[misc]
                msg_val: Any = result[1]  # type: ignore[misc]
                status = "✓" if ok_val else "✗"
                print(f"  {status} {label}: {msg_val}")
            else:
                print(f"  ✓ {label}: {result}")
        except Exception as exc:
            print(f"  ✗ {label}: {exc}", file=sys.stderr)

    if do_claude:
        print("Claude Code:")
        _step("MCP", lambda: _install_mcp("claude", scope, python))
        if scope == "global":
            hooks_dir = Path.home() / ".claude" / "hooks"
            settings_path = Path.home() / ".claude" / "settings.json"
            md_path = Path.home() / ".claude" / "CLAUDE.md"
        else:
            assert project_root is not None
            hooks_dir = project_root / ".claude" / "hooks"
            settings_path = project_root / ".claude" / "settings.json"
            md_path = project_root / "CLAUDE.md"
        _step("hooks files", lambda d=hooks_dir: f"installed {len(_install_hooks_files('claude', d))} file(s) → {d}")
        _step("hooks settings", lambda: (_update_hooks_settings("claude", settings_path, hooks_dir, scope), "updated")[1])  # noqa: E501
        _step("instructions", lambda p=md_path: _inject_instructions(p))

    if do_gemini:
        print("Gemini CLI:")
        _step("MCP", lambda: _install_mcp("gemini", scope, python))
        if scope == "global":
            hooks_dir = Path.home() / ".gemini" / "hooks"
            settings_path = Path.home() / ".gemini" / "settings.json"
            md_path = Path.home() / ".gemini" / "GEMINI.md"
        else:
            assert project_root is not None
            hooks_dir = project_root / ".gemini" / "hooks"
            settings_path = project_root / ".gemini" / "settings.json"
            md_path = project_root / "GEMINI.md"
        _step("hooks files", lambda d=hooks_dir: f"installed {len(_install_hooks_files('gemini', d))} file(s) → {d}")
        _step("hooks settings", lambda: (_update_hooks_settings("gemini", settings_path, hooks_dir, scope), "updated")[1])  # noqa: E501
        _step("instructions", lambda p=md_path: _inject_instructions(p))

    if do_codex:
        print("Codex:")
        if scope == "global":
            _step("MCP", lambda: _install_mcp("codex", scope, python))
            global_toml = Path.home() / ".codex" / "config.toml"
            _step("hooks feature flag", lambda p=global_toml: _toml_set_key(p, "features", "codex_hooks", "true"))
            hooks_dir = Path.home() / ".codex" / "hooks"
            hooks_json = Path.home() / ".codex" / "hooks.json"
            md_path = Path.home() / ".codex" / "AGENTS.md"
        else:
            assert project_root is not None
            toml_path = project_root / ".codex" / "config.toml"
            _step("MCP (config.toml)", lambda p=toml_path: _edit_codex_toml(p, python))
            hooks_dir = project_root / ".codex" / "hooks"
            hooks_json = project_root / ".codex" / "hooks.json"
            md_path = project_root / "AGENTS.md"
        _step("hooks files", lambda d=hooks_dir: f"installed {len(_install_hooks_files('codex', d))} file(s) → {d}")
        _step("hooks settings", lambda: (_update_hooks_settings("codex", hooks_json, hooks_dir, scope), "updated")[1])  # noqa: E501
        _step("instructions", lambda p=md_path: _inject_instructions(p))


def _install_hooks(args: list[str]) -> None:
    """Install HGP hook files into .claude/hooks/ and/or .gemini/hooks/."""
    import importlib.resources
    import shutil
    import sys

    unknown = [a for a in args if a not in _VALID_INSTALL_FLAGS]
    if unknown:
        print(f"hgp install-hooks: unknown flag(s): {' '.join(unknown)}", file=sys.stderr)
        print(_INSTALL_HOOKS_USAGE, file=sys.stderr)
        sys.exit(1)

    do_claude = not args or "--claude" in args
    do_gemini = not args or "--gemini" in args

    try:
        project_root = find_project_root(Path.cwd())
    except ProjectRootError:
        print(
            "hgp install-hooks: no git repository found from current directory.\n"
            "Run this command from inside a git repository.",
            file=sys.stderr,
        )
        sys.exit(1)

    installed: list[str] = []

    def _copy_hooks(pkg: str, dest_dir: Path) -> None:
        dest_dir.mkdir(parents=True, exist_ok=True)
        pkg_ref = importlib.resources.files(f"hgp.hooks.{pkg}")
        for item in pkg_ref.iterdir():
            if item.name.endswith(".py") and not item.name.startswith("__"):
                dest = dest_dir / item.name
                with importlib.resources.as_file(item) as src:
                    shutil.copy2(src, dest)
                installed.append(str(dest.relative_to(project_root)))

    if do_claude:
        _copy_hooks("claude", project_root / ".claude" / "hooks")
    if do_gemini:
        _copy_hooks("gemini", project_root / ".gemini" / "hooks")

    if installed:
        print("Installed HGP hooks:")
        for p in installed:
            print(f"  {p}")
    else:
        print("No hooks installed.", file=sys.stderr)


_VALID_HOOK_POLICIES = {"advisory", "block"}

_HOOK_POLICY_USAGE = (
    "usage: hgp hook-policy [advisory|block]\n"
    "\n"
    "  (no args)   show current policy\n"
    "  advisory    warn only — native file tools allowed (default)\n"
    "  block       block native file tools (Write/Edit/write_file/replace)\n"
)


def _hook_policy(args: list[str]) -> None:
    """Read or set the persistent hook enforcement policy."""
    import sys

    if len(args) > 1 or (args and args[0] not in _VALID_HOOK_POLICIES):
        print(f"hgp hook-policy: invalid argument: {' '.join(args)}", file=sys.stderr)
        print(_HOOK_POLICY_USAGE, file=sys.stderr)
        sys.exit(1)

    try:
        project_root = find_project_root(Path.cwd())
    except ProjectRootError:
        print(
            "hgp hook-policy: no git repository found from current directory.\n"
            "Run this command from inside a git repository.",
            file=sys.stderr,
        )
        sys.exit(1)

    policy_file = project_root / ".hgp" / "hook-policy"

    if not args:
        # read current policy
        if policy_file.exists():
            print(policy_file.read_text().strip())
        else:
            print("advisory")
        return

    new_policy = args[0]
    policy_file.parent.mkdir(parents=True, exist_ok=True)
    policy_file.write_text(new_policy)
    print(f"Hook policy set to: {new_policy}")

    # warn if installed hooks predate hook-policy support (policy enforcement broken)
    _hook_clients = [
        (project_root / ".claude" / "hooks" / "pre_tool_use_hgp.py", "--claude"),
        (project_root / ".gemini" / "hooks" / "pre_tool_use_hgp.py", "--gemini"),
        (project_root / ".codex" / "hooks" / "pre_tool_use_hgp.py", "--codex"),
    ]
    stale_policy: list[tuple[str, str]] = []
    for hook_path, flag in _hook_clients:
        if hook_path.exists() and not re.search(
                r"^def\s+_resolve_block_mode\s*\(",
                hook_path.read_text(),
                re.MULTILINE,
            ):
            stale_policy.append((str(hook_path.relative_to(project_root)), flag))
    if stale_policy:
        lines = "".join(f"  {p}  →  hgp install {flag} --local\n" for p, flag in stale_policy)
        print(
            "\nWarning: the following installed hook(s) predate hook-policy support\n"
            "and will not honor the advisory/block policy until reinstalled:\n"
            + lines
            + "Run the indicated command to update each hook.",
            file=sys.stderr,
        )
    # warn if post_tool_use_hgp.py is missing (agent-context advisory degraded, not policy)
    # only emit the "policy still works" sentence when the pre hook is current;
    # if the pre hook is stale, the stale-policy warning above already covers the
    # combined state and the "still works" claim would be contradictory.
    post_tool_use = project_root / ".gemini" / "hooks" / "post_tool_use_hgp.py"
    gemini_pre = project_root / ".gemini" / "hooks" / "pre_tool_use_hgp.py"
    gemini_pre_stale = any(
        ".gemini" in p for p, _ in stale_policy
    )
    if gemini_pre.exists() and not post_tool_use.exists():
        if gemini_pre_stale:
            # stale pre hook already diagnosed; just note the post hook is also absent
            print(
                "\nWarning: .gemini/hooks/post_tool_use_hgp.py is also missing.\n"
                "Run `hgp install-hooks --gemini` to install all current hooks.",
                file=sys.stderr,
            )
        else:
            print(
                "\nWarning: .gemini/hooks/post_tool_use_hgp.py is missing.\n"
                "Advisory/block policy enforcement still works, but the agent will not\n"
                "receive in-context warnings after native file tool use.\n"
                "Run `hgp install-hooks --gemini` to add it.",
                file=sys.stderr,
            )


# ── Backup / Restore / Export / Import ───────────────────────────────────────

# Files that represent history data (backed up).
_BACKUP_HISTORY_FILES = frozenset({"hgp.db", ".hgp_content", "project-meta"})
# Files that represent operational/machine-local state (excluded from backup).
_BACKUP_OPERATIONAL_FILES = frozenset({"mode", "hook-policy"})

_BACKUP_USAGE = """\
Usage:
  hgp backup                          back up .hgp/ to ~/.hgp/projects/<id>/
  hgp restore [--project-id <id>] [--force]
                                       restore from local backup
  hgp export <dest>                   export history snapshot to <dest>/
  hgp import <source> [--force]       import a history snapshot
"""


def _get_projects_dir() -> Path:
    """Return global HGP projects backup directory.

    Overridable via HGP_PROJECTS_DIR env var for test isolation.
    """
    env = os.environ.get("HGP_PROJECTS_DIR")
    return Path(env) if env else Path.home() / ".hgp" / "projects"


def _sqlite_backup(src_db: Path, dst_db: Path) -> None:
    """WAL-safe SQLite hot-backup: src_db → dst_db.

    Uses sqlite3.Connection.backup() which holds an internal shared-cache lock
    and produces a consistent snapshot regardless of WAL/SHM state.
    """
    dst_db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(src_db)) as src, sqlite3.connect(str(dst_db)) as dst:
        src.backup(dst)


def _read_project_meta(hgp_dir: Path) -> dict[str, Any] | None:
    """Read project-meta JSON from hgp_dir. Returns None if absent or invalid."""
    meta_file = hgp_dir / "project-meta"
    if not meta_file.exists():
        return None
    try:
        return json.loads(meta_file.read_text())  # type: ignore[no-any-return]
    except (json.JSONDecodeError, OSError):
        return None


def _write_project_meta(hgp_dir: Path, project_root: Path) -> dict[str, Any]:
    """Return existing project-meta or create a new one.

    Preserves existing project_id. Refreshes git_remote and hgp_version.
    """
    try:
        from importlib.metadata import version as _pkg_version
        hgp_version: str = _pkg_version("history-graph-protocol")
    except Exception:
        hgp_version = "unknown"

    meta_file = hgp_dir / "project-meta"
    existing = _read_project_meta(hgp_dir)
    project_id: str = (existing or {}).get("project_id") or str(uuid.uuid4())

    git_remote: str | None = None
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=project_root, capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            git_remote = result.stdout.strip() or None
    except (OSError, subprocess.TimeoutExpired):
        pass

    repo_name: str = (
        git_remote.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
        if git_remote else project_root.name
    )

    meta: dict[str, Any] = {
        "project_id": project_id,
        "git_remote": git_remote,
        "repo_name": repo_name,
        "hgp_version": hgp_version,
    }
    hgp_dir.mkdir(parents=True, exist_ok=True)
    meta_file.write_text(json.dumps(meta, indent=2))
    return meta


def _get_git_remote(repo_root: Path) -> str | None:
    """Return git remote 'origin' URL for repo_root, or None."""
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=repo_root, capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _check_compatibility(
    source_meta: dict[str, Any] | None,
    current_root: Path,
) -> Literal["compatible", "mismatch", "unverifiable"]:
    """Compare source snapshot metadata against the current repo.

    Returns:
        "compatible"   — git remote URLs match; safe to proceed automatically.
        "mismatch"     — remote URLs differ; require --force.
        "unverifiable" — one or both sides lack a remote URL, or source has no
                         project-meta; require --force.
    """
    if source_meta is None:
        return "unverifiable"
    src_remote: str | None = source_meta.get("git_remote")
    if not src_remote:
        return "unverifiable"
    cur_remote = _get_git_remote(current_root)
    if not cur_remote:
        return "unverifiable"
    return "compatible" if src_remote == cur_remote else "mismatch"


def _copy_history_to(hgp_dir: Path, dest_dir: Path) -> None:
    """Copy history data (hgp.db via SQLite API, .hgp_content/, project-meta) to dest_dir.

    Operational files (mode, hook-policy) are excluded.
    dest_dir must not exist yet (caller responsibility).
    """
    dest_dir.mkdir(parents=True, exist_ok=True)

    src_db = hgp_dir / "hgp.db"
    if src_db.exists():
        _sqlite_backup(src_db, dest_dir / "hgp.db")

    src_content = hgp_dir / ".hgp_content"
    if src_content.exists():
        shutil.copytree(src_content, dest_dir / ".hgp_content")

    src_meta = hgp_dir / "project-meta"
    if src_meta.exists():
        shutil.copy2(src_meta, dest_dir / "project-meta")


def _restore_snapshot(source_dir: Path, repo_root: Path) -> None:
    """Atomically replace repo_root/.hgp/ with history data from source_dir.

    Pattern:
        1. Populate .hgp_restore_tmp/ (same filesystem → rename is atomic)
        2. Rename existing .hgp/ → .hgp_old/
        3. Rename .hgp_restore_tmp/ → .hgp/
        4. Remove .hgp_old/

    Operational files in the current .hgp/ (mode, hook-policy) are preserved:
    they are copied from the existing .hgp/ into the new one after the rename.
    """
    hgp_dir = repo_root / ".hgp"
    tmp_dir = repo_root / ".hgp_restore_tmp"
    old_dir = repo_root / ".hgp_old"

    # Clean up any leftover temp dirs from a previous failed attempt.
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    if old_dir.exists():
        shutil.rmtree(old_dir)

    # Preserve operational files from existing .hgp/ before swapping.
    preserved: dict[str, str] = {}
    if hgp_dir.exists():
        for name in _BACKUP_OPERATIONAL_FILES:
            f = hgp_dir / name
            if f.exists():
                preserved[name] = f.read_text()

    # Build the new .hgp/ content in tmp_dir.
    _copy_history_to(source_dir, tmp_dir)

    # Write back preserved operational files into tmp_dir.
    for name, content in preserved.items():
        (tmp_dir / name).write_text(content)

    # Atomic swap: existing → old, tmp → .hgp, remove old.
    if hgp_dir.exists():
        hgp_dir.rename(old_dir)
    try:
        tmp_dir.rename(hgp_dir)
    except Exception:
        # Roll back: restore the original .hgp/ if rename failed.
        if old_dir.exists():
            old_dir.rename(hgp_dir)
        raise
    if old_dir.exists():
        shutil.rmtree(old_dir)


def _validate_snapshot_source(source: Path, cmd: str) -> None:
    """Exit with a user-facing error if source is not a valid HGP snapshot.

    Called before any destructive swap so the existing .hgp/ is never touched
    when the source is obviously wrong (a plain file, empty directory, etc.).
    """
    import sys

    if not source.is_dir():
        print(
            f"hgp {cmd}: source is not a directory: {source}",
            file=sys.stderr,
        )
        sys.exit(1)
    if not (source / "hgp.db").exists():
        print(
            f"hgp {cmd}: source does not look like an HGP snapshot "
            f"(hgp.db not found in {source})",
            file=sys.stderr,
        )
        sys.exit(1)


def _remove_dest(path: Path) -> None:
    """Remove path whether it is a directory, regular file, or symlink."""
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def _discover_backup(repo_root: Path) -> list[tuple[str, Path]]:
    """Scan ~/.hgp/projects/ for backups whose git_remote matches this repo.

    Returns list of (project_id, backup_dir) tuples.

    If the current repo has no origin remote, auto-discovery is disabled to
    prevent accidentally restoring an unrelated backup. The caller must supply
    --project-id explicitly in that case.
    """
    cur_remote = _get_git_remote(repo_root)
    if not cur_remote:
        return []
    projects_dir = _get_projects_dir()
    if not projects_dir.exists():
        return []
    matches: list[tuple[str, Path]] = []
    for entry in sorted(projects_dir.iterdir()):
        if not entry.is_dir():
            continue
        meta = _read_project_meta(entry)
        if meta is None:
            continue
        project_id = meta.get("project_id", entry.name)
        if meta.get("git_remote") == cur_remote:
            matches.append((project_id, entry))
    return matches


def _hgp_backup(args: list[str]) -> None:
    """Back up .hgp/ history data to ~/.hgp/projects/<project_id>/."""
    import sys

    force = "--force" in args
    extra = [a for a in args if a != "--force"]
    if extra:
        print(f"hgp backup: unexpected arguments: {' '.join(extra)}", file=sys.stderr)
        print(_BACKUP_USAGE, file=sys.stderr)
        sys.exit(1)

    try:
        project_root = find_project_root(Path.cwd())
    except ProjectRootError:
        print(
            "hgp backup: no git repository found from current directory.\n"
            "Run this command from inside a git repository.",
            file=sys.stderr,
        )
        sys.exit(1)

    hgp_dir = project_root / ".hgp"
    if not hgp_dir.exists():
        print("hgp backup: no .hgp/ directory found. Nothing to back up.", file=sys.stderr)
        sys.exit(1)

    meta = _write_project_meta(hgp_dir, project_root)
    project_id: str = meta["project_id"]

    dest = _get_projects_dir() / project_id
    if dest.exists():
        if not force:
            print(
                f"hgp backup: backup already exists at {dest}\n"
                "Use --force to overwrite.",
                file=sys.stderr,
            )
            sys.exit(1)
        _remove_dest(dest)

    _copy_history_to(hgp_dir, dest)
    repo_name = meta.get("repo_name", project_root.name)
    print(f"Backed up '{repo_name}' → {dest}")


def _hgp_restore(args: list[str]) -> None:
    """Restore .hgp/ from a local backup in ~/.hgp/projects/."""
    import sys

    force = "--force" in args
    remaining = [a for a in args if a != "--force"]

    project_id: str | None = None
    if "--project-id" in remaining:
        idx = remaining.index("--project-id")
        if idx + 1 >= len(remaining):
            print("hgp restore: --project-id requires a value.", file=sys.stderr)
            sys.exit(1)
        project_id = remaining[idx + 1]
        remaining = remaining[:idx] + remaining[idx + 2:]

    if remaining:
        print(f"hgp restore: unexpected arguments: {' '.join(remaining)}", file=sys.stderr)
        print(_BACKUP_USAGE, file=sys.stderr)
        sys.exit(1)

    try:
        project_root = find_project_root(Path.cwd())
    except ProjectRootError:
        print(
            "hgp restore: no git repository found from current directory.\n"
            "Run this command from inside a git repository.",
            file=sys.stderr,
        )
        sys.exit(1)

    hgp_dir = project_root / ".hgp"

    # Determine backup_dir
    if project_id:
        backup_dir = _get_projects_dir() / project_id
        if not backup_dir.exists():
            print(f"hgp restore: no backup found for project-id '{project_id}'.", file=sys.stderr)
            sys.exit(1)
    else:
        # Try reading project_id from existing .hgp/project-meta
        existing_meta = _read_project_meta(hgp_dir) if hgp_dir.exists() else None
        local_id: str | None = (existing_meta or {}).get("project_id")
        if local_id:
            candidate = _get_projects_dir() / local_id
            backup_dir = candidate if candidate.exists() else None  # type: ignore[assignment]
        else:
            backup_dir = None  # type: ignore[assignment]

        if backup_dir is None:
            # Auto-discover by git remote
            candidates = _discover_backup(project_root)
            if not candidates:
                print(
                    "hgp restore: no backup found for this repository.\n"
                    "Run `hgp backup` first.",
                    file=sys.stderr,
                )
                sys.exit(1)
            if len(candidates) > 1:
                lines = "\n".join(f"  {pid}  ({d})" for pid, d in candidates)
                print(
                    "hgp restore: multiple backups found. Specify one with --project-id:\n"
                    + lines,
                    file=sys.stderr,
                )
                sys.exit(1)
            backup_dir = candidates[0][1]

    _validate_snapshot_source(backup_dir, "restore")

    # "already exists" guard first — protecting existing data takes priority.
    if hgp_dir.exists() and not force:
        print(
            f"hgp restore: .hgp/ already exists at {hgp_dir}\n"
            "Use --force to overwrite.",
            file=sys.stderr,
        )
        sys.exit(1)

    source_meta = _read_project_meta(backup_dir)
    compat = _check_compatibility(source_meta, project_root)
    if compat != "compatible" and not force:
        if compat == "mismatch":
            src_remote = (source_meta or {}).get("git_remote", "unknown")
            cur_remote = _get_git_remote(project_root) or "none"
            print(
                f"hgp restore: git remote mismatch.\n"
                f"  backup remote : {src_remote}\n"
                f"  current remote: {cur_remote}\n"
                "Use --force to restore anyway.",
                file=sys.stderr,
            )
        else:
            print(
                "hgp restore: cannot verify compatibility (missing git remote or project-meta).\n"
                "Use --force to restore anyway.",
                file=sys.stderr,
            )
        sys.exit(1)

    _restore_snapshot(backup_dir, project_root)
    repo_name = (source_meta or {}).get("repo_name", project_root.name)
    print(f"Restored '{repo_name}' from {backup_dir}")


def _hgp_export(args: list[str]) -> None:
    """Export .hgp/ history snapshot to a specified directory."""
    import sys

    force = "--force" in args
    remaining = [a for a in args if a != "--force"]
    if len(remaining) != 1:
        print("hgp export: requires exactly one destination path.", file=sys.stderr)
        print(_BACKUP_USAGE, file=sys.stderr)
        sys.exit(1)

    dest = Path(remaining[0]).expanduser()

    try:
        project_root = find_project_root(Path.cwd())
    except ProjectRootError:
        print(
            "hgp export: no git repository found from current directory.\n"
            "Run this command from inside a git repository.",
            file=sys.stderr,
        )
        sys.exit(1)

    hgp_dir = project_root / ".hgp"
    if not hgp_dir.exists():
        print("hgp export: no .hgp/ directory found. Nothing to export.", file=sys.stderr)
        sys.exit(1)

    meta = _write_project_meta(hgp_dir, project_root)

    if dest.exists():
        if not force:
            print(
                f"hgp export: destination already exists: {dest}\n"
                "Use --force to overwrite.",
                file=sys.stderr,
            )
            sys.exit(1)
        _remove_dest(dest)

    _copy_history_to(hgp_dir, dest)
    repo_name = meta.get("repo_name", project_root.name)
    print(f"Exported '{repo_name}' → {dest}")


def _hgp_import(args: list[str]) -> None:
    """Import a history snapshot into the current repo's .hgp/."""
    import sys

    force = "--force" in args
    remaining = [a for a in args if a != "--force"]
    if len(remaining) != 1:
        print("hgp import: requires exactly one source path.", file=sys.stderr)
        print(_BACKUP_USAGE, file=sys.stderr)
        sys.exit(1)

    source = Path(remaining[0]).expanduser()
    if not source.exists():
        print(f"hgp import: source path does not exist: {source}", file=sys.stderr)
        sys.exit(1)
    _validate_snapshot_source(source, "import")

    try:
        project_root = find_project_root(Path.cwd())
    except ProjectRootError:
        print(
            "hgp import: no git repository found from current directory.\n"
            "Run this command from inside a git repository.",
            file=sys.stderr,
        )
        sys.exit(1)

    source_meta = _read_project_meta(source)
    compat = _check_compatibility(source_meta, project_root)
    if compat != "compatible" and not force:
        if compat == "mismatch":
            src_remote = (source_meta or {}).get("git_remote", "unknown")
            cur_remote = _get_git_remote(project_root) or "none"
            print(
                f"hgp import: git remote mismatch.\n"
                f"  source remote : {src_remote}\n"
                f"  current remote: {cur_remote}\n"
                "Use --force to import anyway.",
                file=sys.stderr,
            )
        else:
            print(
                "hgp import: cannot verify compatibility (missing git remote or project-meta).\n"
                "Use --force to import anyway.",
                file=sys.stderr,
            )
        sys.exit(1)

    hgp_dir = project_root / ".hgp"
    if hgp_dir.exists() and not force:
        print(
            f"hgp import: .hgp/ already exists at {hgp_dir}\n"
            "Use --force to overwrite.",
            file=sys.stderr,
        )
        sys.exit(1)

    _restore_snapshot(source, project_root)
    repo_name = (source_meta or {}).get("repo_name", project_root.name)
    print(f"Imported '{repo_name}' from {source}")


def run() -> None:
    """Entry point for `hgp` console script.

    Usage:
        hgp                          # start MCP server (stdio)
        hgp install                  # register MCP + install hooks + inject instructions
        hgp install --claude         # Claude Code only
        hgp install --gemini         # Gemini CLI only
        hgp install --codex          # Codex only
        hgp install --local          # project-local scope
        hgp install-hooks            # (deprecated) install hook files only
        hgp hook-policy              # show current hook enforcement policy
        hgp hook-policy advisory     # warn only (default)
        hgp hook-policy block        # block native file tools
        hgp backup                   # back up .hgp/ to ~/.hgp/projects/<id>/
        hgp restore                  # restore .hgp/ from local backup
        hgp export <dest>            # export history snapshot to <dest>/
        hgp import <source>          # import a history snapshot
    """
    import sys

    args = sys.argv[1:]
    if args and args[0] == "install":
        _install(args[1:])
    elif args and args[0] == "mode":
        _mode(args[1:])
    elif args and args[0] == "install-hooks":
        print(
            "Warning: `hgp install-hooks` is deprecated. Use `hgp install` instead.",
            file=sys.stderr,
        )
        _install_hooks(args[1:])
    elif args and args[0] == "hook-policy":
        _hook_policy(args[1:])
    elif args and args[0] == "backup":
        _hgp_backup(args[1:])
    elif args and args[0] == "restore":
        _hgp_restore(args[1:])
    elif args and args[0] == "export":
        _hgp_export(args[1:])
    elif args and args[0] == "import":
        _hgp_import(args[1:])
    else:
        mcp.run()


if __name__ == "__main__":
    run()
