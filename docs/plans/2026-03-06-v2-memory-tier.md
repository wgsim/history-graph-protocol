# HGP V2 Memory Tier Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a 3-tier memory system (short_term / long_term / inactive) on top of V1's append-only DAG, so that query results are naturally prioritized by recency-of-access without ever deleting history.

**Architecture:** V1's immutable event-sourced foundation is untouched. V2 adds access-tracking side-effects to read operations, a `memory_tier` column that reflects access recency, and tier-aware query ordering. "Forgetting" means demotion to `inactive` tier — the data remains, but is excluded from default query results and ranked last when explicitly requested.

**Tech Stack:** Python 3.12, SQLite (WAL), FastMCP, Pydantic v2, pytest, pyright strict

---

## Context: V1 Architecture Summary

```
src/hgp/
  db.py          — SQLite wrapper (query_operations, insert_operation, leases, ...)
  dag.py         — Recursive CTE traversal (get_ancestors, get_descendants, max_depth)
  cas.py         — WORM content-addressable blob store
  lease.py       — LeaseManager (acquire/validate/release with TTL)
  reconciler.py  — Crash recovery (MISSING_BLOB, ORPHAN_CANDIDATE)
  server.py      — FastMCP tool functions (9 tools)
  models.py      — Pydantic enums + models
tests/
  test_server_tools.py   — 30 MCP tool tests (server_components fixture)
  test_db.py, test_dag.py, test_lease.py, test_reconciler.py, ...
```

V1 invariants that MUST NOT change:
- `op_id` is immutable once created
- `object_hash` → CAS blob is WORM (write once, read many)
- `chain_hash` is a computed digest, never stored as a CAS blob
- `commit_seq` is monotonically increasing

---

## V2 Design: Memory Tier System

### Core Principle: Tier Controls Information Depth, Not Visibility

**Tier does NOT hide nodes.** Every node in the DAG is always traversable — causal chains are never broken.
Tier controls **how much detail** is returned for each node in a response.

```
short_term  → Full detail   (all fields + metadata + content reference)
long_term   → Summary       (identity + status fields only)
inactive    → Stub          (op_id + op_type + memory_tier only)
```

This means a subgraph query always returns all connected nodes, but nodes further from
active work appear as stubs rather than being omitted entirely.

### Tier Definitions

| Tier | Meaning | Returned fields |
|------|---------|----------------|
| `short_term` | Actively being worked (lease ACTIVE) | All fields |
| `long_term` | Stable completed history (default) | op_id, op_type, status, commit_seq, agent_id, memory_tier |
| `inactive` | Not accessed in > 30 days | op_id, op_type, memory_tier |

### Response Shape by Tier

```python
# short_term — full
{"op_id": "...", "op_type": "artifact", "status": "COMPLETED",
 "commit_seq": 5, "agent_id": "...", "object_hash": "sha256:...",
 "chain_hash": "sha256:...", "metadata": {...},
 "created_at": "...", "completed_at": "...",
 "access_count": 12, "last_accessed": "...", "memory_tier": "short_term"}

# long_term — summary
{"op_id": "...", "op_type": "artifact", "status": "COMPLETED",
 "commit_seq": 3, "agent_id": "...", "memory_tier": "long_term"}

# inactive — stub
{"op_id": "...", "op_type": "hypothesis", "memory_tier": "inactive"}
```

### Tier Transition Rules

```
CREATE → long_term (default)
         ↑ record_access()        ↑ hgp_acquire_lease()
         │                        │
long_term ←──────────────── short_term
         │                   ↓ lease released/expired
         └─ demote_inactive() ─→ inactive
                                  ↑ record_access() promotes back to long_term
```

### Access Recording — Distance-Based Decay

When `hgp_query_subgraph` traverses a chain, access strength decays with traversal depth:

```
depth 0 (query root):  access_count += 1  (full weight)
depth 1:               access_count += 0.7
depth 2:               access_count += 0.4
depth 3+:              access_count += 0.1  (floor)
```

`access_count` is stored as REAL to support fractional increments.
This is fire-and-forget (best-effort): recorded after the primary read returns,
in a separate lightweight UPDATE — never blocks the main query.

### New Schema Columns

