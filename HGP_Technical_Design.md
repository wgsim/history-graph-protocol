# History Graph Protocol (HGP) - Technical Design Document

**Version:** 0.2.0 (3-Way Consensus Achieved)
**Status:** APPROVED — Claude + ChatGPT + Gemini
**Date:** 2026-03-02
**Parent:** HGP_Master_Plan.md v1.14

---

## 1. V1 Scope

### 1.1 Goals (In-Scope)

| # | Goal | Acceptance Criteria |
|---|------|-------------------|
| G1 | Crash-proof write path | 5-step protocol (stream→fsync→rename→dir-fsync→SQLite TX) fully implemented and tested |
| G2 | MCP tool interface | HGP runs as an MCP server; agents interact via standard MCP tool calls |
| G3 | DAG-based operation tracking | Operations form a directed acyclic graph with parent/child edges |
| G4 | Subgraph CAS (chain_hash) | Concurrent mutations detected and rejected with `409 CHAIN_STALE` |
| G5 | Lease token protocol | Agents acquire, validate (PING), and release leases before committing |
| G6 | Content-Addressable Storage | SHA-256 addressed WORM blob store with deduplication |
| G7 | Crash recovery reconciler | Deterministic 3-rule reconciliation on startup |
| G8 | Git anchor support | Operations can reference Git commit SHAs bidirectionally |

### 1.2 Non-Goals (V2+)

| Item | Reason for Deferral |
|------|-------------------|
| Garbage Collector (Packfile/Delta compression) | Optimization concern, not correctness |
| PostgreSQL migration path | V1 is local-first, single-machine only |
| Multi-machine replication | Requires consensus protocol design |
| Web dashboard / visualization | UI is separate concern |
| Agent SDK / client library | Agents use raw MCP tool calls in V1 |

---

## 2. Language & Runtime

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Language | **Python 3.12+** | Official MCP SDK (FastMCP), native `sqlite3`, fastest V1 iteration |
| MCP Framework | `mcp[cli]` (FastMCP) | Anthropic official, decorator-based tool registration |
| SQLite | stdlib `sqlite3` | Zero-dependency, full PRAGMA/WAL support |
| Hashing | `hashlib.sha256` | stdlib, releases GIL for performance |
| Async | `asyncio` | MCP SDK requirement, single-writer model fits HGP |
| SQLite TX Mode | `BEGIN IMMEDIATE` | Prevents deferred-lock deadlocks under concurrent writes |
| Package Manager | `uv` | Fast dependency resolution |
| Type Checking | `pyright` (strict) | Compensate for dynamic typing in crash-critical paths |

### 2.1 V2 Migration Strategy

Python V1 validates the protocol design. Once proven:
- **Go**: If concurrency scale (100+ agents) becomes the bottleneck
- **Rust**: If latency predictability (sub-ms commits) becomes the requirement
- The protocol design (schemas, algorithms) carries over; only the runtime changes

---

## 3. MCP Tool Interface

HGP exposes the following MCP tools. All inputs/outputs are JSON.

**Payload Size Limit:** V1 limits `payload` to **10 MB** (base64 encoded). Larger artifacts should be stored externally and referenced via metadata. V2 may introduce chunked upload or file-path-based ingestion for agents sharing a filesystem.

**Operation Lifecycle:** `hgp_create_operation` is an **atomic commit** — it creates the operation, writes the blob to CAS, and marks the operation as `COMPLETED` in a single call. The `PENDING` status exists only as a transient internal state during the commit transaction and is never externally visible. If the commit fails at any step, the operation is not created (no partial state).

### 3.1 Operation Tools

#### `hgp_create_operation`

Create a new operation node in the DAG.

```json
{
  "name": "hgp_create_operation",
  "description": "Create a new operation in the causal history DAG",
  "inputSchema": {
    "type": "object",
    "required": ["op_type", "agent_id"],
    "properties": {
      "op_type": {
        "type": "string",
        "enum": ["artifact", "hypothesis", "merge", "invalidation"],
        "description": "Type of operation"
      },
      "agent_id": {
        "type": "string",
        "description": "Identifier of the agent creating this operation"
      },
      "parent_op_ids": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Parent operation IDs (causal dependencies)"
      },
      "invalidates_op_ids": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Operation IDs this operation invalidates"
      },
      "payload": {
        "type": "string",
        "format": "byte",
        "description": "Binary payload to store in CAS (base64 encoded)"
      },
      "mime_type": {
        "type": "string",
        "description": "MIME type of the payload"
      },
      "lease_id": {
        "type": "string",
        "description": "Lease token ID for CAS validation"
      },
      "chain_hash": {
        "type": "string",
        "description": "Expected subgraph chain_hash for optimistic concurrency"
      },
      "metadata": {
        "type": "object",
        "description": "Arbitrary key-value metadata"
      }
    }
  }
}
```

