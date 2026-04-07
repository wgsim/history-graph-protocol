# HGP Storage Topology Recommendation

- Date: 2026-04-07
- Topic: Global vs `repo-local` vs `worktree-local` state layout for HGP V1-V5
- Status: Recommendation draft, reviewed

## Context

From V1 through V5, the implementation has been globally scoped by default:

- `HGP_DB_PATH` defaults to `~/.hgp/hgp.db`
- CAS defaults to `~/.hgp/.hgp_content/`
- file tools introduced in V4 are project-root scoped for path validation, but not for storage placement

V4 introduced one conceptual exception: the `.gitignore` change in `0a50672`, which implied the DB should travel with project state. That direction exposed a deeper design question:

- should HGP remain globally shared across all projects
- should it become project-local
- or should it become worktree-local

This document recommends a target model and explains why.

## Design goals

The storage topology should satisfy these constraints:

- preserve durable project-level file history
- isolate parallel branch or worktree activity enough to avoid state collisions
- avoid Git-tracking raw SQLite state
- keep reconciler scope intuitive
- minimize cross-project leakage risk
- remain operationally understandable for agents and humans

## Option comparison

| Option | Strengths | Weaknesses | Assessment |
|---|---|---|---|
| `global` | simplest startup model; single DB/CAS; lowest migration cost | cross-project mixing; weak file-tracking semantics; reconciler scope too broad; branch/worktree concurrency ambiguity | acceptable only as legacy mode |
| `repo-local` | durable history matches project boundary; reconciler scope becomes intuitive; better alignment with V4 file semantics | requires root resolution; same repo with multiple worktrees still shares mutable runtime state unless explicitly separated | strong base for durable state |
| `worktree-local` | strongest concurrency isolation; pending state naturally follows active checkout | durable history disappears with worktree cleanup; history becomes checkout-scoped instead of project-scoped | good for transient state, weak for durable history |

## Key observation

The debate becomes much clearer if durable state and transient state are treated separately.

Most of the objections raised against “project-local” are actually objections to either:

- `project-local + git-tracked SQLite DB`, or
- using one storage scope for both durable history and transient in-flight state

Those are different problems.

## Recommended architecture

### Recommendation

Use this as the target model:

- `repo-local durable store`
- `worktree-local transient overlay`
- keep both `gitignored`

However, this should be adopted in phases. The full split is the long-term shape, not the first migration step.

### Durable state

Durable state should live at repository scope and survive worktree cleanup.

Recommended contents:

- `COMPLETED` operations
- CAS blobs
- evidence links
- anchors
- long-lived file history

Recommended location:

- `<repo_root>/.hgp/`

Recommended Git policy:

- `.hgp/` remains ignored by Git
- do not commit SQLite DB or CAS blobs into repository history

### Transient state

Transient state should be scoped to the active worktree.

Recommended contents:

- `PENDING` operations
- active leases
- hook marker state
- in-flight mutation bookkeeping
- worktree-specific reconcile scratch state

Recommended location:

- `<worktree_root>/.hgp-tmp/`
  or
- `<repo_root>/.hgp/worktrees/<worktree-id>/`

The exact layout is secondary. The main point is that transient state must not be shared blindly across concurrent worktrees.

## Recommended rollout order

### Phase 1: move from `global` to `repo-local single store`

This is the recommended first implementation step.

Characteristics:

- one repo-local DB
- one repo-local CAS
- both Git-ignored
- no separate worktree-local DB yet

Why this comes first:

- it removes the global/project semantic mismatch
- it avoids immediate cross-DB coordination complexity
- it preserves durable project history
- it keeps migration understandable

### Phase 2: add worktree-local transient partitioning only if needed

This should be added only if real concurrent-worktree pressure justifies it.

Good triggers for Phase 2:

- lease interference across worktrees
- `PENDING` collision or ambiguity
- reconciler confusion tied to concurrent checkouts