```sql
-- operations table
access_count    REAL    NOT NULL DEFAULT 0.0   -- fractional for distance decay
last_accessed   TEXT                            -- ISO 8601, nullable
memory_tier     TEXT    NOT NULL DEFAULT 'long_term'
                CHECK (memory_tier IN ('short_term', 'long_term', 'inactive'))

-- op_edges: NO weight column in V2 (deferred to V3)
```

### New / Modified MCP Tools

| Tool | Change |
|------|--------|
| `hgp_query_operations` | + `include_inactive: bool = False` (list mode only), tier-priority ORDER BY, `record_access` side-effect for `op_id` lookup |
| `hgp_query_subgraph` | Always traverses full graph; returns tier-appropriate detail level; distance-decay access recording (best-effort) |
| `hgp_acquire_lease` | promotes root op to `short_term` |
| `hgp_release_lease` | demotes root op to `long_term` |
| `hgp_set_memory_tier` | **NEW** — explicit tier override |
| `hgp_reconcile` | calls `demote_inactive()` as new pass |

---

## Task 1: Schema Migration & MemoryTier Enum

**Files:**
- Modify: `src/hgp/db.py:9-88` — add columns to `_SCHEMA_SQL`, add migration in `initialize()`
- Modify: `src/hgp/models.py` — add `MemoryTier` enum
- Test: `tests/test_db.py`

### Step 1: Write failing test

```python
# tests/test_db.py
def test_memory_tier_columns_exist(hgp_dirs: dict):
    db = Database(hgp_dirs["db_path"])
    db.initialize()
    cols = {row[1] for row in db.execute("PRAGMA table_info(operations)").fetchall()}
    assert "memory_tier" in cols
    assert "access_count" in cols
    assert "last_accessed" in cols

def test_op_edge_weight_column_exists(hgp_dirs: dict):
    db = Database(hgp_dirs["db_path"])
    db.initialize()
    cols = {row[1] for row in db.execute("PRAGMA table_info(op_edges)").fetchall()}
    assert "weight" in cols

def test_new_operation_default_tier(hgp_dirs: dict):
    db = Database(hgp_dirs["db_path"])
    db.initialize()
    db.begin_immediate()
    db.insert_operation("op-1", "artifact", "agent-1", 1, "sha256:x")
    db.commit()
    op = db.get_operation("op-1")
    assert op["memory_tier"] == "long_term"
    assert op["access_count"] == 0
    assert op["last_accessed"] is None
```

### Step 2: Run to verify failure

```bash
uv run pytest tests/test_db.py::test_memory_tier_columns_exist -v
```
Expected: `FAIL` — column does not exist.

### Step 3: Add `MemoryTier` enum to `models.py`

```python
class MemoryTier(StrEnum):
    SHORT_TERM = "short_term"
    LONG_TERM  = "long_term"
    INACTIVE   = "inactive"
```

### Step 4: Add columns to `_SCHEMA_SQL` in `db.py`

In `operations` CREATE TABLE, add after `completed_at`:
```sql
access_count    INTEGER NOT NULL DEFAULT 0,
last_accessed   TEXT,
memory_tier     TEXT NOT NULL DEFAULT 'long_term'
                    CHECK (memory_tier IN ('short_term', 'long_term', 'inactive')),
```

Also add migration block in `Database.initialize()` after `executescript(_SCHEMA_SQL)`:
```python
# V2 migration: add memory tier columns to existing DBs
existing_ops_cols = {r[1] for r in self._conn.execute("PRAGMA table_info(operations)").fetchall()}
if "memory_tier" not in existing_ops_cols:
    self._conn.executescript("""
        ALTER TABLE operations ADD COLUMN access_count INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE operations ADD COLUMN last_accessed TEXT;
        ALTER TABLE operations ADD COLUMN memory_tier TEXT NOT NULL DEFAULT 'long_term'
            CHECK (memory_tier IN ('short_term', 'long_term', 'inactive'));
    """)
# op_edges weight column deferred to V3
```

### Step 5: Run tests

```bash
uv run pytest tests/test_db.py -v
uv run pyright src/
```
Expected: all PASS, 0 errors.

### Step 6: Commit

```bash
git add src/hgp/db.py src/hgp/models.py tests/test_db.py
git commit -m "feat(v2): add memory_tier/access_count/last_accessed/weight schema + migration"
```

---

## Task 2: DB Layer — `record_access()` and `demote_inactive()`

