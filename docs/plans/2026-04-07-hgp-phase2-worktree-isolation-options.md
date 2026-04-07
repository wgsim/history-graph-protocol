# HGP Phase 2: Worktree Isolation — Option Analysis

- Date: 2026-04-07
- Topic: Implementation approach for worktree-local transient state partitioning
- Status: Decision pending
- Recommended option: **2B — Single file + worktree_id column** (activate only when Phase 2 is actually needed)

## Context

Phase 1 moves HGP storage from `~/.hgp/` to `<repo_root>/.hgp/` (repo-local single DB,
gitignored). Phase 2 adds worktree-level isolation for transient state — specifically
`PENDING` operations and active leases — to prevent cross-worktree interference when
multiple worktrees are active on the same repository.

Phase 2 is deferred until one or more of these signals appear in practice:

- lease interference across worktrees
- `PENDING` op collision or reconciler confusion caused by concurrent checkouts
- explicit agent request for worktree-scoped history

Without those signals, Phase 1 single-DB is sufficient.

## What needs isolation

| State type | Scope needed | Reason |
|---|---|---|
| `PENDING` operations | worktree-local | In-flight writes belong to one checkout context |
| Active leases | worktree-local | Lease acquired in worktree A must not block worktree B |
| Hook marker files | worktree-local | Already use `/tmp/` — naturally isolated |
| `COMPLETED` operations | repo-level durable | File history belongs to the repo, not a worktree |
| CAS blobs | repo-level durable | Content is content-addressed, project-wide |
| Evidence links | repo-level durable | Causal links survive worktree deletion |
| Git anchors | repo-level durable | Commit associations are repo-wide |

## Options

---

### Option 2A — Two separate SQLite files (physical isolation)

```
<repo_root>/.hgp/hgp.db            ← durable: COMPLETED, evidence, anchors
<worktree_root>/.hgp-tmp/hgp.db    ← transient: PENDING, leases
```

**How PENDING → COMPLETED promotion works:**

1. Filesystem operation succeeds
2. Write `COMPLETED` row to durable DB
3. Delete `PENDING` row from transient DB

Steps 2 and 3 are not atomic across files. Failure between them leaves inconsistent state.
The reconciler detects and recovers via Rule 5 (CAS blob exists + file present → finalize).

**Phase 1 schema impact:** none — transient DB has its own schema created on demand.

**Phase 2 activation cost:** high.

- New DB file lifecycle management (create on first worktree use, delete on cleanup)
- `_get_components()` must open both connections
- Reconciler must discover and open transient DBs across all active worktrees
- `finalize_operation()` becomes cross-file
- Two connection pools, two WAL files

**Worktree identification:** path to worktree root (`Path.cwd()` resolved to worktree boundary via `git worktree list`).

**Strengths:**
- True filesystem-level isolation
- Worktree cleanup naturally removes transient DB
- Durable DB never touched by transient writes

**Weaknesses:**
- Loses SQLite single-transaction atomicity for PENDING → COMPLETED
- Reconciler complexity grows significantly
- Two-file coordination is a new failure mode (mitigated but not eliminated)
- More moving parts for a problem that may never materialize

**Assessment:** correct model in principle, but high implementation cost relative to the
actual concurrency risk in typical HGP usage.

---

### Option 2B — Single file + worktree_id column (logical isolation) ★ RECOMMENDED

```
<repo_root>/.hgp/hgp.db
  operations.worktree_id TEXT   ← transient rows only, set when Phase 2 activates
  leases.worktree_id     TEXT   ← transient rows only, set when Phase 2 activates
```

**How PENDING → COMPLETED promotion works:**

Same as today — single SQLite transaction, fully atomic. No cross-file coordination.

**Phase 1 schema impact:** none required.

Recommended approach:

- keep Phase 1 as a repo-local single DB/CAS transition only
- add `worktree_id` columns only when Phase 2 is explicitly activated

SQLite column-add migrations are cheap here, so pre-seeding unused schema in Phase 1 is not necessary.

**Phase 2 activation cost:** low.

