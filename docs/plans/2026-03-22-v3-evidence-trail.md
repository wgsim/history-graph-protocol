# HGP V3 Evidence Trail — Implementation Plan

**Branch**: `feat/v3-evidence-trail`
**Worktree**: `.worktrees/feat-v3-evidence-trail`
**Base**: `main @ 081ed71` (V2 + security fixes)
**Date**: 2026-03-22

---

## Goal

Allow agents to record *why* they made a decision — which prior operations
they used as evidence, which part of that evidence they referenced, and what
conclusion they drew from it.

**Design principle**: The system records and retrieves. Judgment of sufficiency
or validity is left to the model and user.

---

## V2 Prerequisites (all satisfied on base branch)

| Requirement | Status |
|-------------|--------|
| `memory_tier` column on `operations` | ✅ `short_term / long_term / inactive` |
| `op_edges` with `edge_type IN ('causal', 'invalidates')` | ✅ |
| CAS blob store (WORM) | ✅ |
| `LeaseManager` with heartbeat TTL | ✅ |
| `_record_access_with_decay()` in `server.py` | ✅ depth-weighted, best-effort |
| `MemoryTier` enum in `models.py` | ✅ |

Current MCP tool count: **10**
After V3: **12** (`hgp_get_evidence`, `hgp_get_citing_ops` added)

---

## Invariants That Must Not Change

- `op_id` is immutable once created
- `object_hash` → CAS blob is WORM
- `chain_hash` is a computed digest, never stored as canonical value
- `commit_seq` is monotonically increasing
- `op_edges` DAG traversal is unaffected by evidence
- `chain_hash` computation excludes `op_evidence` (evidence is non-causal)

---

## Schema: New Table `op_evidence`

Stored separately from `op_edges` to preserve DAG integrity:
- `op_edges PRIMARY KEY (child_op_id, parent_op_id)` — no collision
- `dag.py` CTEs traverse `op_edges` only — evidence never leaks into DAG
- `chain_hash` computation is unaffected

```sql
CREATE TABLE IF NOT EXISTS op_evidence (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    citing_op_id  TEXT NOT NULL,
    cited_op_id   TEXT NOT NULL,
    relation      TEXT NOT NULL CHECK (relation IN
                    ('supports', 'refutes', 'context', 'method', 'source')),
    scope         TEXT DEFAULT NULL,
    inference     TEXT DEFAULT NULL,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (citing_op_id, cited_op_id),
    FOREIGN KEY (citing_op_id) REFERENCES operations(op_id),
    FOREIGN KEY (cited_op_id)  REFERENCES operations(op_id)
);

CREATE INDEX IF NOT EXISTS idx_evidence_citing ON op_evidence(citing_op_id);
CREATE INDEX IF NOT EXISTS idx_evidence_cited  ON op_evidence(cited_op_id);
```

### `relation` vocabulary

| Value | Meaning |
|-------|---------|
| `supports` | Evidence backs the citing op's conclusion |
| `refutes` | Considered as counter-evidence; addressed and rejected |
| `context` | Background / contextual information |
| `method` | Methodological or procedural reference |
| `source` | Primary data source or raw input |

### Field definitions

| Field | Required | Purpose |
|-------|----------|---------|
| `relation` | ✅ | Type of evidential relationship (controlled vocabulary) |
| `scope` | ❌ | Field path, sequence range, or sub-op list within cited op |
| `inference` | ❌ | One-line conclusion drawn from this evidence |

---

## API Changes

### `hgp_create_operation` — add `evidence_refs` parameter

```python
evidence_refs: list[EvidenceRef] | None = None
```

One row inserted into `op_evidence` per ref, inside the same
`BEGIN IMMEDIATE` transaction as the operation insert.

Rejection rules:
- `cited_op_id == citing_op_id` → self-reference
- `cited_op_id` not found in `operations` → non-existent ref
- duplicate `(citing_op_id, cited_op_id)` → UNIQUE violation

### `hgp_get_evidence` — new tool (Tool #11)

Returns all operations that `op_id` cited as evidence.

```python
def hgp_get_evidence(op_id: str) -> list[EvidenceRecord]
```