**Files:**
- Modify: `src/hgp/db.py` — add `record_access()`, `demote_inactive()`, update `query_operations()`
- Test: `tests/test_db.py`

### Step 1: Write failing tests

```python
def test_record_access(hgp_dirs: dict):
    db = Database(hgp_dirs["db_path"])
    db.initialize()
    db.begin_immediate()
    db.insert_operation("op-1", "artifact", "agent-1", 1, "sha256:x")
    db.commit()
    db.record_access("op-1")
    op = db.get_operation("op-1")
    assert op["access_count"] == 1
    assert op["last_accessed"] is not None

def test_record_access_promotes_inactive(hgp_dirs: dict):
    db = Database(hgp_dirs["db_path"])
    db.initialize()
    db.begin_immediate()
    db.insert_operation("op-2", "artifact", "agent-1", 1, "sha256:x")
    db.commit()
    db.execute("UPDATE operations SET memory_tier = 'inactive' WHERE op_id = 'op-2'")
    db.commit()
    db.record_access("op-2")
    assert db.get_operation("op-2")["memory_tier"] == "long_term"

def test_demote_inactive(hgp_dirs: dict):
    from datetime import datetime, timezone, timedelta
    db = Database(hgp_dirs["db_path"])
    db.initialize()
    db.begin_immediate()
    db.insert_operation("old-op", "artifact", "agent-1", 1, "sha256:x")
    db.commit()
    # Simulate last_accessed 40 days ago
    old_ts = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    db.execute("UPDATE operations SET last_accessed = ? WHERE op_id = 'old-op'", (old_ts,))
    db.commit()
    count = db.demote_inactive(threshold_days=30)
    db.commit()
    assert count == 1
    assert db.get_operation("old-op")["memory_tier"] == "inactive"

def test_query_excludes_inactive_by_default(hgp_dirs: dict):
    db = Database(hgp_dirs["db_path"])
    db.initialize()
    db.begin_immediate()
    db.insert_operation("active-op", "artifact", "agent-1", 1, "sha256:x")
    db.insert_operation("inactive-op", "artifact", "agent-1", 2, "sha256:y")
    db.commit()
    db.execute("UPDATE operations SET memory_tier = 'inactive' WHERE op_id = 'inactive-op'")
    db.commit()
    results = db.query_operations()
    ids = {r["op_id"] for r in results}
    assert "active-op" in ids
    assert "inactive-op" not in ids

def test_query_includes_inactive_when_requested(hgp_dirs: dict):
    db = Database(hgp_dirs["db_path"])
    db.initialize()
    db.begin_immediate()
    db.insert_operation("inactive-op", "artifact", "agent-1", 1, "sha256:x")
    db.commit()
    db.execute("UPDATE operations SET memory_tier = 'inactive' WHERE op_id = 'inactive-op'")
    db.commit()
    results = db.query_operations(include_inactive=True)
    ids = {r["op_id"] for r in results}
    assert "inactive-op" in ids
```

### Step 2: Run to verify failures

```bash
uv run pytest tests/test_db.py::test_record_access tests/test_db.py::test_demote_inactive -v
```

### Step 3: Implement in `db.py`

Add `record_access()`:
```python
def record_access(self, op_id: str) -> None:
    """Record a read access: increment counter, update timestamp, promote from inactive."""
    assert self._conn
    self._conn.execute(
        """
        UPDATE operations
        SET access_count = access_count + 1,
            last_accessed = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
            memory_tier = CASE WHEN memory_tier = 'inactive' THEN 'long_term' ELSE memory_tier END
        WHERE op_id = ?
        """,
        (op_id,),
    )
```

Add `demote_inactive()`:
```python
def demote_inactive(self, threshold_days: int = 30) -> int:
    """Demote ops not accessed in threshold_days to inactive tier. Returns count."""
    assert self._conn
    cur = self._conn.execute(
        """
        UPDATE operations
        SET memory_tier = 'inactive'
        WHERE memory_tier != 'inactive'
          AND memory_tier != 'short_term'
          AND (
              last_accessed IS NULL AND created_at < datetime('now', ? || ' days')
              OR last_accessed < datetime('now', ? || ' days')
          )
        """,
        (f"-{threshold_days}", f"-{threshold_days}"),
    )
    return cur.rowcount
```

