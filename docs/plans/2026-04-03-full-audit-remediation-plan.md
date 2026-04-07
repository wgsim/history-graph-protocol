# Full Audit Remediation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

Created at: 2026-04-03
HEAD at plan creation: `9d9fe96`
Input reviews:
- `docs/reviews/2026-04-03-audit-security.md`
- `docs/reviews/2026-04-03-audit-architecture.md`
- `docs/reviews/2026-04-03-audit-silent-failures.md`
- `docs/reviews/2026-04-03-audit-code-quality.md`
- `docs/reviews/2026-04-03-audit-test-coverage.md`

**Goal:** Close all findings from the five-domain parallel audit, ordered from core-functionality breaks through runtime reliability, contract consistency, architectural debt, test coverage gaps, and long-term structural improvements.

**Architecture:** Keep phases separate. P1 fixes must never touch architecture — they are narrow correctness patches. P2–P3 add safety nets without refactoring. P4–P6 are tracked here for completeness but are separate initiatives.

**Tech Stack:** Python 3.12, FastMCP, SQLite, pytest, Ruff

---

## Phase 1 — Core Functionality Breaks (5 tasks)

These findings produce **incorrect history** or break HGP's primary guarantees. Fix before anything else.

---

### Task 1.1: Fix `_file_matches_hash` — separate EACCES from ENOENT

**Files:**
- Modify: `src/hgp/reconciler.py`
- Modify: `tests/test_reconciler.py`

**Problem:**
`except OSError: return False` conflates permission errors with missing-file errors.
A successfully written file becomes STALE_PENDING if a permission error occurs at reconcile time.

**Step 1:** Split the exception handling:

```python
def _file_matches_hash(file_path: str, expected_hash: str) -> bool:
    try:
        data = Path(file_path).read_bytes()
        computed = f"sha256:{hashlib.sha256(data).hexdigest()}"
        return computed == expected_hash
    except FileNotFoundError:
        return False
    except OSError as exc:
        _log.warning("_file_matches_hash: unexpected OSError for %r: %s", file_path, exc)
        return False
```

**Step 2:** Add test — simulate EACCES scenario:
- Insert stale PENDING artifact whose file has `chmod 000`
- Assert reconciler does NOT mark it STALE_PENDING
- Clean up with `chmod 644` in teardown

**Verify:**
```bash
uv run pytest tests/test_reconciler.py -q
```

---

### Task 1.2: Fix symlink handling in `hgp_delete_file` and `hgp_move_file`

**Files:**
- Modify: `src/hgp/server.py`
- Modify: `tests/test_file_tools.py`

**Problem:**
`path.unlink()` deletes the symlink but `canonical_file_path()` resolved to the symlink target.
DB records "target file deleted" but the real file still exists → history is wrong.

**Step 1:** In `hgp_delete_file`, detect symlinks before the operation:

```python
canonical = canonical_file_path(file_path, root)
path = Path(canonical)
if path.is_symlink():
    return {"error": "SYMLINK_NOT_SUPPORTED",
            "message": f"file_path resolves to a symlink; HGP does not track symlinks directly"}
```

Apply the same guard at the top of `hgp_move_file` for both `old_path` and `new_path`.

**Step 2:** Add regression tests:
- `test_delete_file_rejects_symlink` — create symlink inside project root, assert `SYMLINK_NOT_SUPPORTED`
- `test_move_file_rejects_symlink_src` — same for move source

**Verify:**
```bash
uv run pytest tests/test_file_tools.py -q
```

---

### Task 1.3: Fix `hgp_get_evidence` and `hgp_get_citing_ops` return type

**Files:**
- Modify: `src/hgp/server.py`
- Modify: `tests/test_server_tools.py`

**Problem:**
Success returns a bare `list`, error returns `{"error": ...}`.
Callers using `if "error" in result` treat success as no-error silently.
Callers using list operations on error responses crash.

**Step 1:** Wrap both tool responses in a consistent envelope:

```python
# hgp_get_evidence
return {"op_id": op_id, "evidence": records}   # was: return records

# hgp_get_citing_ops
return {"op_id": op_id, "citing_ops": records}  # was: return records
```

**Step 2:** Update existing tests that assert on the raw list return.

**Step 3:** Add tests for error path — non-existent `op_id` returns `{"error": ...}`, not exception.

**Verify:**
```bash
uv run pytest tests/test_server_tools.py -q
```

---

### Task 1.4: Fix `compute_chain_hash` edge query depth limit

