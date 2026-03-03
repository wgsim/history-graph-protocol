from hgp.models import (
    Operation, OpEdge, StoredObject, Lease, GitAnchor, ReconcileReport,
    OpType, OpStatus, EdgeType, LeaseStatus, ObjectStatus,
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
