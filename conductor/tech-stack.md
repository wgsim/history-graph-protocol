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
   - Enforce Write-Ahead Logging (WAL) mode for concurrency, executing a mandatory tiny write transaction on startup to verify compatibility, falling back automatically to Rollback Journal (`PRAGMA journal_mode=TRUNCATE`) under network drives (e.g. NFS/SMB mounts) and caching this state.
   - Expose the effective journal mode in `hgp status` for transparency.
   - Wrap multi-step writes (such as operations and edges) in immediate transactions (`BEGIN IMMEDIATE`).
   - Use SQLite's native `PRAGMA busy_timeout = 2000` (2s) to cleanly serialize locks before falling back to a python-level exponential backoff.
   - Compile-time allowlists **must** be enforced for any dynamic SQL identifiers (e.g. table/column names).

2. **Path Traversal, Symlink & CAS Protection**:
   - Treat all input paths as untrusted and canonically resolve both the file and root paths using `Path.resolve(strict=True)` to eliminate directory traversal segments (`..`).
   - Assert path containment within the canonical root using `os.path.commonpath([canonical_root, canonical_path]) == canonical_root` to prevent sibling prefix bypasses.
   - **Worktree Boundary Checks**: For linked git worktrees, containment check must validate against the active `working_tree_root`, while storing files relative to that worktree root to ensure portability.
   - **Symlink Escape Guard**: Any mutating operation target path that resolves to a symlink pointing *outside* the project root **must** immediately fail and throw `PATH_SYMLINK_ESCAPE`.
   - **Atomic Write-Replace Pattern**: All mutations to blobs inside `.hgp_content/` must write to a temporary file in the *same directory* as the target (using exclusive `O_CREAT | O_EXCL` flags and `0o600` permissions), copy permissions (`os.chmod`) from the original file, and rename it atomically over the destination.
   - Prevent relative path traversals during `.git` pointer parsing and linked worktree `commondir` resolution.

3. **Global Hook Fail-Open & Mutating Tool Fail-Closed Boundaries**:
   - **Global preflight hook scripts** (in shell profiles) **must** wrap all logic in standard-exception `try...except Exception` blocks and exit with `sys.exit(0)` on any failure, ensuring the developer's session is never blocked. Imports must be lazy-loaded and strictly standard-library-only (`sys`, `os`, `json`, `pathlib`).
   - **Telemetry Hook Logging**: Hook failures will append a single-line summary to `~/.hgp/hook_errors.log` using unbuffered raw OS writes (`os.O_WRONLY | os.O_CREAT | os.O_APPEND`) wrapped in a 5ms timeout to prevent hanging.
   - **HGP Mutating Tools** (`hgp_write_file`, `hgp_edit_file`, etc.) **must** be fail-closed, raising typed JSON errors (`HGP_NOT_INITIALIZED`, `HGP_DB_LOCKED`, etc.) if HGP is not initialized, locked, or corrupt.

4. **Sensitive Data Protection**:
   - `hgp.db` and the `.hgp_content/` blob registry **must** remain locally scoped within the project root and permanently registered in `.gitignore`.
   - No credentials, API tokens, or secrets may be stored inside HGP operations or CAS objects.