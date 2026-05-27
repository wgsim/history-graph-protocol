Here is an analysis of the options to make the History Graph Protocol (HGP) robustly support Git Worktrees.

### High-Level Recommendation

The most robust and developer-friendly solution is to **modify HGP's Python codebase to support Git Worktrees natively**.

Manual configuration through environment variables (`HGP_PROJECT_ROOT`, `HGP_HOOK_BLOCK`) or the consistent creation of symbolic links is fragile, error-prone, and places an unnecessary burden on the developer. Tooling should adapt to standard developer workflows, not the other way around. Native support ensures HGP "just works" in a worktree environment with zero configuration.

---

### Detailed Analysis and Recommendations

#### **Problem 3: HGP Server Database Isolation / Cross-Repo Errors**

This is the most critical issue. The server's incorrect identification of the project root is the source of database fragmentation and permission errors.

*   **Option A: `HGP_PROJECT_ROOT` Environment Variable**
    *   **Pros:** Explicit, requires no code changes.
    *   **Cons:** Not developer-friendly. It requires every developer to know and set this variable for every shell session in a worktree. Forgetting to set it leads to a broken history graph.

*   **Option B: Symbolic Link for `.hgp` folder**
    *   **Pros:** Configuration is stored in the filesystem.
    *   **Cons:** Manual, repetitive setup. A symlink must be created for every new worktree, which is tedious and easy to forget.

*   **Option C: Modify `find_project_root()` in `src/hgp/project.py` (Recommended)**
    *   **Pros:**
        *   **Permanent Solution:** Solves the problem at its source.
        *   **Zero Configuration:** Developers do not need to do anything; it works automatically.
        *   **Robust:** The logic correctly models how Git Worktrees operate.
    *   **Cons:** Requires modifying the HGP codebase (which is the correct approach here).

**Recommendation:** Modify `find_project_root()` to be worktree-aware. The implementation should be:
1.  Begin the search for a `.git` entry upwards from the current directory.
2.  If the found `.git` is a directory, the project root is its parent directory (the current behavior).
3.  If the found `.git` is a **file**, parse its content to read the `gitdir:` path (e.g., `gitdir: /path/to/main/repo/.git/worktrees/feat-branch`).
4.  From this path, derive the location of the main repository's `.git` directory. The project root is the parent of that directory.

---

#### **Problem 2: Hook Script Policy Loading**

The hook script fails for the same reason as the server: it misidentifies the project root.

*   **Option A: `HGP_HOOK_BLOCK` Environment Variable**
    *   **Pros:** Explicit.
    *   **Cons:** Same as above; it's a manual configuration burden on the developer.

*   **Option B: Refactor `_resolve_block_mode()` in `pre_tool_use_hgp.py` (Recommended)**
    *   **Pros:**
        *   **Fixes the issue natively:** The hook becomes self-sufficient and worktree-aware.
        *   **Consistent Logic:** Can reuse or mimic the same worktree-detection logic proposed for `find_project_root()`.
    *   **Cons:** Involves a code change.

**Recommendation:** The `_resolve_block_mode()` function in the hook script should implement the same logic as the recommended change for `find_project_root()`. It must correctly locate the main repository root to find the `.hgp/hook-policy` file. This makes the hook robust and independent of environment variables.

---

#### **Problem 1: AI Client Hook Loading**

This problem is external to the HGP codebase, as it depends on how AI clients (Gemini, Claude, etc.) discover hooks. However, we can still recommend the most effective workaround.

*   **Option A: Global Hooks (`~/.gemini/hooks`)**
    *   **Pros:** Simple to set up once.
    *   **Cons:**
        *   **Poor Portability:** The hook configuration does not travel with the project. New developers won't have it.
        *   **Project-Specific Hooks:** Often, hooks are specific to a project's needs. A global hook is a blunt instrument.

*   **Option B: Symbolic Links for `.gemini`/`.claude` Folders (Recommended)**
    *   **Pros:**
        *   **Project-Contained:** The setup remains within the project directory structure.
        *   **Explicit:** It's clear how the hook is being loaded.
    *   **Cons:** Requires manual creation for each new worktree.

**Recommendation:** The symbolic link approach is superior because it keeps all configuration contained within the project. To overcome the manual setup burden, a simple wrapper script can be created to automate the process:

```bash
# Example: git-wt
#!/bin/bash
git worktree create "$@"
# After creating the worktree, symlink the hooks directory
ln -s ../../.gemini "$1/.gemini"
ln -s ../../.claude "$1/.claude"
```

While the ideal long-term solution is for the AI clients themselves to become worktree-aware, this scripted symlink approach is the most practical and robust interim solution.

### Conclusion

Yes, you should modify HGP's Python codebase to natively support Git Worktrees. By making `find_project_root()` and the hook script's `_resolve_block_mode()` worktree-aware, you eliminate the need for fragile manual configuration and create a seamless, developer-friendly experience. For the external issue of client hook discovery, recommending project-contained symbolic links (ideally managed by a script) is the most sound and portable strategy.