**Response:**
```json
{
  "op_id": "uuid-v4",
  "status": "COMPLETED",
  "commit_seq": 42,
  "object_hash": "sha256:abcdef...",
  "chain_hash": "sha256:new-chain-hash..."
}
```

**Error Responses:**
- `409 CHAIN_STALE`: `chain_hash` mismatch — subgraph mutated concurrently
- `403 LEASE_EXPIRED`: Lease token no longer valid
- `404 PARENT_NOT_FOUND`: Referenced parent operation does not exist

#### `hgp_query_operations`

Query operations in the DAG with filters.

```json
{
  "name": "hgp_query_operations",
  "inputSchema": {
    "type": "object",
    "properties": {
      "op_id": {"type": "string", "description": "Specific operation ID"},
      "agent_id": {"type": "string", "description": "Filter by agent"},
      "op_type": {"type": "string", "description": "Filter by type"},
      "status": {"type": "string", "enum": ["COMPLETED", "INVALIDATED", "MISSING_BLOB"]},
      "since_commit_seq": {"type": "integer", "description": "Operations after this sequence number"},
      "limit": {"type": "integer", "default": 100}
    }
  }
}
```

#### `hgp_query_subgraph`

Traverse the causal subgraph from a root operation.

```json
{
  "name": "hgp_query_subgraph",
  "inputSchema": {
    "type": "object",
    "required": ["root_op_id"],
    "properties": {
      "root_op_id": {"type": "string"},
      "direction": {"type": "string", "enum": ["ancestors", "descendants"], "default": "ancestors"},
      "max_depth": {"type": "integer", "default": 50},
      "include_invalidated": {"type": "boolean", "default": false}
    }
  }
}
```

**Response:**
```json
{
  "root_op_id": "...",
  "chain_hash": "sha256:...",
  "operations": [{"op_id": "...", "parents": [...], "status": "..."}],
  "edges": [{"from": "...", "to": "...", "type": "causal"}]
}
```

### 3.2 Lease Tools

#### `hgp_acquire_lease`

```json
{
  "name": "hgp_acquire_lease",
  "inputSchema": {
    "type": "object",
    "required": ["agent_id", "subgraph_root_op_id"],
    "properties": {
      "agent_id": {"type": "string"},
      "subgraph_root_op_id": {"type": "string"},
      "ttl_seconds": {"type": "integer", "default": 300}
    }
  }
}
```

**Response:**
```json
{
  "lease_id": "uuid-v4",
  "chain_hash": "sha256:...",
  "expires_at": "2026-03-02T12:05:00Z"
}
```

#### `hgp_validate_lease`

PING to confirm lease is still valid before LLM compute.

```json
{
  "name": "hgp_validate_lease",
  "inputSchema": {
    "type": "object",
    "required": ["lease_id"],
    "properties": {
      "lease_id": {"type": "string"},
      "extend": {"type": "boolean", "default": true}
    }
  }
}
```

**Response (valid):**
```json
{"valid": true, "chain_hash": "sha256:...", "expires_at": "2026-03-02T12:10:00Z"}
```

**Response (stale):**
```json
{"valid": false, "reason": "CHAIN_STALE", "current_chain_hash": "sha256:new..."}
```

#### `hgp_release_lease`

```json
{
  "name": "hgp_release_lease",
  "inputSchema": {
    "type": "object",
    "required": ["lease_id"],
    "properties": {
      "lease_id": {"type": "string"}
    }
  }
}
```

### 3.3 Artifact Tools

#### `hgp_get_artifact`

Retrieve blob content from CAS by hash.

```json
{
  "name": "hgp_get_artifact",
  "inputSchema": {
    "type": "object",
    "required": ["object_hash"],
    "properties": {
      "object_hash": {"type": "string"}
    }
  }
}
```

