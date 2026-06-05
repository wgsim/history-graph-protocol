# Implementation Plan - Post-Audit Concurrency & Security Hardening

This document outlines the detailed action plan to address the concurrency and security gaps identified during the multi-model (Claude, Gemini, Codex) consolidated audit of the `feat/worktree-and-isolation` branch.

## 1. Goal Description

Following the comprehensive audit of the Git Worktree and dynamic project isolation mechanisms, several critical (P0/P1) and minor (P2/P3) issues were identified. The primary objectives of this plan are:
1. Ensure SQLite transactional safety and prevent database lock errors under concurrent multi-agent executions.
2. Guarantee that lifecycle hooks work reliably inside Git Worktrees by dynamically resolving storage roots.
3. Harden HGP file mutating tools against TOCTOU (Time-of-Check to Time-of-Use) directory swap attacks.
4. Ensure atomicity of active leases in the database.

---

## 2. Proposed Changes & Action Items

### P0: SQLite Concurrency Model Refactoring
- **Target File**: `src/hgp/db.py` & `src/hgp/server.py`
- **Action**:
  - Decouple the single global connection cached in the `Database` class.
  - Implement a **Connection-per-Request** (or thread-local connection) pattern where each database access session or server request opens its own SQLite handle and safely closes it upon transaction completion.
  - Enforce atomic serialization by wrapping all database write paths inside explicit SQLite `BEGIN IMMEDIATE` blocks.
  - Utilize SQLite's native queueing mechanisms by standardizing on `PRAGMA busy_timeout = 2000` (2 seconds).

### P1: Git Worktree Hook Integration
- **Target Files**: All `pre_*_hgp.py` lifecycle hook templates under `src/hgp/hooks/`
- **Action**:
  - Refactor the `_open_hgp_dir()` preflight helper to no longer rely purely on physical parent directory traversal.
  - When `.git` is detected as a file (indicating a Git Worktree), parse the `gitdir:` and `commondir` declarations.
  - Resolve paths canonically to locate the main repository's `.hgp/` folder to check for the active HGP project state and load `hook-policy`.
  - Maintain absolute zero-dependency and execution overhead constraints (execution budget < 15ms).

### P1: File APIs TOCTOU Hardening
- **Target File**: `src/hgp/server.py`
- **Action**:
  - Refactor mutating endpoints (`hgp_write_file`, `hgp_append_file`, `hgp_edit_file`, `hgp_move_file`, and `hgp_delete_file`) to use the descriptor-pinned relative architecture already implemented in CAS.
  - Open the validated project root directory once to lock down a file descriptor (`root_fd`).
  - Perform all subsequent file interactions using `dir_fd=root_fd` and `os.O_NOFOLLOW` (utilizing low-level POSIX functions) to prevent directory-swap symlink races during tool executions.

### P1: Atomic Lease Acquisition
- **Target File**: `src/hgp/lease.py`
- **Action**:
  - Wrap check-and-insert logic inside `LeaseManager.acquire()` in an immediate SQL transaction block.
  - Add a database-level partial unique index constraint: `UNIQUE(agent_id, subgraph_root_op_id) WHERE status = 'ACTIVE'` to guarantee absolute mutual exclusion on the schema level.

### P2: Minor Hardening & Standardization
- **DAG Chain Hash**: Fail and raise a clear exception if recursive traversals exceed 500 depth.
- **Hook Policy Format**: Standardize hook-policy format on JSON for both initialization (`hgp init`) and hook checks.
- **Windows Portability**: Implement fallback file lock validation paths on Windows where POSIX directory descriptors are unavailable.

---

## 3. Verification & Testing Plan

### Automated Tests
- Introduce a mock concurrent test suite in `tests/test_concurrency.py` that spawns multiple parallel requests writing to the shared database.
- Create tests for worktree hook resolution with simulated worktree pointers.
- Verify 100% test pass rate using `pytest`:
  ```bash
  uv run pytest tests/
  ```

### Manual Verification
- Execute parallel CLI write commands in two separate git worktrees concurrently and assert that transaction requests queue cleanly without raising database lock errors.
- Verify lifecycle hooks log telemetry and read policies properly inside a newly spawned git worktree.
