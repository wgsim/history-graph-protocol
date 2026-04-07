# Task 4.2 Implementation Plan: Repo-Local Storage (Phase 1 Minimal Transition)

- Date: 2026-04-07
- Task: 4.2 from `docs/plans/2026-04-03-full-audit-remediation-plan.md`
- Status: Ready to implement
- Decision basis: `docs/plans/2026-04-07-hgp-storage-topology-recommendation.md`

## Summary

Move HGP storage from the global `~/.hgp/` to repo-local `<repo_root>/.hgp/`, gitignored.
This is a **Phase 1 minimal transition** — single DB per project, no worktree partitioning.
Phase 2 (worktree-local transient overlay via Option 2B) is deferred until real concurrency pressure appears.

## Decision recap

| Aspect | Decision |
|---|---|
| Storage scope | `<repo_root>/.hgp/` (not `~/.hgp/`) |
| DB file | `<repo_root>/.hgp/hgp.db` |
| CAS directory | `<repo_root>/.hgp/.hgp_content/` |
| Git policy | `.hgp/` ignored (add to `.gitignore`) |
| Backward compat | `HGP_GLOBAL_MODE=1` env var falls back to `~/.hgp/` |
| Phase 2 schema | No worktree_id columns — added only when Phase 2 activates |
| Root resolution | Use existing `find_project_root(Path.cwd())` from `project.py` |

## Files to modify

1. `src/hgp/server.py` — replace hardcoded `HGP_DIR` block + update `_get_components()`
2. `.gitignore` — replace `# *.db` comment with `.hgp/`
3. `docs/architecture.md` — rewrite "Git + HGP Consistency" section

## Step-by-step implementation

---

### Step 1: Update `src/hgp/server.py`

**Current state (lines 39–74):**

```python
HGP_DIR = Path.home() / ".hgp"
HGP_CONTENT_DIR = HGP_DIR / ".hgp_content"
HGP_DB_PATH = HGP_DIR / "hgp.db"
...
def _get_components() -> tuple[Database, CAS, LeaseManager, Reconciler]:
    global _db, _cas, _lease_mgr, _reconciler
    if _db is None:
        db = Database(HGP_DB_PATH)
        try:
            HGP_DIR.mkdir(parents=True, exist_ok=True)
            HGP_CONTENT_DIR.mkdir(exist_ok=True)
            db.initialize()
            cas = CAS(HGP_CONTENT_DIR)
            ...
```

**Target behavior:**

- Remove the module-level `HGP_DIR / HGP_CONTENT_DIR / HGP_DB_PATH` constants
  (they hard-code `~/.hgp/` and are not reusable across multiple projects)
- In `_get_components()`:
  1. Check `os.environ.get("HGP_GLOBAL_MODE")` — if set, use `Path.home() / ".hgp"` (legacy fallback)
  2. Otherwise call `find_project_root(Path.cwd())` to get `project_root`
  3. Derive `hgp_dir = project_root / ".hgp"` and `hgp_content_dir = hgp_dir / ".hgp_content"`
  4. Create directories with `hgp_dir.mkdir(parents=True, exist_ok=True)`
  5. Open `Database(hgp_dir / "hgp.db")` and init as before
  6. On `ProjectRootError`: re-raise with a clear message (do not silently fall back)

**New code block (replace lines 37–74):**

```python
# ── Server initialization ───────────────────────────────────

import os

mcp = FastMCP("hgp")

_db: Database | None = None
_cas: CAS | None = None
_lease_mgr: LeaseManager | None = None
_reconciler: Reconciler | None = None


def _resolve_hgp_dir() -> Path:
    """Return the HGP storage directory for the current project.

    Uses ~/.hgp/ as a legacy fallback when HGP_GLOBAL_MODE=1 is set.
    Otherwise resolves <repo_root>/.hgp/ via find_project_root().
    """
    if os.environ.get("HGP_GLOBAL_MODE"):
        return Path.home() / ".hgp"
    root = find_project_root(Path.cwd())
    return root / ".hgp"


def _get_components() -> tuple[Database, CAS, LeaseManager, Reconciler]:
    global _db, _cas, _lease_mgr, _reconciler
    if _db is None:
        hgp_dir = _resolve_hgp_dir()
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
        _db, _cas, _lease_mgr, _reconciler = db, cas, lease_mgr, reconciler
    assert _db and _cas and _lease_mgr and _reconciler
    return _db, _cas, _lease_mgr, _reconciler
```

**Note:** `os` is already imported via stdlib; double-check it is in the import block at the top of `server.py`.

---

### Step 2: Update `.gitignore`