**Response:**
```json
{
  "object_hash": "sha256:...",
  "size": 4096,
  "mime_type": "text/plain",
  "content": "<base64-encoded-bytes>"
}
```

### 3.4 Git Anchor Tools

#### `hgp_anchor_git`

Link an HGP operation to a Git commit.

```json
{
  "name": "hgp_anchor_git",
  "inputSchema": {
    "type": "object",
    "required": ["op_id", "git_commit_sha"],
    "properties": {
      "op_id": {"type": "string"},
      "git_commit_sha": {"type": "string", "pattern": "^[a-f0-9]{40}$"},
      "repository": {"type": "string", "description": "Repository identifier (e.g., 'owner/repo')"}
    }
  }
}
```

### 3.5 Admin Tools

#### `hgp_reconcile`

Run crash recovery reconciler (typically called on server startup).

```json
{
  "name": "hgp_reconcile",
  "inputSchema": {
    "type": "object",
    "properties": {
      "dry_run": {"type": "boolean", "default": false}
    }
  }
}
```

**Response:**
```json
{
  "missing_blobs": ["sha256:aaa...", "sha256:bbb..."],
  "orphan_candidates": ["sha256:ccc..."],
  "repaired": 0,
  "errors": []
}
```

---

## 4. SQLite Schema (DDL)

```sql
-- HGP Database Schema v0.1.0
-- Engine: SQLite 3.40+ with WAL mode

PRAGMA journal_mode = WAL;
PRAGMA synchronous = FULL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;

-- ============================================================
-- Operations: DAG nodes representing agent actions
-- ============================================================
CREATE TABLE operations (
    op_id           TEXT PRIMARY KEY,                           -- UUID v4
    op_type         TEXT NOT NULL CHECK (op_type IN (
                        'artifact', 'hypothesis', 'merge', 'invalidation'
                    )),
    status          TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN (
                        'PENDING', 'COMPLETED', 'INVALIDATED', 'MISSING_BLOB'
                    )),
    commit_seq      INTEGER UNIQUE,                            -- Monotonic, assigned on COMPLETED
    agent_id        TEXT NOT NULL,
    object_hash     TEXT,                                      -- FK to objects.hash (nullable for non-artifact ops)
    chain_hash      TEXT,                                      -- Subgraph state hash at commit time
    metadata        TEXT,                                      -- JSON blob
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    completed_at    TEXT,

    FOREIGN KEY (object_hash) REFERENCES objects(hash)
);

CREATE INDEX idx_operations_agent    ON operations(agent_id);
CREATE INDEX idx_operations_type     ON operations(op_type);
CREATE INDEX idx_operations_status   ON operations(status);
CREATE INDEX idx_operations_seq      ON operations(commit_seq);

-- ============================================================
-- Edges: Parent-child relationships in the DAG
-- ============================================================
CREATE TABLE op_edges (
    child_op_id     TEXT NOT NULL,
    parent_op_id    TEXT NOT NULL,
    edge_type       TEXT NOT NULL DEFAULT 'causal' CHECK (edge_type IN (
                        'causal', 'invalidates'
                    )),

    PRIMARY KEY (child_op_id, parent_op_id),
    FOREIGN KEY (child_op_id) REFERENCES operations(op_id),
    FOREIGN KEY (parent_op_id) REFERENCES operations(op_id)
);

CREATE INDEX idx_edges_parent ON op_edges(parent_op_id);
CREATE INDEX idx_edges_child  ON op_edges(child_op_id);

-- ============================================================
-- Objects: Content-addressable blob registry
-- ============================================================
CREATE TABLE objects (
    hash            TEXT PRIMARY KEY,                           -- sha256:<hex>
    size            INTEGER NOT NULL,
    mime_type       TEXT,
    status          TEXT NOT NULL DEFAULT 'VALID' CHECK (status IN (
                        'VALID', 'MISSING_BLOB', 'ORPHAN_CANDIDATE'
                    )),
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    gc_marked_at    TEXT                                       -- First GC orphan detection timestamp
);

-- ============================================================
-- Leases: Epoch validation tokens
-- ============================================================
CREATE TABLE leases (
    lease_id                TEXT PRIMARY KEY,                   -- UUID v4
    agent_id                TEXT NOT NULL,
    subgraph_root_op_id     TEXT NOT NULL,
    chain_hash              TEXT NOT NULL,                      -- Snapshot at lease issuance
    issued_at               TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    expires_at              TEXT NOT NULL,
    status                  TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN (
                                'ACTIVE', 'EXPIRED', 'RELEASED'
                            )),

    FOREIGN KEY (subgraph_root_op_id) REFERENCES operations(op_id)
);

CREATE INDEX idx_leases_agent  ON leases(agent_id);
CREATE INDEX idx_leases_status ON leases(status);

-- ============================================================
-- Commit counter: Monotonic sequence generator
-- ============================================================
CREATE TABLE commit_counter (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    next_seq        INTEGER NOT NULL DEFAULT 1
);

INSERT INTO commit_counter (id, next_seq) VALUES (1, 1);

-- ============================================================
-- Git anchors: Bidirectional HGP ↔ Git references
-- ============================================================
CREATE TABLE git_anchors (
    op_id           TEXT NOT NULL,
    git_commit_sha  TEXT NOT NULL CHECK (length(git_commit_sha) = 40),
    repository      TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),

    PRIMARY KEY (op_id, git_commit_sha),
    FOREIGN KEY (op_id) REFERENCES operations(op_id)
);
```

