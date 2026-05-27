# SECURITY & ARCHITECTURE REVIEW: HGP DESIGN DOCUMENTS

## CRITICAL FINDINGS

### 1. SYMLINK CHAIN TRAVERSAL VULNERABILITY
**Issue**: The path canonicalization relies on `Path.resolve()` to eliminate `..` segments and symlinks, but the current design treats symlink resolution as a single-point check. An attacker can construct a chain: safe_directory -> symlink -> outside_root -> further_path operations. The check `PATH_SYMLINK_ESCAPE` only verifies the final target, not intermediate symlinks in the resolution chain.

**Location**: Implementation Plan, "Strict Path Canonicalization" section.

**Risk**: Symlink chains could bypass boundary checks. Example: `/project/data` -> symlink to `/tmp` -> read `/tmp/sensitive`, which appears inside project but resolves outside.

**Recommendation**: 
- Validate every intermediate component in the resolved path chain, not just the final target.
- Use `Path.resolve(strict=False)` but then stat() each directory in the chain to detect symlinks. For each symlink encountered, verify its target stays within bounds.
- Alternatively, use `os.path.realpath()` for full resolution, then verify the entire real path tree.

### 2. RACE CONDITION IN .hgp/ ATOMIC CREATION
**Issue**: Implementation plan states "Do not automatically create .hgp/ directory upon server boot" and mutations must fail with `HGP_NOT_INITIALIZED` if missing. However, no atomic creation mechanism is specified. Two concurrent processes can both observe `.hgp/` missing and both attempt creation, causing:
- Concurrent mkdir() calls (one succeeds, others fail with EEXIST)
- Concurrent database initialization (multiple connections to same file)
- Fragmented initialization state

**Location**: Implementation Plan, server.py changes section.

**Risk**: Database initialization corruption under concurrent agent startup.

**Recommendation**:
- Use atomic filesystem primitives: `os.open(..., os.O_CREAT | os.O_EXCL)` with a `.hgp/.init-lock` sentinel file.
- Only one process proceeds with database schema creation; others wait for `.hgp/hgp.db` to exist.
- Set permissions on `.hgp/` to `0700` immediately upon creation to prevent other users' access.