Add `set_memory_tier()`:
```python
def set_memory_tier(self, op_id: str, tier: str) -> None:
    assert self._conn
    self._conn.execute(
        "UPDATE operations SET memory_tier = ? WHERE op_id = ?", (tier, op_id)
    )
```

Update `query_operations()` signature and body:
```python
def query_operations(
    self,
    status: str | None = None,
    agent_id: str | None = None,
    op_type: str | None = None,
    since_commit_seq: int | None = None,
    include_inactive: bool = False,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    ...
    if not include_inactive:
        clauses.append("memory_tier != 'inactive'")
    ...
    # ORDER BY tier priority: short_term first, long_term second, inactive last
    order = """
        ORDER BY CASE memory_tier
            WHEN 'short_term' THEN 1
            WHEN 'long_term'  THEN 2
            WHEN 'inactive'   THEN 3
        END, commit_seq DESC
    """
    rows = self._conn.execute(
        f"SELECT * FROM operations {where} {order} LIMIT ?", params
    ).fetchall()
```

### Step 4: Run tests

```bash
uv run pytest tests/test_db.py -v
uv run pyright src/
```

### Step 5: Commit

```bash
git add src/hgp/db.py tests/test_db.py
git commit -m "feat(v2): add record_access(), demote_inactive(), tier-aware query_operations()"
```

---

## Task 3: Lease Lifecycle → Tier Integration

**Files:**
- Modify: `src/hgp/server.py` — `hgp_acquire_lease`, `hgp_release_lease`
- Modify: `src/hgp/db.py` — `expire_leases()` side-effect
- Test: `tests/test_server_tools.py`

### Step 1: Write failing tests

```python
def test_acquire_lease_promotes_to_short_term(server_components):
    root = hgp_create_operation(op_type="artifact", agent_id="a")
    assert server_components["db"].get_operation(root["op_id"])["memory_tier"] == "long_term"
    hgp_acquire_lease(agent_id="a", subgraph_root_op_id=root["op_id"])
    assert server_components["db"].get_operation(root["op_id"])["memory_tier"] == "short_term"

def test_release_lease_demotes_to_long_term(server_components):
    root = hgp_create_operation(op_type="artifact", agent_id="a")
    lease = hgp_acquire_lease(agent_id="a", subgraph_root_op_id=root["op_id"])
    hgp_release_lease(lease["lease_id"])
    assert server_components["db"].get_operation(root["op_id"])["memory_tier"] == "long_term"
```

### Step 2: Implement in `server.py`

In `hgp_acquire_lease`, after `lease_mgr.acquire(...)`:
```python
db.set_memory_tier(subgraph_root_op_id, "short_term")
db.commit()
```

In `hgp_release_lease`, after `lease_mgr.release(...)`:
```python
# Demote the root op back to long_term if no other active leases
lease_row = db.execute(
    "SELECT subgraph_root_op_id FROM leases WHERE lease_id = ?", (lease_id,)
).fetchone()
if lease_row:
    db.set_memory_tier(lease_row["subgraph_root_op_id"], "long_term")
    db.commit()
```

In `expire_leases()` in `db.py` — add a secondary UPDATE to demote expired roots:
```python
# Also demote tier for ops whose only active lease just expired
self._conn.execute(
    """
    UPDATE operations SET memory_tier = 'long_term'
    WHERE memory_tier = 'short_term'
      AND op_id NOT IN (
          SELECT subgraph_root_op_id FROM leases WHERE status = 'ACTIVE'
      )
    """
)
```

### Step 3: Run tests

```bash
uv run pytest tests/test_server_tools.py -v
uv run pyright src/
```

### Step 4: Commit

```bash
git add src/hgp/db.py src/hgp/server.py tests/test_server_tools.py
git commit -m "feat(v2): lease acquire/release drives short_term/long_term tier transitions"
```

---

## Task 4: Server Tool Updates — `include_inactive` + Read Side-Effects

**Files:**
- Modify: `src/hgp/server.py` — `hgp_query_operations`, `hgp_query_subgraph`
- Test: `tests/test_server_tools.py`

### Step 1: Write failing tests

