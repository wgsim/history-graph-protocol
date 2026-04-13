from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

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


# ── Phase 2: Task 2.5 — concurrent stat() ENOENT is silently skipped ─────────


def test_list_all_blobs_skips_file_not_found_on_stat(hgp_dirs: dict, monkeypatch):
    """list_all_blobs_with_mtime must skip blobs that disappear between glob and stat.

    This simulates a race where another process deletes a blob file after the
    directory is listed but before stat() is called on it.  The iterator must
    yield the surviving blobs and skip the missing one without raising.
    """
    cas = CAS(hgp_dirs["content_dir"])
    cas.store(b"blob-a")
    cas.store(b"blob-b")

    # Track stat() call counts per blob path.
    # Blob files: 62-char name inside a 2-char subdir.
    # is_file() calls stat() first (call 1); the explicit blob_file.stat() is call 2.
    # We raise only on call 2 to simulate TOCTOU: file exists at is_file() but
    # disappears before the explicit mtime stat.
    call_counts: dict[str, int] = {}
    target_path: list[str | None] = [None]
    original_stat = Path.stat

    def patched_stat(self, *args, **kwargs):
        real = original_stat(self, *args, **kwargs)
        if len(self.name) == 62 and len(self.parent.name) == 2:
            key = str(self)
            if target_path[0] is None:
                target_path[0] = key  # pick first blob as the race target
            if key == target_path[0]:
                call_counts[key] = call_counts.get(key, 0) + 1
                if call_counts[key] >= 2:
                    raise FileNotFoundError("simulated concurrent deletion")
        return real

    monkeypatch.setattr(Path, "stat", patched_stat)

    blobs = list(cas.list_all_blobs_with_mtime())
    # One blob disappeared mid-iteration → only one should be yielded
    assert len(blobs) == 1
    assert blobs[0][0].startswith("sha256:")


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
