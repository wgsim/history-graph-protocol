Review of HGP Architecture Blueprint

1. Native Git Worktree Support inside the HGP Codebase

Pros:
- Single Source of Truth: Centralizes the history graph for the entire repository, preventing fragmented databases across different worktrees.
- Solves Cross-Repo Errors: By explicitly mapping worktrees to the main repository, the HGP server will correctly identify the parent directory, eliminating CROSS_REPO_OPERATION errors.
- Alignment with Git: Inherits Git's natural mental model where branches/worktrees belong to a single entity.

Cons and Edge Cases:
- Relative vs. Absolute Paths: The gitdir pointer in a .git file can be absolute or relative. The path resolution logic must strictly handle both formats based on the location of the .git file itself.
- Submodules vs. Worktrees: Git submodules also use a .git file containing a gitdir pointer. If HGP treats submodules the same as worktrees, it might accidentally merge the history of an independent submodule into the parent repository's HGP database. The resolution logic must distinguish between worktree pointers (usually pointing to .git/worktrees/name) and submodule pointers (pointing to .git/modules/name).
- Permissions and Remote Mounts: If a worktree is mounted on a different filesystem or has different permission boundaries, the hook running in the worktree must have read/write access to the main .hgp database.

Feasibility: High. Reading and resolving the .git file is a standard filesystem operation. Distinguishing submodules from worktrees requires checking the path structure of the resolved gitdir.

2. Global Hooks with Dynamic Project-Local Isolation (Opt-in Gate)

Pros:
- Client Agnostic: Completely bypasses the hardcoded traversal limits of AI clients like Claude or Gemini. 
- Simplified Installation: Users only need to install HGP hooks once per machine/agent, rather than per project.
- Graceful Degradation: The zero-overhead pass-through ensures proprietary or non-HGP projects remain untouched.

Cons and Edge Cases:
- Global Blast Radius: Because the hook runs globally for every agent session, any unhandled exception, syntax error, or slow import in the hook script will break the AI agent for all projects on the machine.
- Path Resolution Overhead: The hook script must dynamically find the project root (re-implementing Part 1) on every execution. In deeply nested directories, this means multiple stat calls.
- Race Conditions: If multiple global hooks from different tools are installed, the order of execution might matter.

Feasibility: High, with strict engineering constraints. 
Recommendation: The global hook must be written with a top-level try/except block that immediately defaults to a pass-through (exit 0 or standard passthrough logic) if any error occurs. Imports must be lazy; do not import heavy libraries (like sqlite3 or requests) until the .hgp directory is confirmed to exist.

3. Dynamic Interactive Initialization (Smart Onboarding UX)

Pros:
- Privacy First: Guarantees zero pollution of non-HGP projects.
- Contextual Awareness: Empowers the user to decide on a per-project basis whether history tracking is valuable.
- Seamless Bootstrapping: If the user says Yes, the agent handles the boilerplate, making onboarding frictionless.

Cons and Edge Cases:
- Lack of Persistence for "No": If a user declines HGP initialization, the .hgp folder is not created. In the next session, the agent will see no .hgp folder and ask again. This prompt fatigue will quickly become annoying for proprietary projects.
- Prompt Adherence: Relies heavily on the LLM strictly following the system instruction. If the LLM context drops or hallucinates, it might skip the prompt or falsely claim initialization.

Feasibility: Moderate to High.
Recommendation: To solve the prompt fatigue issue, you need a persistence layer for rejections. This should not be a local file (to avoid polluting the repo). Instead, when the user says "No", the agent should write the project's absolute path to a global configuration file (e.g., ~/.hgp/ignore_list). The system prompt should be updated to: "Check for .hgp/. If missing, check ~/.hgp/ignore_list. If absent from both, ask the user."

Overall Conclusion

This blueprint is highly robust and solves the core constraints imposed by Git Worktrees and global agent configurations. The architectural shift from local to global hooks combined with a fast-fail local isolation check is the correct path forward. The primary technical hurdles will be differentiating submodules from worktrees during path resolution, ensuring the global hook is resilient to crashes, and persisting user opt-outs globally to prevent prompt fatigue.