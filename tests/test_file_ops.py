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

from hgp.server import hgp_write_file, hgp_append_file, hgp_edit_file, hgp_delete_file, hgp_move_file, hgp_file_history


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


def test_write_file_returns_enriched_fields(project):
    target = project / "file.txt"
    result = hgp_write_file(file_path=str(target), content="x", agent_id="agent-1")
    assert {"op_id", "status", "commit_seq", "object_hash", "chain_hash"}.issubset(result.keys())
    assert result["status"] == "COMPLETED"


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
    db = server_module._db
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
    hgp_delete_file(str(target), "agent-1", previous_op_id=write_result["op_id"])
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
    result = hgp_move_file(str(src), str(dst), "agent-1", previous_op_id=write_result["op_id"])
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


def test_move_file_invalid_evidence_refs_rejected_before_rename(project):
    src = project / "source.txt"
    dst = project / "dest.txt"
    src.write_text("content")
    result = hgp_move_file(
        old_path=str(src),
        new_path=str(dst),
        agent_id="agent-1",
        evidence_refs=[{"op_id": "op-123", "relation": "INVALID_RELATION_VALUE"}],
    )
    assert result.get("error") == "INVALID_EVIDENCE_REF"
    # Filesystem must NOT have been changed.
    assert src.exists(), "source file must still exist after rejected move"
    assert not dst.exists(), "destination file must not exist after rejected move"


def test_delete_outside_root_rejected(project):
    result = hgp_delete_file(
        file_path="/etc/passwd",
        previous_op_id=None,
        agent_id="agent-1",
    )
    assert result.get("error") in ("PATH_OUTSIDE_ROOT", "PROJECT_ROOT_NOT_FOUND")


# ── Task 6: hgp_file_history and file_path filter ────────────────────────────

def test_file_history_returns_ops_in_order(project):
    target = project / "tracked.py"
    r1 = hgp_write_file(str(target), "v1", "agent-1")
    r2 = hgp_edit_file(str(target), "v1", "v2", "agent-1")
    result = hgp_file_history(file_path=str(target))
    op_ids = [op["op_id"] for op in result["operations"]]
    assert op_ids[0] == r2["op_id"]
    assert op_ids[1] == r1["op_id"]


def test_file_history_unknown_path_returns_empty(project):
    result = hgp_file_history(file_path=str(project / "unknown.py"))
    assert result["operations"] == []


def test_query_operations_filter_by_file_path(project):
    from hgp.server import hgp_query_operations
    target = project / "q.py"
    r = hgp_write_file(str(target), "x", "agent-1")
    result = hgp_query_operations(file_path=str(target))
    assert any(op["op_id"] == r["op_id"] for op in result["operations"])


# ── Task 1: Rollback / atomicity failure tests ────────────────────────────────

def test_write_file_invalid_evidence_does_not_leave_file_written(project):
    """If evidence_refs references a nonexistent op_id, DB insert fails.
    The file must NOT be left on disk (atomicity guarantee)."""
    target = project / "should_not_exist.txt"
    result = hgp_write_file(
        file_path=str(target),
        content="data",
        agent_id="agent-1",
        evidence_refs=[{"op_id": "nonexistent-uuid-aaaa", "relation": "supports"}],
    )
    assert "error" in result
    assert not target.exists(), "file must not exist after failed write"


def test_append_file_invalid_evidence_does_not_mutate_file(project):
    """If evidence_refs is invalid, append must not modify the original file."""
    target = project / "original.txt"
    target.write_text("original")
    result = hgp_append_file(
        file_path=str(target),
        content=" extra",
        agent_id="agent-1",
        evidence_refs=[{"op_id": "nonexistent-uuid-bbbb", "relation": "supports"}],
    )
    assert "error" in result
    assert target.read_text() == "original", "file content must be unchanged"


def test_edit_file_invalid_evidence_does_not_mutate_file(project):
    """If evidence_refs is invalid, edit must not apply the replacement."""
    target = project / "code.py"
    target.write_text("old content")
    result = hgp_edit_file(
        file_path=str(target),
        old_string="old content",
        new_string="new content",
        agent_id="agent-1",
        evidence_refs=[{"op_id": "nonexistent-uuid-cccc", "relation": "supports"}],
    )
    assert "error" in result
    assert target.read_text() == "old content", "file must still contain original content"


def test_delete_file_invalid_previous_op_id_preserves_file(project):
    """If previous_op_id references a nonexistent op, the file must NOT be deleted."""
    target = project / "keep_me.txt"
    target.write_text("precious data")
    result = hgp_delete_file(
        file_path=str(target),
        agent_id="agent-1",
        previous_op_id="nonexistent-uuid-dddd",
    )
    # Either return an error OR the file must still exist
    if "error" not in result:
        assert target.exists(), "file must still exist when previous_op_id is invalid"
    else:
        assert target.exists(), "file must still exist after rejected delete"


