ARCHITECTURAL REVIEW: HGP GIT WORKTREE SUPPORT BLUEPRINT

STRENGTHS OF THE PROPOSED APPROACH

1. Addresses root cause of worktree failure
The proposed refactor of find_project_root() to resolve .git files to their gitdir targets directly solves the traversal termination problem. This is the correct architectural fix rather than a workaround.

2. Preserves project isolation philosophy
The opt-in gate via .hgp/ directory presence maintains the original design principle: non-HGP projects incur zero overhead and zero unwanted side effects. This is psychologically important for adoption.

3. Solves the dual-problem simultaneously
Rather than patching hooks separately from core logic, centralizing the resolution logic in one place (find_project_root) ensures hooks, server initialization, and client code all see the same root.

4. Graceful degradation
The pre-flight check in global hooks (exit immediately if no .hgp/) means deployment is safe. Global hooks can be installed once and work correctly in mixed environments.

CRITICAL RISKS AND WEAKNESSES

1. Git worktree deletion race condition
If a worktree is deleted while an HGP operation is in flight, the code may:
- Read gitdir path that no longer exists
- Get a file-not-found error when trying to open .git file
- Leave dangling references in .hgp/operations that point to a non-existent worktree

Risk level: Medium. Mitigation: Validate gitdir path exists and is readable before using it. Document that worktree deletion during an operation is unsupported, similar to deleting the main checkout.

2. Multiple worktrees pointing to same main repo creates shared state contention
If two worktrees (feat-a and feat-b) both write to the same .hgp/ database in the main repo root simultaneously, you have:
- Concurrent writes to the same operation log
- Potential .hgp/lock file conflicts
- Unclear which worktree an operation originated from

Risk level: High. Current HGP architecture may not be designed for concurrent writes from different git working trees. You must either:
a) Add worktree context metadata to every operation (agent_id, worktree_path) and handle merge semantics
b) Use file locking at the HGP server level to serialize writes
c) Accept that only one worktree can have an active HGP session at a time (document this)

3. Hook policy resolution across worktrees
If feat-a has a modified .hgp/hook-policy (committed to that branch) but the .hgp/ directory is in the shared main root, which policy wins?

Risk level: Medium-High. The proposal doesn't specify: is hook-policy in .hgp/ always the one from main, or should it be branch-aware? This is a design gap.

4. Client-side hook discovery still fragile
The proposal moves to global hooks to solve client-side discovery issues. However:
- Not all AI clients may support global hook locations (.claude/hooks, .gemini/hooks)
- Installation and management of global hooks is now a prerequisite
- If global hooks are not installed, HGP is silently non-functional
- Uninstall/upgrade of global hooks requires explicit user action

Risk level: Medium. This shifts complexity from "find local hooks" to "global hook lifecycle management."

5. The opt-in prompt UX can still fail
If a user says "No" to HGP initialization, but later runs a command that depends on HGP features, what happens?
- Is HGP completely disabled for the session, or just not auto-initialized?
- Can the user enable it mid-session?
- Does the user get prompted again?

Risk level: Low-Medium, but the proposal leaves this state machine undefined.

EDGE CASES NOT ADDRESSED

1. Nested worktrees (Git 2.31+)
Git now supports worktrees of worktrees. The gitdir resolution logic must handle transitive resolution (.git file -> gitdir -> another .git file), not just one level.

2. Detached worktrees or bare repositories
If .git is a file (worktree) but gitdir points to an invalid or bare repository, how does the code behave?

3. Symlinked worktrees
If a worktree path is a symlink, does find_project_root() resolve it correctly? Does metadata capture the canonical path or the symlink?

4. SSH/network-mounted .git directories
The proposal assumes .git is accessible via normal file I/O. In rare cases (network repositories), this may fail.

5. HGP initialization order in a worktree
If a user creates a new worktree and the main repo has .hgp/ but the user hasn't yet activated any session, does HGP auto-initialize? Does it prompt?

6. Concurrent initialization races
Two agents starting in different worktrees simultaneously, both trying to initialize .hgp/ for the first time. Lock handling?

FEASIBILITY ASSESSMENT

1. Code complexity: Low to Medium
The find_project_root() refactor is straightforward. Reading a .git file and parsing gitdir is simple. Hook script refactoring is also straightforward if reusing the same logic.

2. Testing burden: High
You must test:
- Worktree creation and deletion
- Concurrent access from multiple worktrees
- Hook resolution across worktree boundaries
- Graceful degradation in non-HGP projects
- Edge cases (detached, symlinked, nested worktrees)

3. Backward compatibility: Good
Existing code that doesn't use worktrees is unaffected. The refactor preserves behavior for non-worktree repos.

4. Operational complexity: Medium
Deploying global hooks and managing their lifecycle is more complex than per-project hooks, but standard practice in other tools (git hooks, husky, etc.).

ALTERNATIVE CONSIDERATIONS

1. Worktree-scoped .hgp databases
Instead of sharing one .hgp/ in the main root, each worktree could have its own .hgp/ directory in .worktrees/{name}/.hgp/. Pros: No contention, simpler concurrency. Cons: Fragmented history, unclear which worktree owns an operation, harder to correlate changes across worktrees.

2. Environment variable fallback
Instead of global hooks, set HGP_PROJECT_ROOT via environment before starting the agent. Pros: No hook installation needed. Cons: Requires user setup, not transparent.

3. Require explicit worktree initialization
When entering a worktree, the user must run hgp init --worktree. Pros: Explicit, opt-in. Cons: Extra friction, users will forget.

KEY RECOMMENDATIONS

1. Add explicit worktree context to every HGP operation
Store the resolved worktree path and name in operation metadata. This is essential for multi-worktree debugging and concurrency analysis.

2. Define clear concurrency semantics
Document whether multiple worktrees can have concurrent HGP sessions. If yes, add file locking. If no, add a check that prevents simultaneous access.

3. Clarify hook-policy resolution
Is it main-repo-only, or branch-aware? If branch-aware, how is it stored and resolved?

4. Test all Git worktree edge cases before shipping
Especially nested worktrees, symlinks, and concurrent access. This is the highest-risk area.

5. Make hook installation explicit and idempotent
Provide a simple command (hgp init-hooks) that installs global hooks. Make it safe to run multiple times.

6. Add fallback detection in the agent code
Before relying on HGP, check that hooks are installed and .hgp/ is accessible. Emit a clear diagnostic if not.

OVERALL VERDICT

The blueprint is architecturally sound and addresses the real problems. However, the concurrency model for shared .hgp/ databases across multiple worktrees is underspecified and represents the highest risk. The rest of the proposal (refactoring find_project_root, opt-in gates, global hooks) is solid and incremental.

Feasibility: 7/10. Doable with careful attention to concurrency, testing, and the edge cases outlined above.