# HGP Opt-in and Hook Preflight Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** HGP becomes opt-in per repository: the server never creates `.hgp/` implicitly, `hgp init` enables a repository, and every hook exits silently (and never crashes) where HGP is not enabled.

**Architecture:** "Enabled" means the storage `.hgp/` directory exists (shared main-repo `.hgp/` for linked worktrees, as resolved by `hgp_paths`). The server checks this once per tool call inside `_check_mode()`, which every MCP tool already calls first. `hgp init` is the only path that creates `.hgp/` (besides existing config CLIs and global mode). Hooks gain a preflight gate plus a blanket fail-open wrapper.

**Tech Stack:** Python 3.12/3.13, FastMCP (stdio), SQLite, stdlib-only hook scripts, pytest, ruff, pyright (strict, CI on 3.12).

**Spec:** `docs/implementation_plan.md` — "Defensive Global Hooks" and "Server Context Logic" (no implicit `.hgp/`, `HGP_NOT_INITIALIZED`), as simplified by the necessity review (2026-11-16): explicit `hgp init` replaces the interactive prompt and `~/.hgp/ignore_list`; no error-code renames; hook error dead-letter log is out of scope.

**Base:** branch `wgsim-fix-worktree-discovery` (PR wgsim/history-graph-protocol#1) or `main` after it merges. Work on a new branch `wgsim-hgp-opt-in`.

## Global Constraints

- Hook scripts import only the standard library plus `hgp_paths` (shipped beside them); added overhead per hook call stays under 15 ms.
- Hooks are fail-open: any unexpected exception → exit code 0, no stderr traceback. Deliberate block (exit 2 / deny JSON) is unchanged.
- A repository whose storage `.hgp/` already exists is enabled with no user action (backward compatibility).
- `HGP_GLOBAL_MODE` behaviour is unchanged (`~/.hgp/` is still created on demand).
- Error response shape: `{"error": "HGP_NOT_INITIALIZED", "message": "HGP is not enabled for <root>. Run `hgp init` to enable it."}`. No existing error code is renamed.
- `pyright` (strict) and `ruff check src tests` must pass; run them as CI does: `uv run --extra dev pyright`, `uv run ruff check src tests`, `uv run pytest -q`.

## Review Focus

1. Repository that already has `.hgp/` from an earlier version → tools and hooks work exactly as before (Task 2 test `test_existing_hgp_dir_stays_enabled`).
2. Linked worktree where only the main repository has `.hgp/` → enabled in the worktree for server and hooks (Task 2 `test_worktree_enabled_via_main_repo`, Task 4 `test_hooks_enabled_in_worktree`).
3. `hgp init` run while a server process is already running → the next tool call succeeds without restarting (negative result never cached; Task 2 `test_enable_mid_session`).
4. Hook fed malformed stdin, an unreadable policy (directory instead of file), or no `hgp_paths` → exit 0, empty stderr (Task 4 `test_hooks_fail_open`).
5. `hgp init` twice, or in a repo whose `.gitignore` already ignores `.hgp/` → idempotent, no duplicate exclude line (Task 3 tests).

---

### Task 1: `hgp_paths.hgp_enabled`

**Files:**
- Modify: `src/hgp/hooks/hgp_paths.py`
- Test: `tests/test_opt_in.py` (create)

**Interfaces:**
- Produces: `hgp_enabled(start: Path) -> bool` — `True` iff `find_hgp_dir(start)` returns a path that `is_dir()`.

- [ ] **Step 1: Write failing tests** in `tests/test_opt_in.py` (reuse a real `git init` / `git worktree add` helper like `tests/test_worktrees.py::linked_worktree`):

```python
def test_hgp_enabled_false_without_hgp_dir(tmp_path): repo = git_init(tmp_path / "r"); assert not hgp_paths.hgp_enabled(repo)
def test_hgp_enabled_true_with_hgp_dir(tmp_path): repo = git_init(tmp_path / "r"); (repo / ".hgp").mkdir(); (repo / "sub").mkdir(); assert hgp_paths.hgp_enabled(repo / "sub")
def test_hgp_enabled_worktree_uses_main(linked): repo, wt = linked; assert not hgp_paths.hgp_enabled(wt); (repo / ".hgp").mkdir(); assert hgp_paths.hgp_enabled(wt)
def test_hgp_enabled_outside_git(tmp_path): assert not hgp_paths.hgp_enabled(tmp_path)
```

- [ ] **Step 2:** `uv run pytest tests/test_opt_in.py -q` → FAIL (`hgp_enabled` undefined).
- [ ] **Step 3:** Implement `hgp_enabled` in `hgp_paths.py` (one line over `find_hgp_dir`).
- [ ] **Step 4:** Re-run → PASS.
- [ ] **Step 5:** Commit `feat: add hgp_enabled helper`.

### Task 2: Server never creates `.hgp/` implicitly

**Files:**
- Modify: `src/hgp/server.py` (`_check_mode` ~L1323, new `_check_initialized` beside it)
- Test: `tests/test_opt_in.py`

**Interfaces:**
- Consumes: `find_project_root`, `find_storage_root` (`hgp.project`).
- Produces: `_check_initialized() -> dict[str, Any] | None`; `_check_mode()` returns its result first when not None.

Rules for `_check_initialized`, in order: `_ctx is not None` → None (already bound; keeps test injection working); `HGP_GLOBAL_MODE` set → None; root = `find_project_root(Path.cwd())`, or `Path.cwd()` on `ProjectRootError`; if `(find_storage_root(root) / ".hgp").is_dir()` → None; else the `HGP_NOT_INITIALIZED` error naming `find_storage_root(root)`. Do not cache a negative result.

- [ ] **Step 1: Write failing tests.** Fixture `unbound(monkeypatch, tmp_path)`: set `server_module._ctx = None`, `monkeypatch.chdir`, `monkeypatch.delenv("HGP_PROJECT_ROOT"/"HGP_GLOBAL_MODE")`, and on teardown close `server_module._ctx.db` if set and restore the original `_ctx`.

```python
def test_tool_refuses_when_not_initialized(unbound, tmp_path):
    repo = git_init(tmp_path / "r"); os.chdir(repo)
    r = hgp_create_operation(op_type="hypothesis", agent_id="a")
    assert r["error"] == "HGP_NOT_INITIALIZED" and "hgp init" in r["message"]
    assert not (repo / ".hgp").exists()
def test_non_git_dir_not_created(unbound, tmp_path):  # cwd without .git
    assert hgp_query_operations()["error"] == "HGP_NOT_INITIALIZED"; assert not (tmp_path / ".hgp").exists()
def test_existing_hgp_dir_stays_enabled(unbound, tmp_path): (repo/.hgp).mkdir() → "op_id" in hgp_create_operation(...)
def test_worktree_enabled_via_main_repo(unbound, linked): main .hgp only; chdir worktree → op created; worktree/.hgp absent
def test_enable_mid_session(unbound, tmp_path): first call NOT_INITIALIZED; (repo/.hgp).mkdir(); second call returns "op_id"
def test_global_mode_unaffected(unbound, tmp_path, monkeypatch): HOME=tmp/home, HGP_GLOBAL_MODE=1 → "op_id" returned, tmp/home/.hgp exists
```

- [ ] **Step 2:** Run → FAIL (op created / `.hgp` created).
- [ ] **Step 3:** Implement `_check_initialized` and call it first in `_check_mode`.
- [ ] **Step 4:** `uv run pytest -q` — new tests PASS; whole suite PASS (existing tests inject `_ctx`, so they bypass the gate; fix any that do not by creating `.hgp/` in their fixture, without changing assertions).
- [ ] **Step 5:** Commit `feat: refuse tool calls in repositories without .hgp (HGP_NOT_INITIALIZED)`.

### Task 3: `hgp init`

**Files:**
- Modify: `src/hgp/server.py` (new `_init(args: list[str]) -> None`, `_INIT_USAGE`, dispatch in `run()` and its docstring)
- Test: `tests/test_opt_in.py`

**Interfaces:**
- Produces: CLI `hgp init` (no arguments; any argument → usage on stderr, exit 1).

Behaviour: root = `find_project_root(cwd)` (honours `HGP_PROJECT_ROOT`); on `ProjectRootError` → stderr "hgp init: no git repository found …; set HGP_PROJECT_ROOT to enable HGP outside git", exit 1, create nothing. storage = `find_storage_root(root)`; create `storage/.hgp/` (`exist_ok`); print `HGP enabled: <storage>/.hgp` or `HGP already enabled: <storage>/.hgp`. Then, if `git -C <storage> check-ignore -q .hgp/` exits non-zero, append `.hgp/\n` to `<git common dir>/info/exclude` (`git rev-parse --git-common-dir`, resolved against storage; create `info/`), never duplicating an existing `.hgp/` line, and print that it did. If `git` is unavailable, skip the ignore step with a one-line warning. `hgp init` does not write `hook-policy` (absence already means advisory; the spec's `telemetry_enabled` key has no consumer).

- [ ] **Step 1: Write failing tests** (subprocess `python -m hgp.server init`, `HOME` isolated):

```python
def test_init_creates_dir_and_excludes(tmp_path): rc 0; (repo/".hgp").is_dir(); ".hgp/" in (repo/".git/info/exclude").read_text(); "??" not in git_status_porcelain(repo)
def test_init_idempotent(tmp_path): run twice; second stdout contains "already enabled"; exclude has exactly one ".hgp/" line
def test_init_respects_existing_gitignore(tmp_path): .gitignore contains ".hgp/" (committed) → exclude file unchanged
def test_init_from_worktree_enables_main(linked): run in worktree → repo/.hgp exists, worktree/.hgp absent
def test_init_outside_git_fails(tmp_path): rc 1, no .hgp created
def test_init_rejects_arguments(tmp_path): rc 1, "usage: hgp init" in stderr
```

- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement `_init` and dispatch `elif args and args[0] == "init": _init(args[1:])`.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit `feat: add hgp init to enable HGP for a repository`.

### Task 4: Hook preflight gate and fail-open

**Files:**
- Modify (all hook scripts): `src/hgp/hooks/claude/{pre_tool_use,pre_bash,post_bash,subagent_start,subagent_stop}_hgp.py`, `src/hgp/hooks/gemini/{pre_tool_use,pre_bash,post_bash,post_tool_use}_hgp.py`, `src/hgp/hooks/codex/{pre_tool_use,post_tool_use}_hgp.py`
- Test: `tests/test_opt_in.py`; existing hook tests (`tests/test_bash_hooks.py`, `tests/test_subagent_hooks.py`, `tests/test_hook_policy.py`, `tests/test_worktrees.py`)

**Interfaces:**
- Consumes: `hgp_paths.hgp_enabled` (Task 1).
- Produces, in every hook: `_hgp_paths() -> Any` (module or None), `_enabled(start: Path) -> bool`, `main()` = fail-open wrapper around `_main()`.

Decisions:
- Loader (replaces the file-path `_load_hgp_paths` in the 5 hooks that have it; added to the other 6): `try: import hgp_paths` (installed beside the hook — the script directory is `sys.path[0]`) → `except ImportError: try: from hgp.hooks import hgp_paths` → `except ImportError: return None`.
- `_enabled(start)`: `hgp_paths.hgp_enabled(start)` when available; fallback: walk `start` and parents, `True` at the first directory containing a `.hgp` directory, `False` at the first containing `.git`.
- `start` = event `"cwd"` when present and non-empty, else `Path.cwd()`. Gate runs first in `_main()`, before any output; disabled → `sys.exit(0)` silently.
- Fail-open: `def main() -> None: try: _main() except SystemExit: raise except Exception: sys.exit(0)`.
- Remove module-level `BLOCK_MODE = _resolve_block_mode()`; call `_resolve_block_mode()` inside `_main()` after the gate. Keep the `def _resolve_block_mode(` name (the stale-hook detector in `_hook_policy` greps for it).

- [ ] **Step 1: Write failing tests** (parametrize over all 11 scripts with a minimal event each):

```python
def test_hooks_silent_without_hgp(tmp_path, hook, event): repo without .hgp → rc 0, stdout "", stderr ""
def test_hooks_enabled_in_worktree(linked): main .hgp only; claude pre_tool_use with {"tool_name":"Write"} in worktree → warning on stderr
def test_hooks_fail_open(tmp_path, hook): enabled repo; stdin "not json{" → rc 0, no "Traceback" in stderr
def test_pre_tool_use_fail_open_on_unreadable_policy(tmp_path): .hgp/hook-policy is a directory → rc 0, no traceback
def test_hook_without_helper_or_package(tmp_path): copy claude pre_tool_use alone; run `python -S` (no site-packages, no helper) in enabled repo with legacy "block" policy → rc 2
```

- [ ] **Step 2:** Run → FAIL (warnings printed / tracebacks).
- [ ] **Step 3:** Apply the decisions above to all 11 scripts.
- [ ] **Step 4:** Run the whole suite. Existing hook tests that expect output now run in repos without `.hgp/`: enable HGP in their fixtures (`mkdir .hgp`) — do not change their assertions. Update `tests/test_worktrees.py::test_installed_hook_without_helper_degrades_safely` to run with `python -S` (package fallback otherwise finds `hgp_paths`). Then `uv run --extra dev pyright` and `uv run ruff check src tests` → clean.
- [ ] **Step 5:** Commit `feat: hooks exit silently where HGP is not enabled and fail open on errors`.

### Task 5: Agent instructions and docs

**Files:**
- Modify: `src/hgp/server.py` (`_HGP_INSTRUCTIONS_BLOCK`), `README.md` (Storage section, CLI list), `docs/tools-reference.md` (Error Code Reference)
- Test: `tests/test_install.py`

- [ ] **Step 1: Write failing test:** `test_instructions_mention_not_initialized` — `_HGP_INSTRUCTIONS_BLOCK` contains `HGP_NOT_INITIALIZED` and `hgp init`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Add to the instructions block, verbatim: "If an HGP tool returns `HGP_NOT_INITIALIZED`, HGP is not enabled for this repository: skip HGP for the session, use native tools, and do not run `hgp init` unless the user asks." Docs: README — HGP is opt-in per repository (`hgp init`; existing `.hgp/` keeps a repo enabled; delete `.hgp/` or `hgp mode off` to stop); replace "Graph tools … fall back to `<cwd>/.hgp/` with a warning" with the new rule (outside git: `HGP_PROJECT_ROOT` + `hgp init`, or `HGP_GLOBAL_MODE`); hooks do nothing in repositories without `.hgp/`. tools-reference: add `HGP_NOT_INITIALIZED` row.
- [ ] **Step 4:** `uv run pytest -q`, pyright, ruff → all clean.
- [ ] **Step 5:** Commit `docs: document opt-in HGP and HGP_NOT_INITIALIZED`.