**Current state:**

```
.hgp_content/

# *.db  ← intentionally not ignored; only WAL/SHM lock files are excluded
*.db-wal
*.db-shm
```

**Target state:**

```
# HGP repo-local storage (not committed to git)
.hgp/
*.db-wal
*.db-shm
```

Remove the `.hgp_content/` line (it moves inside `.hgp/` and is covered by `.hgp/`).
Remove the `# *.db` comment block — it documented the V4 decision that is now reversed.

---

### Step 3: Rewrite `docs/architecture.md` — "Git + HGP Consistency" section

**Current state (lines 530–541):**

```markdown
### Git + HGP Consistency

V4 removes `*.db` from `.gitignore`. The HGP database is committed to git alongside project files. This provides:

- **Consistent restore** — `git restore` or `git checkout` on a previous commit restores both code and the HGP history that corresponds to that code
- **Branch isolation** — Each git branch carries its own HGP history
- **No orphaned history** — Deleting a branch removes its history too

**WAL/SHM files** (`.db-wal`, `.db-shm`) remain in `.gitignore` because they are SQLite lock files that are meaningless outside an active connection.

The `hgp_reconcile` tool handles crash-recovery for the case where the DB is in an inconsistent state after an unexpected shutdown.
```

**Target state:**

```markdown
### Storage placement

HGP stores its database and content-addressable blobs in `<repo_root>/.hgp/`, which is added to `.gitignore`. The HGP store is **not** committed to git.

```
<repo_root>/
  .hgp/
    hgp.db            ← SQLite database (gitignored)
    .hgp_content/     ← CAS blob store (gitignored)
```

This keeps durable project-level file history co-located with the project without introducing binary blob churn into git history.

**Legacy mode:** Set `HGP_GLOBAL_MODE=1` to use `~/.hgp/` instead (single global store, all projects share one DB). This is retained for backward compatibility only and is not recommended for new setups.

**WAL/SHM files** (`.db-wal`, `.db-shm`) are also gitignored — they are SQLite lock files meaningless outside an active connection.

The `hgp_reconcile` tool handles crash-recovery for the case where the DB is in an inconsistent state after an unexpected shutdown.
```

---

### Step 4: Also update `full-audit-remediation-plan.md` Task 4.2

Update the "Step 1 / Step 2" in Task 4.2 to reflect the decision made:

```markdown
**Decision:** Option A — Move DB and CAS to `<project_root>/.hgp/` (repo-local, gitignored).
Phase 1 minimal transition: single DB per project, no worktree-level partitioning.
Phase 2 (worktree_id column isolation) deferred — see `docs/plans/2026-04-07-hgp-phase2-worktree-isolation-options.md`.
```

---

## Verification

```bash
# 1. All existing tests must pass
uv run --extra dev pytest -q

# 2. Confirm no reference to ~/.hgp hardcoded outside legacy path
grep -r 'Path.home().*\.hgp\|home.*hgp' src/hgp/ --include='*.py'
# Expected: only one match in _resolve_hgp_dir() inside server.py

# 3. Confirm .gitignore no longer references *.db comment
grep '\.db' .gitignore
# Expected: *.db-wal and *.db-shm only

# 4. Smoke test: server starts and creates repo-local DB
cd /tmp && git init test-hgp-smoke && cd test-hgp-smoke
HGP_PROJECT_ROOT=/tmp/test-hgp-smoke python -c "
import sys; sys.path.insert(0, '/path/to/src')
from hgp.server import _get_components
db, cas, _, _ = _get_components()
print('hgp_dir:', db._path.parent)
db.close()
"
# Expected: hgp_dir ends with .hgp, not ~/.hgp
```

## Constraints

- Do **not** pre-seed `worktree_id` columns — Phase 1 schema is unchanged
- Do **not** add migration logic from `~/.hgp/` to the new path (that is a separate future initiative)
- `HGP_GLOBAL_MODE=1` fallback must work end-to-end (same as current behavior)
- The `_get_components()` singleton pattern is preserved — no re-initialization mid-session

## Commit message

```
feat(v4): Phase 1 — move HGP storage from ~/.hgp to <repo_root>/.hgp

_get_components() now resolves project root via find_project_root()
and creates <repo_root>/.hgp/{hgp.db,.hgp_content/} on first use.

HGP_GLOBAL_MODE=1 retains legacy ~/.hgp/ behavior for backward compat.

.gitignore: replace *.db comment block with .hgp/ directory rule.
docs/architecture.md: remove git-tracked DB claims, document repo-local
gitignored store and HGP_GLOBAL_MODE fallback.
```