---

## 5. Core Type Definitions

```python
"""HGP Core Types — Pydantic models for internal and MCP interface use."""

from __future__ import annotations
from enum import StrEnum
from typing import Any
from pydantic import BaseModel, Field
from datetime import datetime
import uuid


# ── Enums ──────────────────────────────────────────────────

class OpType(StrEnum):
    ARTIFACT = "artifact"
    HYPOTHESIS = "hypothesis"
    MERGE = "merge"
    INVALIDATION = "invalidation"


class OpStatus(StrEnum):
    PENDING = "PENDING"
    COMPLETED = "COMPLETED"
    INVALIDATED = "INVALIDATED"
    MISSING_BLOB = "MISSING_BLOB"


class EdgeType(StrEnum):
    CAUSAL = "causal"
    INVALIDATES = "invalidates"


class LeaseStatus(StrEnum):
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    RELEASED = "RELEASED"


class ObjectStatus(StrEnum):
    VALID = "VALID"
    MISSING_BLOB = "MISSING_BLOB"
    ORPHAN_CANDIDATE = "ORPHAN_CANDIDATE"


# ── Core Models ────────────────────────────────────────────

class Operation(BaseModel):
    op_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    op_type: OpType
    status: OpStatus = OpStatus.PENDING
    commit_seq: int | None = None
    agent_id: str
    object_hash: str | None = None
    chain_hash: str | None = None
    metadata: dict[str, Any] | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    completed_at: datetime | None = None


class OpEdge(BaseModel):
    child_op_id: str
    parent_op_id: str
    edge_type: EdgeType = EdgeType.CAUSAL


class StoredObject(BaseModel):
    hash: str                     # "sha256:<hex>"
    size: int
    mime_type: str | None = None
    status: ObjectStatus = ObjectStatus.VALID
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Lease(BaseModel):
    lease_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    agent_id: str
    subgraph_root_op_id: str
    chain_hash: str
    issued_at: datetime = Field(default_factory=datetime.utcnow)
    expires_at: datetime
    status: LeaseStatus = LeaseStatus.ACTIVE


class GitAnchor(BaseModel):
    op_id: str
    git_commit_sha: str           # 40-char hex
    repository: str | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
```

---

## 6. chain_hash Algorithm

The `chain_hash` captures the exact state of a subgraph — including both **nodes and edges** — enabling Subgraph CAS (Compare-And-Swap) for optimistic concurrency control.

### 6.1 Computation

```
FUNCTION compute_chain_hash(root_op_id: str) -> str:
    1. Traverse FULL ancestor subgraph via recursive CTE from root_op_id
       (no depth limit — always traverses to genesis)
    2. Collect all reachable operations: [(op_id, status, commit_seq)]
    3. Collect all edges within the subgraph: [(child_op_id, parent_op_id, edge_type)]
    4. Sort operations by op_id (lexicographic, deterministic)
    5. Sort edges by (child_op_id, parent_op_id) (lexicographic, deterministic)
    6. Build canonical string:
         ops_part = "|".join(f"{op_id}:{status}:{commit_seq}" for each op)
         edges_part = "|".join(f"{child}>{parent}:{edge_type}" for each edge)
         canonical = f"OPS[{ops_part}]EDGES[{edges_part}]"
    7. Return "sha256:" + SHA-256(canonical.encode("utf-8")).hexdigest()
```

