from hgp.models import (
    Operation, OpEdge, StoredObject, Lease, GitAnchor,
    OpType, OpStatus, EdgeType, LeaseStatus, ObjectStatus,
)
import uuid
from datetime import datetime, timedelta


def test_operation_defaults():
    op = Operation(op_type=OpType.ARTIFACT, agent_id="agent-1")
    assert op.status == OpStatus.COMPLETED  # Will fail until implemented
    assert op.commit_seq is None
    assert uuid.UUID(op.op_id)  # Valid UUID


def test_lease_model():
    now = datetime.utcnow()
    lease = Lease(
        agent_id="agent-1",
        subgraph_root_op_id=str(uuid.uuid4()),
        chain_hash="sha256:abc",
        expires_at=now + timedelta(minutes=5),
    )
    assert lease.status == LeaseStatus.ACTIVE
    assert uuid.UUID(lease.lease_id)
