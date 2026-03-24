import pytest

from hgp.models import (
    Operation, OpEdge, StoredObject, Lease, GitAnchor, ReconcileReport,
    OpType, OpStatus, EdgeType, LeaseStatus, ObjectStatus,
    EvidenceRelation, EvidenceRef, EvidenceRecord, CitingRecord,
)
import uuid
from datetime import datetime, timedelta, timezone


def test_operation_defaults():
    op = Operation(op_type=OpType.ARTIFACT, agent_id="agent-1")
    assert op.status == OpStatus.COMPLETED
    assert op.commit_seq is None
    assert uuid.UUID(op.op_id)  # Valid UUID


def test_lease_model():
    now = datetime.now(timezone.utc)
    lease = Lease(
        agent_id="agent-1",
        subgraph_root_op_id=str(uuid.uuid4()),
        chain_hash="sha256:abc",
        expires_at=now + timedelta(minutes=5),
    )
    assert lease.status == LeaseStatus.ACTIVE
    assert uuid.UUID(lease.lease_id)


def test_op_edge_defaults():
    edge = OpEdge(child_op_id="a", parent_op_id="b")
    assert edge.edge_type == EdgeType.CAUSAL


def test_stored_object_defaults():
    obj = StoredObject(hash="sha256:abc123", size=42)
    assert obj.status == ObjectStatus.VALID
    assert obj.mime_type is None
    assert obj.gc_marked_at is None


def test_reconcile_report_independent_lists():
    r1 = ReconcileReport()
    r2 = ReconcileReport()
    r1.missing_blobs.append("sha256:abc")
    assert r2.missing_blobs == []  # not shared


def test_op_type_string_equality():
    assert OpType.ARTIFACT == "artifact"
    assert OpStatus.COMPLETED == "COMPLETED"
    assert EdgeType.CAUSAL == "causal"


# ── V3 Evidence Trail Models ──────────────────────────────────

def test_evidence_relation_values():
    assert EvidenceRelation.SUPPORTS == "supports"
    assert EvidenceRelation.REFUTES == "refutes"
    assert EvidenceRelation.CONTEXT == "context"
    assert EvidenceRelation.METHOD == "method"
    assert EvidenceRelation.SOURCE == "source"


def test_evidence_ref_required_fields():
    ref = EvidenceRef(op_id="op-1", relation=EvidenceRelation.SUPPORTS)
    assert ref.scope is None
    assert ref.inference is None


def test_evidence_ref_invalid_relation():
    import pytest
    with pytest.raises(Exception):
        EvidenceRef(op_id="op-1", relation="invalid")


def test_evidence_record_fields():
    rec = EvidenceRecord(
        cited_op_id="op-2",
        op_type="artifact",
        status="COMPLETED",
        memory_tier="long_term",
        relation="supports",
        scope=None,
        inference="conclusion",
        created_at="2026-03-22T00:00:00.000Z",
    )
    assert rec.cited_op_id == "op-2"
    assert rec.inference == "conclusion"


def test_citing_record_fields():
    rec = CitingRecord(
        citing_op_id="op-3",
        op_type="hypothesis",
        status="COMPLETED",
        memory_tier="long_term",
        relation="context",
        scope="field.x",
        inference=None,
        created_at="2026-03-22T00:00:00.000Z",
    )
    assert rec.citing_op_id == "op-3"
    assert rec.scope == "field.x"


def test_evidence_ref_empty_op_id_rejected():
    """EvidenceRef with empty string op_id must fail validation."""
    with pytest.raises(Exception):
        EvidenceRef(op_id="", relation=EvidenceRelation.SUPPORTS)


def test_evidence_ref_whitespace_only_op_id_rejected():
    """EvidenceRef with whitespace-only op_id must fail the custom validator."""
    with pytest.raises(Exception):
        EvidenceRef(op_id="   ", relation=EvidenceRelation.SUPPORTS)


def test_evidence_ref_op_id_stripped():
    """EvidenceRef strips leading/trailing whitespace from op_id."""
    ref = EvidenceRef(op_id="  abc-123  ", relation=EvidenceRelation.SUPPORTS)
    assert ref.op_id == "abc-123"


def test_evidence_ref_op_id_max_length():
    """EvidenceRef with op_id exceeding max_length=128 must fail validation."""
    with pytest.raises(Exception):
        EvidenceRef(op_id="x" * 129, relation=EvidenceRelation.SUPPORTS)
