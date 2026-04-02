# HGP V4 File Tracking — Implementation Plan (Retrospective)

**Branch**: `feat/v4-file-tracking`
**Base**: `main` after V3 evidence trail merge (commit `de56e33`)
**Date**: 2026-04-01
**Status**: IMPLEMENTED — this document is a post-hoc record of design decisions

---

## Breaking Changes

- **`hgp_query_operations` return type changed**: The tool now returns `{"operations": [...]}` instead of `[...]` directly.
  - Clients using `result[0]` or iterating directly over the result must update to `result["operations"][0]` / `result["operations"]`.

---

## Goal

Give agents a first-class, HGP-tracked way to perform file I/O so that every
file write, edit, delete, and move is automatically recorded as an immutable
operation in the causal graph — without requiring agents to call
`hgp_create_operation` manually after each file action.

Additionally, introduce an enforcement layer (hook system) that warns or blocks
agents when they use native file tools (Write, Edit) instead of the HGP-aware
equivalents.

---

## V3 Prerequisites (all satisfied on base branch)

| Requirement | Status |
|-------------|--------|
| `op_evidence` table + `hgp_get_evidence` / `hgp_get_citing_ops` | ✅ |
| `evidence_refs` parameter on `hgp_create_operation` | ✅ |
| `EvidenceRef` Pydantic model in `models.py` | ✅ |
| `memory_tier` depth-decay (`_record_access_with_decay`) | ✅ |
| CAS blob store (WORM) | ✅ |
| `LeaseManager` with heartbeat TTL | ✅ |

MCP tool count entering V4: **13** (12 from V3 + 1 retroactive count of `hgp_file_history`)
After V4: **18**

---

## Invariants That Must Not Change

- `op_id` is immutable once created
- `object_hash` → CAS blob is WORM
- `chain_hash` computation is unaffected by `file_path`
- `op_edges` DAG traversal is unaffected by file operations
- Evidence (`op_evidence`) links are not affected by file operations
- `commit_seq` is monotonically increasing

---

## Schema Change: `file_path` Column on `operations`

### Design decision

File operations need to be queryable by path without scanning all operations.
Adding `file_path TEXT` (nullable) to the existing `operations` table is the
minimal change — it does not affect non-file operations, which leave it `NULL`.

Two indexes cover the two primary query patterns:

```sql
-- existence / filter queries
CREATE INDEX IF NOT EXISTS idx_operations_file_path
    ON operations(file_path);

-- history queries: most-recent-first for a given path
CREATE INDEX IF NOT EXISTS idx_operations_file_path_seq
    ON operations(file_path, commit_seq DESC);
```

### Migration

Implemented as a named migration (`v4_file_path`) tracked in the
`_hgp_migrations` table. The `_apply_migrations()` method in `db.py` guards
the `ALTER TABLE` with a column-existence check, making it idempotent on
fresh databases that already include the column in `_SCHEMA_SQL`.

```sql
-- _MIGRATION_FILE_PATH (applied once, guarded by _hgp_migrations)
ALTER TABLE operations ADD COLUMN file_path TEXT;
CREATE INDEX IF NOT EXISTS idx_operations_file_path ON operations(file_path);
CREATE INDEX IF NOT EXISTS idx_operations_file_path_seq
    ON operations(file_path, commit_seq DESC);
```

---

## New Module: `project.py`

### Purpose

File-scoped tools must validate that the target path is within a known project
root before performing any I/O. This prevents agents from accidentally writing
outside the working repository.

### Resolution order

1. `HGP_PROJECT_ROOT` environment variable (explicit override)
2. Walk up from the file's parent directory to find the nearest `.git` directory

Raises `ProjectRootError` if no root found; raises `PathOutsideRootError` if
the resolved path escapes the root (symlinks are followed before comparison).

### Why `.git` traversal?

`.git` is the universal, language-agnostic project boundary marker present in
every Git repository. It requires no per-project configuration.

---

## Internal Helper: `_record_file_op`

All write-class tools (`hgp_write_file`, `hgp_append_file`, `hgp_edit_file`,
`hgp_move_file`) share a single internal helper rather than duplicating the
CAS upload + `BEGIN IMMEDIATE` transaction logic. This ensures:

- Consistent `op_type = "artifact"` for all write operations
- Consistent `file_path` recording
- Consistent `evidence_refs` cap (max 10 refs per operation)
- Single rollback / error path

`hgp_delete_file` does not use `_record_file_op` because it records an
`"invalidation"` op type with no payload (deleted content is not stored in CAS).

---

## New MCP Tools (6)

### Tool count progression

| Version | Tools |
|---------|-------|
| V1 | 10 |
| V3 | 12 |
| V4 | 18 |

### `hgp_write_file` (Tool #14)

Write (create or overwrite) a file and record an `artifact` operation.

```
file_path      str           required — absolute path
content        str           required — full file content (UTF-8)
agent_id       str           required
reason         str | None    optional — defaults to "CREATE <path>"
parent_op_ids  list[str]     optional — causal parents
evidence_refs  list[dict]    optional — EvidenceRef list (max 10)
```

