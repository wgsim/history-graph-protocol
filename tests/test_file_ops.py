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

from hgp.server import hgp_write_file, hgp_append_file, hgp_edit_file, hgp_delete_file, hgp_move_file


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


def test_edit_replaces_string(project):
    target = project / "main.py"
    target.write_text("x = 1\ny = 2\n")
    result = hgp_edit_file(
        file_path=str(target),
        old_string="x = 1",
        new_string="x = 42",
        agent_id="agent-1",
    )
    assert "op_id" in result
    assert "x = 42" in target.read_text()
    assert "x = 1" not in target.read_text()


def test_edit_old_string_not_found(project):
    target = project / "main.py"
    target.write_text("hello world")
    result = hgp_edit_file(
        file_path=str(target),
        old_string="NOT_PRESENT",
        new_string="x",
        agent_id="agent-1",
    )
    assert result.get("error") == "STRING_NOT_FOUND"


def test_edit_ambiguous_match(project):
    target = project / "main.py"
    target.write_text("aa\naa\n")
    result = hgp_edit_file(
        file_path=str(target),
        old_string="aa",
        new_string="bb",
        agent_id="agent-1",
    )
    assert result.get("error") == "AMBIGUOUS_MATCH"


def test_edit_file_not_found(project):
    result = hgp_edit_file(
        file_path=str(project / "nonexistent.py"),
        old_string="x",
        new_string="y",
        agent_id="agent-1",
    )
    assert result.get("error") == "FILE_NOT_FOUND"


def test_edit_records_op_with_file_path(project):
    target = project / "code.py"
    target.write_text("a = 1")
    result = hgp_edit_file(
        file_path=str(target), old_string="a = 1",
        new_string="a = 2", agent_id="agent-1"
    )
    from hgp.server import _get_components
    db, _, _, _ = _get_components()
    op = db.get_operation(result["op_id"])
    assert op["file_path"] == str(target)


def test_delete_removes_file(project):
    target = project / "old.txt"
    target.write_text("content")
    write_result = hgp_write_file(str(target), "content", "agent-1")
    result = hgp_delete_file(
        file_path=str(target),
        previous_op_id=write_result["op_id"],
        agent_id="agent-1",
    )
    assert "op_id" in result
    assert not target.exists()


def test_delete_marks_previous_op_invalidated(project):
    target = project / "bye.txt"
    target.write_text("x")
    write_result = hgp_write_file(str(target), "x", "agent-1")
    hgp_delete_file(str(target), write_result["op_id"], "agent-1")
    from hgp.server import _get_components
    db, _, _, _ = _get_components()
    prev_op = db.get_operation(write_result["op_id"])
    assert prev_op["status"] == "INVALIDATED"


def test_delete_nonexistent_file_rejected(project):
    result = hgp_delete_file(
        file_path=str(project / "ghost.txt"),
        previous_op_id=None,
        agent_id="agent-1",
    )
    assert result.get("error") == "FILE_NOT_FOUND"


def test_move_file(project):
    src = project / "old.py"
    dst = project / "new.py"
    src.write_text("code")
    write_result = hgp_write_file(str(src), "code", "agent-1")
    result = hgp_move_file(
        old_path=str(src),
        new_path=str(dst),
        previous_op_id=write_result["op_id"],
        agent_id="agent-1",
    )
    assert "op_id" in result
    assert not src.exists()
    assert dst.read_text() == "code"


def test_move_records_new_path_op(project):
    src = project / "a.txt"
    dst = project / "b.txt"
    src.write_text("hello")
    write_result = hgp_write_file(str(src), "hello", "agent-1")
    result = hgp_move_file(str(src), str(dst), write_result["op_id"], "agent-1")
    from hgp.server import _get_components
    db, _, _, _ = _get_components()
    new_op = db.get_operation(result["op_id"])
    assert new_op["file_path"] == str(dst)
    assert new_op["op_type"] == "artifact"
    prev_op = db.get_operation(write_result["op_id"])
    assert prev_op["status"] == "INVALIDATED"


def test_move_file_outside_root_rejected(project):
    src = project / "a.txt"
    src.write_text("x")
    result = hgp_move_file(
        old_path=str(src),
        new_path="/etc/evil.txt",
        agent_id="agent-1",
    )
    assert result.get("error") == "PATH_OUTSIDE_ROOT"


def test_delete_outside_root_rejected(project):
    result = hgp_delete_file(
        file_path="/etc/passwd",
        previous_op_id=None,
        agent_id="agent-1",
    )
    assert result.get("error") in ("PATH_OUTSIDE_ROOT", "PROJECT_ROOT_NOT_FOUND")
