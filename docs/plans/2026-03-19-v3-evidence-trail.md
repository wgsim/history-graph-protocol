# HGP V3 Evidence Trail Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Allow agents to record *why* they made a decision — which prior operations they used as evidence, which part of that evidence they referenced, and what conclusion they drew from it. The system records and serves this data; judgment of sufficiency or validity is left to the model and user.

**Design Principle:** The system's role is to record and retrieve. Trust assessment ("is this evidence reliable?") and invalidation propagation ("should this conclusion be revisited?") are out of scope for V3.

**Tech Stack:** Python 3.12, SQLite (WAL), FastMCP, Pydantic v2, pytest, pyright strict

---

## Context: V2 Architecture (prerequisite)

V3 builds on top of V2. The following must be in place:

```
memory_tier column on operations table
op_edges with edge_type IN ('causal', 'invalidates')
CAS blob store (WORM)
LeaseManager with heartbeat-based TTL
```

V1/V2 invariants that MUST NOT change:
- `op_id` is immutable once created
- `object_hash` → CAS blob is WORM
- `chain_hash` is a computed digest, never stored
- `commit_seq` is monotonically increasing
- All nodes are always traversable regardless of tier or evidence state

---

## V3 Design: Evidence Trail

### Core Concept

A new table `op_evidence` records which prior operations an agent used as evidence
when creating a new operation, along with structured metadata about how each piece
was used. Evidence is stored separately from `op_edges` to preserve DAG integrity.

```
causal     → A led to B (temporal, sequential)      stored in: op_edges
invalidates → A supersedes/negates B                stored in: op_edges
evidence   → A was informed by B (referential)      stored in: op_evidence
```

Evidence relationships are **non-causal** and **non-hierarchical**: citing an
operation as evidence does not imply temporal succession or parent-child ordering.
An agent may cite operations from anywhere in the DAG.

---

## Schema Changes

### New table: `op_evidence`

`op_edges`를 확장하지 않고 별도 테이블로 분리한다. 이유:
- `op_edges`의 `PRIMARY KEY (child_op_id, parent_op_id)`와 충돌 없음
- `dag.py` CTE가 `op_edges`만 순회하므로 DAG 탐색에 evidence가 누출되지 않음
- `chain_hash` 계산 영향 없음
- `relation`을 DB 수준 CHECK 제약으로 보장 가능
- `citing/cited` 명칭으로 계층 없는 인용 관계를 명확히 표현

```sql
CREATE TABLE op_evidence (
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
| `supports` | Evidence backs the citing operation's conclusion |
| `refutes` | Considered as counter-evidence; addressed and rejected |
| `context` | Background/contextual information |
| `method` | Methodological or procedural reference |
| `source` | Primary data source or raw input |

### Field definitions

| Field | Required | Purpose |
|-------|----------|---------|
| `relation` | ✅ | Type of evidential relationship (controlled vocabulary) |
| `scope` | ❌ | Field path, sequence range, or sub-op list within cited op |
| `inference` | ❌ | One-line conclusion drawn from this evidence |

---

## Design Decisions

### Post-creation evidence addition: NOT allowed

Evidence records the agent's reasoning **at the moment of decision**. Allowing
post-hoc additions breaks the trail's trustworthiness — it becomes indistinguishable
from retrospective justification.

If additional evidence is discovered later, create a new operation (e.g. `annotation`
type) with causal edge to the original and `evidence_refs` to the new evidence.

### Access recording: triggers V2 decay

The two query tools apply different access recording rules based on what was
actually *read*, not merely *discovered*:

| Tool | Depth 0 | Depth 1 |
|------|---------|---------|
| `hgp_get_evidence(X)` | X (weight 1.0) | cited ops (weight 0.7) |
| `hgp_get_citing_ops(X)` | X (weight 1.0) | — not recorded |

**Rationale:** `hgp_get_evidence` actively reads the cited ops' content to
understand X's reasoning → depth 1 recorded. `hgp_get_citing_ops` only discovers
*who* cited X; the citing ops' content is not read → recording them would
incorrectly reset their `last_accessed` and prevent natural demotion to inactive.

depth 1 weight (0.7) exceeds the 0.4 threshold, so `last_accessed` is updated.
Inactive cited ops are automatically promoted to `long_term`. No new logic —
reuses existing V2 `_record_access_with_decay()`.

---

## API Changes

### `hgp_create_operation` — add `evidence_refs` parameter

```python
evidence_refs: list[EvidenceRef] | None = None
```

```python
class EvidenceRef(BaseModel):
    op_id: str
    relation: Literal["supports", "refutes", "context", "method", "source"]
    scope: str | None = None
    inference: str | None = None
```

One row inserted into `op_evidence` per ref. Self-reference and non-existent
`op_id` are rejected.

### `hgp_get_evidence` — new tool

Returns all operations that `op_id` cited as evidence.

```python
def hgp_get_evidence(op_id: str) -> list[EvidenceRecord]
```

```python
class EvidenceRecord(BaseModel):
    cited_op_id: str
    op_type: str        # of the cited operation
    status: str         # of the cited operation
    memory_tier: str    # of the cited operation (V2)
    relation: str
    scope: str | None
    inference: str | None
    created_at: str
```

Triggers V2 access recording (depth 0 = citing op, depth 1 = cited ops).

### `hgp_get_citing_ops` — new tool

Returns all operations that cited `op_id` as evidence (reverse direction).

```python
def hgp_get_citing_ops(op_id: str) -> list[CitingRecord]
```

```python
class CitingRecord(BaseModel):
    citing_op_id: str
    op_type: str        # of the citing operation
    status: str
    memory_tier: str
    relation: str
    scope: str | None
    inference: str | None
    created_at: str
```

---

## Implementation Steps

### Step 1 — Schema migration

- Create `op_evidence` table with indexes in `db.py` (`_ensure_schema`)
- No changes to `op_edges`

### Step 2 — Pydantic models

- Add `EvidenceRef`, `EvidenceRecord`, `CitingRecord` to `models.py`

### Step 3 — `db.py`

- `insert_evidence(citing_op_id, refs)` — batch insert into `op_evidence`
- `get_evidence(op_id)` — SELECT from `op_evidence` WHERE `citing_op_id`
- `get_citing_ops(op_id)` — SELECT from `op_evidence` WHERE `cited_op_id`
- Both query functions call `_record_access_with_decay()` with depth mapping

### Step 4 — `server.py`

- `hgp_create_operation`: accept `evidence_refs`, call `db.insert_evidence`
- `hgp_get_evidence`: new tool
- `hgp_get_citing_ops`: new tool
- Total MCP tools: 9 → 11

### Step 5 — Tests

- `test_db.py`: insert, query both directions, UNIQUE constraint enforcement
- `test_server_tools.py`: end-to-end evidence recording and retrieval
- Edge cases:
  - Self-reference → reject
  - Non-existent `op_id` → reject
  - Duplicate (same citing + cited) → reject
  - Missing `relation` → reject (Pydantic + DB CHECK)
  - Inactive cited op access → verify tier promotion

---

## Out of Scope (explicitly excluded)

| Feature | Reason |
|---------|---------|
| Confidence scoring | Judgment belongs to model/user, not system |
| Cascading invalidation via evidence | Over-extension; invalidation is a separate edge type |
| Evidence strength weighting | Analytical role, not recording role |
| Automatic evidence suggestion | Agent decides what to cite |
| Post-creation evidence addition | Breaks trail trustworthiness; use annotation op instead |
| Evidence cycle detection | Evidence is non-hierarchical; cycles are valid (A cites B, B cites A) |
