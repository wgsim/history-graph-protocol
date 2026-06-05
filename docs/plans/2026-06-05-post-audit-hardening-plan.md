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
  - Preserve the existing `PRAGMA busy_timeout = 5000` (5 seconds) as the default native timeout to prevent early lock failures, and specify benchmark-driven criteria for any future 2000ms threshold tuning.

### P1: Git Worktree Hook Integration & Storage Alignment
- **Target Files**: All `pre_*_hgp.py` lifecycle hook templates under `src/hgp/hooks/`
- **Action**:
  - Define the canonical storage topology for Git Worktrees: Both server (`src/hgp/project.py`) and hooks must resolve storage to the shared main repository's `.hgp/` directory (`storage_root`), while limiting file mutation boundaries to the active checkout directory (`working_tree_root`).
  - Modify the server's `find_project_root()` and path resolver alongside hooks to ensure they follow this exact shared topology, preventing split-brain advisory/block decisions.
  - Instead of looking up a physical `.hgp` parent directory, hooks must dynamically parse `.git` pointer files if the workspace is inside a Git Worktree.
  - Introduce a lightweight, zero-dependency helper to extract `gitdir` and `commondir` declarations, locating the shared `.hgp/` folder in the main repository root where `hook-policy` resides.
  - Target the existing parent traversal loop inside `_resolve_block_mode()` and hook body routines for this consolidation.
  - Maintain absolute zero-dependency constraints and ensure hook execution overhead remains strictly under 15ms.

### P1: File APIs TOCTOU Hardening
- **Target File**: `src/hgp/server.py`
- **Action**:
  - Refactor mutating endpoints (`hgp_write_file`, `hgp_append_file`, `hgp_edit_file`, `hgp_move_file`, and `hgp_delete_file`) to use the descriptor-pinned relative architecture already implemented in CAS.
  - Open the validated project root directory once to lock down a file descriptor (`root_fd`).
  - Perform all subsequent file interactions using `dir_fd=root_fd` and `os.O_NOFOLLOW` (utilizing low-level POSIX functions) to prevent directory-swap symlink races during tool executions.

### P1: Atomic Lease Acquisition & DB Migration
- **Target File**: `src/hgp/lease.py`
- **Action**:
  - Wrap lease check-and-insert checks inside `LeaseManager.acquire()` in an immediate SQLite transaction block.
  - Implement an explicit DB schema migration path to safely backfill existing SQLite database instances with the new index constraint.
  - Define the partial unique constraint explicitly as `UNIQUE(subgraph_root_op_id) WHERE status = 'ACTIVE'`. This prevents duplicate active leases on the same subgraph root. If cross-agent concurrency is desired, document that same-agent reacquire races are mitigated while other agents are excluded, ensuring strict database-level safety.

### P2: Minor Hardening & Hook policy Compatibility
- **DAG Chain Hash**: Fail and raise a clear exception if recursive DAG traversal depth exceeds 500 nodes.
- **Hook Policy Format**: Standardize hook-policy files on a JSON schema (`{ "block_mode": false, "telemetry_enabled": true }`). Replace references to the non-existent `hgp init` or `hgp setup` commands with actual onboarding hooks and interface commands (`hgp install` and `hgp hook-policy`).
- **Fallback Compatibility**: Maintain strict backward compatibility by resolving legacy plain-text `hook-policy` files (falling back to plain text parsing) to prevent regressions on older repositories.
- **Windows Portability**: Implement fallback file lock validation paths on Windows where POSIX directory descriptors are unavailable.

---

## 3. Verification & Testing Plan

### Automated Tests
- Introduce a mock concurrent test suite in `tests/test_concurrency.py` that spawns multiple parallel requests writing to the shared database.
- Create tests for worktree hook resolution with simulated worktree pointers.
- **Migration & Backward Compatibility Tests**:
  - Add integration tests in `tests/test_migration.py` verifying that older databases migrate to the new `UNIQUE` lease index schema seamlessly.
  - Add tests in `tests/test_hooks.py` validating that the hook telemetry resolves both legacy plain-text `hook-policy` files and the new JSON schemas without error.
- Verify 100% test pass rate using `pytest`:
  ```bash
  uv run pytest tests/
  ```

### Manual Verification
- Execute parallel CLI write commands in two separate git worktrees concurrently and assert that transaction requests queue cleanly without raising database lock errors.
- Verify lifecycle hooks log telemetry and read policies properly inside a newly spawned git worktree.