### 3. INCOMPLETE commondir FILE PARSING (DoS & CORRUPTION)
**Issue**: Implementation Plan specifies reading `commondir` file from resolved gitdir but lacks:
- File size validation (attacker could create 1GB+ file)
- Line count validation (format is single line, but code doesn't enforce)
- Whitespace trimming (relative path may have trailing newlines)
- Existence check before read

**Location**: Implementation Plan, "Git Pointer Verification" section.

**Risk**: Denial of service via large commondir file; parsing of malformed paths leading to incorrect root resolution.

**Recommendation**:
- Read with size limit (e.g., max 4KB)
- Use strict line parsing: split on first newline, reject if multiple lines or trailing content
- Use `.strip()` for whitespace normalization
- Use pathlib: Path(gitdir) / commondir_content.strip(); verify result exists before use
- Add debug logging of parsed gitdir and commondir values

### 4. WINDOWS PATH COMPARISON BYPASS IN commonpath CHECK
**Issue**: Using `os.path.commonpath([root, path]) == root` is case-sensitive on Windows. An attacker can use alternate case: `/Project/...` vs `/project/...` to bypass containment checks.

**Location**: Implementation Plan, "Prefix-Bypass Prevention" section; also Tech Stack security standards.

**Risk**: Path containment check fails on Windows systems.

**Recommendation**:
```python
# Normalize case before comparison
root_norm = os.path.normcase(os.path.abspath(root))
path_norm = os.path.normcase(os.path.abspath(path))
if os.path.commonpath([root_norm, path_norm]) != root_norm:
    raise PATH_OUTSIDE_PROJECT
```

### 5. SUBMODULE DETECTION FRAGILE (String Matching Anti-Pattern)
**Issue**: Implementation Plan detects submodules by checking `if '/modules/' in resolved_path`. This is fragile:
- User can have legitimate directory named `modules`
- Does not handle nested submodules (`modules/foo/modules/bar`)
- Doesn't distinguish between `modules/` and `/path/modules/something`

**Location**: Implementation Plan, "Submodule Check" section.

**Risk**: False positives prevent legitimate operations; false negatives allow submodule-parent boundary violations.

**Recommendation**:
- Detect submodule by checking for `.git` file (not directory) in the target directory itself, then parsing for `gitdir:` pointer.
- If present, treat directory as submodule root.
- This is more reliable: actual Git semantics determine the boundary, not string patterns.
- Build a submodule registry at initialization time to avoid repeated lookups.

### 6. IGNORE_LIST CANONICAL PATH BYPASS (CASE SENSITIVITY, SYMLINKS)
**Issue**: Implementation Plan requires appending project path to `~/.hgp/ignore_list` but no canonical resolution is specified. Example bypasses:
- Add `/tmp/foo`, then access from `/private/tmp/foo` (macOS `/tmp` is symlink to `/private/tmp`)
- Add `/Project`, then access from `/project` (case-insensitive filesystem)
- Add `/path/to/project`, then access via relative path from a different location

**Location**: Implementation Plan, "Onboarding & ignore_list Guardrails" section.

**Risk**: Users can be re-prompted for HGP enablement on the same project via alternate paths.

**Recommendation**:
- Canonicalize all ignore_list entries using `Path.resolve().as_posix()` on insertion.
- On lookup, canonicalize the query path identically before checking membership.
- Store in a JSON file with version: `{"version": 1, "entries": [canonicalized_paths]}` to support future format changes.
- Protect ignore_list with file permissions `0600`.

### 7. FAIL-OPEN EXCEPTION CATCHING TOO BROAD
**Issue**: Implementation Plan specifies `try...except Exception:` in hooks. In Python 3, this does NOT catch `BaseException` subclasses like `KeyboardInterrupt`, `SystemExit`, but it does catch programming errors (AttributeError, KeyError) that should crash loudly for debugging.

**Location**: Implementation Plan, "Defensive Global Hooks" sections; pre_tool_use_hgp.py changes.

**Risk**: Silent failures mask bugs in hook code; developers can't debug hook issues.

**Recommendation**:
```python
try:
    # hook logic
except (FileNotFoundError, PermissionError, sqlite3.OperationalError, json.JSONDecodeError):
    # expected errors: missing .hgp, locked db, corrupt json
    sys.exit(0)
except Exception as e:
    # unexpected errors: log to stderr for debugging
    print(f"HGP hook unexpected error: {type(e).__name__}: {e}", file=sys.stderr)
    sys.exit(0)
```

This allows debugging while still failing open.

### 8. SQL IDENTIFIER ALLOWLIST NOT ENFORCED (CODE INJECTION RISK)
**Issue**: Tech Stack mandates "strict compile-time white-lists for any dynamic SQL identifiers" but the implementation plan provides no mechanism:
- Where are allowlists defined?
- How do they prevent new code from concatenating identifiers?
- No linting rule or type enforcement mentioned.

**Location**: Tech Stack, SQL Injection Prevention section; Implementation Plan, server.py concurrency section.

**Risk**: Future developer could add `f"SELECT * FROM {table_name}"` without noticing allowlist requirement.

**Recommendation**:
- Create an enum for all table/column names:
  ```python
  class Tables(str, Enum):
      OPERATIONS = "operations"
      EDGES = "edges"
  ```
- Replace all string literals with enum values in database code.
- Add a pre-commit linting rule that forbids `f"..."` or `.format()` in files containing SQL.
- Document: "Database schema identifiers must use the Tables/Columns enums. No string interpolation in SQL queries."

### 9. INCOMPLETE CONCURRENCY TEST PLAN
**Issue**: Implementation Plan states "simultaneous active sessions will be officially limited to separate transaction serialization" and says tests will verify "under heavy load," but:
- No load parameters specified (how many concurrent writers? for how long?)
- No performance baselines established (acceptable latency under contention?)
- No stress test scenarios defined (interleaved reads/writes, lock timeouts, etc.)
- No verification that WAL mode checkpointing doesn't cause issues

**Location**: Implementation Plan, "SQLite Concurrency Safety Controls"; Verification Plan section.

**Risk**: Concurrent worktree access could perform unexpectedly badly or deadlock under load.

**Recommendation**:
- Define explicit concurrency test suite:
  ```
  - Test 1: 5 concurrent writes to same operation table (verify no corruption)
  - Test 2: Read while write is in progress (verify no SQLITE_BUSY)
  - Test 3: Two worktrees interleaving writes (verify causal ordering)
  - Test 4: Lock timeout after 2 seconds (verify HGP_DB_LOCKED is raised)
  - Baseline: measure latency with 0, 1, 5, 10 concurrent writers
  ```
- Add to regression suite with max acceptable latency thresholds.

---

## HIGH-PRIORITY FINDINGS

### 10. MISSING HOOK INITIALIZATION GUARD IN GEMINI.md INJECTION
**Issue**: Implementation Plan specifies inserting "Interactive Opt-in & Onboarding" logic into GEMINI.md but:
- No equivalent guidance for CLAUDE.md (which agent takes precedence?)
- No handling of `.copilot/` hooks or other CLI environments
- Session root op binding is described but no error handling if binding fails

**Location**: Implementation Plan, "System Prompt & Onboarding Guidelines" section.

**Risk**: Inconsistent behavior across agent platforms; missing initialization in one agent type breaks HGP contract.

**Recommendation**:
- Define priority order: if both CLAUDE.md and GEMINI.md exist, which agent's HGP initialization takes precedence?
- Create a shared `~/.hgp/agent-context.json` file to track initialization state across agents (with file permissions `0600`).
- If initialization fails in one agent, the next agent can detect and retry or fail fast.
- Document expected behavior for each platform.

### 11. RELATIVE PATH TRAVERSAL IN gitdir PARSING NOT BOUNDED
**Issue**: Implementation Plan says "resolve the declared gitdir path relative to the pointer file's parent directory and canonically verify it," but:
- No explicit check that relative gitdir paths like `../../../outside` are rejected
- `Path.resolve()` handles this, but verification step should be explicit
- No check if gitdir is absolute path (should it be rejected? allowed with extra validation?)

**Location**: Implementation Plan, "Git Pointer Verification" section.

**Risk**: Malformed .git pointer with `gitdir: ../outside` could traverse out of bounds.

**Recommendation**:
```python
gitdir_line = "gitdir: /path/to/.git"  # or relative like "gitdir: ../foo/.git"
gitdir_value = gitdir_line.replace("gitdir:", "").strip()
gitdir_path = Path(git_file.parent) / gitdir_value  # resolve relative to .git pointer
gitdir_canonical = gitdir_path.resolve()
project_root_canonical = git_file.parent.resolve()

if not str(gitdir_canonical).startswith(str(project_root_canonical.parent)):
    raise UNSUPPORTED_GIT_LAYOUT("gitdir escapes repository bounds")
```

### 12. HOOK POLICY FILE FORMAT UNSPECIFIED
**Issue**: Implementation Plan mentions `.hgp/hook-policy` file to determine "block/advisory settings" but:
- Format not specified (JSON, YAML, INI, plain text?)
- Required fields not documented
- No schema version for future compatibility
- Default behavior if file missing not specified
- Permissions on hook-policy file not mentioned

**Location**: Implementation Plan, pre_tool_use_hgp.py changes.

**Risk**: Inconsistent parsing across implementations; missing file treated unpredictably.

**Recommendation**:
```json
{
  "version": 1,
  "hook_mode": "advisory",  // "block" or "advisory"
  "record_tool_calls": true,
  "record_bash_commands": true
}
```
- Document as required file created during `hgp init`.
- Default to `advisory` mode if missing.
- Add schema validation on read.
- Set file permissions to `0600`.

### 13. DATABASE PRAGMA SETTINGS INCOMPLETE
**Issue**: Tech Stack mandates WAL mode but doesn't specify other critical pragmas:
- `PRAGMA synchronous = IMMEDIATE` (fsync frequency)
- `PRAGMA journal_mode = WAL` (explicitly set)
- `PRAGMA foreign_keys = ON` (if using FK constraints)
- `PRAGMA temp_store = MEMORY` (temporary tables in RAM)
- Connection timeout/busy timeout values

**Location**: Tech Stack, SQL Injection Prevention & SQLite Transaction Safety section.

**Risk**: Database could be corrupted on unclean shutdown; performance could be unpredictable.

**Recommendation**:
```python
def init_db_connection(db_path):
    conn = sqlite3.connect(str(db_path), timeout=2.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")  # balance safety/perf
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA busy_timeout=2000")  # 2 second timeout
    return conn
```
Document rationale for each setting.

### 14. JSON ERROR RESPONSE FORMAT AMBIGUOUS
**Issue**: JSON Error Taxonomy lists errors but doesn't specify response structure:
- Is response `{"error": "HGP_NOT_INITIALIZED", "message": "..."}` or `{"type": "HGP_NOT_INITIALIZED", ...}`?
- How does MCP protocol convey JSON errors vs exceptions?
- Should HTTP-like status codes be included?
- Client-facing message vs debug details not separated

**Location**: Implementation Plan, "JSON Error Taxonomy" section.

**Risk**: Clients can't reliably parse errors; inconsistent error handling across tools.

**Recommendation**:
```json
{
  "error_code": "HGP_NOT_INITIALIZED",
  "error_type": "STATE",
  "message": "HGP not initialized. Run 'hgp init' to enable.",
  "details": {
    "project_root": "/path/to/project",
    "expected_dir": "/path/to/project/.hgp"
  }
}
```
Document error_type taxonomy: STATE, PERMISSION, PATH, DATABASE, INTEGRITY. Use for client-side error categorization.

---

## MEDIUM-PRIORITY FINDINGS

### 15. CONCURRENT WORKTREE WRITE ORDERING UNSPECIFIED
**Issue**: Implementation Plan allows multiple worktrees to write to shared `.hgp/hgp.db`, but the causal ordering guarantees are undefined:
- If worktree A and B both create operations simultaneously, what's the ordering?
- Can operation B reference operation A if A hasn't been committed yet?
- What's the semantics of `parent_op_ids` under concurrent writes?

**Location**: Implementation Plan, server.py changes and "Concurrent SQLite Writes" warning section.

**Risk**: Causal graph could be inconsistent; operations could have invalid parent references.

**Recommendation**:
- Define explicit ordering: operations are causally ordered by `(timestamp, operation_id)` where timestamp is SQLite rowid (monotonically increasing).
- Document: "A new operation can only reference operations that have been committed to the database before the new operation's BEGIN IMMEDIATE transaction."
- Add integrity check on operation retrieval: verify all parent_op_ids exist and have older rowids.

### 16. SUBMODULE INITIALIZATION AND ISOLATION NOT FULLY SPECIFIED
**Issue**: Implementation Plan says submodules are isolated ("Do not merge submodule history into parent database") but:
- Can parent operations reference submodule operations?
- If submodule is deleted, what happens to its `.hgp/` directory?
- Can you move a file from parent to submodule and track it?
- What if parent and submodule are both HGP-enabled?

**Location**: Implementation Plan, "Submodule Scoping Policy" open question.

**Risk**: Submodule operations are silently lost or inaccessible; cross-boundary references cause failures.

**Recommendation**:
- Create a submodule registry in parent's `.hgp/config.json`: map submodule path -> separate HGP root.
- Forbid operations from referencing across submodule boundaries (raise `PATH_CROSSES_SUBMODULE_BOUNDARY`).
- Document: submodules must be independently initialized with `hgp init`.
- Add cleanup: if submodule is deleted, optionally archive its `.hgp/` to parent's `.hgp/archived_submodules/`.

### 17. EXPONENTIAL BACKOFF IMPLEMENTATION DETAILS MISSING
**Issue**: Tech Stack and Implementation Plan specify "jittered exponential backoff for up to 2 seconds" but lack specifics:
- Initial backoff (1ms? 10ms?)
- Max backoff (before 2s limit reached)
- Jitter algorithm (uniform random? exponential?)
- Retry count vs. time limit (which takes precedence?)

**Location**: Tech Stack, SQL Injection Prevention; Implementation Plan, "SQLite Concurrency & Safety Controls" section.

**Risk**: Non-deterministic behavior; performance unpredictable.

**Recommendation**:
```python
MAX_RETRY_TIME = 2.0  # seconds
INITIAL_BACKOFF = 0.001  # 1ms
MAX_BACKOFF = 0.512  # 512ms

def execute_with_retry(db, query, params):
    backoff = INITIAL_BACKOFF
    start = time.time()
    while True:
        try:
            return db.execute(query, params)
        except sqlite3.OperationalError as e:
            if "database is locked" not in str(e):
                raise
            if time.time() - start > MAX_RETRY_TIME:
                raise HGPDatabaseLockedError(f"Lock timeout after {MAX_RETRY_TIME}s")
            jitter = backoff * (0.5 + random.random())  # 50-150% of backoff
            time.sleep(jitter)
            backoff = min(backoff * 2, MAX_BACKOFF)
```

### 18. IGNORE_LIST CRITICAL PATH VALIDATION INSUFFICIENT
**Issue**: Implementation Plan forbids adding `.git/` and `.hgp/` to ignore_list but mechanism is unclear:
- Is it a simple string check (`if ".git" in path`)? -> can add `/my.git/project`
- Is it exact match (`if path.endswith(".git")`)? -> can add `/path/.git/subdir`
- What about symlinks to `.git/`?

**Location**: Implementation Plan, "Onboarding & ignore_list Guardrails" section.

**Risk**: Sensitive paths can be inadvertently added to ignore_list.

**Recommendation**:
```python
FORBIDDEN_IGNORE_PATHS = {".git", ".gitmodules", ".hgp", ".hgp_content"}

def validate_ignore_entry(path):
    canonical = Path(path).resolve()
    for forbidden in FORBIDDEN_IGNORE_PATHS:
        if forbidden in canonical.parts or canonical.name == forbidden:
            raise ValueError(f"Cannot ignore {forbidden}")
```

### 19. HOOK LAZY IMPORT GUIDANCE UNCLEAR
**Issue**: Implementation Plan specifies "Imports must be lazy-loaded and strictly standard-library-only (sys, os, json, pathlib)" but:
- How do you lazy-load in Python? (import inside functions?)
- What if sqlite3 is needed to check lock status? Is it allowed?
- What about subprocess for shell integration?
- No example provided

**Location**: Implementation Plan, "Defensive Global Hooks" section.

**Risk**: Developers may import wrong libraries or use eager imports, defeating the fail-open guarantee.

**Recommendation**:
```python
#!/usr/bin/env python3
"""Fail-open hook. Must not import non-stdlib."""

def main():
    try:
        import sys, os, json
        from pathlib import Path
        # lazy import sqlite3 only if needed
        import sqlite3
        
        # hook logic
        return 0
    except Exception:
        # fail open
        return 0

if __name__ == "__main__":
    sys.exit(main())
```
Document: "Do not import at module level. All imports must be inside main() to fail gracefully if a module is missing."

### 20. VERIFICATION PLAN MISSING ERROR CASE TESTING
**Issue**: Implementation Plan's Verification section tests happy path ("Does it work?") and ignore_list ("Does it skip?") but not error cases:
- What happens with malformed `.git` pointer?
- What happens with corrupted commondir file?
- What happens with symlink escape attempts?
- What happens with concurrent writes to ignore_list?

**Location**: Implementation Plan, Verification Plan section.

**Risk**: Edge cases and error paths untested; bugs hide in error handling.

**Recommendation**:
Add to test suite:
```
test_corrupted_git_pointer()
test_symlink_escape_attempt()
test_concurrent_db_writes_under_load()
test_ignore_list_concurrent_append()
test_missing_commondir_file()
test_circular_symlink_in_worktree_chain()
test_submodule_boundary_protection()
```

---

## LOWER-PRIORITY FINDINGS

### 21. SESSION ROOT OP CONTEXT BINDING ERROR HANDLING
When `hgp_create_operation()` or `hgp_set_context()` fails, the agent session will proceed without HGP context. Implementation should define fallback: continue with best-effort recording, or fail the entire session?

### 22. PERMISSION CHECKS ON .hgp DIRECTORY
Implementation doesn't specify owner/group validation. On shared systems, ensure `.hgp/` is readable only by owner (0700).

### 23. RECOVERY PROCEDURES FOR CORRUPT DATABASE
No guidance on `hgp repair` or recovery workflows if `hgp.db` is corrupted. Should there be a backup/checkpoint mechanism?

### 24. GIT LAYOUT VERSION TRACKING
As Git evolves (new pointer file formats, future worktree metadata), include a version check: `UNSUPPORTED_GIT_LAYOUT` should include git version and format version in error message.

### 25. SYMLINK IN HOOK SCRIPT PATHS
Hooks are registered in `~/.claude/hooks` and `~/.gemini/hooks`. If these paths are symlinks, ensure the symlink target is validated (doesn't escape home directory).

---

## SUMMARY OF CRITICAL MITIGATIONS REQUIRED

| Finding | Severity | Mitigation |
|---------|----------|-----------|
| Symlink chain traversal | CRITICAL | Validate every intermediate symlink in resolution chain |
| Race condition in .hgp/ creation | CRITICAL | Use atomic fs primitives (O_CREAT \| O_EXCL) + lock file |
| commondir DoS vulnerability | CRITICAL | Enforce 4KB size limit, validate format strictly |
| Windows path bypass | CRITICAL | Normalize case before commonpath comparison |
| Submodule detection fragile | HIGH | Use .git file presence check, not string pattern |
| Ignore_list bypass via symlinks | HIGH | Canonicalize all paths, use resolve() consistently |
| SQL identifier injection risk | HIGH | Enforce enum-based identifier usage, lint rules |
| Concurrency test plan incomplete | HIGH | Define explicit load scenarios and performance baselines |