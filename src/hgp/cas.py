"""Content-Addressable Storage for HGP blobs."""

from __future__ import annotations

import hashlib
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Iterator

from hgp.errors import PayloadTooLargeError

MAX_PAYLOAD_BYTES = 10 * 1024 * 1024  # 10 MB V1 limit


class CAS:
    """WORM Content-Addressable blob store with 5-step crash-safe write path."""

    def __init__(self, content_dir: Path) -> None:
        self._content_dir = content_dir
        self._staging_dir = content_dir / ".staging"
        self._staging_dir.mkdir(parents=True, exist_ok=True)

    def store(self, payload: bytes) -> str:
        """Store payload, return 'sha256:<hex>'. Idempotent (WORM)."""
        if len(payload) > MAX_PAYLOAD_BYTES:
            raise PayloadTooLargeError(
                f"Payload {len(payload)} bytes exceeds {MAX_PAYLOAD_BYTES} byte limit"
            )

        # Step 1: Compute hash
        hex_hash = hashlib.sha256(payload).hexdigest()
        object_key = f"sha256:{hex_hash}"
        final_dir = self._content_dir / hex_hash[:2]
        final_path = final_dir / hex_hash[2:]

        # Fast path: already exists (deduplication)
        if final_path.exists():
            return object_key

        final_dir.mkdir(parents=True, exist_ok=True)
        staging_path = self._staging_dir / f"{uuid.uuid4()}.tmp"

        # Step 2: Write to staging + fsync file
        # IMPORTANT: fsync must be called on the write fd, not a re-opened O_RDONLY fd.
        with open(staging_path, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())

        # Step 3: Atomic rename
        try:
            os.rename(str(staging_path), str(final_path))
        except OSError:
            if final_path.exists():
                # Concurrent writer produced the same hash — idempotent success
                staging_path.unlink(missing_ok=True)
                return object_key
            raise

        # Step 4: fsync source and destination directories
        for dir_path in [self._staging_dir, final_dir]:
            dfd = os.open(str(dir_path), os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)

        return object_key

    def read(self, object_hash: str) -> bytes | None:
        """Read blob by hash. Returns None if missing."""
        path = self._hash_to_path(object_hash)
        if path.exists():
            return path.read_bytes()
        return None

    def exists(self, object_hash: str) -> bool:
        return self._hash_to_path(object_hash).exists()

    def list_all_blobs_with_mtime(self) -> Iterator[tuple[str, datetime]]:
        """Yield (object_hash, mtime) for all stored blobs."""
        for subdir in self._content_dir.iterdir():
            if subdir.name.startswith(".") or not subdir.is_dir():
                continue
            for blob_file in subdir.iterdir():
                if blob_file.is_file():
                    hex_hash = subdir.name + blob_file.name
                    mtime = datetime.fromtimestamp(blob_file.stat().st_mtime)
                    yield f"sha256:{hex_hash}", mtime

    def _hash_to_path(self, object_hash: str) -> Path:
        hex_hash = object_hash.removeprefix("sha256:")
        return self._content_dir / hex_hash[:2] / hex_hash[2:]
