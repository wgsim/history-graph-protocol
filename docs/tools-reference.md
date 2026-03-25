# HGP Tools Reference

This document is the complete API reference for all 12 MCP tools exposed by the History Graph Protocol (HGP) server. Each tool is documented with its parameters, return values, error codes, and a minimal usage example. For setup and quick-start instructions, see the [README](../README.md).

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
13. [Error Code Reference](#error-code-reference)

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

### Returns

A list of operation dicts. Detail level varies by `memory_tier`:

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
[
  {
    "op_id": "01HZ1234ABCD",
    "op_type": "hypothesis",
    "status": "COMPLETED",
    "memory_tier": "short_term",
    "agent_id": "analyst-1",
    "commit_seq": 42
  }
]
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