**Files:**
- Modify: `src/hgp/dag.py`
- Modify: `tests/test_db.py` or `tests/test_server_tools.py`

**Problem:**
`_EDGES_IN_SUBGRAPH_SQL` (used inside `compute_chain_hash`) has no `LIMIT` or depth cap.
On a deep or cyclic DAG this defeats `MAX_CHAIN_HASH_DEPTH` and can hang or produce wrong hashes.

**Step 1:** Inspect `dag.py` — identify exactly where `_EDGES_IN_SUBGRAPH_SQL` is used in `compute_chain_hash`.

**Step 2:** Apply depth cap consistent with `MAX_CHAIN_HASH_DEPTH = 500`:
- Either add `LIMIT` clause to the SQL, or
- Pass `max_depth=MAX_CHAIN_HASH_DEPTH` to the underlying CTE query

**Step 3:** Add test — chain of 600 ops, assert `compute_chain_hash` returns in finite time without error.

**Verify:**
```bash
uv run pytest -q -k "chain_hash"
```

---

### Task 1.5: Fix `hgp_anchor_git` — validate op_id existence, wrap DB call

**Files:**
- Modify: `src/hgp/server.py`
- Modify: `tests/test_server_tools.py`

**Problem:**
No existence check for `op_id`; `sqlite3.IntegrityError` leaks as raw exception.
Callers checking `if "error" in result` silently treat it as success.

**Step 1:**
```python
if not db.get_operation(op_id):
    return {"error": "OP_NOT_FOUND", "message": f"op_id not found: {op_id!r}"}
try:
    db.execute("INSERT OR IGNORE INTO git_anchors ...", (...))
    db.commit()
except sqlite3.Error as exc:
    _log.error("hgp_anchor_git DB error op_id=%r sha=%r: %s", op_id, git_commit_sha, exc)
    return {"error": "DB_ERROR", "message": "Failed to anchor git commit"}
```

**Step 2:** Add test — `hgp_anchor_git` with non-existent op_id returns `{"error": "OP_NOT_FOUND"}`, no exception.

**Verify:**
```bash
uv run pytest tests/test_server_tools.py -q
```

---

## Phase 2 — Runtime Reliability / Silent Failures (8 tasks)

These don't break history correctness directly, but leave operators blind to failures and make diagnosis impossible.

---

### Task 2.1: Add logging to FILESYSTEM_ERROR paths in write/append/edit

**Files:**
- Modify: `src/hgp/server.py`

**Problem:**
`hgp_write_file`, `hgp_append_file`, `hgp_edit_file` return `{"error": "FILESYSTEM_ERROR"}` with no server-side log.
Operators cannot see that a PENDING op was stranded.

**Step 1:** In each `except OSError` block:
```python
except OSError as exc:
    _log.warning(
        "%s filesystem write failed for op_id=%s path=%r; "
        "PENDING op will be triaged by reconciler: %s",
        "hgp_write_file", result["op_id"], file_path, exc,
    )
    return {"error": "FILESYSTEM_ERROR", "message": str(exc), "op_id": result["op_id"]}
```

Apply to all three tools.

**Verify:**
```bash
uv run pytest tests/test_file_tools.py -q
```

---

### Task 2.2: Log rollback failures in `_record_file_op`, `hgp_delete_file`, `hgp_move_file`

**Files:**
- Modify: `src/hgp/server.py`

**Problem:**
Three locations have `except Exception: pass` around `db.rollback()` — completely silent on rollback failure.
`hgp_delete_file` and `hgp_move_file` are especially dangerous because the file is already gone when rollback fails.

**Step 1:** Replace all three `except Exception: pass` rollback silencers with:
```python
except Exception as rb_exc:
    _log.error(
        "<function>: ROLLBACK failed after <context> op_id=%s: %s",
        op_id, rb_exc,
    )
```

**Step 2:** Confirm `hgp_delete_file` and `hgp_move_file` already log at `_log.error` for the outer failure — verify consistency.

**Verify:**
```bash
uv run pytest tests/test_file_tools.py -q
```

---

### Task 2.3: Handle `cas.store()` exceptions in `hgp_move_file` and `_record_file_op`

**Files:**
- Modify: `src/hgp/server.py`

**Problem:**
`cas.store()` raises `PayloadTooLargeError` and `BlobWriteError` without being caught.
These propagate as raw exceptions — callers checking `if "error" in result` silently continue.