- `find_project_worktree()` — new helper resolving CWD to worktree root
- Pass `worktree_id` when inserting `PENDING` ops and leases
- Reconciler filters `PENDING` query by `worktree_id`
- Query tools remain unchanged (COMPLETED ops stay repo-scoped)

**Worktree identification:** resolved absolute path of the worktree root.

Recommended semantics:

- transient rows always carry an explicit `worktree_id`
- durable repo-level rows keep `worktree_id = NULL`
- avoid using `NULL` as a special “main worktree transient state” sentinel

```python
def find_project_worktree(start: Path) -> str:
    """Return the canonical worktree root path for the current checkout."""
    root = find_project_root(start)
    return str(root.resolve())
```

**Strengths:**
- SQLite atomicity fully preserved — PENDING → COMPLETED stays in one transaction
- Reconciler stays simple — single DB, add one WHERE clause
- Phase 2 activation is a feature-flag-level change, not an architecture change
- No new failure modes

**Weaknesses:**
- No physical file-level isolation — a bug that ignores `worktree_id` can pollute across worktrees
- Worktree cleanup does not automatically remove its PENDING entries (reconciler must GC orphaned rows)
- All worktrees share one WAL file (contention possible under high concurrent write load)

**Assessment:** the right choice for the concurrency levels HGP is designed for. Physical
isolation adds complexity that only pays off under sustained multi-worktree write pressure,
which is not a documented requirement.

---

### Option 2C — SQLite ATTACH (hybrid)

```
Main connection attaches both:
  ATTACH '<repo>/.hgp/hgp.db'         AS durable
  ATTACH '<worktree>/.hgp-tmp/hgp.db' AS transient
```

Cross-DB queries (JOIN across ATTACH'd databases) are possible within one connection.
PENDING → COMPLETED can be written as a sequenced pair with explicit ordering, not a
true cross-DB transaction.

**Assessment:** non-standard usage pattern, harder to reason about under WAL mode,
and does not actually solve the atomicity problem. Adds complexity without a clear
advantage over 2A or 2B. **Not recommended.**

---

## Comparison table

| Dimension | 2A (two files) | 2B (worktree_id column) ★ | 2C (ATTACH) |
|---|---|---|---|
| PENDING→COMPLETED atomicity | ✗ cross-file | ✓ single transaction | ✗ sequenced pair |
| Physical worktree isolation | ✓ | ✗ logical only | ✓ |
| Phase 1 schema change | none | none required | none |
| Phase 2 activation cost | high | low | high |
| Reconciler complexity | increases significantly | minimal increase | increases |
| Worktree cleanup GC | automatic | needs reconciler GC | automatic |
| New failure modes | cross-file partial write | none | partial sequence |
| Recommended | no | **yes** | no |

## Impact on Phase 1 implementation

If Option 2B is adopted, Phase 1 must include:

- no schema change is strictly required
- no query behavior changes are required
- no `worktree_id` plumbing should be added before an actual Phase 2 trigger exists

Optional alternative:

- pre-seed the columns in Phase 1 if the team strongly prefers migration work to happen earlier

But this is not necessary for correctness or for the repo-local single-store transition.

## Phase 2 activation checklist (when triggered)

When worktree concurrency signals appear:

1. Add `worktree_id TEXT` to `operations`
2. Add `worktree_id TEXT` to `leases`
3. Implement `find_project_worktree(start: Path) -> str` in `project.py`
4. Pass explicit `worktree_id` in transient inserts (`PENDING` ops, active leases)
5. Update reconciler `query_pending_ops()` to filter by current worktree_id
6. Add reconciler GC step: mark PENDING ops whose worktree no longer exists as `STALE_PENDING`
7. Add index: `CREATE INDEX idx_operations_worktree ON operations(worktree_id)`
8. Consider index: `CREATE INDEX idx_leases_worktree ON leases(worktree_id)`
9. Update `docs/architecture.md` — Phase 2 section

This keeps Phase 1 smaller and makes Phase 2 the first point where schema and behavior actually change together.
