"""Tests for V4 file tracking tools."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import hgp.server as server_module
from hgp.db import Database
from hgp.cas import CAS
from hgp.lease import LeaseManager
from hgp.reconciler import Reconciler

from hgp.server import hgp_write_file, hgp_append_file


@pytest.fixture
def project(tmp_path):
    """Inject temp DB/CAS into server module globals and set up a fake git project root."""
    (tmp_path / ".git").mkdir()

    content_dir = tmp_path / ".hgp_content"
    content_dir.mkdir()

    db = Database(tmp_path / "hgp.db")
    db.initialize()
    cas = CAS(content_dir)
    lease_mgr = LeaseManager(db)
    reconciler = Reconciler(db, cas, content_dir)

    # Save originals
    orig = (
        server_module._db,
        server_module._cas,
        server_module._lease_mgr,
        server_module._reconciler,
    )

    # Patch globals
    server_module._db = db
    server_module._cas = cas
    server_module._lease_mgr = lease_mgr
    server_module._reconciler = reconciler

    # Also patch HGP_PROJECT_ROOT via env var by monkeypatching os.environ
    import os
    orig_env = os.environ.get("HGP_PROJECT_ROOT")
    os.environ["HGP_PROJECT_ROOT"] = str(tmp_path)

    yield tmp_path

    # Restore
    if orig_env is None:
        os.environ.pop("HGP_PROJECT_ROOT", None)
    else:
        os.environ["HGP_PROJECT_ROOT"] = orig_env

    server_module._db, server_module._cas, server_module._lease_mgr, server_module._reconciler = orig
    db.close()


def test_write_file_creates_file(project):
    target = project / "src" / "main.py"
    result = hgp_write_file(
        file_path=str(target),
        content="print('hello')",
        agent_id="agent-1",
    )
    assert "op_id" in result
    assert target.read_text() == "print('hello')"


def test_write_file_returns_only_op_id(project):
    target = project / "file.txt"
    result = hgp_write_file(file_path=str(target), content="x", agent_id="agent-1")
    assert set(result.keys()) == {"op_id"}


def test_write_file_creates_parent_dirs(project):
    target = project / "a" / "b" / "c" / "file.py"
    hgp_write_file(file_path=str(target), content="x", agent_id="agent-1")
    assert target.exists()


def test_write_file_outside_root_rejected(project):
    result = hgp_write_file(
        file_path="/etc/evil.txt", content="bad", agent_id="agent-1"
    )
    assert result.get("error") == "PATH_OUTSIDE_ROOT"


def test_write_file_default_reason_in_metadata(project):
    target = project / "readme.md"
    result = hgp_write_file(
        file_path=str(target), content="# Title", agent_id="agent-1"
    )
    db, _, _, _ = server_module._db, server_module._cas, server_module._lease_mgr, server_module._reconciler
    op = db.get_operation(result["op_id"])
    meta = json.loads(op["metadata"] or "{}")
    assert "CREATE" in meta.get("reason", "")


def test_append_file_adds_content(project):
    target = project / "log.txt"
    target.write_text("line1\n")
    result = hgp_append_file(
        file_path=str(target), content="line2\n", agent_id="agent-1"
    )
    assert "op_id" in result
    assert target.read_text() == "line1\nline2\n"


def test_append_nonexistent_file_creates_it(project):
    target = project / "new.txt"
    hgp_append_file(file_path=str(target), content="hello", agent_id="agent-1")
    assert target.read_text() == "hello"