**Step 1:** In `_record_file_op` and `hgp_move_file`, wrap `cas.store()`:
```python
try:
    object_hash = cas.store(raw)
except (PayloadTooLargeError, BlobWriteError) as exc:
    return {"error": exc.__class__.__name__.upper().replace("ERROR", "_ERROR"),
            "message": str(exc)}
```

**Verify:**
```bash
uv run pytest tests/test_file_tools.py tests/test_server_tools.py -q
```

---

### Task 2.4: Record unparseable `created_at` in reconciler `report.errors`

**Files:**
- Modify: `src/hgp/reconciler.py`
- Modify: `tests/test_reconciler.py`

**Problem:**
PENDING op with malformed `created_at` is silently skipped forever — no operator signal.

**Step 1:**
```python
except (ValueError, AttributeError) as exc:
    report.errors.append(
        f"PENDING op {op.get('op_id')!r} has unparseable created_at={created_at_str!r}: {exc}"
    )
    continue
```

**Step 2:** Add test — insert PENDING op with `created_at = "not-a-date"`, assert `report.errors` contains the op_id.

**Verify:**
```bash
uv run pytest tests/test_reconciler.py -q
```

---

### Task 2.5: Fix `cas.py:list_all_blobs_with_mtime` — handle concurrent deletion race

**Files:**
- Modify: `src/hgp/cas.py`
- Modify: `tests/test_cas.py`

**Problem:**
Between `is_file()` and `stat()`, a concurrent rename/delete raises `FileNotFoundError`.
This aborts the entire reconciler and can abort server startup.

**Step 1:**
```python
try:
    mtime = datetime.fromtimestamp(blob_file.stat().st_mtime, tz=timezone.utc)
except FileNotFoundError:
    continue  # raced with concurrent deletion/rename — expected
```

**Step 2:** Add test — mock `stat()` to raise `FileNotFoundError` on first call, assert `list_all_blobs_with_mtime` continues without raising.

**Verify:**
```bash
uv run pytest tests/test_cas.py tests/test_reconciler.py -q
```

---

### Task 2.6: Narrow `OperationalError` silent-pass in `get_evidence` / `get_citing_ops`

**Files:**
- Modify: `src/hgp/db.py`

**Problem:**
`except sqlite3.OperationalError: pass` catches DB corruption errors silently.
`sqlite3.OperationalError` includes `SQLITE_CORRUPT`, `SQLITE_NOTADB` — not just lock contention.

**Step 1:** Narrow the catch to lock-contention only:
```python
except sqlite3.OperationalError as exc:
    if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
        _log.warning("record_access OperationalError (non-lock): %s", exc)
    # still pass — access recording is best-effort
```

**Verify:**
```bash
uv run pytest -q
```

---

### Task 2.7: Add structured warning to `hgp_query_operations` canonicalization fallback

**Files:**
- Modify: `src/hgp/server.py`

**Problem:**
When `canonical_file_path` fails, the tool silently falls back to raw path and returns `{"operations": []}`.
Agent sees empty results, indistinguishable from "no history" — silently degrades.

**Step 1:**
```python
except (ProjectRootError, PathOutsideRootError) as exc:
    _log.debug("hgp_query_operations: file_path canonicalization failed, using raw path: %s", exc)
    canonical_fp = file_path  # fallback
    # Add warning to response
```

Or optionally return:
```python
return {"operations": [], "warning": "file_path could not be canonicalized; results may be incomplete"}
```

**Verify:**
```bash
uv run pytest tests/test_server_tools.py -q
```

---

### Task 2.8: Wrap post-acquire memory tier update in `hgp_acquire_lease`

**Files:**
- Modify: `src/hgp/server.py`

**Problem:**
If `db.set_memory_tier()` raises after `lease_mgr.acquire()` commits, the caller gets a raw exception but the lease exists in DB (stranded ACTIVE lease).

**Step 1:**
```python
try:
    db.set_memory_tier(subgraph_root_op_id, "short_term")
    db.commit()
except sqlite3.Error as exc:
    _log.error("hgp_acquire_lease: memory tier update failed for lease %s: %s",
               lease["lease_id"], exc)
    # Lease is committed — return it with a warning rather than crashing
    lease["warning"] = "memory tier could not be updated to short_term"
```

**Verify:**
```bash
uv run pytest tests/test_server_tools.py -q
```

---

## Phase 3 — Input Validation / Contract Consistency (4 tasks)

---

### Task 3.1: Add `validate=True` to `base64.b64decode`

**Files:**
- Modify: `src/hgp/server.py`
- Modify: `tests/test_server_tools.py`

