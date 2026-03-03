"""Lease token management for HGP epoch validation."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from hgp.dag import compute_chain_hash
from hgp.db import Database
from hgp.models import Lease, LeaseStatus


class LeaseManager:
    def __init__(self, db: Database) -> None:
        self._db = db

    def acquire(
        self,
        agent_id: str,
        subgraph_root_op_id: str,
        ttl_seconds: int = 300,
    ) -> Lease:
        """Acquire a lease on a subgraph. Auto-releases any prior active lease."""
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=ttl_seconds)
        chain_hash = compute_chain_hash(self._db, subgraph_root_op_id)

        # Auto-release previous active lease for this agent+subgraph
        self._db.execute(
            """
            UPDATE leases SET status = 'RELEASED'
            WHERE agent_id = ? AND subgraph_root_op_id = ? AND status = 'ACTIVE'
            """,
            (agent_id, subgraph_root_op_id),
        )

        lease_id = str(uuid.uuid4())
        self._db.execute(
            """
            INSERT INTO leases (lease_id, agent_id, subgraph_root_op_id,
                                chain_hash, issued_at, expires_at, status)
            VALUES (?, ?, ?, ?, ?, ?, 'ACTIVE')
            """,
            (
                lease_id,
                agent_id,
                subgraph_root_op_id,
                chain_hash,
                now.isoformat(),
                expires_at.isoformat(),
            ),
        )
        self._db.commit()

        return Lease(
            lease_id=lease_id,
            agent_id=agent_id,
            subgraph_root_op_id=subgraph_root_op_id,
            chain_hash=chain_hash,
            issued_at=now,
            expires_at=expires_at,
            status=LeaseStatus.ACTIVE,
        )

    def validate(self, lease_id: str, extend: bool = True) -> dict[str, Any]:
        """Validate lease is still valid and chain_hash hasn't changed.

        Uses BEGIN IMMEDIATE to atomically check + extend TTL, preventing
        a race where two concurrent validates could both see valid and extend.
        """
        # Read-only pre-check (no lock) for fast rejection
        row = self._db.execute(
            "SELECT * FROM leases WHERE lease_id = ?", (lease_id,)
        ).fetchone()

        if not row:
            return {"valid": False, "reason": "LEASE_NOT_FOUND"}

        now = datetime.now(timezone.utc)
        expires_at = datetime.fromisoformat(row["expires_at"])

        if row["status"] != "ACTIVE" or now > expires_at:
            return {"valid": False, "reason": "LEASE_EXPIRED"}

        # Recompute chain_hash and conditionally extend under write lock
        self._db.begin_immediate()
        try:
            current_hash = compute_chain_hash(self._db, row["subgraph_root_op_id"])
            if current_hash != row["chain_hash"]:
                self._db.rollback()
                return {
                    "valid": False,
                    "reason": "CHAIN_STALE",
                    "current_chain_hash": current_hash,
                }

            issued_at = datetime.fromisoformat(row["issued_at"])
            original_ttl = int((expires_at - issued_at).total_seconds())
            new_expires = now + timedelta(seconds=original_ttl)
            if extend:
                self._db.execute(
                    "UPDATE leases SET expires_at = ? WHERE lease_id = ?",
                    (new_expires.isoformat(), lease_id),
                )
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise

        return {
            "valid": True,
            "chain_hash": current_hash,
            "expires_at": new_expires.isoformat(),
        }

    def release(self, lease_id: str) -> None:
        self._db.execute(
            "UPDATE leases SET status = 'RELEASED' WHERE lease_id = ?",
            (lease_id,),
        )
        self._db.commit()
