# HGP Codex Hook — End-to-End Test Guide

**Purpose:** Verify that HGP MCP tools and hooks installed in `.codex/hooks/` behave
correctly in a real Codex session. Run all steps in order; record actual output for each.

**Working directory:** `<repo_root>` (the directory containing `.codex/`)  
**Hook policy at start:** advisory (default)

---

## Pre-flight

### Step 1 — Confirm installation

```bash
ls .codex/hooks/
# expected: post_tool_use_hgp.py  pre_tool_use_hgp.py

cat .codex/hooks.json | python3 -m json.tool
# expected: PreToolUse and PostToolUse entries referencing pre_tool_use_hgp.py / post_tool_use_hgp.py

cat .codex/config.toml
# expected: [mcp_servers.hgp] section + [features] codex_hooks = true
```

If any artifact is missing, run:

```bash
hgp install --codex --local
```

### Step 2 — Confirm HGP tools are visible in Codex

Start a Codex session and ask:

```
What MCP tools do you have available?
```

**Expected:** `hgp_create_operation`, `hgp_write_file`, `hgp_edit_file`, and other `hgp_*`
tools are listed.

If HGP tools are not listed, the MCP server is not running. Verify `[features] codex_hooks = true`
is present in `.codex/config.toml` and that the Python running HGP has
`history-graph-protocol` installed.

### Step 3 — Confirm hook policy

```bash
hgp hook-policy
# expected: advisory
```

---

## Test 1 — HGP MCP tool call records an artifact

**Purpose:** Verify the MCP connection works and HGP records operations correctly.

**Prompt to use:**

```
Use hgp_write_file to create a file called hgp_codex_smoke.txt in the repo root
with the content "codex smoke test". Use agent_id "codex-smoke".
```

**Expected behavior:**
- Codex calls `hgp_write_file` via MCP
- Tool returns an `op_id`
- File `hgp_codex_smoke.txt` is created on disk

**Verification (in terminal after Codex responds):**

```bash
cat hgp_codex_smoke.txt
# expected: codex smoke test

hgp_query_operations --agent codex-smoke   # or ask Codex: hgp_query_operations(agent_id="codex-smoke")
# expected: at least one artifact operation listed
```

**Pass condition:** File created AND HGP records the operation with a valid `op_id`.

---

## Test 2 — Bash command fires PreToolUse hook

**Purpose:** Verify that `pre_tool_use_hgp.py` fires before a Bash command that mutates files.

**Prompt to use:**

```
Run this bash command: cp hgp_codex_smoke.txt hgp_codex_smoke_copy.txt
```

**Expected behavior:**
- `pre_tool_use_hgp.py` fires before the Bash tool executes
- Hook output appears (advisory warning about untracked file mutation)
- Command executes and `hgp_codex_smoke_copy.txt` is created

**Verification:**

```bash
ls hgp_codex_smoke_copy.txt
# expected: file exists
```

**Pass condition:** Hook fires (Codex session shows hook output or advisory message) AND
command executes successfully.

> **Note:** Codex currently fires `PreToolUse`/`PostToolUse` hooks for **Bash commands only**.
> `apply_patch` (file edits) does not trigger hooks — see Test 4.

---

## Test 3 — PostToolUse hook fires after Bash command

**Purpose:** Verify that `post_tool_use_hgp.py` fires after a Bash command completes.

**Prompt to use:**

```
Run this bash command: echo "hook test" >> hgp_codex_smoke.txt
```

**Expected behavior:**
- `pre_tool_use_hgp.py` fires before the command
- `post_tool_use_hgp.py` fires after the command completes
- Both hooks produce advisory output visible in the Codex session
- File is appended

**Verification:**

```bash
cat hgp_codex_smoke.txt
# expected: last line is "hook test"
```

**Pass condition:** PostToolUse hook output visible AND file mutation applied.

---

## Test 4 — apply_patch does NOT fire hooks (known limitation)