Without those signals, a single repo-local store is the better cost/benefit choice.

## Why this recommendation fits the concerns

### 1. Parallel branch / worktree isolation

This is the strongest argument against a purely global model.

If multiple worktrees share one mutable state store, then:

- `PENDING` entries can collide
- leases can interfere
- reconciler runs can observe the wrong checkout context

A worktree-local transient overlay solves this without sacrificing project-level durability.

### 2. Project root must be determined at startup

This is a real cost, but it is a bounded bootstrap problem, not an architectural blocker.

Recommended resolution order:

1. `HGP_PROJECT_ROOT` if explicitly set
2. nearest `.git` from current working directory
3. fail fast with a clear startup error

That is simpler than continuing to pretend file tracking is project-scoped while storage remains globally shared.

### 3. Reconciler must run per project

That is not a drawback. It is the correct boundary.

A file-tracking reconciler should operate over the state that corresponds to one repository, not over a global mixed store of unrelated projects.

### 4. `git checkout` can jump DB state and create pending mismatches

This is the strongest argument against Git-tracking the SQLite DB.

The fix is not “stay global”. The fix is:

- do not commit the DB
- keep durable state local but Git-ignored
- reconcile transient state against the current checkout explicitly

### 5. Clone portability

If the requirement is “clone should bring HGP history with it”, raw SQLite-in-Git is the wrong mechanism.

Recommended answer:

- add explicit `export` / `import` / `snapshot` workflows later if portability is needed

That is operationally cleaner than using Git history as a transport for binary runtime state.

### 6. Implementation complexity

This recommendation is more complex than the current global model, but less problematic than either:

- `global` with increasingly project-specific semantics layered on top, or
- `project-local + git-tracked DB`

The complexity pays for clearer boundaries.

### 7. Cross-project causal chains

Moving away from a global DB changes the meaning of `parent_op_ids` and other graph references.

Under a repo-local model, cross-project causal edges are no longer naturally representable inside one SQLite file. That is a real trade-off.

Current assessment:

- no validated V1-V5 workflow depends on cross-project graph edges
- the capability exists implicitly in a global store, but it is not a documented core requirement

Recommendation:

- treat cross-project causal chains as explicitly unsupported in the local-store design
- document that trade-off
- if the need appears later, solve it with explicit federation, not by returning to one global mutable DB

## Explicit non-recommendations

The following should not be the target design:

- `global` as the long-term architecture for V4/V5 file tracking
- `repo-local + git-tracked SQLite DB`
- `worktree-local only` for all HGP state

Why:

- `global` keeps semantic mismatch alive
- Git-tracked SQLite creates merge/diff/churn and checkout-jump issues
- `worktree-local only` loses durable project history when worktrees are deleted

## Final recommendation

If a single storage direction must be chosen for the next architecture step, choose:

- `repo-local`, not `global`

If the fuller model is acceptable, choose:

- `repo-local durable + worktree-local transient`

If only one immediately actionable rule is adopted, it should be:

- stop treating the SQLite DB as Git-tracked project state

## Practical decision summary

### Best long-term model

- durable: `<repo_root>/.hgp/`
- transient: worktree-local overlay
- Git policy: ignored

### Best minimal transition

- move from `global` to `repo-local`
- keep a single repo-local DB/CAS first
- add worktree-local transient partitioning only for mutable runtime state

### Preferred migration strategy

- treat `~/.hgp/` as legacy
- send all new writes to repo-local storage
- keep global data available as a read-only import source during transition
- add a per-project import tool later if preserving historical development-era data matters

This corresponds most closely to:

- preferred: **M2** first
- optional follow-up: **M1**
- avoid using Git history itself as the migration transport

### Legacy fallback

- retain `global` mode only as an explicit compatibility mode, not as the architectural default

---

## Implementation review

Reviewed: 2026-04-07
Reviewer: Claude (Phase 4 planning — full codebase read)

### Assessment: the core recommendation is sound