def test_write_file_canonical_path_stored(project):
    """Path with ./ component is stored and queried under the canonical absolute path."""
    target = project / "src" / "file.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    r = hgp_write_file(str(target), "hello", "agent-1")
    assert "op_id" in r
    # Query with a path that has ./ in it — should find the same op
    dotslash = str(project / "src" / "." / "file.py")
    history = hgp_file_history(file_path=dotslash)
    op_ids = [op["op_id"] for op in history["operations"]]
    assert r["op_id"] in op_ids, "same op must appear in history regardless of ./ in path"


def test_file_history_same_for_absolute_and_dotdot_path(project):
    """Path with ../ component resolves to same canonical key."""
    target = project / "a" / "b.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    r = hgp_write_file(str(target), "x", "agent-1")
    assert "op_id" in r
    # /project/a/../a/b.py should resolve to same file
    dotdot = str(project / "a" / ".." / "a" / "b.py")
    history = hgp_file_history(file_path=dotdot)
    op_ids = [op["op_id"] for op in history["operations"]]
    assert r["op_id"] in op_ids, "same op must appear for path with ../ component"


def test_move_file_without_previous_op_id_shows_old_path_history(project):
    """Moving without previous_op_id must still produce a history entry for the old path."""
    src = project / "original_loc.py"
    dst = project / "new_loc.py"
    src.write_text("code here")
    # Record the file via write so it has an op
    hgp_write_file(str(src), "code here", "agent-1")
    # Move without supplying previous_op_id
    move_result = hgp_move_file(
        old_path=str(src),
        new_path=str(dst),
        agent_id="agent-1",
        previous_op_id=None,
    )
    assert "op_id" in move_result, f"move failed: {move_result}"
    # Old path history must contain a move/invalidation event
    old_history = hgp_file_history(file_path=str(src))
    op_types = [op["op_type"] for op in old_history["operations"]]
    assert "invalidation" in op_types, (
        f"old path history must include an invalidation event after move; got {op_types}"
    )


# ── Follow-up review: Post-commit filesystem failure regression tests ─────────

