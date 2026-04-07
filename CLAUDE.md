# HGP — Agent Instructions

HGP (History Graph Protocol) is an MCP server connected to this session.
Use HGP tools to record every significant action and decision with its causal context.

## Core Rule

**Use HGP file tools instead of native Write/Edit tools whenever tracking matters.**

| Native tool | HGP equivalent | When to use HGP |
|---|---|---|
| `Write` | `hgp_write_file` | Creating or overwriting a tracked file |
| `Edit` | `hgp_edit_file` | Modifying a tracked file |
| `Bash` (append) | `hgp_append_file` | Appending to a tracked file |

When to use native tools instead: configuration files, temp files, files explicitly outside the project scope.

## What to Record

Record HGP operations for:

- **Artifact** — any produced output: file written, analysis result, generated content
- **Hypothesis** — a decision, plan, or conclusion ("I will refactor X because Y")
- **Invalidation** — superseding a prior operation ("previous approach was wrong because Z")
- **Merge** — combining multiple prior operations into a new result

## Minimal Recording Pattern

```
# Before writing a file, record the decision that led to it
decision = hgp_create_operation(
    op_type="hypothesis",
    agent_id="<your-agent-id>",
    metadata={"description": "why this file is being written"},
)

# Write the file — HGP records the artifact automatically
hgp_write_file(
    file_path="path/to/file.py",
    content="...",
    agent_id="<your-agent-id>",
    parent_op_ids=[decision["op_id"]],
)
```

## Leases (multi-step work)

Acquire a lease before any multi-step sequence that must be atomic:

```
lease = hgp_acquire_lease(agent_id="...", subgraph_root_op_id="...")
# ... do work ...
hgp_validate_lease(lease["lease_id"])  # extend if needed
hgp_release_lease(lease["lease_id"])   # release when done
```

## Agent ID Convention

Use a stable, descriptive agent ID per session or role:
- `"claude-code"` for Claude Code sessions
- `"claude-code:<task-slug>"` for specific tasks

## Querying History

```
# All operations on a file
hgp_file_history(file_path="src/hgp/server.py")

# Operations in a causal subgraph
hgp_query_subgraph(root_op_id="<op-id>", direction="ancestors")

# What evidence did a decision cite?
hgp_get_evidence(op_id="<op-id>")
```

## Storage

HGP stores its database at `<repo_root>/.hgp/` (gitignored). No setup needed — the server initializes on first tool call.