**Why edges are included:** Without edges, two DAGs with identical nodes but different causal structures (e.g., `A→B` vs `B→A`) would produce the same hash, completely breaking CAS integrity.

**Why no depth limit:** The chain_hash must represent the **complete causal history** to be a reliable CAS token. `hgp_query_subgraph` may use `max_depth` for display, but chain_hash computation always traverses the full ancestor graph.

### 6.2 SQL Implementation (Recursive CTE)

```sql
-- Step 1: Collect all ancestor operation IDs
WITH RECURSIVE subgraph(op_id) AS (
    SELECT :root_op_id
    UNION
    SELECT e.parent_op_id
    FROM op_edges e
    JOIN subgraph s ON e.child_op_id = s.op_id
)
-- Step 2: Get operation data
SELECT o.op_id, o.status, o.commit_seq
FROM operations o
JOIN subgraph s ON o.op_id = s.op_id
ORDER BY o.op_id;

-- Step 3: Get edges within subgraph
SELECT e.child_op_id, e.parent_op_id, e.edge_type
FROM op_edges e
WHERE e.child_op_id IN (SELECT op_id FROM subgraph)
  AND e.parent_op_id IN (SELECT op_id FROM subgraph)
ORDER BY e.child_op_id, e.parent_op_id;
```

### 6.3 Performance Note

For deep DAGs, full ancestor traversal may become expensive. V1 accepts this cost since the typical agent workflow produces shallow graphs (< 1000 nodes). V2 may introduce **incremental chain_hash** (Merkle DAG) where each node's hash incorporates its parent hashes, enabling O(1) verification.

### 6.4 Invariants

- **Deterministic**: Same subgraph state always produces the same hash
- **Structure-sensitive**: Any edge addition/removal changes the hash
- **State-sensitive**: Any operation status or commit_seq change produces a different hash
- **Direction**: Computes over **ancestors** (causal past), not descendants
- **Complete**: Always traverses to genesis (no depth limit)

---

## 7. Lease Token Protocol

### 7.1 Lifecycle

```
Agent                                HGP Server
  │                                       │
  │── hgp_acquire_lease ─────────────────▶│
  │   {agent_id, subgraph_root_op_id}     │── compute chain_hash
  │                                       │── INSERT lease (ACTIVE)
  │◀── {lease_id, chain_hash, expires_at} │
  │                                       │
  │   ... LLM thinking ...                │
  │                                       │
  │── hgp_validate_lease ────────────────▶│
  │   {lease_id}                          │── recompute chain_hash
  │                                       │── compare with stored
  │◀── {valid: true, expires_at: +TTL}    │   (extend if valid)
  │                                       │
  │── hgp_create_operation ──────────────▶│
  │   {lease_id, chain_hash, payload}     │── verify chain_hash
  │                                       │── 5-step write path
  │◀── {op_id, commit_seq}               │── release lease
  │                                       │
```

### 7.2 Rules

1. **One active lease per agent per subgraph root.** Acquiring a new lease auto-releases the previous one.
2. **TTL default: 300 seconds (5 min).** Configurable per-request.
3. **Validation extends TTL** by the original TTL duration (sliding window).
4. **Expired leases** do not block operations — they only serve as advisory signals. The `chain_hash` CAS check is the real gate.
5. **Lease-free commits** are allowed if `chain_hash` is provided directly (power-user mode). The lease is a convenience for the acquire→validate→commit flow.

---

## 8. Content-Addressable Storage (CAS)

### 8.1 Directory Layout

```
.hgp_content/
├── .staging/                    # Temp files during write
│   └── <uuid>.tmp
├── ab/                          # Fan-out: first 2 hex chars
│   └── cdef1234567890...        # Remaining 62 hex chars
├── ff/
│   └── 01a2b3c4d5e6f7...
└── ...
```

Fan-out by first 2 hex chars of SHA-256 (256 possible directories) to avoid filesystem bottlenecks.

### 8.2 Write Path (5-Step Protocol)

