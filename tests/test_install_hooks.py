"""Tests for `hgp install-hooks` CLI subcommand."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def _run(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "hgp.server"] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
    )


def _make_git_repo(path: Path) -> Path:
    """Create a minimal git repo at path and return it."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    return path


# ---------------------------------------------------------------------------
# install destination
# ---------------------------------------------------------------------------


def test_install_hooks_from_repo_root(tmp_path: Path) -> None:
    repo = _make_git_repo(tmp_path / "repo")
    result = _run(["install-hooks"], cwd=repo)
    assert result.returncode == 0
    assert (repo / ".claude" / "hooks" / "pre_tool_use_hgp.py").exists()
    assert (repo / ".gemini" / "hooks" / "pre_tool_use_hgp.py").exists()


def test_install_hooks_from_subdirectory_lands_in_repo_root(tmp_path: Path) -> None:
    repo = _make_git_repo(tmp_path / "repo")
    subdir = repo / "src" / "nested"
    subdir.mkdir(parents=True)
    result = _run(["install-hooks"], cwd=subdir)
    assert result.returncode == 0
    # hooks must be at repo root, not inside subdir
    assert (repo / ".claude" / "hooks" / "pre_tool_use_hgp.py").exists()
    assert not (subdir / ".claude").exists()


# ---------------------------------------------------------------------------
# selective install
# ---------------------------------------------------------------------------


def test_install_hooks_claude_only(tmp_path: Path) -> None:
    repo = _make_git_repo(tmp_path / "repo")
    result = _run(["install-hooks", "--claude"], cwd=repo)
    assert result.returncode == 0
    assert (repo / ".claude" / "hooks" / "pre_tool_use_hgp.py").exists()
    assert not (repo / ".gemini").exists()


def test_install_hooks_gemini_only(tmp_path: Path) -> None:
    repo = _make_git_repo(tmp_path / "repo")
    result = _run(["install-hooks", "--gemini"], cwd=repo)
    assert result.returncode == 0
    assert (repo / ".gemini" / "hooks" / "pre_tool_use_hgp.py").exists()
    assert not (repo / ".claude").exists()


# ---------------------------------------------------------------------------
# error handling
# ---------------------------------------------------------------------------


def test_install_hooks_invalid_flag_exits_nonzero(tmp_path: Path) -> None:
    repo = _make_git_repo(tmp_path / "repo")
    result = _run(["install-hooks", "--bogus"], cwd=repo)
    assert result.returncode != 0
    assert "unknown flag" in result.stderr
    assert not (repo / ".claude").exists()
    assert not (repo / ".gemini").exists()


def test_install_hooks_outside_git_repo_exits_nonzero(tmp_path: Path) -> None:
    no_repo = tmp_path / "no_repo"
    no_repo.mkdir()
    result = _run(["install-hooks"], cwd=no_repo)
    assert result.returncode != 0
    assert "no git repository" in result.stderr
