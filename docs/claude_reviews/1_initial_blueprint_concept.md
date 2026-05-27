ANALYSIS OF SOLUTIONS

PROBLEM 1: HOOK LOADING

Global Hooks Approach
Pros: Centralized, one-time setup per machine, survives worktree creation/deletion, works across all projects
Cons: Requires out-of-band setup, users must install hooks manually, reduces per-project hook customization, harder to version-control hooks

Symlinks Approach
Pros: Keeps hooks project-specific, discoverable in repo, versioned with code
Cons: Extra setup per worktree, fragile (symlinks can break), requires documenting worktree creation workflow, CI/CD complications, scales poorly with multiple worktrees

Native Support (Resolve via find_project_root)
Pros: Automatic, zero-config, works without user intervention
Cons: Requires code changes in both HGP and client implementations, needs thorough testing


PROBLEM 2: HOOK POLICY RESOLUTION

Environment Variable Approach
Pros: Immediate, no code changes
Cons: Manual per-session setup, easy to forget or misconfigure, error-prone, not portable across shells/machines

Refactor _resolve_block_mode()
Pros: Automatic, transparent to user, handles root-finding in one place
Cons: Slightly more code complexity, needs edge case testing (symlinks, mount points, permission issues), requires validation of gitdir path


PROBLEM 3: DATABASE ISOLATION

HGP_PROJECT_ROOT Approach
Pros: Explicit control, works immediately
Cons: Manual setup, silent failures if misconfigured, breaks if forgotten across sessions, not portable

Symlinks for .hgp
Pros: No env var management
Cons: Confusing: data lives elsewhere but is accessed locally, hard to troubleshoot, may conflict with git operations, doesn't scale to multiple worktrees

Modify find_project_root()
Pros: Automatic detection, single fix point, transparent, best UX
Cons: Code changes required, needs testing for edge cases (broken symlinks, mount issues, permissions)


RECOMMENDED APPROACH

Implement native Git Worktree support directly in HGP's Python codebase. This is the only solution that scales and provides zero-config behavior.

Specific changes:

1. Modify src/hgp/project.py find_project_root():
   - When .git is a file (not a directory), read the gitdir path
   - Parse "gitdir: ../path/to/main/.git/worktrees/X" 
   - Resolve the actual main repo root
   - Return main repo path instead of worktree path
   - Verify the main repo has .hgp before returning (fallback to worktree if not)

2. Refactor pre_tool_use_hgp.py _resolve_block_mode():
   - Apply the same .git file detection logic
   - Continue traversing up to find .hgp/hook-policy in main repo
   - Cache the resolved main root to avoid repeated parsing

3. Document the detection logic as a utility function to avoid duplication

RATIONALE

This approach solves all three problems simultaneously with a single root cause fix. When HGP correctly identifies the main repo, then:
- Hook loading works (hooks found at main repo root)
- Hook policy resolution works (traversal reaches main repo)
- Database binding works (isolated to main repo, not worktree)

All without requiring users to set env vars, create symlinks, or install global hooks. The behavior is automatic and transparent.

RISK ASSESSMENT

Low risk:
- Git's gitdir format is stable and well-documented
- Fallback to worktree root preserves current behavior if main repo detection fails
- Changes are localized to two functions

Testing required:
- Symlinked repos (should work)
- Detached/orphaned worktrees (edge case)
- Permission issues reading .git file or gitdir path
- Multiple nested worktrees
- Non-standard Git configurations

WHY NOT ENV VARS OR SYMLINKS

Environment variables place the burden on users (setup friction, forgetting, misconfiguration). Symlinks are fragile (broken after git worktree prune, conflicts with git gc, confusion about data location). Both approaches suggest HGP doesn't understand Git's fundamental abstractions, which erodes developer trust.

Native support demonstrates that HGP is aware of modern Git workflows and handles them transparently. This is the expectation for production-grade tools.

IMPLEMENTATION PRIORITY

1. find_project_root() in project.py (solves Problems 1, 2, 3)
2. _resolve_block_mode() in hook script (redundant after #1, but necessary for hook phase detection)
3. Add integration tests for worktree scenarios

This is the most robust, scalable, and developer-friendly path forward.