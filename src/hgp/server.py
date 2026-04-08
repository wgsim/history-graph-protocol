"""HGP MCP Server — FastMCP entry point."""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

from pydantic import ValidationError
from mcp.server.fastmcp import FastMCP

_VALID_OP_TYPES = frozenset({"artifact", "hypothesis", "merge", "invalidation"})
_VALID_STATUSES = frozenset({"PENDING", "COMPLETED", "INVALIDATED", "MISSING_BLOB", "STALE_PENDING"})
_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_MAX_TTL_SECONDS = 86400
# Limits evidence refs per operation to cap O(N) existence checks inside BEGIN IMMEDIATE.
_MAX_EVIDENCE_REFS = 50
_MAX_QUERY_LIMIT = 1000
_MAX_SUBGRAPH_DEPTH = 500

from hgp.cas import CAS
from hgp.dag import compute_chain_hash, get_ancestors, get_descendants
from hgp.db import Database
from hgp.errors import BlobWriteError, ChainStaleError, InvalidationTargetNotFoundError, ParentNotFoundError, PayloadTooLargeError
from hgp.lease import LeaseManager
from hgp.models import EvidenceRef
from hgp.project import find_project_root, assert_within_root, canonical_file_path, ProjectRootError, PathOutsideRootError
from hgp.reconciler import Reconciler

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
                _log.warning(
                    "No .git repository found from cwd; using global store ~/.hgp/. "
                    "Set HGP_PROJECT_ROOT or run from inside a git repository for repo-local storage."
                )
                project_root = None
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
) -> dict[str, Any]:
    """Create a new operation in the causal history DAG."""
    if op_type not in _VALID_OP_TYPES:
        return {"error": "INVALID_OP_TYPE", "message": f"op_type must be one of {sorted(_VALID_OP_TYPES)}"}

    # Validate evidence_refs early (before any DB work) to fail fast on bad input
    parsed_refs: list[EvidenceRef] = []
    if evidence_refs:
        if len(evidence_refs) > _MAX_EVIDENCE_REFS:
            return {"error": "TOO_MANY_EVIDENCE_REFS", "message": f"max {_MAX_EVIDENCE_REFS} evidence refs per operation"}
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
                return {"error": "CHAIN_STALE", "message": f"CHAIN_STALE (under lock): expected {chain_hash}, got {current}"}

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

    return {
        "op_id": op_id,
        "status": "COMPLETED",
        "commit_seq": seq,
        "object_hash": object_hash,
        "chain_hash": final_chain_hash,
    }


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
    """Query operations with optional filters. By default excludes inactive-tier ops; pass include_inactive=True to include them."""
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
    lease_mgr = _get_context().lease_mgr
    return lease_mgr.validate(lease_id, extend=extend)


@mcp.tool()
def hgp_release_lease(lease_id: str) -> dict[str, Any]:
    """Release a lease token explicitly."""
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


@mcp.tool()
def hgp_reconcile(dry_run: bool = False) -> dict[str, Any]:
    """Run crash recovery reconciler."""
    reconciler = _get_context().reconciler
    report = reconciler.reconcile(dry_run=dry_run)
    return report.model_dump()


@mcp.tool()
def hgp_get_evidence(op_id: str) -> dict[str, Any]:
    """Return all operations that op_id cited as evidence."""
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
            return {"error": "TOO_MANY_EVIDENCE_REFS", "message": f"max {_MAX_EVIDENCE_REFS} evidence refs per operation"}
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
) -> dict[str, Any]:
    """Write (create or overwrite) a file and record it as an artifact operation."""
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
    return result


@mcp.tool()
def hgp_append_file(
    file_path: str,
    content: str,
    agent_id: str,
    reason: str | None = None,
    parent_op_ids: list[str] | None = None,
    evidence_refs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Append content to a file (creates it if absent) and record as artifact."""
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
) -> dict[str, Any]:
    """Replace the first (and only) occurrence of old_string with new_string."""
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
    return result


@mcp.tool()
def hgp_delete_file(
    file_path: str,
    agent_id: str,
    previous_op_id: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """Delete a file and record an invalidation operation."""
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
    return {
        "op_id": op_id,
        "status": "COMPLETED",
        "commit_seq": seq,
        "chain_hash": final_hash,
    }


@mcp.tool()
def hgp_move_file(
    old_path: str,
    new_path: str,
    agent_id: str,
    previous_op_id: str | None = None,
    reason: str | None = None,
    evidence_refs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Move/rename a file: invalidates old path op, creates new artifact op."""
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
            return {"error": "TOO_MANY_EVIDENCE_REFS", "message": f"max {_MAX_EVIDENCE_REFS} evidence refs per operation"}
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
    return {
        "invalidation_op_id": inv_op_id,
        "op_id": op_id,
        "status": "COMPLETED",
        "commit_seq": seq,
        "object_hash": object_hash,
        "chain_hash": final_hash,
    }


_INSTALL_HOOKS_USAGE = (
    "usage: hgp install-hooks [--claude] [--gemini]\n"
    "\n"
    "  (no flags)   install both Claude Code and Gemini CLI hooks\n"
    "  --claude     install Claude Code hooks only\n"
    "  --gemini     install Gemini CLI hooks only\n"
)

_VALID_INSTALL_FLAGS = {"--claude", "--gemini"}


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

    # warn if installed hooks predate hook-policy support
    stale: list[str] = []
    for hook_path in [
        project_root / ".claude" / "hooks" / "pre_tool_use_hgp.py",
        project_root / ".gemini" / "hooks" / "pre_tool_use_hgp.py",
    ]:
        if hook_path.exists() and "def _resolve_block_mode" not in hook_path.read_text():
            stale.append(str(hook_path.relative_to(project_root)))
    if stale:
        print(
            "\nWarning: the following installed hook(s) predate hook-policy support\n"
            "and will not honor the new policy until reinstalled:\n"
            + "".join(f"  {p}\n" for p in stale)
            + "Run `hgp install-hooks` to update them.",
            file=sys.stderr,
        )


def run() -> None:
    """Entry point for `hgp` console script.

    Usage:
        hgp                          # start MCP server (stdio)
        hgp install-hooks            # install both Claude Code and Gemini CLI hooks
        hgp install-hooks --claude   # Claude Code hooks only
        hgp install-hooks --gemini   # Gemini CLI hooks only
        hgp hook-policy              # show current hook enforcement policy
        hgp hook-policy advisory     # warn only (default)
        hgp hook-policy block        # block native file tools
    """
    import sys

    args = sys.argv[1:]
    if args and args[0] == "install-hooks":
        _install_hooks(args[1:])
    elif args and args[0] == "hook-policy":
        _hook_policy(args[1:])
    else:
        mcp.run()


if __name__ == "__main__":
    run()
