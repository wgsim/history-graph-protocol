"""HGP error types."""

from __future__ import annotations


class HGPError(Exception):
    """Base HGP error."""
    code: str = "HGP_ERROR"


class ChainStaleError(HGPError):
    """Subgraph mutated concurrently — chain_hash mismatch."""
    code = "CHAIN_STALE"


class LeaseExpiredError(HGPError):
    """Lease token has expired."""
    code = "LEASE_EXPIRED"


class ParentNotFoundError(HGPError):
    """Referenced parent operation does not exist."""
    code = "PARENT_NOT_FOUND"


class BlobWriteError(HGPError):
    """CAS blob write failed (fsync/rename)."""
    code = "BLOB_WRITE_FAILED"


class InvalidHashError(HGPError):
    """Provided hash does not match computed hash."""
    code = "INVALID_HASH"


class PayloadTooLargeError(HGPError):
    """Payload exceeds 10 MB V1 limit."""
    code = "PAYLOAD_TOO_LARGE"
