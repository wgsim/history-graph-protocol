Findings

1. Critical: The design conflates storage root with file-authority root for linked worktrees.
Location: ../history-graph-protocol/docs/implementation_plan.md:12, 73-78, 120
Issue: The plan says a linked worktree should resolve to the canonical main repository root and then says all paths must be validated against that root. A linked worktree’s files are normally outside the main checkout directory, so strict containment against the main repo root will reject legitimate worktree files. If you loosen that check later, you risk creating an overly broad trust boundary.
Why it matters: This is both a correctness and security problem. Either valid worktree writes fail, or path checks become ambiguous enough to admit unintended directories.
Recommendation: Separate:
- storage_root: the shared HGP store anchored to the common Git dir / main repo identity
- working_tree_root: the actual checkout root for the current worktree
- allowed_roots: the set of working-tree roots proven to belong to the same Git common-dir
Contain file mutations to the current working_tree_root, not the shared storage_root. Only use the shared identity to choose the database/CAS location and to allow cross-worktree graph association.

2. High: Git layout classification is underspecified and currently vulnerable to path-shape heuristics.
Location: ../history-graph-protocol/docs/implementation_plan.md:70-77
Issue: The spec classifies submodules and worktrees by checking whether the resolved gitdir path “contains /modules/” or “contains /worktrees/”.
Why it matters: Substring tests are not a trustworthy parser for Git internals. They are brittle across platform/path variations, can misclassify unexpected layouts, and are easy to encode incorrectly in implementation.
Recommendation: Specify a strict parser for .git pointer files and commondir:
- require exact `gitdir: ` syntax handling
- resolve relative to the pointer file location
- canonicalize with strict existence checks where applicable
- identify linked worktrees from Git’s actual metadata structure, not string containment
- identify submodules by Git metadata semantics, not `/modules/` substring presence
- add negative tests for malformed pointer files, fake “modules/worktrees” path segments, missing commondir, and absolute/relative mixed forms

3. High: Fail-open hooks weaken the audit guarantee, and the error taxonomy is internally inconsistent.
Location: ../history-graph-protocol/conductor/tech-stack.md:46-48, ../history-graph-protocol/docs/implementation_plan.md:87-98, 163-165
Issue: Hooks must exit 0 on all failures, but the taxonomy also defines `HOOK_CAPTURE_FAILED`. The documents do not define how that error is durably surfaced if the hook intentionally suppresses failure.
Why it matters: Silent hook failure means real operations can happen with missing provenance, while the system still appears healthy. That undermines the “permanent causal history” claim.
Recommendation: Define a separate observable channel for fail-open hook failures:
- append a structured failure record to a durable local queue/log
- have the server reconcile and emit a first-class event on next healthy interaction
- distinguish “developer session must not break” from “history loss is acceptable”; those are different policies
Also specify whether shell hooks are advisory-only while MCP mutating tools remain the only integrity-enforcing path.

4. High: `Path.resolve()` plus `commonpath` is not sufficient protection for mutation-time symlink races.
Location: ../history-graph-protocol/conductor/tech-stack.md:40-44, ../history-graph-protocol/docs/implementation_plan.md:60-65
Issue: The design validates a resolved path before mutation, but does not address TOCTOU races between validation and file open/write.
Why it matters: An attacker or concurrent process can swap a symlink after validation and redirect a write outside the intended boundary.
Recommendation: Extend the spec to require mutation-time anti-symlink controls:
- use `os.open(..., O_NOFOLLOW)` where supported
- validate parent directories with `lstat`
- prefer dirfd-bounded operations for final path components
- re-check canonical containment immediately before commit
- add tests that swap symlinks between check and write

5. Medium: The ignore-list design is contradictory and too prompt-dependent for a security boundary.
Location: ../history-graph-protocol/docs/implementation_plan.md:137-143
Issue: The document first says the ignore list stores/checks absolute project paths, then says checks should be canonicalized against the project’s root relative path.
Why it matters: A global `~/.hgp/ignore_list` cannot safely use relative paths without collisions and bypasses. Also, forbidding critical entries only in system prompt guidance is not enforcement.
Recommendation: Standardize on one format only: canonical absolute project-root paths. Enforce guardrails server-side:
- reject entries that resolve to `.git`, `.hgp`, hook locations, `$HOME`, or ancestors thereof
- reject non-root paths if the ignore-list is meant to be per-project
- de-duplicate after canonicalization
- define file permissions and atomic update semantics for `~/.hgp/ignore_list`

6. Medium: Database concurrency requirements are incomplete for the non-SQLite parts of the write path.
Location: ../history-graph-protocol/conductor/tech-stack.md:32-38, ../history-graph-protocol/docs/implementation_plan.md:121-125
Issue: WAL, `BEGIN IMMEDIATE`, and retry/backoff cover DB writes, but the design is silent on CAS/blob concurrency in `.hgp_content/`.
Why it matters: Concurrent artifact writes can still produce torn blobs, inconsistent object registration, or hash/file mismatches even if the database is safe.
Recommendation: Specify CAS atomicity explicitly:
- write blobs to unique temp files in the target filesystem
- `fsync` file and directory as appropriate
- publish with atomic rename
- make DB object registration occur only after the blob is durable
- define recovery behavior for orphan temp files and blob-present/DB-missing states