```python
def test_query_inactive_excluded_by_default(server_components):
    r = hgp_create_operation(op_type="artifact", agent_id="a")
    server_components["db"].set_memory_tier(r["op_id"], "inactive")
    server_components["db"].commit()
    ops = hgp_query_operations()
    assert r["op_id"] not in {o["op_id"] for o in ops}

def test_query_inactive_included_when_requested(server_components):
    r = hgp_create_operation(op_type="artifact", agent_id="a")
    server_components["db"].set_memory_tier(r["op_id"], "inactive")
    server_components["db"].commit()
    ops = hgp_query_operations(include_inactive=True)
    assert r["op_id"] in {o["op_id"] for o in ops}

def test_query_by_op_id_records_access(server_components):
    r = hgp_create_operation(op_type="artifact", agent_id="a")
    hgp_query_operations(op_id=r["op_id"])
    op = server_components["db"].get_operation(r["op_id"])
    assert op["access_count"] == 1

def test_subgraph_records_access_for_all_traversed_ops(server_components):
    a = hgp_create_operation(op_type="artifact", agent_id="a")
    b = hgp_create_operation(op_type="artifact", agent_id="a", parent_op_ids=[a["op_id"]])
    hgp_query_subgraph(root_op_id=b["op_id"], direction="ancestors")
    db = server_components["db"]
    assert db.get_operation(a["op_id"])["access_count"] == 1
    assert db.get_operation(b["op_id"])["access_count"] == 1

def test_subgraph_increments_edge_weight(server_components):
    a = hgp_create_operation(op_type="artifact", agent_id="a")
    b = hgp_create_operation(op_type="artifact", agent_id="a", parent_op_ids=[a["op_id"]])
    hgp_query_subgraph(root_op_id=b["op_id"], direction="ancestors")
    db = server_components["db"]
    edge = db.execute(
        "SELECT weight FROM op_edges WHERE child_op_id=? AND parent_op_id=?",
        (b["op_id"], a["op_id"]),
    ).fetchone()
    assert edge["weight"] > 1.0

def test_query_tier_ordering(server_components):
    """short_term ops appear before long_term in results."""
    a = hgp_create_operation(op_type="artifact", agent_id="a")
    b = hgp_create_operation(op_type="artifact", agent_id="a")
    server_components["db"].set_memory_tier(a["op_id"], "short_term")
    server_components["db"].commit()
    ops = hgp_query_operations()
    ids = [o["op_id"] for o in ops]
    assert ids.index(a["op_id"]) < ids.index(b["op_id"])
```

### Step 2: Implement in `server.py`

Update `hgp_query_operations`:
```python
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
    db, _, _, _ = _get_components()
    if op_id:
        op = db.get_operation(op_id)
        if op:
            db.record_access(op_id)
            db.commit()
        return [op] if op else []
    return db.query_operations(
        status=status, agent_id=agent_id, op_type=op_type,
        since_commit_seq=since_commit_seq,
        include_inactive=include_inactive, limit=limit,
    )
```

Update `hgp_query_subgraph` — tier-based detail projection + distance-decay access recording:
```python
# Tier-based field projection
_FULL_FIELDS = None  # all fields (short_term)
_SUMMARY_FIELDS = {"op_id", "op_type", "status", "commit_seq", "agent_id", "memory_tier"}
_STUB_FIELDS    = {"op_id", "op_type", "memory_tier"}

def _project(op: dict, tier: str) -> dict:
    if tier == "short_term":
        return op
    if tier == "long_term":
        return {k: v for k, v in op.items() if k in _SUMMARY_FIELDS}
    return {k: v for k, v in op.items() if k in _STUB_FIELDS}

@mcp.tool()
def hgp_query_subgraph(
    root_op_id: str,
    direction: str = "ancestors",
    max_depth: int = 50,
    include_invalidated: bool = False,
) -> dict[str, Any]:
    db, _, _, _ = _get_components()
    chain_hash = compute_chain_hash(db, root_op_id)
    if direction == "ancestors":
        ops = get_ancestors(db, root_op_id, max_depth=max_depth)
    else:
        ops = get_descendants(db, root_op_id, max_depth=max_depth)

    # Filter invalidated (but always include all tiers — never cut the graph)
    if not include_invalidated:
        ops = [o for o in ops if o["status"] != "INVALIDATED"]

    # Tier-based detail projection
    projected = [_project(op, op.get("memory_tier", "long_term")) for op in ops]

    # Best-effort distance-decay access recording (fire-and-forget, non-blocking)
    _record_access_with_decay(db, ops)

    return {"root_op_id": root_op_id, "chain_hash": chain_hash, "operations": projected}


def _record_access_with_decay(db: Database, ops: list[dict]) -> None:
    """Record access with depth-based decay. Best-effort: never raises."""
    DECAY = [1.0, 0.7, 0.4, 0.1]  # depth 0,1,2,3+
    try:
        for depth, op in enumerate(ops):
            weight = DECAY[min(depth, len(DECAY) - 1)]
            db.execute(
                """UPDATE operations
                   SET access_count = access_count + ?,
                       last_accessed = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
                       memory_tier = CASE WHEN memory_tier = 'inactive' THEN 'long_term'
                                         ELSE memory_tier END
                   WHERE op_id = ?""",
                (weight, op["op_id"]),
            )
        db.commit()
    except Exception:
        pass  # Best-effort: access recording must never block read results
```

