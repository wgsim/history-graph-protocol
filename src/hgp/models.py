"""HGP Core Types — Pydantic models for internal and MCP interface use."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


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
    created_at: datetime = Field(default_factory=datetime.utcnow)
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
    created_at: datetime = Field(default_factory=datetime.utcnow)
    gc_marked_at: datetime | None = None


class Lease(BaseModel):
    lease_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    agent_id: str
    subgraph_root_op_id: str
    chain_hash: str
    issued_at: datetime = Field(default_factory=datetime.utcnow)
    expires_at: datetime
    status: LeaseStatus = LeaseStatus.ACTIVE


class GitAnchor(BaseModel):
    op_id: str
    git_commit_sha: str  # 40-char hex
    repository: str | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ReconcileReport(BaseModel):
    missing_blobs: list[str] = Field(default_factory=list)
    orphan_candidates: list[str] = Field(default_factory=list)
    staging_cleaned: int = 0
    skipped_young_blobs: int = 0
    errors: list[str] = Field(default_factory=list)