**Problem:**
`base64.b64decode(payload)` silently strips non-base64 characters → CAS stores corrupted content.

**Step 1:**
```python
try:
    raw = base64.b64decode(payload, validate=True)
except Exception:
    return {"error": "INVALID_PAYLOAD", "message": "payload is not valid base64"}
```

Apply in both `hgp_create_operation` and `_record_file_op`.

**Step 2:** Add test — pass payload with embedded whitespace/garbage, assert `INVALID_PAYLOAD` error.

**Verify:**
```bash
uv run pytest tests/test_server_tools.py tests/test_file_tools.py -q
```

---

### Task 3.2: Clamp `limit` and `max_depth` parameters

**Files:**
- Modify: `src/hgp/server.py`
- Modify: `tests/test_server_tools.py`

**Problem:**
`limit=999999999` in `hgp_query_operations` / `hgp_file_history` causes memory exhaustion.
`max_depth` in `hgp_query_subgraph` has no upper bound.

**Step 1:**
```python
_MAX_QUERY_LIMIT = 1000
_MAX_SUBGRAPH_DEPTH = 500

limit = min(limit, _MAX_QUERY_LIMIT)         # hgp_query_operations, hgp_file_history
max_depth = min(max_depth, _MAX_SUBGRAPH_DEPTH)  # hgp_query_subgraph
```

**Step 2:** Add tests asserting clamped behavior.

**Verify:**
```bash
uv run pytest tests/test_server_tools.py -q
```

---

### Task 3.3: Unify error signaling — standardize `hgp_create_operation` to return error dicts

**Files:**
- Modify: `src/hgp/server.py`
- Modify: `tests/test_server_tools.py`

**Problem:**
`hgp_create_operation` raises `ParentNotFoundError` and `InvalidationTargetNotFoundError` for some failures
but returns `{"error": ...}` for others.
MCP callers must handle two patterns from the same tool.

**Step 1:** Catch domain errors at the tool handler boundary and convert to dict:
```python
try:
    ...existing body...
except ParentNotFoundError as exc:
    return {"error": "PARENT_NOT_FOUND", "message": str(exc)}
except InvalidationTargetNotFoundError as exc:
    return {"error": "INVALIDATION_TARGET_NOT_FOUND", "message": str(exc)}
except ChainStaleError as exc:
    return {"error": "CHAIN_STALE", "message": str(exc)}
```

**Step 2:** Update tests that currently use `pytest.raises(...)` to assert on returned dict instead.

**Step 3:** Update `docs/tools-reference.md` — change "Raises X" wording to "Returns `{"error": "X"}`".

**Verify:**
```bash
uv run pytest tests/test_server_tools.py -q
```

---

### Task 3.4: Document `SYMLINK_NOT_SUPPORTED` in tools-reference

**Files:**
- Modify: `docs/tools-reference.md`

**Step 1:** Add `SYMLINK_NOT_SUPPORTED` to the error sections for `hgp_delete_file` and `hgp_move_file`, and to the consolidated error code table.

**Verify:** Manual doc review.

---

## Phase 4 — Architecture Debt (2 tasks, separate initiative)

These require broader refactoring. Track here but do not mix with Phase 1–3.

---

### Task 4.1: Centralize raw SQL from `server.py` into `db.py`

**Files:**
- Modify: `src/hgp/server.py`
- Modify: `src/hgp/db.py`

**Problem:**
10+ raw `db.execute()` calls with inline SQL in `server.py` violate the documented "all SQL in db.py" invariant.

**Affected SQL sites:**
- `UPDATE operations SET chain_hash = ?` (line ~177)
- Lease status queries (lines ~183-197, ~387-393)
- `UPDATE operations SET memory_tier` (line ~404)
- `INSERT INTO git_anchors` (line ~435)
- Various file-tool queries

**Step 1:** For each raw SQL call in `server.py`, create a named method on `Database`:
e.g., `db.update_chain_hash(op_id, hash)`, `db.get_active_lease_root(lease_id)`, `db.insert_git_anchor(op_id, sha, repo)`, `db.update_memory_tier(op_id, tier)`.

**Step 2:** Replace inline SQL with method calls.

**Step 3:** Apply same treatment to `dag.py` raw SQL calls.

**Verify:**
```bash
uv run pytest -q
```

---

### Task 4.2: Resolve `~/.hgp/` vs per-project storage discrepancy

**Files:**
- Modify: `src/hgp/server.py`
- Modify: `.gitignore`
- Modify: `docs/architecture.md`