### Step 3: Run tests

```bash
uv run pytest tests/test_server_tools.py -v
uv run pyright src/
```

### Step 4: Commit

```bash
git add src/hgp/server.py tests/test_server_tools.py
git commit -m "feat(v2): query tools add include_inactive, read side-effects, edge weight tracking"
```

---

## Task 5: New Tool `hgp_set_memory_tier`

**Files:**
- Modify: `src/hgp/server.py`
- Test: `tests/test_server_tools.py`

### Step 1: Write failing test

```python
def test_set_memory_tier_explicit(server_components):
    r = hgp_create_operation(op_type="artifact", agent_id="a")
    result = hgp_set_memory_tier(op_id=r["op_id"], tier="inactive")
    assert result["tier"] == "inactive"
    assert server_components["db"].get_operation(r["op_id"])["memory_tier"] == "inactive"

def test_set_memory_tier_invalid(server_components):
    r = hgp_create_operation(op_type="artifact", agent_id="a")
    result = hgp_set_memory_tier(op_id=r["op_id"], tier="nonexistent")
    assert "error" in result
```

### Step 2: Implement

```python
@mcp.tool()
def hgp_set_memory_tier(op_id: str, tier: str) -> dict[str, Any]:
    """Explicitly set the memory tier of an operation."""
    valid = {"short_term", "long_term", "inactive"}
    if tier not in valid:
        return {"error": "INVALID_TIER", "valid_tiers": sorted(valid)}
    db, _, _, _ = _get_components()
    db.set_memory_tier(op_id, tier)
    db.commit()
    return {"op_id": op_id, "tier": tier}
```

Also add to imports in `server.py`: nothing new needed.

### Step 3: Run tests

```bash
uv run pytest tests/test_server_tools.py::test_set_memory_tier_explicit -v
uv run pyright src/
```

### Step 4: Commit

```bash
git add src/hgp/server.py tests/test_server_tools.py
git commit -m "feat(v2): add hgp_set_memory_tier MCP tool"
```

---

## Task 6: Reconciler Extension — Background Demotion Pass

**Files:**
- Modify: `src/hgp/reconciler.py`
- Modify: `src/hgp/models.py` — add `demoted_to_inactive: int` to `ReconcileReport`
- Test: `tests/test_reconciler.py`

### Step 1: Write failing test

```python
def test_reconcile_demotes_inactive_ops(hgp_dirs: dict):
    from datetime import datetime, timezone, timedelta
    db, cas, rec = _setup(hgp_dirs)
    db.begin_immediate()
    db.insert_operation("old-op", "artifact", "agent-1", 1, "sha256:x")
    db.commit()
    old_ts = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    db.execute("UPDATE operations SET last_accessed = ? WHERE op_id = 'old-op'", (old_ts,))
    db.commit()
    report = rec.reconcile()
    assert report.demoted_to_inactive >= 1
    assert db.get_operation("old-op")["memory_tier"] == "inactive"

def test_reconcile_demote_dry_run(hgp_dirs: dict):
    from datetime import datetime, timezone, timedelta
    db, cas, rec = _setup(hgp_dirs)
    db.begin_immediate()
    db.insert_operation("old-op", "artifact", "agent-1", 1, "sha256:x")
    db.commit()
    old_ts = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    db.execute("UPDATE operations SET last_accessed = ? WHERE op_id = 'old-op'", (old_ts,))
    db.commit()
    report = rec.reconcile(dry_run=True)
    assert report.demoted_to_inactive >= 1
    assert db.get_operation("old-op")["memory_tier"] == "long_term"  # not mutated
```

