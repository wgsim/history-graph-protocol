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
        """Validate lease is still valid and chain_hash hasn't changed."""
        # Fast read-only pre-check to avoid acquiring the write lock for obvious rejects
        row = self._db.execute(
            "SELECT * FROM leases WHERE lease_id = ?", (lease_id,)
        ).fetchone()

        if not row:
            return {"valid": False, "reason": "LEASE_NOT_FOUND"}

        now = datetime.now(timezone.utc)
        expires_at = datetime.fromisoformat(row["expires_at"])

        if row["status"] != "ACTIVE" or now > expires_at:
            return {"valid": False, "reason": "LEASE_EXPIRED"}

        # Acquire write lock, then re-validate to close TOCTOU gap
        self._db.begin_immediate()
        try:
            # Re-fetch inside the lock: another thread may have released/expired the lease
            locked_row = self._db.execute(
                "SELECT * FROM leases WHERE lease_id = ?", (lease_id,)
            ).fetchone()
            now = datetime.now(timezone.utc)
            locked_expires_at = datetime.fromisoformat(locked_row["expires_at"])

            if locked_row["status"] != "ACTIVE" or now > locked_expires_at:
                self._db.rollback()
                return {"valid": False, "reason": "LEASE_EXPIRED"}

            current_hash = compute_chain_hash(self._db, locked_row["subgraph_root_op_id"])
            if current_hash != locked_row["chain_hash"]:
                self._db.rollback()
                return {
                    "valid": False,
                    "reason": "CHAIN_STALE",
                    "current_chain_hash": current_hash,
                }

            if extend:
                issued_at = datetime.fromisoformat(locked_row["issued_at"])
                original_ttl = int((locked_expires_at - issued_at).total_seconds())
                new_expires = now + timedelta(seconds=original_ttl)
                self._db.execute(
                    "UPDATE leases SET expires_at = ? WHERE lease_id = ?",
                    (new_expires.isoformat(), lease_id),
                )
                returned_expires = new_expires
            else:
                returned_expires = locked_expires_at

            self._db.commit()
        except Exception:
            self._db.rollback()
            raise

        return {
            "valid": True,
            "chain_hash": current_hash,
            "expires_at": returned_expires.isoformat(),
        }

    def release(self, lease_id: str) -> None:
        self._db.execute(
            "UPDATE leases SET status = 'RELEASED' WHERE lease_id = ?",
            (lease_id,),
        )
        self._db.commit()