```python
async def store_blob(payload: bytes) -> str:
    """Crash-proof blob storage. Returns 'sha256:<hex>'."""

    # Step 1: Stream & Hash
    content_hash = hashlib.sha256(payload).hexdigest()
    object_key = f"sha256:{content_hash}"
    staging_path = STAGING_DIR / f"{uuid.uuid4()}.tmp"
    final_dir = CONTENT_DIR / content_hash[:2]
    final_path = final_dir / content_hash[2:]

    # Fast path: deduplication
    if final_path.exists():
        return object_key

    final_dir.mkdir(parents=True, exist_ok=True)

    # Step 2: Write + fsync file
    staging_path.write_bytes(payload)
    fd = os.open(str(staging_path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)

    # Step 3: Atomic rename
    try:
        os.rename(str(staging_path), str(final_path))
    except OSError:
        # EEXIST: concurrent write produced same hash — validate and succeed
        if final_path.exists():
            staging_path.unlink(missing_ok=True)
            return object_key
        raise

    # Step 4: Directory fsync
    for dir_path in [STAGING_DIR, final_dir]:
        dfd = os.open(str(dir_path), os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)

    return object_key
```

### 8.3 Full Commit Flow (CAS + SQLite Integration)

The complete `hgp_create_operation` execution order, with crash semantics at each step:

```
hgp_create_operation(op_type, agent_id, parent_op_ids, payload, lease_id, chain_hash)
│
├─ Phase 1: Validation (no side effects)
│  ├─ Validate parent_op_ids exist
│  ├─ Validate lease (if provided): not expired, chain_hash matches
│  ├─ Compute current chain_hash from subgraph
│  └─ Compare submitted chain_hash with computed → 409 CHAIN_STALE if mismatch
│
├─ Phase 2: Blob Write (idempotent, crash-safe)
│  ├─ Step 1: Compute SHA-256 of payload
│  ├─ Step 2: Write to .staging/<uuid>.tmp + fsync
│  ├─ Step 3: Atomic rename to .hgp_content/<hash[:2]>/<hash[2:]>
│  ├─ Step 4: fsync source and destination directories
│  └─ [CRASH HERE → orphan blob on disk, cleaned by reconciler grace period]
│
├─ Phase 3: DB Commit (atomic, all-or-nothing)
│  ├─ BEGIN IMMEDIATE
│  ├─ Re-validate chain_hash (double-check after acquiring write lock)
│  ├─ INSERT operation (status=COMPLETED, with commit_seq)
│  ├─ INSERT op_edges (parent relationships)
│  ├─ INSERT OR IGNORE object registry
│  ├─ UPDATE invalidated operations (if invalidates_op_ids provided)
│  ├─ RELEASE lease (if lease_id provided)
│  ├─ COMMIT
│  └─ [CRASH HERE → blob exists, no DB record → reconciler marks ORPHAN_CANDIDATE
│      after grace period. Safe because ORPHAN_CANDIDATE requires 2 GC passes to delete]
│
└─ Phase 4: Response
   └─ Return op_id, commit_seq, object_hash, new chain_hash
```

**Key guarantee:** Blob is **always written before DB commit**. This means:
- If DB commits but blob is missing → impossible (blob written first)
- If blob exists but DB didn't commit → reconciler handles via ORPHAN_CANDIDATE with grace period
- If both succeed → normal operation (Rule 1 of reconciler)

### 8.4 Read Path

```python
def read_blob(object_hash: str) -> bytes | None:
    """Read blob by hash. Returns None if missing."""
    hex_hash = object_hash.removeprefix("sha256:")
    path = CONTENT_DIR / hex_hash[:2] / hex_hash[2:]
    if path.exists():
        return path.read_bytes()
    return None
```

---

## 9. Crash Recovery Reconciler

Runs on server startup. Implements the 3 deterministic rules from the Master Plan.

### 9.1 Algorithm

```python
ORPHAN_GRACE_PERIOD = timedelta(minutes=15)  # Prevent race with active writers

def reconcile() -> ReconcileReport:
    report = ReconcileReport()
    now = datetime.utcnow()

    # Rule 1 & 2: Check DB COMPLETED operations against blob existence
    for op in db.query_operations(status="COMPLETED"):
        if op.object_hash and not cas.blob_exists(op.object_hash):
            db.update_status(op.op_id, "MISSING_BLOB")
            report.missing_blobs.append(op.object_hash)

    # Rule 3: Check blobs without DB references (with grace period)
    # Grace period prevents race condition: writer may have completed
    # CAS write (step 4) but not yet committed to DB (step 5).
    for blob_hash, blob_mtime in cas.list_all_blobs_with_mtime():
        if not db.object_referenced(blob_hash):
            if now - blob_mtime > ORPHAN_GRACE_PERIOD:
                db.upsert_object(blob_hash, status="ORPHAN_CANDIDATE")
                report.orphan_candidates.append(blob_hash)
            else:
                report.skipped_young_blobs += 1

    # Clean incomplete staging files older than grace period
    for tmp_file in STAGING_DIR.glob("*.tmp"):
        if now - datetime.fromtimestamp(tmp_file.stat().st_mtime) > ORPHAN_GRACE_PERIOD:
            tmp_file.unlink()
            report.staging_cleaned += 1

    return report
```