Access recording (V2 depth-decay):
- `op_id` (citing op): `weight=1.0`
- each cited op: `weight=0.7`
- threshold: `weight >= 0.4` → `last_accessed` updated, `inactive → long_term`

### `hgp_get_citing_ops` — new tool (Tool #12)

Returns all operations that cited `op_id` as evidence (reverse direction).

```python
def hgp_get_citing_ops(op_id: str) -> list[CitingRecord]
```

Access recording:
- `op_id` (cited op): `weight=1.0`
- citing ops: **not recorded** — content not actually read

---

## Pydantic Models (add to `models.py`)

```python
class EvidenceRelation(StrEnum):
    SUPPORTS = "supports"
    REFUTES  = "refutes"
    CONTEXT  = "context"
    METHOD   = "method"
    SOURCE   = "source"

class EvidenceRef(BaseModel):
    op_id:     str
    relation:  EvidenceRelation
    scope:     str | None = None
    inference: str | None = None

class EvidenceRecord(BaseModel):
    cited_op_id:  str
    op_type:      str
    status:       str
    memory_tier:  str
    relation:     str
    scope:        str | None
    inference:    str | None
    created_at:   str

class CitingRecord(BaseModel):
    citing_op_id: str
    op_type:      str
    status:       str
    memory_tier:  str
    relation:     str
    scope:        str | None
    inference:    str | None
    created_at:   str
```

---

## Implementation Steps

### Step 1 — Schema (`db.py`)

- Add `op_evidence` table + indexes to `_SCHEMA_SQL`
- No changes to `op_edges`

### Step 2 — Pydantic models (`models.py`)

- Add `EvidenceRelation`, `EvidenceRef`, `EvidenceRecord`, `CitingRecord`

### Step 3 — DB layer (`db.py`)

- `insert_evidence(citing_op_id: str, refs: list[EvidenceRef]) -> None`
  - batch insert, must be called inside existing transaction
  - validates `cited_op_id` exists; raises `ValueError` on self-reference
- `get_evidence(op_id: str) -> list[dict]`
  - SELECT from `op_evidence JOIN operations` WHERE `citing_op_id = op_id`
  - calls `record_access(op_id, 1.0)` + `record_access(cited, 0.7)` per result
- `get_citing_ops(op_id: str) -> list[dict]`
  - SELECT from `op_evidence JOIN operations` WHERE `cited_op_id = op_id`
  - calls `record_access(op_id, 1.0)` only

### Step 4 — MCP tools (`server.py`)

- `hgp_create_operation`: accept `evidence_refs`, call `db.insert_evidence`
  inside the existing `BEGIN IMMEDIATE` block (after edges, before chain_hash)
- `hgp_get_evidence(op_id)`: call `db.get_evidence`, commit, return list
- `hgp_get_citing_ops(op_id)`: call `db.get_citing_ops`, commit, return list

### Step 5 — Tests

**`test_db.py`**
- insert evidence, query both directions
- self-reference → ValueError
- non-existent cited_op_id → ValueError
- duplicate (citing, cited) → integrity error
- inactive cited op access → `memory_tier` promoted to `long_term`

**`test_server_tools.py`**
- `hgp_create_operation` with `evidence_refs` → rows in `op_evidence`
- `hgp_get_evidence` returns correct cited ops + access recorded
- `hgp_get_citing_ops` returns correct citing ops; cited op access recorded, citing ops NOT recorded
- invalid `relation` → Pydantic validation error
- self-reference in `evidence_refs` → error response
- non-existent `op_id` in `evidence_refs` → error response

---

## Out of Scope

| Feature | Reason |
|---------|---------|
| Confidence scoring | Judgment belongs to model/user |
| Cascading invalidation via evidence | Invalidation is a separate edge type |
| Evidence strength weighting | Analytical role, not recording role |
| Automatic evidence suggestion | Agent decides what to cite |
| Post-creation evidence addition | Breaks trail trustworthiness |
| Evidence cycle detection | Non-hierarchical; A↔B cycles are valid |

---

## Test Count Expectation

| Suite | Current | After V3 |
|-------|---------|----------|
| `test_db.py` | 5 | +5 ≈ 10 |
| `test_server_tools.py` | 57 | +7 ≈ 64 |
| others | 50 | 50 |
| **Total** | **112** | **~124** |