**Decision:** Option A — move DB and CAS to `<repo_root>/.hgp/` (repo-local, gitignored).
Phase 1 minimal transition: single DB per project, no worktree-level partitioning.
Phase 2 (worktree_id column isolation) deferred until real concurrency signals appear.
See `docs/plans/2026-04-07-task42-repo-local-storage-plan.md` for full implementation steps.
See `docs/plans/2026-04-07-hgp-phase2-worktree-isolation-options.md` for Phase 2 design.

**Step 1:** In `server.py`, replace hardcoded `HGP_DIR = Path.home() / ".hgp"` block with
`_resolve_hgp_dir()` using `find_project_root(Path.cwd())`. Add `HGP_GLOBAL_MODE=1` fallback.

**Step 2:** Update `.gitignore` — replace `# *.db` comment block with `.hgp/`.

**Step 3:** Rewrite `docs/architecture.md` "Git + HGP Consistency" section — remove git-tracked
DB claims, document repo-local gitignored store and `HGP_GLOBAL_MODE` fallback.

**Verify:**
```bash
uv run --extra dev pytest -q
grep -r 'Path.home().*\.hgp' src/hgp/ --include='*.py'
```

---

## Phase 5 — Test Coverage Gaps (6 tasks)

---

### Task 5.1: Test `LeaseManager.acquire` prior-lease auto-release

**Files:**
- Modify: `tests/test_server_tools.py`

Add test: acquire lease for agent+subgraph twice — verify first lease is RELEASED, only one ACTIVE lease exists.

---

### Task 5.2: Test `_get_components()` failed init globals cleanup

**Files:**
- Modify: `tests/test_server_tools.py`

Add test: monkeypatch `db.initialize()` to raise, call any tool, assert globals remain `None` and second call retries init.

---

### Task 5.3: Test `hgp_file_history` outside-root path contract

**Files:**
- Modify: `tests/test_server_tools.py`

Add test: call `hgp_file_history` with a path outside project root — assert returns `{"operations": []}` (not raise), and optionally includes `"warning"` key if Task 2.7 is implemented.

---

### Task 5.4: Test `canonical_file_path` symlink-outside-root protection

**Files:**
- Modify: `tests/test_file_tools.py` or `tests/test_server_tools.py`

Add test: create symlink inside project root pointing outside, call `hgp_write_file` — assert `PATH_OUTSIDE_ROOT` error, not write.

---

### Task 5.5: Test `lease.validate(extend=True)` actually advances `expires_at`

**Files:**
- Modify: `tests/test_server_tools.py`

Add test: acquire lease, record `expires_at`, validate with `extend=True`, assert new `expires_at > original`.

---

### Task 5.6: Fix `test_pre_bash_writes_marker_on_mutating_command`

**Files:**
- Modify: `tests/test_bash_hooks.py`

Current test: does `unlink(missing_ok=True)` without first asserting the marker **exists**.

Fix: assert marker exists before cleanup.

---

## Phase 6 — Long-Term Structural (1 task, future initiative)

---

### Task 6.1: Replace global singletons with `HGPContext`

**Files:**
- Modify: `src/hgp/server.py`
- Potentially: all tool handlers

**Problem:**
`_db`, `_cas`, `_lease_mgr`, `_reconciler` are module-level mutable globals.
Prevents multi-project use in same process, forces monkeypatching in tests, no retry on init failure.

**Step 1:** Define `HGPContext` dataclass holding the four components.

**Step 2:** Convert `_get_components()` to return an `HGPContext` instance.

**Step 3:** Thread context through FastMCP dependency injection or `contextvars.ContextVar`.

**Step 4:** Update all tool handlers to receive context explicitly.

**Note:** This is a large refactor. Do in a dedicated branch with full test suite coverage at every step.

---

## Verification Gates

After Phase 1:
```bash
uv run pytest -q   # all tests pass
```

After Phase 2–3:
```bash
uv run pytest -q
uv run ruff check src/hgp --select F8,W
```

After Phase 4–5:
```bash
uv run pytest -q
uv run ruff check src tests --select F8,W
```

---

## Closure Criteria

Do not call the codebase fully hardened until:
- Phase 1 all 5 tasks complete and tested
- Phase 2 HIGH items (2.1–2.3) complete
- Phase 3.1 (`b64decode validate=True`) complete
- Phase 5.6 (marker test fix) complete

Phase 4, 5 (except 5.6), and 6 may be tracked as separate initiatives.
