"""HGP MCP Server — FastMCP entry point."""

from __future__ import annotations

import base64
import json
import logging
import re
import sqlite3
import uuid
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

from pydantic import ValidationError
from mcp.server.fastmcp import FastMCP

_VALID_OP_TYPES = frozenset({"artifact", "hypothesis", "merge", "invalidation"})
_VALID_STATUSES = frozenset({"PENDING", "COMPLETED", "INVALIDATED", "MISSING_BLOB"})
_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_MAX_TTL_SECONDS = 86400
# Limits evidence refs per operation to cap O(N) existence checks inside BEGIN IMMEDIATE.
_MAX_EVIDENCE_REFS = 50

from hgp.cas import CAS
from hgp.dag import compute_chain_hash, get_ancestors, get_descendants
from hgp.db import Database
from hgp.errors import ChainStaleError, ParentNotFoundError
from hgp.lease import LeaseManager
from hgp.models import EvidenceRef
from hgp.reconciler import Reconciler

# ── Server initialization ───────────────────────────────────

HGP_DIR = Path.home() / ".hgp"
HGP_CONTENT_DIR = HGP_DIR / ".hgp_content"
HGP_DB_PATH = HGP_DIR / "hgp.db"

mcp = FastMCP("hgp")

_db: Database | None = None
_cas: CAS | None = None
_lease_mgr: LeaseManager | None = None
_reconciler: Reconciler | None = None


def _get_components() -> tuple[Database, CAS, LeaseManager, Reconciler]:
    global _db, _cas, _lease_mgr, _reconciler
    if _db is None:
        # Use locals to avoid partial global state on failure: only assign globals
        # after all components initialize successfully.
        db = Database(HGP_DB_PATH)
        try:
            HGP_DIR.mkdir(parents=True, exist_ok=True)
            HGP_CONTENT_DIR.mkdir(exist_ok=True)
            db.initialize()
            cas = CAS(HGP_CONTENT_DIR)
            lease_mgr = LeaseManager(db)
            reconciler = Reconciler(db, cas, HGP_CONTENT_DIR)
            db.expire_leases()
            db.commit()
            startup_report = reconciler.reconcile()
            if startup_report.errors:
                _log.warning("startup reconcile reported errors: %s", startup_report.errors)
        except Exception:
            db.close()
            raise
        _db, _cas, _lease_mgr, _reconciler = db, cas, lease_mgr, reconciler
    assert _db and _cas and _lease_mgr and _reconciler
    return _db, _cas, _lease_mgr, _reconciler


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

    db, cas, _, _ = _get_components()

    # Validate parents exist
    for pid in (parent_op_ids or []):
        if not db.get_operation(pid):
            raise ParentNotFoundError(f"Parent operation not found: {pid}")

    root_op_id = subgraph_root_op_id or (parent_op_ids[0] if parent_op_ids else None)

    # Phase 1: Pre-flight chain_hash check (advisory)
    if chain_hash and root_op_id:
        current = compute_chain_hash(db, root_op_id)
        if current != chain_hash:
            raise ChainStaleError(f"CHAIN_STALE: expected {chain_hash}, got {current}")

    # Phase 2: Write blob to CAS (idempotent, outside transaction)
    object_hash: str | None = None
    if payload:
        raw = base64.b64decode(payload)
        object_hash = cas.store(raw)

    # Phase 3: Atomic DB commit (BEGIN IMMEDIATE)
    op_id = str(uuid.uuid4())
    db.begin_immediate()
    try:
        # Re-validate under write lock (closes TOCTOU)
        if chain_hash and root_op_id:
            current = compute_chain_hash(db, root_op_id)
            if current != chain_hash:
                db.rollback()
                raise ChainStaleError(
                    f"CHAIN_STALE (under lock): expected {chain_hash}, got {current}"
                )

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
        db.execute(
            "UPDATE operations SET chain_hash = ? WHERE op_id = ?",
            (final_chain_hash, op_id),
        )

        if lease_id:
            lease_root = db.execute(
                "SELECT subgraph_root_op_id FROM leases WHERE lease_id = ? AND status = 'ACTIVE'",
                (lease_id,),
            ).fetchone()
            db.execute(
                "UPDATE leases SET status = 'RELEASED' WHERE lease_id = ? AND status = 'ACTIVE'",
                (lease_id,),
            )
            if lease_root:
                other_active = db.execute(
                    "SELECT COUNT(*) FROM leases WHERE subgraph_root_op_id = ? AND status = 'ACTIVE'",
                    (lease_root["subgraph_root_op_id"],),
                ).fetchone()[0]
                if other_active == 0:
                    db.set_memory_tier(lease_root["subgraph_root_op_id"], "long_term")

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
) -> list[dict[str, Any]]:
    """Query operations with optional filters. By default excludes inactive-tier ops; pass include_inactive=True to include them."""
    if status is not None and status not in _VALID_STATUSES:
        return {"error": "INVALID_STATUS", "message": f"status must be one of {sorted(_VALID_STATUSES)}"}


    db, _, _, _ = _get_components()
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
        return [op] if op else []
    return db.query_operations(
        status=status, agent_id=agent_id, op_type=op_type,
        since_commit_seq=since_commit_seq,
        include_inactive=include_inactive, limit=limit,
    )