The durable/transient separation is the key insight. Most objections to any single-scope model are actually objections to mixing these two concerns in one storage location. The "best minimal transition" (repo-local single DB, gitignored) is the correct first step.

### Existing infrastructure already provides the key root-resolution building block

`project.py:find_project_root()` already implements the recommended startup resolution order:

1. `HGP_PROJECT_ROOT` env var
2. nearest `.git` traversal from `start`
3. `ProjectRootError` on failure

This function is already used by every file tool in `server.py` (7 call sites). The main gap is `_get_components()` — it still uses hardcoded `Path.home() / ".hgp"` instead of deriving storage placement from a resolved project root.

So the codebase already contains the root-resolution primitive needed for the transition, but it does not yet support the transition end-to-end.

### Concerns

#### 1. Two-store coordination creates a new failure mode

PENDING → COMPLETED promotion is currently a single SQLite transaction within one file. The full model (repo-local durable + worktree-local transient) turns this into cross-file coordination:

- worktree DB: `INSERT PENDING`
- repo DB: `INSERT COMPLETED`
- worktree DB: `DELETE PENDING`

Partial failure creates state inconsistency that does not exist today. The reconciler would need cross-DB recovery logic.

**Mitigation**: the minimal transition avoids this entirely by keeping a single repo-local DB. This concern applies only to the full durable/transient split.

#### 2. Cross-project causal chains are silently dropped

The document does not address this. Repo-local means `parent_op_ids` cannot reference operations in another project's DB. The global model allowed cross-project causal links.

**Assessment**: this capability is unvalidated in practice — no known cross-project edge usage exists in V1–V5. Documenting it as an explicit trade-off is sufficient. If needed later, a federation mechanism is the right answer, not a shared global DB.

#### 3. Migration from `~/.hgp/` not addressed

Existing data in `~/.hgp/hgp.db` needs a migration path:

- **M1**: per-project migration tool — filter ops by `file_path` prefix, copy to target repo DB
- **M2**: leave `~/.hgp/` as read-only legacy, new writes go to repo-local
- **M3**: discard — acceptable if existing data is development-era only

**Recommended handling**: adopt **M2** as the default transition path, and add **M1** only if historical retention becomes important enough to justify the tooling.

#### 4. Worktree-local transient location needs a concrete choice

| Location | Lifecycle | Implication |
|---|---|---|
| `<worktree>/.hgp-tmp/` | dies with worktree | clean; transient state is disposable by definition |
| `<repo>/.hgp/worktrees/<id>/` | persists in repo root | survives cleanup but needs its own GC |

For the minimal transition this is not blocking. For the full model, `<worktree>/.hgp-tmp/` is preferred — transient state is disposable and reconciler handles recovery.

### Recommended implementation sequence

**Step 1 — Minimal transition** (Task 4.2 scope):
- `_get_components()` resolves project root via `find_project_root(Path.cwd())`
- DB and CAS created at `<repo_root>/.hgp/`
- `.hgp/` added to `.gitignore`
- Optional: `HGP_GLOBAL_MODE=1` env var retains `~/.hgp/` as backward-compat fallback
- Architecture docs updated: remove git-tracked DB claims, document repo-local + gitignored model

**Step 2 — Durable/transient split** (future, separate initiative):
- Second DB file or schema partitioning
- Cross-DB promotion logic for PENDING → COMPLETED
- Reconciler updates for cross-store recovery

**Step 3 — Migration tooling** (future, separate initiative):
- `hgp_migrate_from_global` MCP tool to move ops from `~/.hgp/` into the repo-local DB

### Immediately actionable rule

The document's "if only one rule is adopted" recommendation is correct:

> Stop treating the SQLite DB as Git-tracked project state.

Regardless of which implementation path is chosen, the `.gitignore` comment
`# HGP database (committed to git per-project for consistent restore — V4 design)`
and the `architecture.md` section "Git + HGP Consistency" should be corrected immediately.