Returns: standard `_record_file_op` response (`op_id`, `status`, `commit_seq`,
`object_hash`, `chain_hash`).

Errors: `PROJECT_ROOT_NOT_FOUND`, `PATH_OUTSIDE_ROOT`

### `hgp_append_file` (Tool #15)

Append content to a file (creates it if absent) and record as `artifact`.
After appending, reads the full file bytes into CAS so the stored blob reflects
the complete post-append state.

```
file_path      str           required
content        str           required — content to append
agent_id       str           required
reason         str | None    optional — defaults to "APPEND <path>"
parent_op_ids  list[str]     optional
evidence_refs  list[dict]    optional
```

### `hgp_edit_file` (Tool #16)

Replace the **first and only** occurrence of `old_string` with `new_string`.
Returns `AMBIGUOUS_MATCH` if `old_string` appears more than once — callers must
provide enough context to make the target unique.

```
file_path      str    required
old_string     str    required — must appear exactly once
new_string     str    required
agent_id       str    required
reason         str | None
parent_op_ids  list[str]
evidence_refs  list[dict]
```

Errors: `PROJECT_ROOT_NOT_FOUND`, `PATH_OUTSIDE_ROOT`, `FILE_NOT_FOUND`,
`STRING_NOT_FOUND`, `AMBIGUOUS_MATCH`

### `hgp_delete_file` (Tool #17)

Delete a file and record an `invalidation` operation. The deleted content is
**not** stored in CAS — HGP only records that the file ceased to exist and why.

```
file_path       str           required
agent_id        str           required
previous_op_id  str | None    optional — op to invalidate (marks it INVALIDATED)
reason          str | None    optional — defaults to "DELETE <path>"
```

Returns: `{ "op_id": str }`

### `hgp_move_file` (Tool #18)

Move or rename a file. Records two operations atomically (best-effort):

1. An `invalidation` op on `old_path` (optionally invalidating `previous_op_id`)
2. An `artifact` op on `new_path` (content stored in CAS)

If the DB transaction for the new artifact fails, the rename is reversed
(`new_path.rename(old_path)`) to maintain filesystem consistency.

```
old_path        str           required
new_path        str           required
agent_id        str           required
previous_op_id  str | None    optional — op to invalidate
reason          str | None    optional — defaults to "MOVE old → new"
evidence_refs   list[dict]    optional — applied to the new artifact op
```

### `hgp_file_history` (Tool #13)

Return all operations recorded for a `file_path`, ordered most-recent-first
(by `commit_seq DESC`). Applies depth-decay access recording: the most recent
op receives `weight=1.0`, older ops decay geometrically.

```
file_path  str    required
limit      int    optional — default 50
```

Returns: `{ "file_path": str, "operations": list[dict] }`

---

## `hgp_query_operations` Extension

`file_path: str | None` filter added to the existing tool. When provided,
adds `AND file_path = ?` to the query. No schema change required — the
`idx_operations_file_path` index is used automatically.

---

## Enforcement Hook System

### Design goal

Agents using Claude Code or Gemini CLI may call native file tools (`Write`,
`Edit`, `replace`, etc.) that bypass HGP. The hook system intercepts these
calls and warns the agent to use the HGP-aware equivalents instead.

### Claude Code hook (`.claude/settings.json`)

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Write|Edit|MultiEdit",
        "hooks": [{ "type": "command", "command": "python .claude/hooks/pre_tool_use_hgp.py" }]
      }
    ]
  }
}
```

Hook script exits 0 (warn, allow) by default. Set `HGP_HOOK_BLOCK=1` to exit 2
(block), which causes Claude Code to reject the native tool call.

### Gemini CLI hook (`.gemini/settings.json`)

```json
{
  "hooks": {
    "BeforeTool": [
      {
        "matcher": "^(write_file|replace)$",
        "hooks": [{ "name": "hgp-enforcement", "type": "command", "command": "python .gemini/hooks/pre_tool_use_hgp.py" }]
      }
    ]
  }
}
```

### `HGP_HOOK_BLOCK` environment variable

| Value | Behaviour |
|-------|-----------|
| unset / `0` | Warn only — prints to stderr, exits 0, tool call proceeds |
| `1` | Block — exits 2, tool call is rejected by the agent runtime |

---

## Out of Scope

| Feature | Reason |
|---------|---------|
| Directory-level operations (`mkdir`, `rmdir`) | Directories have no content to CAS-store |
| Binary file diffing / delta CAS | Scope creep; full-blob CAS is sufficient for V4 |
| Automatic `previous_op_id` resolution | Agent has causal context; auto-lookup risks hallucination |
| Hook coverage for `Bash`-based file writes | `Bash` tool is too broad to intercept safely |
| Blocking mode as default | Backwards-compatible opt-in; forcing block would break workflows |

---

## Test Count

| Suite | Before V4 | After V4 |
|-------|-----------|----------|
| `test_server_tools.py` | 64 | 69 |
| `test_db.py` | 10 | 38 |
| `test_file_ops.py` | 0 | 22 (new) |
| `test_project.py` | 0 | 6 (new) |
| others | 85 | 85 |
| `test_models.py` | 15 | 15 |
| **Total** | **~159** | **~191** |
