# HGP Gemini CLI Hook — End-to-End Test Guide

**Purpose:** Verify that the four HGP hooks installed in `.gemini/hooks/` behave correctly  
in a real Gemini CLI session. Run all steps in order; record actual output for each.

**Working directory:** `<repo_root>` (the directory containing `.gemini/`)  
**Hook policy at start:** advisory (default — do NOT set `HGP_HOOK_BLOCK=1` unless instructed)

---

## Pre-flight

### Step 1 — Install HGP MCP server in Gemini CLI

HGP hooks warn agents to use `hgp_*` tools instead of native file tools. For those tools
to actually be available in the session, HGP must be registered as an MCP server.

Add the following to `~/.gemini/settings.json` under `mcpServers`:

```json
"mcpServers": {
  "hgp": {
    "command": "python",
    "args": ["-m", "hgp.server"]
  }
}
```

> **Note:** The server is started from the working directory at session start, so launch
> Gemini CLI from inside the repo root. `HGP_PROJECT_ROOT` is not needed in normal use.

Verify HGP tools are visible after launching Gemini CLI:

```
/tools
# expected: hgp_create_operation, hgp_write_file, hgp_edit_file, ... listed
```

If HGP tools are not listed, the MCP server failed to start. Check that
`history-graph-protocol` is installed (`pip show history-graph-protocol` or
`uv pip show history-graph-protocol`).

### Step 2 — Confirm hooks and policy

```bash
ls .gemini/hooks/
# expected: post_bash_hgp.py  post_tool_use_hgp.py  pre_bash_hgp.py  pre_tool_use_hgp.py

hgp hook-policy
# expected: advisory
# Note: .hgp/hook-policy file is only created after explicitly setting a policy;
# on a fresh repo the default advisory mode is implicit and the file may not exist.

echo "${HGP_HOOK_BLOCK:-unset}"
# expected: unset  (if not unset, run: unset HGP_HOOK_BLOCK)
```

If any hook file is missing, run `hgp install-hooks --gemini` to reinstall.

---

## Test 1 — `write_file` triggers warnings in advisory mode

**Action:** Ask Gemini to create a new file using its native file tool.

**Prompt to use:**
```
Create a file called /tmp/hgp_test_gemini.txt with the content "hello from gemini".
Use the write_file tool directly.
```

**Expected behavior (two channels):**

| Channel | Hook | When | Content |
|---------|------|------|---------|
| Terminal (user) | `pre_tool_use_hgp.py` (BeforeTool) | Before tool runs | `[HGP] Native 'write_file' detected. Use 'hgp_write_file' instead…` |
| Agent context | `post_tool_use_hgp.py` (AfterTool) | After tool completes | `[HGP] Native 'write_file' was used. Prefer 'hgp_write_file'…` |

- The file **is still created** (advisory mode does not block)
- The BeforeTool `systemMessage` appears in the terminal UI, not in the agent's reasoning
- The AfterTool `additionalContext` is appended to the tool result, so the agent sees it
  and can prefer `hgp_write_file` on subsequent calls

**Pass condition:** Terminal warning appears AND file creation succeeds AND agent
acknowledges or references the advisory in its response.

---

## Test 2 — `replace` triggers warnings in advisory mode

**Action:** Ask Gemini to edit an existing file using the `replace` tool.

**Setup:**
```bash
echo "original content" > /tmp/hgp_test_replace.txt
```

**Prompt to use:**
```
In the file /tmp/hgp_test_replace.txt, replace the text "original content" with "replaced content".
Use the replace tool directly.
```

**Expected behavior (two channels):**

| Channel | Hook | Content |
|---------|------|---------|
| Terminal (user) | `pre_tool_use_hgp.py` | `[HGP] Native 'replace' detected. Use 'hgp_edit_file' instead…` |
| Agent context | `post_tool_use_hgp.py` | `[HGP] Native 'replace' was used. Prefer 'hgp_edit_file'…` |

- The replacement **is still applied** (advisory mode)

**Pass condition:** Terminal warning appears AND replacement succeeds AND agent context
contains the advisory.

---

## Test 3 — `read_file` does NOT trigger a warning

**Action:** Ask Gemini to read a file using its native read tool.

**Prompt to use:**
```
Read the file /tmp/hgp_test_gemini.txt and tell me its contents.
```

**Expected behavior:**
- No `[HGP]` warning appears in terminal or agent context
- File contents are returned normally

**Pass condition:** No warning output from any hook.

---