@mcp.tool()
def hgp_query_subgraph(
    root_op_id: str,
    direction: str = "ancestors",
    max_depth: int = 50,
    include_invalidated: bool = False,
) -> dict[str, Any]:
    """Traverse the causal subgraph from root_op_id."""
    db, _, _, _ = _get_components()
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
    db, _, lease_mgr, _ = _get_components()
    lease = lease_mgr.acquire(agent_id, subgraph_root_op_id, min(ttl_seconds, _MAX_TTL_SECONDS))
    db.set_memory_tier(subgraph_root_op_id, "short_term")
    db.commit()
    return {
        "lease_id": lease.lease_id,
        "chain_hash": lease.chain_hash,
        "expires_at": lease.expires_at.isoformat(),
    }


@mcp.tool()
def hgp_validate_lease(lease_id: str, extend: bool = True) -> dict[str, Any]:
    """Validate (PING) a lease token before LLM compute."""
    _, _, lease_mgr, _ = _get_components()
    return lease_mgr.validate(lease_id, extend=extend)


@mcp.tool()
def hgp_release_lease(lease_id: str) -> dict[str, Any]:
    """Release a lease token explicitly."""
    db, _, lease_mgr, _ = _get_components()
    root_row = db.execute(
        "SELECT subgraph_root_op_id FROM leases WHERE lease_id = ?", (lease_id,)
    ).fetchone()
    lease_mgr.release(lease_id)
    if root_row:
        other_active = db.execute(
            "SELECT COUNT(*) FROM leases WHERE subgraph_root_op_id = ? AND status = 'ACTIVE'",
            (root_row["subgraph_root_op_id"],),
        ).fetchone()[0]
        if other_active == 0:
            db.set_memory_tier(root_row["subgraph_root_op_id"], "long_term")
            db.commit()
    return {"released": True, "lease_id": lease_id}


@mcp.tool()
def hgp_set_memory_tier(op_id: str, tier: str) -> dict[str, Any]:
    """Explicitly set the memory tier of an operation."""
    valid = {"short_term", "long_term", "inactive"}
    if tier not in valid:
        return {"error": "INVALID_TIER", "valid_tiers": sorted(valid)}
    db, _, _, _ = _get_components()
    cur = db.execute("UPDATE operations SET memory_tier = ? WHERE op_id = ?", (tier, op_id))
    db.commit()
    if cur.rowcount == 0:
        return {"error": "OP_NOT_FOUND", "op_id": op_id}
    return {"op_id": op_id, "tier": tier}


@mcp.tool()
def hgp_get_artifact(object_hash: str) -> dict[str, Any]:
    """Retrieve blob content from CAS by hash."""
    _, cas, _, _ = _get_components()
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
    db, _, _, _ = _get_components()
    if not _GIT_SHA_RE.fullmatch(git_commit_sha):
        return {"error": "INVALID_SHA", "message": "git_commit_sha must be 40 lowercase hex chars"}
    db.execute(
        "INSERT OR IGNORE INTO git_anchors (op_id, git_commit_sha, repository) VALUES (?, ?, ?)",
        (op_id, git_commit_sha, repository),
    )
    db.commit()
    return {"anchored": True, "op_id": op_id, "git_commit_sha": git_commit_sha}


@mcp.tool()
def hgp_reconcile(dry_run: bool = False) -> dict[str, Any]:
    """Run crash recovery reconciler."""
    _, _, _, reconciler = _get_components()
    report = reconciler.reconcile(dry_run=dry_run)
    return report.model_dump()


@mcp.tool()
def hgp_get_evidence(op_id: str) -> dict[str, Any] | list[dict[str, Any]]:
    """Return all operations that op_id cited as evidence."""
    db, _, _, _ = _get_components()
    try:
        if not db.get_operation(op_id):
            return {"error": "OP_NOT_FOUND", "message": f"Operation not found: {op_id!r}"}
        return db.get_evidence(op_id)
    except sqlite3.Error as exc:
        _log.error("DB error in hgp_get_evidence op_id=%r: %s", op_id, exc, exc_info=True)
        return {"error": "DB_ERROR", "message": "Internal database error"}