7. Medium: The retry/backoff policy for `HGP_DB_LOCKED` is too vague and likely too small.
Location: ../history-graph-protocol/docs/implementation_plan.md:124, 160
Issue: “retrying for up to 2 seconds” is underspecified and may be too aggressive for multi-agent contention.
Why it matters: Under realistic concurrent sessions, this will create avoidable fail-closed behavior and poor UX even when the system is healthy.
Recommendation: Specify:
- exact retry budget and jitter formula
- interaction with SQLite `busy_timeout`
- which operations are safe to retry idempotently
- metrics/logging thresholds for lock contention
I would expect a larger upper bound than 2 seconds for shared-worktree usage, or at least explicit justification for why 2 seconds is acceptable.

8. Medium: Initialization/state errors are not normalized.
Location: ../history-graph-protocol/docs/implementation_plan.md:117-119, 155-161
Issue: The plan uses `HGP_DISABLED` or `NOT_INITIALIZED` in prose, while the taxonomy defines `HGP_NOT_INITIALIZED` and does not define `HGP_DISABLED`.
Why it matters: Clients need stable machine-parsable semantics. Different names for similar states will produce brittle agent behavior.
Recommendation: Define a single canonical taxonomy with exact applicability:
- `HGP_NOT_INITIALIZED`: project root found, HGP absent, mutation refused
- `HGP_DISABLED`: optional, only if there is an explicit disabled state distinct from absence
- `GIT_ROOT_UNRESOLVED`: no repo/worktree root could be determined
Also define whether read-only tools behave differently from mutating tools in each state.

9. Medium: Submodule boundary policy is directionally correct but incomplete for nested and path-crossing cases.
Location: ../history-graph-protocol/docs/implementation_plan.md:30-33, 72, 153
Issue: The design says parent operations cannot cross submodule boundaries, but does not define how boundary discovery is maintained during path traversal or how nested submodules/worktrees are handled.
Why it matters: Without a precise model, parent and child roots can disagree about the same path, especially in nested repos or partially initialized submodules.
Recommendation: Specify boundary evaluation rules:
- boundary detection must happen on every candidate path ancestor during mutation
- the nearest valid Git root/submodule root wins for file authority
- nested submodule traversal must fail closed with `PATH_CROSSES_SUBMODULE_BOUNDARY`
- add tests for nested submodules, deinitialized submodules, and sibling worktrees containing submodules

10. Medium: Sensitive-data protection is policy-only, not control-backed.
Location: ../history-graph-protocol/conductor/tech-stack.md:50-52
Issue: “No credentials, API tokens, or secrets may be stored” is a requirement, but no control is specified.
Why it matters: In practice, agents will accidentally capture sensitive payloads unless the system enforces some limit or scanning policy.
Recommendation: Either:
- define server-side secret screening/redaction before persistence, with false-positive handling, or
- explicitly mark secret detection as out of scope and state that HGP must never be used on secret-bearing payloads without an external redaction layer
Also define maximum payload sizes and metadata limits to reduce accidental exfiltration and storage abuse.

11. Medium: Global hook installation/chaining needs an integrity and ordering spec.
Location: ../history-graph-protocol/docs/implementation_plan.md:20-24
Issue: “chain safely with any existing hooks” is not enough specification.
Why it matters: Hook ordering, double-execution, recursion, and partial uninstall behavior all affect correctness and safety.
Recommendation: Define:
- deterministic insertion order
- recursion guards
- uninstall/rollback behavior
- what happens if an existing hook mutates environment or cwd
- validation that the chain still contains HGP after reinstall/update

12. Low: The verification plan is missing several adversarial tests required by the security claims.
Location: ../history-graph-protocol/docs/implementation_plan.md:173-196
Issue: Current tests cover happy-path worktree parsing and manual onboarding, but not the hard failure modes the design introduces.
Why it matters: Most of the risk here is in malformed metadata, concurrent mutation, and boundary bypass.
Recommendation: Add tests for:
- malformed `.git` pointer content and invalid encodings
- malicious `commondir` traversal attempts
- fake `gitdir` pointing outside expected ownership
- symlink race during mutation
- concurrent multiprocess writers, not just threaded/unit-level contention
- `ignore_list` rejection of `.git`, `.hgp`, home, and ancestor paths
- fail-open hook telemetry generation
- linked worktree file path acceptance while still rejecting unrelated sibling directories

Open questions / assumptions

- Assumption: the intent is to share one HGP store across linked worktrees of the same Git common-dir, not to permit arbitrary cross-directory mutation. If that assumption is correct, the design needs separate notions of repository identity and writable path boundary.
- Assumption: `HOOK_CAPTURE_FAILED` is meant to be observable by clients, not just a log-only condition. If so, the current fail-open text is insufficient.
- I agree with the proposed submodule isolation policy, but only if it is implemented as a hard nearest-root authority rule rather than a string-pattern heuristic.

Short change summary

The strongest parts of the update are the move to explicit fail-closed mutating tools, the decision to stop auto-creating `.hgp`, and the push toward canonical path handling plus WAL-backed concurrency. The main problems are that the spec currently mixes identity and path-authority in worktrees, relies on fragile Git path heuristics, and does not fully define how silent hook failures and non-DB concurrency are made observable and safe.

--- [Routing Log] ---
[1] Primary (codex): Trying...
    [SUCCESS]