"""Tests for `hgp mode` CLI and MCP tool mode gating."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from hgp.server import _check_mode, _mode, _read_mode


# ── helpers ───────────────────────────────────────────────────

def _make_git_repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    return tmp_path


def _write_mode(repo: Path, mode: str) -> None:
    mode_file = repo / ".hgp" / "mode"
    mode_file.parent.mkdir(parents=True, exist_ok=True)
    mode_file.write_text(mode)


# ── _read_mode ────────────────────────────────────────────────

def test_mode_default_is_on(tmp_path):
    repo = _make_git_repo(tmp_path)
    with patch("hgp.server._get_context") as mock_ctx:
        mock_ctx.return_value.project_root = repo
        result = _read_mode()
    assert result == "on"


def test_mode_reads_advisory(tmp_path):
    repo = _make_git_repo(tmp_path)
    _write_mode(repo, "advisory")
    with patch("hgp.server._get_context") as mock_ctx:
        mock_ctx.return_value.project_root = repo
        result = _read_mode()
    assert result == "advisory"


def test_mode_reads_off(tmp_path):
    repo = _make_git_repo(tmp_path)
    _write_mode(repo, "off")
    with patch("hgp.server._get_context") as mock_ctx:
        mock_ctx.return_value.project_root = repo
        result = _read_mode()
    assert result == "off"


def test_mode_invalid_file_content_defaults_on(tmp_path):
    repo = _make_git_repo(tmp_path)
    _write_mode(repo, "unknown_value")
    with patch("hgp.server._get_context") as mock_ctx:
        mock_ctx.return_value.project_root = repo
        result = _read_mode()
    assert result == "on"


def test_mode_no_project_root_defaults_on():
    with patch("hgp.server._get_context") as mock_ctx:
        mock_ctx.return_value.project_root = None
        result = _read_mode()
    assert result == "on"


# ── _check_mode ───────────────────────────────────────────────

def test_check_mode_on_mutation_passes(tmp_path):
    repo = _make_git_repo(tmp_path)
    with patch("hgp.server._get_context") as mock_ctx:
        mock_ctx.return_value.project_root = repo
        assert _check_mode(mutation=True) is None


def test_check_mode_advisory_mutation_blocked(tmp_path):
    repo = _make_git_repo(tmp_path)
    _write_mode(repo, "advisory")
    with patch("hgp.server._get_context") as mock_ctx:
        mock_ctx.return_value.project_root = repo
        result = _check_mode(mutation=True)
    assert result is not None
    assert result["status"] == "HGP_ADVISORY"


def test_check_mode_advisory_query_passes(tmp_path):
    repo = _make_git_repo(tmp_path)
    _write_mode(repo, "advisory")
    with patch("hgp.server._get_context") as mock_ctx:
        mock_ctx.return_value.project_root = repo
        assert _check_mode(mutation=False) is None


def test_check_mode_off_mutation_blocked(tmp_path):
    repo = _make_git_repo(tmp_path)
    _write_mode(repo, "off")
    with patch("hgp.server._get_context") as mock_ctx:
        mock_ctx.return_value.project_root = repo
        result = _check_mode(mutation=True)
    assert result is not None
    assert result["status"] == "HGP_DISABLED"


def test_check_mode_off_query_blocked(tmp_path):
    repo = _make_git_repo(tmp_path)
    _write_mode(repo, "off")
    with patch("hgp.server._get_context") as mock_ctx:
        mock_ctx.return_value.project_root = repo
        result = _check_mode(mutation=False)
    assert result is not None
    assert result["status"] == "HGP_DISABLED"


# ── _mode CLI ─────────────────────────────────────────────────

def _run(args: list[str], cwd: Path) -> tuple[int, str, str]:
    """Run _mode() and capture stdout/stderr + exit code."""
    import io
    from contextlib import redirect_stdout, redirect_stderr
    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    exit_code = 0
    with patch("hgp.server.find_project_root", return_value=cwd):
        try:
            with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
                _mode(args)
        except SystemExit as e:
            exit_code = e.code
    return exit_code, stdout_buf.getvalue(), stderr_buf.getvalue()


def test_cli_mode_default_shows_on(tmp_path):
    repo = _make_git_repo(tmp_path)
    rc, out, _ = _run([], repo)
    assert rc == 0
    assert out.strip() == "on"


def test_cli_mode_set_advisory(tmp_path):
    repo = _make_git_repo(tmp_path)
    rc, out, _ = _run(["advisory"], repo)
    assert rc == 0
    assert "advisory" in out
    assert (repo / ".hgp" / "mode").read_text() == "advisory"


def test_cli_mode_set_off(tmp_path):
    repo = _make_git_repo(tmp_path)
    rc, out, _ = _run(["off"], repo)
    assert rc == 0
    assert (repo / ".hgp" / "mode").read_text() == "off"


def test_cli_mode_restore_on(tmp_path):
    repo = _make_git_repo(tmp_path)
    _write_mode(repo, "off")
    rc, out, _ = _run(["on"], repo)
    assert rc == 0
    assert (repo / ".hgp" / "mode").read_text() == "on"


def test_cli_mode_shows_current(tmp_path):
    repo = _make_git_repo(tmp_path)
    _write_mode(repo, "advisory")
    rc, out, _ = _run([], repo)
    assert rc == 0
    assert out.strip() == "advisory"


def test_cli_mode_invalid_arg(tmp_path):
    repo = _make_git_repo(tmp_path)
    rc, _, err = _run(["badarg"], repo)
    assert rc == 1
    assert "invalid argument" in err


def test_cli_mode_too_many_args(tmp_path):
    repo = _make_git_repo(tmp_path)
    rc, _, err = _run(["on", "off"], repo)
    assert rc == 1


def test_cli_mode_no_git_repo(tmp_path, capsys):
    from hgp.project import ProjectRootError
    with patch("hgp.server.find_project_root", side_effect=ProjectRootError("no git")):
        with pytest.raises(SystemExit) as exc:
            _mode([])
    assert exc.value.code == 1