@mcp.tool()
def hgp_get_citing_ops(op_id: str) -> dict[str, Any] | list[dict[str, Any]]:
    """Return all operations that cited op_id as evidence (reverse direction)."""
    db, _, _, _ = _get_components()
    try:
        if not db.get_operation(op_id):
            return {"error": "OP_NOT_FOUND", "message": f"Operation not found: {op_id!r}"}
        return db.get_citing_ops(op_id)
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
) -> dict[str, Any]:
    """Store content in CAS and insert an artifact operation. Returns {op_id}."""
    parsed_refs: list[EvidenceRef] = []
    if evidence_refs:
        if len(evidence_refs) > _MAX_EVIDENCE_REFS:
            return {"error": "TOO_MANY_EVIDENCE_REFS", "message": f"max {_MAX_EVIDENCE_REFS} evidence refs per operation"}
        try:
            parsed_refs = [EvidenceRef.model_validate(r) for r in evidence_refs]
        except ValidationError as exc:
            return {"error": "INVALID_EVIDENCE_REF", "message": str(exc)}

    db, cas, _, _ = _get_components()

    for pid in (parent_op_ids or []):
        if not db.get_operation(pid):
            raise ParentNotFoundError(f"Parent operation not found: {pid}")

    object_hash = cas.store(content_bytes)
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
        )
        for pid in (parent_op_ids or []):
            db.insert_edge(op_id, pid, "causal")
        if parsed_refs:
            db.insert_evidence(op_id, parsed_refs)
        final_hash = compute_chain_hash(db, op_id)
        db.execute(
            "UPDATE operations SET chain_hash = ? WHERE op_id = ?",
            (final_hash, op_id),
        )
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception as rb_exc:
            _log.error("ROLLBACK failed after transaction error: %s", rb_exc)
        raise

    return {"op_id": op_id}


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
    from hgp.project import find_project_root, assert_within_root, ProjectRootError, PathOutsideRootError
    try:
        root = find_project_root(Path(file_path).parent)
        assert_within_root(Path(file_path), root)
    except ProjectRootError as e:
        return {"error": "PROJECT_ROOT_NOT_FOUND", "message": str(e)}
    except PathOutsideRootError as e:
        return {"error": "PATH_OUTSIDE_ROOT", "message": str(e)}

    path = Path(file_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")

    effective_reason = reason or f"CREATE {file_path}"
    return _record_file_op(
        file_path=file_path,
        content_bytes=content.encode("utf-8"),
        agent_id=agent_id,
        reason=effective_reason,
        parent_op_ids=parent_op_ids,
        evidence_refs=evidence_refs,
    )


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
    from hgp.project import find_project_root, assert_within_root, ProjectRootError, PathOutsideRootError
    try:
        root = find_project_root(Path(file_path).parent)
        assert_within_root(Path(file_path), root)
    except ProjectRootError as e:
        return {"error": "PROJECT_ROOT_NOT_FOUND", "message": str(e)}
    except PathOutsideRootError as e:
        return {"error": "PATH_OUTSIDE_ROOT", "message": str(e)}

    path = Path(file_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(content)

    effective_reason = reason or f"APPEND {file_path}"
    return _record_file_op(
        file_path=file_path,
        content_bytes=path.read_bytes(),
        agent_id=agent_id,
        reason=effective_reason,
        parent_op_ids=parent_op_ids,
        evidence_refs=evidence_refs,
    )


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
    from hgp.project import find_project_root, assert_within_root, ProjectRootError, PathOutsideRootError
    try:
        root = find_project_root(Path(file_path).parent)
        assert_within_root(Path(file_path), root)
    except ProjectRootError as e:
        return {"error": "PROJECT_ROOT_NOT_FOUND", "message": str(e)}
    except PathOutsideRootError as e:
        return {"error": "PATH_OUTSIDE_ROOT", "message": str(e)}

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
    path.write_text(updated, encoding="utf-8")

    effective_reason = reason or f"MODIFY {file_path}"
    return _record_file_op(
        file_path=file_path,
        content_bytes=updated.encode("utf-8"),
        agent_id=agent_id,
        reason=effective_reason,
        parent_op_ids=parent_op_ids,
        evidence_refs=evidence_refs,
    )


if __name__ == "__main__":
    mcp.run()