**Grace period rationale:** Between CAS blob write (Phase 2) and DB commit (Phase 3), there is a window where a blob exists on disk without a corresponding DB record. Without the grace period, the reconciler would incorrectly classify this blob as an orphan. The 15-minute default far exceeds any realistic commit latency.

### 9.2 Startup Sequence

```
Server Boot
    │
    ├── 1. Open/Create SQLite DB
    ├── 2. Apply PRAGMA settings (WAL, synchronous=FULL)
    ├── 3. Run schema migrations
    ├── 4. Run reconciler
    ├── 5. Expire stale leases (status → EXPIRED where expires_at < now)
    └── 6. Register MCP tools and start serving
```

---

## 10. Project Structure

```
history-graph-protocol/
├── src/
│   └── hgp/
│       ├── __init__.py            # Package root, version
│       ├── server.py              # MCP server entry point (FastMCP)
│       ├── models.py              # Pydantic models (Section 5)
│       ├── db.py                  # SQLite operations, schema init
│       ├── cas.py                 # Content-Addressable Storage (Section 8)
│       ├── dag.py                 # DAG traversal, chain_hash (Section 6)
│       ├── lease.py               # Lease token management (Section 7)
│       ├── reconciler.py          # Crash recovery (Section 9)
│       └── errors.py              # Error types (ChainStale, LeaseExpired, etc.)
├── tests/
│   ├── conftest.py                # Shared fixtures (tmp dirs, test DB)
│   ├── test_cas.py                # CAS write/read/dedup/crash tests
│   ├── test_db.py                 # Schema, CRUD, constraint tests
│   ├── test_dag.py                # DAG traversal, chain_hash tests
│   ├── test_lease.py              # Lease lifecycle tests
│   ├── test_reconciler.py         # 3-rule reconciliation tests
│   └── test_integration.py        # End-to-end MCP tool call tests
├── pyproject.toml
├── HGP_Master_Plan.md
├── HGP_Technical_Design.md        # This document
└── .gitignore
```

---

## 11. Dependencies (pyproject.toml)

```toml
[project]
name = "hgp"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "mcp[cli]>=1.0.0",
    "pydantic>=2.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.24",
    "pyright>=1.1",
    "ruff>=0.8",
]

[tool.pyright]
typeCheckingMode = "strict"

[tool.ruff]
target-version = "py312"
line-length = 100
```

---

## 12. Error Taxonomy

| Error Code | HTTP Analog | When Raised | Recovery |
|-----------|-------------|-------------|----------|
| `CHAIN_STALE` | 409 Conflict | `chain_hash` mismatch on commit | Re-acquire lease, re-read subgraph, retry |
| `LEASE_EXPIRED` | 403 Forbidden | Lease TTL exceeded | Acquire new lease |
| `PARENT_NOT_FOUND` | 404 Not Found | Referenced parent op_id doesn't exist | Verify parent op_ids |
| `BLOB_WRITE_FAILED` | 500 Internal | fsync or rename failed | Retry; check disk space |
| `MISSING_BLOB` | 500 Internal | DB references blob that doesn't exist on disk | Reconciler queues for repair |
| `INVALID_HASH` | 400 Bad Request | Provided hash doesn't match computed hash | Client bug — recompute |

---

## Appendix A: Key SQL Queries

### A.1 Atomic Commit (Single Transaction)

