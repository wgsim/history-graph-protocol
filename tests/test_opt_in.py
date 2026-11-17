"""Opt-in HGP: enablement detection, HGP_NOT_INITIALIZED gate, `hgp init`, hook preflight."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hgp.hooks import hgp_paths


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def git_init(path: Path) -> Path:
    path.mkdir(parents=True)
    _git("init", "-q", cwd=path)
    return path.resolve()


@pytest.fixture
def linked(tmp_path: Path) -> tuple[Path, Path]:
    repo = git_init(tmp_path / "repo")
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    (repo / "tracked.txt").write_text("initial")
    _git("add", "tracked.txt", cwd=repo)
    _git("commit", "-qm", "initial", cwd=repo)
    worktree = tmp_path / "worktree"
    _git("worktree", "add", "-q", str(worktree), "-b", "feature", cwd=repo)
    return repo, worktree.resolve()


# ── Task 1: hgp_enabled ─────────────────────────────────────────────────────

def test_hgp_enabled_false_without_hgp_dir(tmp_path: Path) -> None:
    repo = git_init(tmp_path / "r")
    assert not hgp_paths.hgp_enabled(repo)


def test_hgp_enabled_true_with_hgp_dir(tmp_path: Path) -> None:
    repo = git_init(tmp_path / "r")
    (repo / ".hgp").mkdir()
    (repo / "sub").mkdir()
    assert hgp_paths.hgp_enabled(repo / "sub")


def test_hgp_enabled_worktree_uses_main(linked: tuple[Path, Path]) -> None:
    repo, worktree = linked
    assert not hgp_paths.hgp_enabled(worktree)
    (repo / ".hgp").mkdir()
    assert hgp_paths.hgp_enabled(worktree)


def test_hgp_enabled_outside_git(tmp_path: Path) -> None:
    assert not hgp_paths.hgp_enabled(tmp_path)
