# V4 File Tracking Remediation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make V4 file tracking merge-safe by eliminating filesystem/HGP divergence, normalizing file identity, and restoring a documented/stable MCP contract.

**Architecture:** Treat file operations as a two-phase flow: canonicalize and fully validate input first, then perform the filesystem mutation and HGP write under one explicit failure-handling path with deterministic rollback. File identity should be the resolved canonical path inside the project root, and move semantics should not rely on optional caller memory for old-path invalidation.

**Tech Stack:** Python 3.12, FastMCP, SQLite, pytest

---

### Task 1: Lock in the failure cases with tests

**Files:**
- Modify: `tests/test_file_ops.py`
- Modify: `tests/test_db.py`

**Step 1: Add failing rollback tests for write/append/edit/delete**

Cover these cases explicitly:
- invalid `evidence_refs` does not leave the file written
- missing `parent_op_ids` does not leave the file written
- invalid `previous_op_id` does not delete the file

**Step 2: Add failing move tests**

Cover these cases explicitly:
- invalid `evidence_refs` does not leave the file renamed
- CAS/DB failure after rename restores the original path
- omitting `previous_op_id` still yields consistent old-path history

**Step 3: Add a real pre-V4 migration test**

Create a DB with an `operations` table that lacks `file_path`, then initialize `Database` and assert:
- column exists after initialization
- both new indexes exist
- existing rows remain queryable

**Step 4: Run focused tests**

Run:
```bash
pytest tests/test_file_ops.py tests/test_db.py -q
```

Expected:
- New rollback/migration tests fail on the current branch state before implementation

---

### Task 2: Canonicalize file identity at the API boundary

**Files:**
- Modify: `src/hgp/project.py`
- Modify: `src/hgp/server.py`
- Modify: `src/hgp/db.py`
- Modify: `tests/test_project.py`
- Modify: `tests/test_file_ops.py`

**Step 1: Introduce a canonical path helper**

Add a helper that:
- resolves the project root once
- resolves the target path against that root
- returns a canonical absolute string for storage/query use
- rejects non-root paths with the existing structured error types

**Step 2: Store only canonical paths**

Update all V4 tool writes so `file_path` / `old_path` / `new_path` are normalized before:
- filesystem I/O
- DB inserts
- `file_path` history queries

**Step 3: Make query semantics match storage semantics**

Ensure:
- `hgp_file_history(file_path=...)` canonicalizes its input before querying
- `hgp_query_operations(file_path=...)` canonicalizes its input before filtering

**Step 4: Add coverage for aliasing cases**

Add tests proving the same file is treated as one history across:
- absolute path
- path with `.` / `..`
- symlink target path if supported by the test environment

**Step 5: Run focused tests**

Run:
```bash
pytest tests/test_project.py tests/test_file_ops.py -q
```

Expected:
- Canonical and alias paths collapse to one history

---

### Task 3: Make file mutation and HGP recording failure-atomic

**Files:**
- Modify: `src/hgp/server.py`
- Modify: `src/hgp/project.py`
- Modify: `tests/test_file_ops.py`

**Step 1: Split preflight validation from side effects**

Before touching the filesystem:
- canonicalize the path(s)
- validate `evidence_refs`
- validate or resolve parent/invalidation references
- prepare reason/metadata

**Step 2: Rework write/append/edit flow**

Target behavior:
- no permanent file mutation occurs until the HGP insert path is known-valid
- if CAS/DB work fails, the original on-disk content is restored or no file is created

**Step 3: Rework delete flow**

Target behavior:
- deletion is reversible until the DB write succeeds
- bad `previous_op_id` returns a structured error and preserves the file

**Step 4: Rework move flow**

Target behavior:
- rename happens only after preflight passes
- CAS/DB failure restores the original path
- invalid evidence/duplicate evidence never leaves the file moved

**Step 5: Standardize structured error handling**

Ensure V4 file tools return error dicts instead of raw sqlite/value exceptions for:
- bad parent references
- bad previous references
- duplicate/nonexistent evidence refs

**Step 6: Run focused tests**

Run:
```bash
pytest tests/test_file_ops.py -q
```

Expected:
- All rollback and structured-error tests pass

---

### Task 4: Fix move semantics for old-path history

**Files:**
- Modify: `src/hgp/server.py`
- Modify: `src/hgp/db.py`
- Modify: `docs/tools-reference.md`
- Modify: `tests/test_file_ops.py`

**Step 1: Decide the source of truth for old-path invalidation**

Preferred direction:
- resolve the latest tracked op for `old_path` inside the tool instead of trusting an optional caller-supplied `previous_op_id`

**Step 2: Make old-path history explicit**

Choose one of these and document it clearly:
1. record an explicit invalidation op for `old_path`, then create the new artifact op for `new_path`
2. keep the current graph shape, but still guarantee that old-path history reflects the move even when the caller omits `previous_op_id`

**Step 3: Add the missing tests**

Cover:
- move without `previous_op_id`
- `hgp_file_history(old_path)` after move
- `hgp_file_history(new_path)` after move
- double-move chains

**Step 4: Run focused tests**

Run:
```bash
pytest tests/test_file_ops.py -q
```

Expected:
- Old-path history is never left logically active after a successful move

---

### Task 5: Restore and document the MCP contract

**Files:**
- Modify: `src/hgp/server.py`
- Modify: `docs/tools-reference.md`
- Modify: `README.md`
- Modify: `tests/test_server_tools.py`

**Step 1: Decide the `hgp_query_operations` contract**

Pick one:
1. restore the old bare-list response for compatibility
2. keep the wrapper dict and explicitly version/document the breaking change

**Step 2: Update docs to match reality**

Document:
- actual response shape
- new `file_path` filter
- true atomicity guarantees after Task 3
- actual move semantics after Task 4

**Step 3: Add compatibility tests**

Add tests that lock the chosen schema so future changes cannot silently drift again.

**Step 4: Run focused tests**

Run:
```bash
pytest tests/test_server_tools.py tests/test_file_ops.py -q
```

Expected:
- Tool responses and docs agree

---

### Task 6: Final verification before merge

**Files:**
- No new product code expected beyond prior tasks

**Step 1: Run the targeted suite**

Run:
```bash
pytest tests/test_project.py tests/test_db.py tests/test_file_ops.py tests/test_server_tools.py -q
```

Expected:
- All targeted V4 tests pass

**Step 2: Run static checks**

Run:
```bash
ruff check src tests
pyright
```

Expected:
- No new lint/type regressions attributable to the V4 remediation

**Step 3: Re-read the user-facing docs**

Confirm these statements are all true after implementation:
- file ops are atomic enough for the documented claim
- `hgp_query_operations` response shape is accurately documented
- `file_path` semantics are canonical and reproducible
- move/delete behavior is accurately described

**Step 4: Merge gate**

Do not merge until:
- rollback tests exist and pass
- move-without-`previous_op_id` is defined and tested
- docs and implementation agree on every changed MCP tool contract