**Purpose:** Document and confirm that file edits via `apply_patch` do not trigger hooks.
This is a known upstream Codex limitation ([github.com/openai/codex/issues/16732](https://github.com/openai/codex/issues/16732)).

**Prompt to use:**

```
Edit the file hgp_codex_smoke.txt and add a new line "apply_patch test" at the end.
Use your native file edit tool (apply_patch).
```

**Expected behavior:**
- No `[HGP]` hook output appears
- File is edited
- HGP does NOT record this as an artifact (no MCP call was made)

**Pass condition:** Edit succeeds AND no hook fires. This is expected — use `hgp_edit_file`
instead of `apply_patch` when tracking matters.

---

## Test 5 — Read-only Bash command does NOT fire a warning

**Purpose:** Verify hooks do not produce false positives on read-only commands.

**Prompt to use:**

```
Run this bash command: git status
```

**Expected behavior:**
- `git status` output is returned normally
- No `[HGP]` warning appears in hook output

**Pass condition:** No hook advisory output. Command output visible.

---

## Test 6 — hgp_create_operation records a hypothesis

**Purpose:** Verify graph operations (non-file) are also recorded correctly.

**Prompt to use:**

```
Record a hypothesis using hgp_create_operation with:
- op_type: "hypothesis"
- agent_id: "codex-smoke"
- metadata description: "Codex smoke test decision"
```

**Expected behavior:**
- Codex calls `hgp_create_operation` via MCP
- Returns an `op_id`

**Verification — ask Codex:**

```
Use hgp_query_operations to list all operations with agent_id "codex-smoke".
```

**Pass condition:** At least two operations listed (artifact from Test 1 + hypothesis from this test).

---

## Test 7 — Block mode blocks Bash commands

**Setup:**

```bash
hgp hook-policy block
cat .hgp/hook-policy
# expected: block
```

**Prompt to use:**

```
Run this bash command: touch hgp_codex_live_blocked.txt
```

**Expected behavior:**
- `pre_tool_use_hgp.py` returns `permissionDecision: "deny"`
- Bash command does NOT execute
- `hgp_codex_live_blocked.txt` is NOT created

**Verification:**

```bash
ls hgp_codex_live_blocked.txt 2>/dev/null || echo "not created (correct)"
```

**Pass condition:** Command blocked AND file not created.

## Test 8 — No false PostToolUse advisory after block-mode deny

Immediately after Test 7 (still in block mode), run a read-only command:

**Prompt to use:**

```
Run this bash command: git status --short
```

**Expected behavior:**
- No `[HGP]` advisory appears in PostToolUse output
- The deny from Test 7 must NOT have left a stale marker that triggers a false advisory here

**Pass condition:** `git status` output returned normally, no HGP advisory.

**Cleanup:**

```bash
hgp hook-policy advisory
```

---

## Cleanup

```bash
/opt/homebrew/opt/trash/bin/trash hgp_codex_smoke.txt hgp_codex_smoke_copy.txt 2>/dev/null; true
```

---

## Results Template

Fill in after running all tests:

| Test | Expected | Actual | Pass/Fail |
|------|----------|--------|-----------|
| 1. hgp_write_file | file created + op_id returned | | |
| 2. PreToolUse on cp | hook fires + command executes | | |
| 3. PostToolUse on echo >> | hook fires + file appended | | |
| 4. apply_patch no hook | no hook fires (expected) | | |
| 5. git status silent | no hook warning | | |
| 6. hgp_create_operation | hypothesis recorded + query lists it | | |
| 7. block mode | command blocked + file not created | | |
| 8. no false advisory after deny | git status returns cleanly, no HGP advisory | | |

---

## Hook architecture summary

| Hook file | Event | Trigger | Behavior |
|-----------|-------|---------|----------|
| `pre_tool_use_hgp.py` | PreToolUse | Bash commands | Advisory warning (or deny in block mode) |
| `post_tool_use_hgp.py` | PostToolUse | Bash commands | Advisory context appended after execution |

> **Known limitation:** Codex fires hooks for `Bash` tool only. `apply_patch` (file edits)
> does not trigger `PreToolUse`/`PostToolUse`. Use `hgp_edit_file` via MCP for tracked edits.
