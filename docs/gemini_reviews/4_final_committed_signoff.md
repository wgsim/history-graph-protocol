Architecture and Code Review: History Graph Protocol (HGP) Design

Executive Summary
The proposed design for the History Graph Protocol (HGP) demonstrates a robust approach to isolating project environments and securing the file system boundaries. However, several critical vulnerabilities and edge cases remain, particularly regarding Time-of-Check to Time-of-Use (TOCTOU) file system races, SQLite concurrency limits on networked file systems, and fragile Git path matching heuristics.

1. Path Traversal & Symlink Escapes
- TOCTOU Vulnerability: The use of Path.resolve() followed by a separate mutating file operation introduces a classic Time-of-Check to Time-of-Use (TOCTOU) race condition. An attacker or concurrent background process could replace a verified directory with a symlink pointing outside the project boundary during the microsecond window between resolution and execution.
Suggestion: Enforce the use of os.open() with the os.O_NOFOLLOW and os.O_CLOEXEC flags for all file mutations. This ensures the kernel enforces the symlink boundary atomically at the time of the file handle creation.
- Canonicalization Overheads: Repeatedly calling Path.resolve() on every operation path can introduce severe performance penalties on macOS and Windows, especially in deeply nested directories.
Suggestion: Implement an LRU cache for path canonicalization bound to the session lifecycle, ensuring that repeated validations of identical paths do not incur disk I/O penalties.

2. SQLite Concurrency & Reliability
- PRAGMA busy_timeout Omission: Implementing a custom Python-level jittered exponential backoff for HGP_DB_LOCKED is suboptimal and prone to thundering herd problems.
Suggestion: Rely on SQLite's native PRAGMA busy_timeout setting to handle lock contention at the C-extension level, queuing connections cleanly. Use the Python-level backoff only as a last resort for extended deadlocks that exceed the timeout.
- Networked File Systems (NFS/SMB) and WAL Mode: Write-Ahead Logging (WAL) mode requires POSIX shared memory primitives (mmap) that are notoriously unreliable or completely broken on NFS and SMB mounts. If a developer maps a Git worktree over a network drive, WAL mode will cause immediate database corruption.
Suggestion: Introduce a preflight filesystem capability check. If the .hgp directory resides on a networked filesystem, fallback to rollback journal mode (PRAGMA journal_mode=TRUNCATE) and strictly serialize access, or reject initialization with a specific error.

3. Git Boundary & Submodule Fragility
- Fragile Substring Matching: Relying on the presence of "/modules/" or "/worktrees/" in the resolved path to identify submodules or worktrees is highly fragile. Developers frequently create directories named "modules" or "worktrees" in their source code, which would trigger false positives.
Suggestion: Do not use substring matching. Instead, determine Git boundaries by parsing the core.worktree value in the local git config, or by checking for the exact presence of .git/commondir and .git/gitdir structured pointer semantics.
- Absolute vs Relative Git Pointers: The design specifies resolving the gitdir path "relative to the pointer file's parent directory". However, git worktree pointers can (and often do) contain absolute paths.
Suggestion: Ensure the path parser correctly differentiates between absolute and relative paths in the gitdir: directive to prevent path concatenation errors.

4. Hook Design & Fail-Open Observability
- Silent Failure Anti-Pattern: While fail-open hooks (sys.exit(0)) correctly protect the developer terminal, completely silent failures subvert the core guarantee of HGP (permanent, append-only history). If hooks fail silently due to bad Python environments or permissions, the user loses their causal history without knowing.
Suggestion: Implement an asynchronous telemetry or local dead-letter queue (e.g., appending to ~/.hgp/hook_errors.log). This preserves the zero-latency fail-open requirement while providing an audit trail for diagnostics.

5. Ignore List & Onboarding Safeguards
- Concurrency on Global Ignore List: Multiple AI agents or parallel sessions bootstrapping simultaneously could cause race conditions when reading and writing to the global ~/.hgp/ignore_list.
Suggestion: Use file-level locking (fcntl.flock or msvcrt.locking) when updating the ignore_list to prevent file corruption and race conditions.
- Path Normalization in Ignore List: If paths appended to the ignore_list are not strictly canonicalized, bypasses will occur (e.g., /Users/dev/project vs /Users/dev/project/).
Suggestion: Enforce Path.resolve() prior to querying or appending to the ignore_list to ensure a normalized, absolute path is stored.

6. JSON Error Taxonomy Additions
- Missing PERMISSION_DENIED: File permission boundaries are the most common source of local errors. Add an explicit error for OS-level access violations.
- Missing HGP_FS_UNSUPPORTED: To handle the NFS/SMB edge case if WAL mode cannot be safely configured.

Conclusion
The architecture is fundamentally sound, but the path resolution logic requires atomic execution guarantees, and the Git detection logic must move away from string heuristics toward structural git config parsing. Implementing native SQLite timeouts and local logging for fail-open hooks will mature the design into a production-ready protocol.

--- [Routing Log] ---
[1] Primary (gemini): Trying...
    [SUCCESS]