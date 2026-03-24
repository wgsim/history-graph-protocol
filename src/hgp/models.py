"""HGP Core Types — Pydantic models for internal and MCP interface use."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class OpType(StrEnum):
    ARTIFACT = "artifact"
    HYPOTHESIS = "hypothesis"
    MERGE = "merge"
    INVALIDATION = "invalidation"


class OpStatus(StrEnum):
    PENDING = "PENDING"
    COMPLETED = "COMPLETED"
    INVALIDATED = "INVALIDATED"
    MISSING_BLOB = "MISSING_BLOB"


class EdgeType(StrEnum):
    CAUSAL = "causal"
    INVALIDATES = "invalidates"


class LeaseStatus(StrEnum):
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    RELEASED = "RELEASED"


class ObjectStatus(StrEnum):
    VALID = "VALID"
    MISSING_BLOB = "MISSING_BLOB"
    ORPHAN_CANDIDATE = "ORPHAN_CANDIDATE"


class Operation(BaseModel):
    op_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    op_type: OpType
    status: OpStatus = OpStatus.COMPLETED
    commit_seq: int | None = None
    agent_id: str
    object_hash: str | None = None
    chain_hash: str | None = None
    metadata: dict[str, Any] | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: datetime | None = None


class OpEdge(BaseModel):
    child_op_id: str
    parent_op_id: str
    edge_type: EdgeType = EdgeType.CAUSAL


class StoredObject(BaseModel):
    hash: str  # "sha256:<hex>"
    size: int
    mime_type: str | None = None
    status: ObjectStatus = ObjectStatus.VALID
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    gc_marked_at: datetime | None = None


class Lease(BaseModel):
    lease_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    agent_id: str
    subgraph_root_op_id: str
    chain_hash: str
    issued_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime
    status: LeaseStatus = LeaseStatus.ACTIVE


class GitAnchor(BaseModel):
    op_id: str
    git_commit_sha: str  # 40-char hex
    repository: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class MemoryTier(StrEnum):
    SHORT_TERM = "short_term"
    LONG_TERM = "long_term"
    INACTIVE = "inactive"


class ReconcileReport(BaseModel):
    missing_blobs: list[str] = Field(default_factory=list)
    orphan_candidates: list[str] = Field(default_factory=list)
    staging_cleaned: int = 0
    skipped_young_blobs: int = 0
    demoted_to_inactive: int = 0
    errors: list[str] = Field(default_factory=list)


# ── V3 Evidence Trail ─────────────────────────────────────────

class EvidenceRelation(StrEnum):
    SUPPORTS = "supports"
    REFUTES  = "refutes"
    CONTEXT  = "context"
    METHOD   = "method"
    SOURCE   = "source"


class EvidenceRef(BaseModel):
    op_id:     str = Field(min_length=1, max_length=128)
    relation:  EvidenceRelation
    scope:     str | None = Field(default=None, max_length=1024)
    inference: str | None = Field(default=None, max_length=4096)

    @field_validator("op_id")
    @classmethod
    def op_id_not_whitespace(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("op_id must not be whitespace-only")
        return stripped


class EvidenceRecord(BaseModel):
    cited_op_id:  str
    op_type:      str
    status:       str
    memory_tier:  str
    relation:     str
    scope:        str | None
    inference:    str | None
    created_at:   str


class CitingRecord(BaseModel):
    citing_op_id: str
    op_type:      str
    status:       str
    memory_tier:  str
    relation:     str
    scope:        str | None
    inference:    str | None
    created_at:   str