def test_write_file_fs_failure_returns_error(project, monkeypatch):
    """If write_text raises after HGP commit, tool must return an error dict, not COMPLETED."""
    target = project / "write_fail.txt"

    def failing_write_text(self, *args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(Path, "write_text", failing_write_text)

    result = hgp_write_file(str(target), "content", "agent-1")
    assert "error" in result, f"expected error dict, got: {result}"
    assert result.get("status") != "COMPLETED"
    assert not target.exists()


def test_append_file_fs_failure_returns_error(project, monkeypatch):
    """If the append write raises after HGP commit, tool must return an error dict."""
    target = project / "append_fail.txt"
    target.write_text("original")

    original_open = Path.open
    def failing_open(self, mode="r", **kwargs):
        if "a" in str(mode):
            raise OSError("disk full")
        return original_open(self, mode, **kwargs)
    monkeypatch.setattr(Path, "open", failing_open)

    result = hgp_append_file(str(target), " extra", "agent-1")
    assert "error" in result, f"expected error dict, got: {result}"
    assert result.get("status") != "COMPLETED"
    assert target.read_text() == "original"


def test_edit_file_fs_failure_returns_error(project, monkeypatch):
    """If write_text raises after HGP commit for edit, tool must return an error dict."""
    target = project / "edit_fail.txt"
    target.write_text("old content")

    def failing_write_text(self, data, *args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(Path, "write_text", failing_write_text)

    result = hgp_edit_file(str(target), "old content", "new content", "agent-1")
    assert "error" in result, f"expected error dict, got: {result}"
    assert result.get("status") != "COMPLETED"
    assert target.read_text("utf-8") == "old content"


def test_delete_file_fs_failure_returns_error(project, monkeypatch):
    """If unlink raises after HGP commit, tool must return error, not silent COMPLETED."""
    target = project / "delete_fail.txt"
    target.write_text("keep me")

    def failing_unlink(self, *args, **kwargs):
        raise OSError("permission denied")
    monkeypatch.setattr(Path, "unlink", failing_unlink)

    result = hgp_delete_file(str(target), "agent-1")
    assert "error" in result, f"expected error dict, got: {result}"
    assert result.get("status") != "COMPLETED"
    assert target.exists(), "file must still exist after failed delete"


def test_move_file_fs_failure_returns_error(project, monkeypatch):
    """If rename raises after HGP commit, tool must return error, not silent COMPLETED."""
    src = project / "move_fail_src.py"
    dst = project / "move_fail_dst.py"
    src.write_text("content")
    hgp_write_file(str(src), "content", "agent-1")

    def failing_rename(self, *args, **kwargs):
        raise OSError("cross-device link")
    monkeypatch.setattr(Path, "rename", failing_rename)

    result = hgp_move_file(str(src), str(dst), "agent-1")
    assert "error" in result, f"expected error dict, got: {result}"
    assert result.get("status") != "COMPLETED"
    assert src.exists(), "source must still exist after failed move"
    assert not dst.exists(), "destination must not exist after failed move"


def test_delete_file_fs_failure_preserves_prior_op_status(project, monkeypatch):
    """Failed delete must not leave the prior successful artifact as INVALIDATED."""
    target = project / "prior_op_delete.txt"
    target.write_text("data")
    write_result = hgp_write_file(str(target), "data", "agent-1")
    prior_op_id = write_result["op_id"]

    def failing_unlink(self, *args, **kwargs):
        raise OSError("permission denied")
    monkeypatch.setattr(Path, "unlink", failing_unlink)

    result = hgp_delete_file(
        str(target), "agent-1", previous_op_id=prior_op_id
    )
    assert "error" in result, f"expected error dict, got: {result}"

    db = server_module._db
    prior_op = db.get_operation(prior_op_id)
    assert prior_op["status"] == "COMPLETED", (
        f"prior artifact must remain COMPLETED after failed delete, got: {prior_op['status']}"
    )


def test_move_file_fs_failure_preserves_prior_op_status(project, monkeypatch):
    """Failed move must not leave the prior old-path artifact as INVALIDATED."""
    src = project / "prior_op_src.py"
    dst = project / "prior_op_dst.py"
    src.write_text("content")
    write_result = hgp_write_file(str(src), "content", "agent-1")
    prior_op_id = write_result["op_id"]

    def failing_rename(self, *args, **kwargs):
        raise OSError("cross-device link")
    monkeypatch.setattr(Path, "rename", failing_rename)

    result = hgp_move_file(str(src), str(dst), "agent-1")
    assert "error" in result, f"expected error dict, got: {result}"

    db = server_module._db
    prior_op = db.get_operation(prior_op_id)
    assert prior_op["status"] == "COMPLETED", (
        f"prior artifact must remain COMPLETED after failed move, got: {prior_op['status']}"
    )


def test_delete_file_finalize_failure_no_partial_state(project, monkeypatch):
    """If post-unlink DB finalization fails, prior artifact must not be left INVALIDATED."""
    target = project / "finalize_fail_delete.txt"
    target.write_text("data")
    write_result = hgp_write_file(str(target), "data", "agent-1")
    prior_op_id = write_result["op_id"]

    db = server_module._db

    def failing_finalize(op_id):
        raise RuntimeError("simulated finalize failure")
    monkeypatch.setattr(db, "finalize_operation", failing_finalize)

    result = hgp_delete_file(str(target), "agent-1", previous_op_id=prior_op_id)
    assert "error" in result, f"expected error dict, got: {result}"

    prior_op = db.get_operation(prior_op_id)
    assert prior_op["status"] == "COMPLETED", (
        f"prior artifact must remain COMPLETED after failed finalize, got: {prior_op['status']}"
    )


def test_move_file_finalize_failure_no_partial_state(project, monkeypatch):
    """If post-rename DB finalization fails, prior old-path artifact must not be left INVALIDATED."""
    src = project / "finalize_fail_src.py"
    dst = project / "finalize_fail_dst.py"
    src.write_text("content")
    write_result = hgp_write_file(str(src), "content", "agent-1")
    prior_op_id = write_result["op_id"]

    db = server_module._db

    def failing_finalize(op_id):
        raise RuntimeError("simulated finalize failure")
    monkeypatch.setattr(db, "finalize_operation", failing_finalize)

    result = hgp_move_file(str(src), str(dst), "agent-1")
    assert "error" in result, f"expected error dict, got: {result}"

    prior_op = db.get_operation(prior_op_id)
    assert prior_op["status"] == "COMPLETED", (
        f"prior artifact must remain COMPLETED after failed finalize, got: {prior_op['status']}"
    )


# ── Schema lock tests for file-tool response shapes ───────────────────────────

def test_write_file_response_schema(project):
    """hgp_write_file must return the full enriched schema on success."""
    target = project / "schema_write.txt"
    result = hgp_write_file(str(target), "content", "agent-1")
    assert {"op_id", "status", "commit_seq", "object_hash", "chain_hash"}.issubset(result.keys())
    assert result["status"] == "COMPLETED"
    assert isinstance(result["commit_seq"], int)
    assert result["object_hash"].startswith("sha256:")
    assert result["chain_hash"].startswith("sha256:")


def test_append_file_response_schema(project):
    """hgp_append_file must return the full enriched schema on success."""
    target = project / "schema_append.txt"
    result = hgp_append_file(str(target), "line\n", "agent-1")
    assert {"op_id", "status", "commit_seq", "object_hash", "chain_hash"}.issubset(result.keys())
    assert result["status"] == "COMPLETED"


def test_edit_file_response_schema(project):
    """hgp_edit_file must return the full enriched schema on success."""
    target = project / "schema_edit.txt"
    target.write_text("old")
    hgp_write_file(str(target), "old", "agent-1")
    result = hgp_edit_file(str(target), "old", "new", "agent-1")
    assert {"op_id", "status", "commit_seq", "object_hash", "chain_hash"}.issubset(result.keys())
    assert result["status"] == "COMPLETED"


def test_delete_file_response_schema(project):
    """hgp_delete_file must return op_id, status, commit_seq, chain_hash on success."""
    target = project / "schema_delete.txt"
    target.write_text("bye")
    result = hgp_delete_file(str(target), "agent-1")
    assert {"op_id", "status", "commit_seq", "chain_hash"}.issubset(result.keys())
    assert result["status"] == "COMPLETED"
    assert "object_hash" not in result  # invalidation ops have no content blob