```sql
-- Called within a single transaction AFTER CAS blob is durable on disk.
-- BEGIN IMMEDIATE acquires the write lock immediately, preventing
-- deferred-lock deadlocks when multiple agents commit concurrently.
BEGIN IMMEDIATE;

-- Re-validate chain_hash (double-check under write lock)
-- Application code computes current chain_hash and compares with submitted value.
-- If mismatch: ROLLBACK and return 409 CHAIN_STALE.

-- Get and increment commit sequence (atomic under BEGIN IMMEDIATE)
UPDATE commit_counter SET next_seq = next_seq + 1 WHERE id = 1;
SELECT next_seq - 1 AS commit_seq FROM commit_counter WHERE id = 1;

-- Insert operation as COMPLETED (no PENDING intermediate state externally visible)
INSERT INTO operations (op_id, op_type, status, commit_seq, agent_id,
                        object_hash, chain_hash, metadata, completed_at)
VALUES (:op_id, :op_type, 'COMPLETED', :commit_seq, :agent_id,
        :object_hash, :chain_hash, :metadata,
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

-- Insert causal edges
INSERT INTO op_edges (child_op_id, parent_op_id, edge_type)
VALUES (:op_id, :parent_op_id, :edge_type);
-- (repeated for each parent)

-- Upsert object registry (if payload exists)
INSERT OR IGNORE INTO objects (hash, size, mime_type)
VALUES (:object_hash, :size, :mime_type);

-- Mark invalidated operations (if invalidates_op_ids provided)
UPDATE operations SET status = 'INVALIDATED'
WHERE op_id IN (:invalidated_op_ids) AND status = 'COMPLETED';

-- Release lease (if lease_id provided)
UPDATE leases SET status = 'RELEASED'
WHERE lease_id = :lease_id AND status = 'ACTIVE';

COMMIT;
-- If COMMIT succeeds: operation is durable. Blob already durable from Phase 2.
-- If COMMIT fails (crash): blob remains as orphan, cleaned after grace period.
```

**Note on commit_seq atomicity:** `BEGIN IMMEDIATE` acquires the SQLite write lock before any reads, guaranteeing that `UPDATE commit_counter` + `SELECT` is atomic. No two concurrent transactions can observe the same `next_seq` value.

### A.2 Subgraph Traversal (Recursive CTE)

```sql
-- Traverse ancestors from a given operation
WITH RECURSIVE ancestors(op_id, depth) AS (
    SELECT :root_op_id, 0

    UNION ALL

    SELECT e.parent_op_id, a.depth + 1
    FROM op_edges e
    JOIN ancestors a ON e.child_op_id = a.op_id
    WHERE a.depth < :max_depth
)
SELECT DISTINCT o.*
FROM operations o
JOIN ancestors a ON o.op_id = a.op_id
ORDER BY o.commit_seq;
```

---

---

## Appendix B: Review Changelog

### Round 1 (v0.1.0 → v0.2.0)

| # | Severity | Source | Issue | Resolution |
|---|----------|--------|-------|------------|
| 1 | CRITICAL | ChatGPT | chain_hash ignores edges — same nodes with different causal structure produce identical hash | Added edges to canonical string: `EDGES[child>parent:type]` (Section 6.1) |
| 2 | CRITICAL | Gemini | chain_hash max_depth vs full history inconsistency | chain_hash always traverses full ancestors, no depth limit (Section 6.1, 6.4) |
| 3 | CRITICAL | Gemini | CAS 5-step and SQLite TX integration order not specified | Added Section 8.3: Full Commit Flow with 4 phases and crash semantics |
| 4 | CRITICAL | Gemini | commit_seq race condition under concurrent access | Mandated `BEGIN IMMEDIATE` for all write paths (Section 2, Appendix A) |
| 5 | HIGH | ChatGPT | Reconciler vs active writer race (blob exists, DB not yet committed → false ORPHAN) | Added 15-min grace period using blob mtime (Section 9.1) |
| 6 | HIGH | ChatGPT | PENDING status purpose unclear when create is atomic | Clarified: PENDING is internal-only transient state, INSERT directly as COMPLETED (Section 3) |
| 7 | MEDIUM | ChatGPT | `BEGIN` should be `BEGIN IMMEDIATE` to prevent deferred-lock deadlocks | All write paths use `BEGIN IMMEDIATE` (Appendix A) |
| 8 | MEDIUM | ChatGPT | Base64 payload size limit needed for large artifacts | Added 10MB limit with V2 chunked upload path (Section 3) |
| 9 | LOW | Gemini | Missing index on `op_edges.child_op_id` | Added `idx_edges_child` index (Section 4) |

*End of Technical Design Document — 3-Way Consensus Achieved (Claude + ChatGPT + Gemini)*
