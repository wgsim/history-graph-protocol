"""Shared test fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def tmp_dir(tmp_path: Path) -> Path:
    """Temporary directory for test isolation."""
    return tmp_path


@pytest.fixture
def hgp_dirs(tmp_path: Path) -> dict:
    """Create HGP directory structure."""
    content_dir = tmp_path / ".hgp_content"
    staging_dir = content_dir / ".staging"
    content_dir.mkdir()
    staging_dir.mkdir()
    db_path = tmp_path / "hgp.db"
    return {
        "root": tmp_path,
        "content_dir": content_dir,
        "staging_dir": staging_dir,
        "db_path": db_path,
    }