### Step 2: Add `demoted_to_inactive` to `ReconcileReport`

```python
class ReconcileReport(BaseModel):
    missing_blobs: list[str] = Field(default_factory=list)
    orphan_candidates: list[str] = Field(default_factory=list)
    staging_cleaned: int = 0
    skipped_young_blobs: int = 0
    demoted_to_inactive: int = 0   # NEW
    errors: list[str] = Field(default_factory=list)
```

### Step 3: Add demotion pass to `reconciler.py`

In `reconcile()`, before the final `if not dry_run: self._db.commit()`:
```python
# Rule 4: Tier demotion — ops not accessed within threshold become inactive
if not dry_run:
    report.demoted_to_inactive = self._db.demote_inactive(threshold_days=30)
else:
    # Count candidates without mutating
    from datetime import timedelta
    threshold = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    row = self._db.execute(
        """SELECT COUNT(*) FROM operations
           WHERE memory_tier != 'inactive' AND memory_tier != 'short_term'
             AND (last_accessed IS NULL AND created_at < ? OR last_accessed < ?)""",
        (threshold, threshold),
    ).fetchone()
    report.demoted_to_inactive = row[0] if row else 0
```

### Step 4: Run full suite

```bash
uv run pytest tests/ -v --tb=short
uv run pyright src/
```
Expected: all PASS, 0 errors.

### Step 5: Commit

```bash
git add src/hgp/reconciler.py src/hgp/models.py tests/test_reconciler.py
git commit -m "feat(v2): reconciler adds demotion pass (Rule 4: inactive tier)"
```

---

## Verification

```bash
uv run pytest tests/ -v --tb=short   # all PASS
uv run pyright src/                    # 0 errors
```

---

## File Change Summary

| File | Action |
|------|--------|
| `src/hgp/db.py` | Schema V2 migration, `record_access()`, `demote_inactive()`, `set_memory_tier()`, `query_operations()` updated |
| `src/hgp/server.py` | `hgp_query_operations` + `hgp_query_subgraph` updated, `hgp_set_memory_tier` added, lease tier integration |
| `src/hgp/models.py` | `MemoryTier` enum, `ReconcileReport.demoted_to_inactive` |
| `src/hgp/reconciler.py` | Rule 4 demotion pass |
| `tests/test_db.py` | Schema, `record_access`, `demote_inactive`, tier-aware query tests |
| `tests/test_server_tools.py` | `include_inactive`, access side-effect, edge weight, tier ordering, `hgp_set_memory_tier` tests |
| `tests/test_reconciler.py` | Demotion pass tests |

---

## Finalized Design Decisions (post-review)

### 1. Inactivity threshold: Relative baseline + absolute max

```
project_pulse = MAX(last_accessed) across all operations

inactive condition:
  project_pulse - COALESCE(op.last_accessed, op.created_at) > 30 days
```

- **Relative baseline** prevents mass-demotion after project hibernation
- **30-day absolute max** ensures truly stale nodes eventually demote
- **COALESCE fallback** handles never-accessed ops via created_at
- **Percentile pruning: explicitly excluded** — old nodes are causal evidence,
  not clutter. Percentile conflicts with the append-only immutability principle.

### 2. short_term TTL: Tied to lease TTL, not reconciler

```
lease TTL expires → memory_tier demoted to long_term immediately
lease heartbeat (validate_lease) → keeps both lease AND short_term alive
crash → heartbeat stops → TTL expires → immediate demotion
```

### 3. Access recording: depth-based, conditional last_accessed update

```
depth 0-2 (weight >= 0.4): access_count += weight  AND  last_accessed = now
depth 3+  (weight = 0.1):  access_count += 0.1     ONLY (last_accessed unchanged)
```

Depth comes from CTE `depth` column (already in _ANCESTOR_DEPTH_SQL),
not list index.

### 4. Detail projection fields (confirmed)

```
short_term → all fields
long_term  → {op_id, op_type, status, commit_seq, agent_id, memory_tier}
inactive   → {op_id, op_type, memory_tier}
```

Traversal always visits all nodes. `include_inactive` applies only to
list queries (hgp_query_operations), not subgraph traversal.
