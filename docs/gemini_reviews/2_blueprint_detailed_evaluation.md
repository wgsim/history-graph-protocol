This is a robust and well-considered architectural blueprint that correctly identifies and solves the primary challenges of using HGP with Git Worktrees and in mixed-project environments. The three proposed pillars are complementary and create a cohesive, user-centric system.

Here is a detailed analysis:

### Overall Assessment

The proposed architecture is strong. It moves from a fragile, implicit system to a robust, explicit one. By centralizing the worktree resolution logic, moving to global hooks, and adding an explicit user opt-in, it addresses the core issues of discovery, isolation, and user consent simultaneously. The feasibility is high, as the proposed changes are localized to specific components (project root resolution, hook logic, and agent session initialization) and do not require a fundamental rewrite of the entire HGP system.

---

### 1. Native Git Worktree Support

**Pros:**
*   **Correctness:** This is the technically correct solution. It treats a repository as a single entity for history tracking, regardless of how many worktrees are checked out. This ensures a unified, coherent history graph.
*   **Solves Root Cause:** It directly fixes the issue of the HGP server seeing worktrees as separate repositories and the hook failing to find its policy file.
*   **Efficiency:** Avoids database fragmentation and duplication of history data that would occur if each worktree had its own `.hgp` directory.

**Cons / Considerations:**
*   **Implementation Detail:** The robustness of `find_project_root()` is critical. Relying on parsing the `gitdir:` path from the `.git` file is good, but a more resilient implementation would be to shell out to `git rev-parse --show-toplevel`. This command is the canonical way to find the root of a repository, and it works correctly from within a worktree. This approach would be less brittle than manual path parsing.
*   **Consistency:** This new worktree-aware `find_project_root` logic MUST be packaged into a shared utility and used consistently by *all* components: the HGP server itself, the global hook script, and the agent's pre-session check. Any divergence in implementation will reintroduce bugs.

---

### 2. Global Hooks with Dynamic Project-Local Isolation

**Pros:**
*   **Resolves Client Failure:** This completely eliminates the problem of different AI clients having different hook discovery mechanisms or stopping traversal at a worktree's `.git` file. Hooks will now always be found.
*   **Zero Overhead:** The pre-flight check for a `.hgp/` directory is extremely fast and effective. It provides a near-zero-cost "gate," ensuring that HGP has no performance or privacy impact on projects where it is not active. This is an elegant solution to the project isolation problem.
*   **Simplified User Setup:** Users install the hooks once globally, which is a much simpler and less error-prone setup than managing per-project hook installations.

**Cons / Considerations:**
*   **Global State:** As with any global configuration, users must be aware that the hooks are present. However, the opt-in model and the non-intrusive nature of the no-op check make this a very low risk. The benefits far outweigh this minor concern.

---

### 3. Dynamic Interactive Initialization

**Pros:**
*   **Excellent User Experience:** This is the cornerstone of the new user-centric approach. It replaces implicit, "magical" behavior with an explicit, understandable choice.
*   **Privacy and Consent:** It directly solves the problem of HGP creating files in proprietary or personal projects without permission. The user is always in control.
*   **Clean Onboarding:** It provides a clear and simple path for enabling HGP on new projects.

**Cons / Considerations / Edge Cases:**
*   **Non-Interactive Sessions:** The interactive prompt will fail in non-interactive environments like CI/CD pipelines or scripted agent runs. The system needs a way to handle this. A command-line flag or an environment variable (e.g., `HGP_MODE=enable`, `HGP_MODE=disable`) should be introduced to bypass the interactive prompt. The default behavior in a non-interactive session should probably be `disable`.
*   **Default Configuration:** When a user says "Yes," the agent should do more than just create an empty `.hgp` directory. It should also create a default `.hgp/hook-policy.json` file (e.g., with all operations in "advisory" mode) to ensure the system is immediately functional and configurable.
*   **Session State:** The agent needs to remember the user's choice ("Yes" or "No") for the duration of the session to avoid asking repeatedly.

### Feasibility and Recommendation

The feasibility of this blueprint is high. The required changes are logical and can be implemented with moderate effort.

**Recommendation:**
Proceed with this architectural plan.

**Key implementation priorities should be:**
1.  Create a single, shared, and robust `find_project_root` function (preferably using `git rev-parse --show-toplevel`) and ensure it's used by the HGP server, the hook, and the agent.
2.  Implement the non-interactive override (e.g., environment variable) for the dynamic initialization check.
3.  Ensure the initialization process creates a sensible default hook policy file.