## Test 4 — Mutating shell command triggers a warning

**Action:** Ask Gemini to run a shell command that copies a file.

**Prompt to use:**
```
Run this shell command: cp /tmp/hgp_test_gemini.txt /tmp/hgp_test_copy.txt
```

**Expected behavior:**
- `pre_bash_hgp.py` fires on the `run_shell_command` tool (not `shell` — Gemini CLI's
  actual tool name is `run_shell_command`)
- `systemMessage` warning appears in terminal: `[HGP] Bash command may mutate files`
- The matched pattern is reported (e.g., `matched: '\\bcp\\b'`)
- The command **still executes** (advisory mode)
- `post_bash_hgp.py` may fire after the command and report changed tracked files via
  `git status --porcelain` (files in `/tmp/` are untracked so they won't appear)

**Pass condition:** Pre-bash terminal warning appears; command executes.

---

## Test 5 — Read-only shell command does NOT trigger a warning

**Action:** Ask Gemini to run a read-only shell command.

**Prompt to use:**
```
Run this shell command: git status
```

**Expected behavior:**
- No `[HGP]` warning appears
- `git status` output is returned normally

**Pass condition:** No hook warning output.

---

## Test 6 — Block mode blocks `write_file`

**Setup:** Switch to block mode:
```bash
hgp hook-policy block
cat .hgp/hook-policy
# expected: block
```

**Prompt to use:**
```
Create a file called /tmp/hgp_test_blocked.txt with the content "should be blocked".
Use the write_file tool directly.
```

**Expected behavior:**
- `pre_tool_use_hgp.py` returns `decision: deny` before the tool runs
- `post_tool_use_hgp.py` does **not** fire (tool was denied before execution)
- The file is **NOT created**
- Gemini reports that the tool was denied

**Verification:**
```bash
ls /tmp/hgp_test_blocked.txt 2>/dev/null || echo "file not created (correct)"
```

**Pass condition:** Tool denied AND file not created.

**Cleanup:** Restore advisory mode after this test:
```bash
hgp hook-policy advisory
```

---

## Test 7 — Stale hook detection (upgrade path)

**Action:** Verify that `hgp hook-policy advisory` warns if installed hooks are outdated.

This test only applies if you have an older hook installation. Skip if hooks were freshly
installed with the current version.

```bash
# Check hook is current (should NOT trigger a stale warning)
hgp hook-policy advisory
# expected: "Hook policy set to: advisory" with no stale-hook warning
```

**Pass condition:** No stale hook warning on a fresh install.

---

## Results Template

Fill in after running all tests:

| Test | Expected | Actual | Pass/Fail |
|------|----------|--------|-----------|
| 1. write_file warn | terminal systemMessage + agent additionalContext, file created | | |
| 2. replace warn | terminal systemMessage + agent additionalContext, edit applied | | |
| 3. read_file silent | no warning | | |
| 4. cp shell warn | terminal systemMessage (run_shell_command), command runs | | |
| 5. git status silent | no warning | | |
| 6. block mode deny | tool denied, file not created, post hook silent | | |
| 7. stale hook check | no stale warning on fresh install | | |

---

## Hook architecture summary

| Hook file | Event | Tool matcher | Advisory output | Block output |
|-----------|-------|-------------|-----------------|--------------|
| `pre_tool_use_hgp.py` | BeforeTool | `write_file`, `replace` | `systemMessage` (terminal only) | `decision: deny` (agent sees reason) |
| `post_tool_use_hgp.py` | AfterTool | `write_file`, `replace` | `additionalContext` (agent context) | *(never fires — tool was denied)* |
| `pre_bash_hgp.py` | BeforeTool | `run_shell_command` | `systemMessage` (terminal only) | *(advisory only, no block mode)* |
| `post_bash_hgp.py` | AfterTool | `run_shell_command` | `systemMessage` with git diff | *(advisory only)* |

---

## Known Limitations

- `post_bash_hgp.py` reports changed **tracked** files only (`git status --porcelain`).  
  Untracked files outside the repo (e.g., `/tmp/`) will not appear in the post-hook report.
- The marker file keyed to `ppid` means post-bash output only fires when Gemini CLI itself
  spawns the hook process as a child — this is the normal execution path in a real session.
- `systemMessage` is displayed to the **user in the terminal** only. It does not appear in
  the agent's reasoning. Agent-side advisory warnings come from `post_tool_use_hgp.py` via
  `hookSpecificOutput.additionalContext`.
