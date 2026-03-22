from __future__ import annotations

import hashlib
import pytest
from pathlib import Path
from hgp.cas import CAS
from hgp.errors import PayloadTooLargeError


def test_store_and_read(hgp_dirs: dict):
    cas = CAS(hgp_dirs["content_dir"])
    payload = b"hello, world"
    obj_hash = cas.store(payload)
    assert obj_hash.startswith("sha256:")
    assert cas.read(obj_hash) == payload


def test_deduplication(hgp_dirs: dict):
    cas = CAS(hgp_dirs["content_dir"])
    payload = b"duplicate content"
    h1 = cas.store(payload)
    h2 = cas.store(payload)
    assert h1 == h2
    # Only one file on disk
    hex_hash = h1.removeprefix("sha256:")
    matches = list(hgp_dirs["content_dir"].rglob(hex_hash[2:]))
    assert len(matches) == 1


def test_hash_correctness(hgp_dirs: dict):
    cas = CAS(hgp_dirs["content_dir"])
    payload = b"test content"
    obj_hash = cas.store(payload)
    expected = "sha256:" + hashlib.sha256(payload).hexdigest()
    assert obj_hash == expected


def test_missing_blob_returns_none(hgp_dirs: dict):
    cas = CAS(hgp_dirs["content_dir"])
    result = cas.read("sha256:" + "a" * 64)
    assert result is None


def test_blob_exists(hgp_dirs: dict):
    cas = CAS(hgp_dirs["content_dir"])
    payload = b"exists test"
    obj_hash = cas.store(payload)
    assert cas.exists(obj_hash)
    assert not cas.exists("sha256:" + "b" * 64)


def test_payload_too_large(hgp_dirs: dict):
    cas = CAS(hgp_dirs["content_dir"])
    large = b"x" * (11 * 1024 * 1024)  # 11 MB
    with pytest.raises(PayloadTooLargeError):
        cas.store(large)


def test_list_all_blobs_with_mtime(hgp_dirs: dict):
    cas = CAS(hgp_dirs["content_dir"])
    cas.store(b"first")
    cas.store(b"second")
    blobs = list(cas.list_all_blobs_with_mtime())
    assert len(blobs) == 2
    assert all(h.startswith("sha256:") for h, _ in blobs)


# ── Security: C-1 Path traversal ────────────────────────────────────────────

def test_path_traversal_rejected(hgp_dirs: dict):
    """_hash_to_path must reject path traversal sequences."""
    cas = CAS(hgp_dirs["content_dir"])
    with pytest.raises(ValueError, match="Invalid object_hash"):
        cas.read("sha256:../../etc/passwd")


def test_invalid_hash_length_rejected(hgp_dirs: dict):
    """_hash_to_path must reject hashes that are not 64 hex chars."""
    cas = CAS(hgp_dirs["content_dir"])
    with pytest.raises(ValueError, match="Invalid object_hash"):
        cas.read("sha256:abc123")


def test_invalid_hash_uppercase_rejected(hgp_dirs: dict):
    """_hash_to_path must reject uppercase hex (sha256 output is always lowercase)."""
    cas = CAS(hgp_dirs["content_dir"])
    with pytest.raises(ValueError, match="Invalid object_hash"):
        cas.read("sha256:" + "A" * 64)
