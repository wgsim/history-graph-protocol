# HGP Tools Reference

This document is the complete API reference for all 18 MCP tools exposed by the History Graph Protocol (HGP) server. Each tool is documented with its parameters, return values, error codes, and a minimal usage example. For setup and quick-start instructions, see the [README](../README.md).

---

## Table of Contents

1. [hgp_create_operation](#hgp_create_operation)
2. [hgp_query_operations](#hgp_query_operations)
3. [hgp_query_subgraph](#hgp_query_subgraph)
4. [hgp_acquire_lease](#hgp_acquire_lease)
5. [hgp_validate_lease](#hgp_validate_lease)
6. [hgp_release_lease](#hgp_release_lease)
7. [hgp_set_memory_tier](#hgp_set_memory_tier)
8. [hgp_get_artifact](#hgp_get_artifact)
9. [hgp_anchor_git](#hgp_anchor_git)
10. [hgp_reconcile](#hgp_reconcile)
11. [hgp_get_evidence](#hgp_get_evidence)
12. [hgp_get_citing_ops](#hgp_get_citing_ops)
13. [hgp_write_file](#hgp_write_file)
14. [hgp_append_file](#hgp_append_file)
15. [hgp_edit_file](#hgp_edit_file)
16. [hgp_delete_file](#hgp_delete_file)
17. [hgp_move_file](#hgp_move_file)
18. [hgp_file_history](#hgp_file_history)
19. [Error Code Reference](#error-code-reference)

---

## hgp_create_operation

### Description

Creates a new operation node in the history graph. This is the primary write entry point; every artifact, hypothesis, merge, or invalidation is recorded as an operation.

### Parameters

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `op_type` | `string` | Yes | — | Operation type. One of: `"artifact"`, `"hypothesis"`, `"merge"`, `"invalidation"` |
| `agent_id` | `string` | Yes | — | Identifier for the calling agent |
| `parent_op_ids` | `list[string]` | No | `null` | Op IDs of causal parent operations |
| `invalidates_op_ids` | `list[string]` | No | `null` | Op IDs to mark as `INVALIDATED` |
| `payload` | `string` | No | `null` | Base64-encoded binary content to store |
| `mime_type` | `string` | No | `null` | MIME type of `payload` |
| `lease_id` | `string` | No | `null` | Lease token; auto-released on success |
| `chain_hash` | `string` | No | `null` | Expected chain hash for optimistic concurrency check |
| `subgraph_root_op_id` | `string` | No | `null` | Root op used to compute `chain_hash` |
| `metadata` | `dict` | No | `null` | Free-form JSON metadata |
| `evidence_refs` | `list[dict]` | No | `null` | Evidence references (max 50). See [EvidenceRef schema](#evidenceref-schema) below |

#### EvidenceRef Schema

Each item in `evidence_refs` must conform to:

| Field | Type | Required | Constraints | Description |
|---|---|---|---|---|
| `op_id` | `string` | Yes | 1–128 chars, no whitespace | Op ID of the cited operation |
| `relation` | `string` | Yes | One of: `supports`, `refutes`, `context`, `method`, `source` | How the cited op relates to this one |
| `scope` | `string \| null` | No | Max 1024 chars | Which part of the cited op was used |
| `inference` | `string \| null` | No | Max 4096 chars | What conclusion was drawn from the cited op |

### Returns

```json
{
  "op_id": "string",
  "status": "string",
  "commit_seq": "integer",
  "object_hash": "string",
  "chain_hash": "string"
}
```

| Field | Description |
|---|---|
| `op_id` | Unique identifier assigned to the new operation |
| `status` | Initial status of the operation (e.g., `"COMPLETED"`) |
| `commit_seq` | Monotonically increasing sequence number of this commit |
| `object_hash` | Content-addressable hash of the stored payload (if any) |
| `chain_hash` | Current chain hash of the subgraph after this write |

### Error Codes

| Code | Condition |
|---|---|
| `INVALID_OP_TYPE` | `op_type` is not one of the four valid values |
| `CHAIN_STALE` | Supplied `chain_hash` does not match the current subgraph hash (concurrent modification detected) |
| `INVALID_EVIDENCE_REF` | One or more `evidence_refs` entries fail schema validation |
| `TOO_MANY_EVIDENCE_REFS` | More than 50 `evidence_refs` provided |
| `DUPLICATE_EVIDENCE_REF` | Two or more `evidence_refs` reference the same `op_id` |

Raises `ParentNotFoundError` if any `op_id` in `parent_op_ids` does not exist.

### Example

Request:
```json
{
  "op_type": "artifact",
  "agent_id": "analyst-1",
  "payload": "SGVsbG8gV29ybGQ=",
  "mime_type": "text/plain",
  "metadata": { "label": "initial draft" }
}
```

Response:
```json
{
  "op_id": "01HZ1234ABCD",
  "status": "COMPLETED",
  "commit_seq": 42,
  "object_hash": "sha256:abc123...",
  "chain_hash": "sha256:def456..."
}
```

---

## hgp_query_operations

### Description

Queries operation records by one or more filter criteria. When `op_id` is supplied, returns that single operation and records an access event (which may update its memory tier). Otherwise, returns a filtered list.

### Parameters

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `op_id` | `string` | No | `null` | Return the single operation with this ID |
| `agent_id` | `string` | No | `null` | Filter to operations created by this agent |
| `op_type` | `string` | No | `null` | Filter by operation type |
| `status` | `string` | No | `null` | Filter by status: `"PENDING"`, `"COMPLETED"`, `"INVALIDATED"`, `"MISSING_BLOB"` |
| `since_commit_seq` | `integer` | No | `null` | Return only operations with `commit_seq` greater than this value |
| `include_inactive` | `boolean` | No | `false` | Whether to include operations in the `inactive` memory tier |
| `limit` | `integer` | No | `100` | Maximum number of results to return |
| `file_path` | `string` | No | `null` | Filter to operations recorded for this file path (canonicalized before matching) |

### Returns

**V4 breaking change:** the response is now a wrapper object `{"operations": [...]}`, not a bare list.

```json
{"operations": [...]}
```

Detail level of each operation dict varies by `memory_tier`:

| Memory Tier | Fields Returned |
|---|---|
| `short_term` | Full fields |
| `long_term` | Summary fields |
| `inactive` | Stub only: `op_id`, `op_type`, `memory_tier` |

### Error Codes

| Code | Condition |
|---|---|
| `INVALID_STATUS` | `status` is not one of the four valid values |

### Example

Request:
```json
{
  "agent_id": "analyst-1",
  "op_type": "hypothesis",
  "limit": 10
}
```

Response:
```json
{
  "operations": [
    {
      "op_id": "01HZ1234ABCD",
      "op_type": "hypothesis",
      "status": "COMPLETED",
      "memory_tier": "short_term",
      "agent_id": "analyst-1",
      "commit_seq": 42
    }
  ]
}
```

---

## hgp_query_subgraph

### Description

Traverses the causal graph from a root operation and returns all reachable operations along with a chain hash computed over the entire subgraph. Evidence links are not traversed; only causal parent/child edges are followed.

### Parameters

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `root_op_id` | `string` | Yes | — | Starting node for the traversal |
| `direction` | `string` | No | `"ancestors"` | Traversal direction: `"ancestors"` or `"descendants"` |
| `max_depth` | `integer` | No | `50` | Maximum edge depth to traverse |
| `include_invalidated` | `boolean` | No | `false` | Whether to include `INVALIDATED` operations in results |

### Returns

```json
{
  "root_op_id": "string",
  "chain_hash": "string",
  "operations": [ "...list of operation dicts..." ]
}
```

| Field | Description |
|---|---|
| `root_op_id` | The root operation ID supplied in the request |
| `chain_hash` | Hash computed over the full reachable subgraph |
| `operations` | List of operation dicts for all reachable nodes |

### Error Codes

None defined. Returns an empty `operations` list if `root_op_id` is not found.

### Example

Request:
```json
{
  "root_op_id": "01HZ1234ABCD",
  "direction": "descendants",
  "max_depth": 5
}
```

Response:
```json
{
  "root_op_id": "01HZ1234ABCD",
  "chain_hash": "sha256:abc123...",
  "operations": [
    { "op_id": "01HZ1234ABCD", "op_type": "artifact", "status": "COMPLETED" },
    { "op_id": "01HZ5678EFGH", "op_type": "hypothesis", "status": "COMPLETED" }
  ]
}
```

---

## hgp_acquire_lease

### Description

Acquires an exclusive lease on a subgraph rooted at the specified operation. Returns a `chain_hash` snapshot that can be used for optimistic concurrency control during subsequent multi-step writes. Acquiring a lease promotes the root operation to `short_term` tier.

### Parameters

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `agent_id` | `string` | Yes | — | Identifier for the agent acquiring the lease |
| `subgraph_root_op_id` | `string` | Yes | — | Root operation of the subgraph to lease |
| `ttl_seconds` | `integer` | No | `300` | Lease lifetime in seconds; capped at `86400` (24 hours) |

### Returns

```json
{
  "lease_id": "string",
  "chain_hash": "string",
  "expires_at": "ISO 8601 timestamp"
}
```

| Field | Description |
|---|---|
| `lease_id` | Opaque token to use in subsequent calls |
| `chain_hash` | Subgraph hash at the moment the lease was acquired |
| `expires_at` | UTC timestamp when the lease expires |

### Error Codes

None defined. Returns an error dict if `subgraph_root_op_id` does not exist.

### Example

Request:
```json
{
  "agent_id": "analyst-1",
  "subgraph_root_op_id": "01HZ1234ABCD",
  "ttl_seconds": 600
}
```

Response:
```json
{
  "lease_id": "lease-abc-123",
  "chain_hash": "sha256:def456...",
  "expires_at": "2026-03-25T12:10:00Z"
}
```

---

## hgp_validate_lease

### Description

Checks whether a lease is still valid (not expired, not released). Optionally extends the TTL by the original lease duration.

### Parameters

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `lease_id` | `string` | Yes | — | The lease token to validate |
| `extend` | `boolean` | No | `true` | If `true`, extends the lease TTL by its original duration |

### Returns

On success:
```json
{ "valid": true, "chain_hash": "string", "expires_at": "ISO 8601 timestamp" }
```

On failure (not found):
```json
{ "valid": false, "reason": "LEASE_NOT_FOUND" }
```

On failure (expired or released):
```json
{ "valid": false, "reason": "LEASE_EXPIRED" }
```

On failure (concurrent modification detected):
```json
{ "valid": false, "reason": "CHAIN_STALE", "current_chain_hash": "string" }
```

### Error Codes

Returns an error dict (not an exception) when the lease is not found, expired, or the subgraph has been concurrently modified.

### Example

Request:
```json
{ "lease_id": "lease-abc-123", "extend": true }
```

Response:
```json
{ "valid": true, "chain_hash": "sha256:def456...", "expires_at": "2026-03-25T12:20:00Z" }
```

---

## hgp_release_lease

### Description

Explicitly releases a lease before it expires. If no other active leases remain on the subgraph, the root operation is demoted to `long_term` tier.

### Parameters

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `lease_id` | `string` | Yes | — | The lease token to release |

### Returns

```json
{ "released": true, "lease_id": "string" }
```

| Field | Description |
|---|---|
| `released` | Always `true`; the server marks the lease released regardless of prior state |
| `lease_id` | The lease ID from the request |

### Error Codes

None defined. The server always returns `{ "released": true, "lease_id": "..." }` regardless of whether the lease existed.

### Example

Request:
```json
{ "lease_id": "lease-abc-123" }
```

Response:
```json
{ "released": true, "lease_id": "lease-abc-123" }
```

---

## hgp_set_memory_tier

### Description

Manually sets the memory tier of an operation, controlling the level of detail returned by query tools and whether the operation is eligible for automated demotion.

### Parameters

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `op_id` | `string` | Yes | — | ID of the operation to update |
| `tier` | `string` | Yes | — | Target tier: `"short_term"`, `"long_term"`, or `"inactive"` |

### Returns

```json
{ "op_id": "string", "tier": "string" }
```

### Error Codes

| Code | Condition |
|---|---|
| `INVALID_TIER` | `tier` is not one of the three valid values |
| `OP_NOT_FOUND` | No operation exists with the given `op_id` |

### Example

Request:
```json
{ "op_id": "01HZ1234ABCD", "tier": "long_term" }
```

Response:
```json
{ "op_id": "01HZ1234ABCD", "tier": "long_term" }
```

---

## hgp_get_artifact

### Description

Retrieves a stored binary artifact by its content-addressable hash. The content is returned as a base64-encoded string.

### Parameters

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `object_hash` | `string` | Yes | — | Content hash of the artifact to retrieve (as returned in `hgp_create_operation`) |

### Returns

```json
{
  "object_hash": "string",
  "size": "integer",
  "content": "string (base64)"
}
```

| Field | Description |
|---|---|
| `object_hash` | Echo of the requested hash |
| `size` | Size of the artifact in bytes |
| `content` | Base64-encoded binary content |

### Error Codes

| Code | Condition |
|---|---|
| `NOT_FOUND` | No artifact exists for the given `object_hash` |

### Example

Request:
```json
{ "object_hash": "sha256:abc123..." }
```

Response:
```json
{
  "object_hash": "sha256:abc123...",
  "size": 11,
  "content": "SGVsbG8gV29ybGQ="
}
```

---

## hgp_anchor_git

### Description

Associates an HGP operation with a specific Git commit, creating a durable link between the history graph and a version-controlled repository state.

### Parameters

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `op_id` | `string` | Yes | — | HGP operation ID to anchor |
| `git_commit_sha` | `string` | Yes | — | Exactly 40 lowercase hexadecimal characters |
| `repository` | `string` | No | `null` | Repository identifier or URL (informational) |

### Returns

```json
{
  "anchored": true,
  "op_id": "string",
  "git_commit_sha": "string"
}
```

### Error Codes

| Code | Condition |
|---|---|
| `INVALID_SHA` | `git_commit_sha` is not exactly 40 lowercase hex characters |

### Example

Request:
```json
{
  "op_id": "01HZ1234ABCD",
  "git_commit_sha": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2",
  "repository": "github.com/org/repo"
}
```

Response:
```json
{
  "anchored": true,
  "op_id": "01HZ1234ABCD",
  "git_commit_sha": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2"
}
```

---

## hgp_reconcile

### Description

Runs a reconciliation pass over the operation store to detect and repair inconsistencies such as missing blobs, orphaned staging files, and operations eligible for demotion to `inactive` tier. Supports a dry-run mode that reports findings without applying changes.

### Parameters

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `dry_run` | `boolean` | No | `false` | If `true`, reports findings without making any changes |

### Returns

A ReconcileReport dict:

```json
{
  "missing_blobs": [ "...list of op_ids with missing content..." ],
  "orphan_candidates": [ "...list of orphaned staging paths..." ],
  "staging_cleaned": "integer",
  "skipped_young_blobs": "integer",
  "demoted_to_inactive": "integer",
  "errors": [ "...list of error strings..." ]
}
```

| Field | Description |
|---|---|
| `missing_blobs` | Op IDs whose payload blob could not be located |
| `orphan_candidates` | Staging paths with no associated operation |
| `staging_cleaned` | Number of orphan staging entries removed (0 in dry-run) |
| `skipped_young_blobs` | Blobs skipped because they are below the age threshold |
| `demoted_to_inactive` | Number of operations demoted to `inactive` tier (0 in dry-run) |
| `errors` | Non-fatal errors encountered during reconciliation |

### Error Codes

None defined. Internal errors are reported in the `errors` field of the response.

### Example

Request:
```json
{ "dry_run": true }
```

Response:
```json
{
  "missing_blobs": [],
  "orphan_candidates": ["staging/tmp-xyz"],
  "staging_cleaned": 0,
  "skipped_young_blobs": 3,
  "demoted_to_inactive": 0,
  "errors": []
}
```

---

## hgp_get_evidence

### Description

Returns all evidence references recorded on a given operation — that is, the operations that this operation cited when it was created.

### Parameters

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `op_id` | `string` | Yes | — | ID of the operation whose evidence to retrieve |

### Returns

A list of up to 200 EvidenceRecord dicts:

```json
[
  {
    "cited_op_id": "string",
    "op_type": "string",
    "status": "string",
    "memory_tier": "string",
    "relation": "string",
    "scope": "string | null",
    "inference": "string | null",
    "created_at": "ISO 8601 timestamp"
  }
]
```

| Field | Description |
|---|---|
| `cited_op_id` | Op ID of the operation that was cited as evidence |
| `op_type` | Type of the cited operation |
| `status` | Current status of the cited operation |
| `memory_tier` | Current memory tier of the cited operation |
| `relation` | Relationship type (e.g., `supports`, `refutes`) |
| `scope` | Which part of the cited op was used (may be null) |
| `inference` | Conclusion drawn from the cited op (may be null) |
| `created_at` | Timestamp when the evidence link was created |

### Error Codes

| Code | Condition |
|---|---|
| `OP_NOT_FOUND` | No operation exists with the given `op_id` |
| `DB_ERROR` | An internal database error occurred |

### Example

Request:
```json
{ "op_id": "01HZ5678EFGH" }
```

Response:
```json
[
  {
    "cited_op_id": "01HZ1234ABCD",
    "op_type": "artifact",
    "status": "COMPLETED",
    "memory_tier": "short_term",
    "relation": "supports",
    "scope": "section 2",
    "inference": "The data confirms the hypothesis.",
    "created_at": "2026-03-25T10:00:00Z"
  }
]
```

---

## hgp_get_citing_ops

### Description

Returns all operations that cited the given operation as evidence — the inverse of `hgp_get_evidence`. Useful for impact analysis when an operation is modified or invalidated.

### Parameters

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `op_id` | `string` | Yes | — | ID of the operation to find citations for |

### Returns

A list of up to 200 CitingRecord dicts:

```json
[
  {
    "citing_op_id": "string",
    "op_type": "string",
    "status": "string",
    "memory_tier": "string",
    "relation": "string",
    "scope": "string | null",
    "inference": "string | null",
    "created_at": "ISO 8601 timestamp"
  }
]
```

| Field | Description |
|---|---|
| `citing_op_id` | Op ID of the operation that cited this one |
| `op_type` | Type of the citing operation |
| `status` | Current status of the citing operation |
| `memory_tier` | Current memory tier of the citing operation |
| `relation` | Relationship type from the citing op's perspective |
| `scope` | Which part of this op was used by the citing op (may be null) |
| `inference` | Conclusion drawn by the citing op (may be null) |
| `created_at` | Timestamp when the citation was created |

### Error Codes

| Code | Condition |
|---|---|
| `OP_NOT_FOUND` | No operation exists with the given `op_id` |
| `DB_ERROR` | An internal database error occurred |

### Example

Request:
```json
{ "op_id": "01HZ1234ABCD" }
```

Response:
```json
[
  {
    "citing_op_id": "01HZ5678EFGH",
    "op_type": "hypothesis",
    "status": "COMPLETED",
    "memory_tier": "short_term",
    "relation": "supports",
    "scope": null,
    "inference": "Artifact supports the core claim.",
    "created_at": "2026-03-25T10:05:00Z"
  }
]
```

---

## File Tracking Tools (V4)

> **Prerequisites:** A project root must be discoverable — either via a `.git` directory in the file's ancestor directories, or via the `HGP_PROJECT_ROOT` environment variable. All file paths must be within the project root.

---

## hgp_write_file

### Description

Creates or overwrites a file and records the result as an `artifact` operation in HGP. Two-phase commit model: the HGP op is first committed to DB as `PENDING` (CAS store + DB insert), then the filesystem write is attempted, and only on success is the op finalized to `COMPLETED`. If the filesystem write fails, the tool returns a `FILESYSTEM_ERROR` and the op remains `PENDING` (visible but not complete). File paths are canonicalized (symlinks resolved, `.`/`..` normalized) before storage, so the same file always maps to one history entry regardless of how the path was expressed.

### Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `file_path` | string | ✓ | Absolute path of the file to write (must be within project root) |
| `content` | string | ✓ | UTF-8 content to write |
| `agent_id` | string | ✓ | Identifier of the calling agent |
| `reason` | string | | Human-readable reason for the write (default: `"CREATE <file_path>"`) |
| `parent_op_ids` | string[] | | Op IDs this operation causally depends on |
| `evidence_refs` | object[] | | Evidence citations (same schema as `hgp_create_operation`) |

### Returns

```json
{
  "op_id": "op-...",
  "status": "COMPLETED",
  "commit_seq": 42,
  "object_hash": "sha256:...",
  "chain_hash": "sha256:..."
}
```

| Field | Description |
|-------|-------------|
| `op_id` | Unique identifier assigned to the new artifact operation |
| `status` | `"COMPLETED"` when the filesystem write succeeded |
| `commit_seq` | Monotonically increasing sequence number of this commit |
| `object_hash` | SHA-256 hash of the stored content blob |
| `chain_hash` | Chain hash of the subgraph after this operation |

### Error Codes

| Code | Meaning |
|------|---------|
| `PATH_OUTSIDE_ROOT` | `file_path` is outside the project root |
| `PROJECT_ROOT_NOT_FOUND` | No `.git` directory found and `HGP_PROJECT_ROOT` not set |
| `PARENT_NOT_FOUND` | A `parent_op_ids` entry does not exist |
| `INVALID_EVIDENCE_REF` | An `evidence_refs` entry failed validation |
| `FILESYSTEM_ERROR` | HGP op committed as PENDING but the filesystem write failed (op remains PENDING) |
| `DB_FINALIZE_ERROR` | Filesystem write succeeded but post-write DB finalization failed; op remains PENDING, file has new content |

---

## hgp_append_file

### Description

Appends content to a file (creates it if it does not exist) and records the result as an `artifact` operation in HGP. The combined content (existing + appended) is computed in memory and committed to CAS and DB as `PENDING` before the filesystem write. The op is finalized to `COMPLETED` only after the append succeeds. Uses the same two-phase commit model as `hgp_write_file`.

### Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `file_path` | string | ✓ | Absolute path of the file to append to |
| `content` | string | ✓ | UTF-8 content to append |
| `agent_id` | string | ✓ | Identifier of the calling agent |
| `reason` | string | | Human-readable reason (default: `"APPEND <file_path>"`) |
| `parent_op_ids` | string[] | | Causal parent op IDs |
| `evidence_refs` | object[] | | Evidence citations |

### Returns

Same shape as `hgp_write_file`: `op_id`, `status`, `commit_seq`, `object_hash`, `chain_hash`.

### Error Codes

Same as `hgp_write_file` (including `FILESYSTEM_ERROR` and `DB_FINALIZE_ERROR`).

---

## hgp_edit_file

### Description

Replaces the first (and only) occurrence of `old_string` with `new_string` in a file and records the result as an `artifact` operation. The replacement is computed in memory, committed to CAS and DB as `PENDING`, and only then written to disk. The op is finalized to `COMPLETED` only after the disk write succeeds. If the disk write fails, the original file content is preserved and a `FILESYSTEM_ERROR` is returned. Uses the same two-phase commit model as `hgp_write_file`.

### Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `file_path` | string | ✓ | Absolute path of the file to edit |
| `old_string` | string | ✓ | Exact string to replace (must appear exactly once) |
| `new_string` | string | ✓ | Replacement string |
| `agent_id` | string | ✓ | Identifier of the calling agent |
| `reason` | string | | Human-readable reason (default: `"MODIFY <file_path>"`) |
| `parent_op_ids` | string[] | | Causal parent op IDs |
| `evidence_refs` | object[] | | Evidence citations |

### Returns

Same shape as `hgp_write_file`: `op_id`, `status`, `commit_seq`, `object_hash`, `chain_hash`.

### Error Codes

| Code | Meaning |
|------|---------|
| `FILE_NOT_FOUND` | `file_path` does not exist |
| `STRING_NOT_FOUND` | `old_string` not found in file |
| `AMBIGUOUS_MATCH` | `old_string` found more than once |
| `PATH_OUTSIDE_ROOT` | `file_path` is outside the project root |
| `PROJECT_ROOT_NOT_FOUND` | No `.git` directory found and `HGP_PROJECT_ROOT` not set |
| `FILESYSTEM_ERROR` | HGP op committed as PENDING but the filesystem write failed (op remains PENDING) |
| `DB_FINALIZE_ERROR` | Filesystem write succeeded but post-write DB finalization failed; op remains PENDING, file has new content |

---

## hgp_delete_file

### Description

Deletes a file and records an `invalidation` operation in HGP. Optionally marks a previous operation as `INVALIDATED`. Two-phase model: the invalidation op is committed to DB as `PENDING` (with an edge to `previous_op_id` if supplied, but **without** yet changing its status) before the filesystem unlink; only after a successful unlink is the op finalized to `COMPLETED` and `previous_op_id` marked `INVALIDATED`. If the unlink fails, `FILESYSTEM_ERROR` is returned, the op remains `PENDING`, the file is untouched, and the prior artifact is preserved as `COMPLETED`.

### Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `file_path` | string | ✓ | Absolute path of the file to delete |
| `agent_id` | string | ✓ | Identifier of the calling agent |
| `previous_op_id` | string | | Op ID of the last write/edit op for this file; will be marked INVALIDATED |
| `reason` | string | | Human-readable reason (default: `"DELETE <file_path>"`) |

### Returns

```json
{
  "op_id": "op-...",
  "status": "COMPLETED",
  "commit_seq": 42,
  "chain_hash": "sha256:..."
}
```

| Field | Description |
|-------|-------------|
| `op_id` | Unique identifier assigned to the invalidation operation |
| `status` | `"COMPLETED"` when the filesystem unlink succeeded |
| `commit_seq` | Monotonically increasing sequence number of this commit |
| `chain_hash` | Chain hash of the subgraph after this operation |

### Error Codes

| Code | Meaning |
|------|---------|
| `FILE_NOT_FOUND` | `file_path` does not exist |
| `PATH_OUTSIDE_ROOT` | `file_path` is outside the project root |
| `PROJECT_ROOT_NOT_FOUND` | No `.git` directory found and `HGP_PROJECT_ROOT` not set |
| `INVALID_PARENT_OP_ID` | `previous_op_id` was supplied but does not exist in HGP |
| `FILESYSTEM_ERROR` | HGP op committed as PENDING but the filesystem unlink failed (op remains PENDING, file preserved) |
| `DB_FINALIZE_ERROR` | Unlink succeeded but post-unlink DB finalization failed atomically; op remains PENDING, prior artifact remains COMPLETED |

---

## hgp_move_file

### Description

Moves or renames a file using a three-phase model:

1. **DB transaction (PENDING):** inserts an invalidation op for `old_path` with an edge to the prior artifact (but does **not** yet change the prior artifact's status), then inserts an artifact op for `new_path` causally linked to the invalidation op. Both ops are committed as `PENDING`.
2. **Filesystem rename:** `old_path` is renamed to `new_path`. If this fails, `FILESYSTEM_ERROR` is returned, both ops remain `PENDING`, and the prior old-path artifact is preserved as `COMPLETED`.
3. **Finalize:** on rename success, the prior old-path artifact is marked `INVALIDATED` and both new ops are finalized to `COMPLETED`.

If `previous_op_id` is omitted, the tool auto-resolves the most recent tracked op for `old_path`. Calling `hgp_file_history(old_path)` after a successful move will always show the invalidation event.

### Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `old_path` | string | ✓ | Source file path (must exist) |
| `new_path` | string | ✓ | Destination file path |
| `agent_id` | string | ✓ | Identifier of the calling agent |
| `previous_op_id` | string | | Op ID of the last op for `old_path`; will be marked INVALIDATED. When omitted the tool auto-resolves the most recent tracked op for `old_path`. |
| `reason` | string | | Human-readable reason (default: `"MOVE <old_path> → <new_path>"`) |
| `evidence_refs` | object[] | | Evidence citations for the new artifact op |

### Returns

```json
{
  "invalidation_op_id": "op-...",
  "op_id": "op-...",
  "status": "COMPLETED",
  "commit_seq": 42,
  "object_hash": "sha256:...",
  "chain_hash": "sha256:..."
}
```

`invalidation_op_id` is the op recorded for `old_path`; `op_id` is the new artifact op for `new_path`.

### Error Codes

| Code | Meaning |
|------|---------|
| `FILE_NOT_FOUND` | `old_path` does not exist |
| `PATH_OUTSIDE_ROOT` | `old_path` or `new_path` is outside the project root |
| `PROJECT_ROOT_NOT_FOUND` | No `.git` directory found and `HGP_PROJECT_ROOT` not set |
| `INVALID_PARENT_OP_ID` | `previous_op_id` was supplied but does not exist in HGP |
| `FILESYSTEM_ERROR` | Both ops committed as PENDING but the filesystem rename failed (ops remain PENDING, prior artifact preserved as COMPLETED) |
| `DB_FINALIZE_ERROR` | Rename succeeded but post-rename DB finalization failed atomically; ops remain PENDING, prior artifact remains COMPLETED |

---

## hgp_file_history

### Description

Returns the operation history for a given file path, ordered most-recent-first. Accessing history also updates memory tier access weights for the returned operations.

### Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `file_path` | string | ✓ | Absolute path of the file |
| `limit` | integer | | Maximum number of operations to return (default: 50) |

### Returns

```json
{
  "file_path": "/absolute/path/to/file.py",
  "operations": [
    {
      "op_id": "op-...",
      "op_type": "artifact",
      "agent_id": "agent-1",
      "file_path": "/absolute/path/to/file.py",
      ...
    }
  ]
}
```

Returns `{"file_path": "...", "operations": []}` when no operations exist for the path.

### Error Codes

None (unknown paths return empty list).

---

## Error Code Reference

The following table consolidates all error codes across all tools.

| Error Code | Tool(s) | Condition |
|---|---|---|
| `INVALID_OP_TYPE` | `hgp_create_operation` | `op_type` is not one of the four valid values |
| `CHAIN_STALE` | `hgp_create_operation` | Supplied `chain_hash` does not match the current subgraph hash |
| `INVALID_EVIDENCE_REF` | `hgp_create_operation` | One or more `evidence_refs` entries fail schema validation |
| `TOO_MANY_EVIDENCE_REFS` | `hgp_create_operation` | More than 50 `evidence_refs` provided |
| `DUPLICATE_EVIDENCE_REF` | `hgp_create_operation` | Two or more `evidence_refs` reference the same `op_id` |
| `INVALID_STATUS` | `hgp_query_operations` | `status` value is not one of `PENDING`, `COMPLETED`, `INVALIDATED`, `MISSING_BLOB` |
| `INVALID_TIER` | `hgp_set_memory_tier` | `tier` is not one of `short_term`, `long_term`, `inactive` |
| `OP_NOT_FOUND` | `hgp_set_memory_tier`, `hgp_get_evidence`, `hgp_get_citing_ops` | No operation exists with the given `op_id` |
| `NOT_FOUND` | `hgp_get_artifact` | No artifact exists for the given `object_hash` |
| `INVALID_SHA` | `hgp_anchor_git` | `git_commit_sha` is not exactly 40 lowercase hex characters |
| `DB_ERROR` | `hgp_get_evidence`, `hgp_get_citing_ops` | An internal database error occurred |
| `FILE_NOT_FOUND` | `hgp_edit_file`, `hgp_delete_file`, `hgp_move_file` | The target file does not exist on disk |
| `STRING_NOT_FOUND` | `hgp_edit_file` | `old_string` not found in file |
| `AMBIGUOUS_MATCH` | `hgp_edit_file` | `old_string` appears more than once |
| `PATH_OUTSIDE_ROOT` | V4 file tools | File path resolves outside the project root |
| `PROJECT_ROOT_NOT_FOUND` | V4 file tools | No `.git` directory found and `HGP_PROJECT_ROOT` not set |
| `INVALID_PARENT_OP_ID` | `hgp_delete_file`, `hgp_move_file` | `previous_op_id` supplied but does not exist in HGP |
