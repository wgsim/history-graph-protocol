# Tech Stack - History Graph Protocol (HGP)

This document defines the official technical stack, dependencies, build tools, and security standards for HGP.

---

## Core Technologies

| Layer | Technology | Version | Rationale |
|---|---|---|---|
| **Runtime** | Python | `>=3.12` | Modern typing, high-performance async features. |
| **Framework** | Model Context Protocol (MCP) | `>=1.0.0` | Industry-standard protocol for connecting AI agents to tools. |
| **Database** | SQLite3 | `built-in` | Zero-configuration, serverless, transactional, and local. |
| **Validation** | Pydantic v2 | `>=2.0` | High-performance Rust-backed data modeling and verification. |
| **Build Backend** | Hatchling | `built-in` | Lightweight, standard-compliant PEP 517 build backend. |
| **Package Manager** | uv | `latest` | High-speed Python package resolver, sync engine, and environment manager. |

---

## Testing & Quality Assurance

- **Test Runner**: `pytest` and `pytest-asyncio` for comprehensive asynchronous unit and integration testing.
- **Linter & Formatter**: `ruff` (Rules: `E`, `F`, `I` for linting, line length: 120).
- **Type Checker**: `pyright` in strict mode (`typeCheckingMode = "strict"`) for 100% type safety.

---

## Security Vulnerability Standards (Critical)

To safeguard developers' local workspaces and secure the HGP runtime:

1. **SQL Injection Prevention & SQLite Concurrency**:
   - Every database query in `src/hgp/db.py` **must** use parameterized SQLite queries (`?` syntax).
   - Dynamic query string concatenation using user input is strictly **forbidden**.
   - Enforce Write-Ahead Logging (WAL) mode for concurrency, executing a mandatory tiny write transaction on startup to verify compatibility (inserting a sentinel row into `__hgp_wal_test__` followed by an immediate rollback). If it fails, fall back automatically to Rollback Journal (`PRAGMA journal_mode=TRUNCATE`) under network drives (e.g. NFS/SMB mounts).
   - Cache NFS/SMB rollback fallback state inside a JSON file (`.hgp/nfs_mode`) containing the mode and timestamp, automatically expiring and re-probing after 24 hours.
   - Expose the effective journal mode in `hgp status` for transparency.
   - Wrap multi-step writes (such as operations and edges) in immediate transactions (`BEGIN IMMEDIATE`).
   - Use SQLite's native `PRAGMA busy_timeout = 2000` (2s) to cleanly serialize locks before falling back to a python-level exponential backoff capped at **1 second** maximum total wait.
   - Compile-time allowlists **must** be enforced for any dynamic SQL identifiers (e.g. table/column names).

2. **Path Traversal, Symlink & CAS Protection**:
   - Treat all input paths as untrusted. Canonically resolve paths using `Path.resolve(strict=True)`.
   - **Non-existent Target Path Resolution**: For non-existent files, resolve the existing parent chain strictly (`Path.parent.resolve(strict=True)`), validate containment of the resolved parent directory, and join the final leaf filename without requiring the leaf to pre-exist.
   - Assert path containment within the canonical root using `os.path.commonpath([canonical_root, canonical_path]) == canonical_root` to prevent sibling prefix bypasses.
   - **Single File-Identity Schema**: Standardize on **repo-relative paths** (relative to `working_tree_root`) as the single canonical representation for tracked file identity. Do not key by `worktree_id`.
   - **Worktree Boundary Checks**: For linked git worktrees, containment check must validate against the active `working_tree_root`, while storing files relative to that worktree root to ensure portability.
   - **Structural Path Component Matching**: Inspect canonical path components strictly using `Path.parts` for submodule (`'modules'` and `'.git'` in parts) and worktree boundaries instead of fragile substring matching.
   - **Symlink Escape Guard & TOCTOU Hardening**: Any mutating operation target path that resolves to a symlink pointing *outside* the project root **must** immediately fail and throw `PATH_SYMLINK_ESCAPE`. All mutating writes must use the atomic `os.O_NOFOLLOW` flag during file handles/open operations.
   - **Atomic Write-Replace Pattern**: All mutations to blobs inside `.hgp_content/` must write to a unique UUID-based temporary file `[target].hgp-[UUID]-temp` in the *same directory* as the target (using exclusive `O_CREAT | O_EXCL` flags and `0o600` permissions). Stale temp files older than 24 hours are scanned and cleaned up during any routine HGP CLI execution.
   - **Permission fallbacks**: Copy permissions (`os.chmod`) from the original file, falling back to **`0o644`** for brand new files.
   - Prevent relative/absolute path traversal issues during `.git` pointer parsing and linked worktree `commondir` resolution.

3. **Global Hook Fail-Open & Mutating Tool Fail-Closed Boundaries**:
   - **Global preflight hook scripts** (in shell profiles) **must** wrap all logic in standard-exception `try...except Exception` blocks and exit with `sys.exit(0)` on any failure, ensuring the developer's session is never blocked. Imports must be lazy-loaded and strictly standard-library-only (`sys`, `os`, `json`, `pathlib`).
   - **Telemetry Hook Logging**: Hook failures will append a single-line summary to `~/.hgp/hook_errors.log` using unbuffered raw OS writes with non-blocking open (`os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NONBLOCK`) wrapped in a short sleep-retry loop up to 10ms to prevent hanging.
   - **Log Rotation Trigger**: Rotation of the log (truncating to 1,000 lines or 1MB) is triggered during *any* CLI command reading from the project root.
   - **HGP Mutating Tools** (`hgp_write_file`, `hgp_edit_file`, etc.) **must** be fail-closed, raising typed JSON errors (`HGP_NOT_INITIALIZED`, `HGP_DB_LOCKED`, etc.) if HGP is not initialized, locked, or corrupt.
   - **Windows locking & case-insensitivity**: Updates to `ignore_list` must use `fcntl.flock` or `.hgp/ignore_list.lock` on Windows (with stale process PID override). Detect case-insensitive systems and lowercase-normalize paths to prevent boundary bypasses due to case variance.

4. **Sensitive Data Protection**:
   - `hgp.db` and the `.hgp_content/` blob registry **must** remain locally scoped within the project root and permanently registered in `.gitignore`.
   - No credentials, API tokens, or secrets may be stored inside HGP operations or CAS objects.