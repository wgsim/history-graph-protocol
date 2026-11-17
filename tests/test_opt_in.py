"""Opt-in HGP: enablement detection, HGP_NOT_INITIALIZED gate, `hgp init`, hook preflight."""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

import hgp.server as server_module
from hgp.hooks import hgp_paths
from hgp.server import hgp_create_operation, hgp_query_operations


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


# ── Task 2: server refuses instead of creating .hgp/ ────────────────────────

@pytest.fixture
def unbound(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Run tools through the real _get_context() (no injected context)."""
    original = server_module._ctx
    server_module._ctx = None
    monkeypatch.delenv("HGP_PROJECT_ROOT", raising=False)
    monkeypatch.delenv("HGP_GLOBAL_MODE", raising=False)
    monkeypatch.chdir(tmp_path)
    yield
    if server_module._ctx is not None:
        server_module._ctx.db.close()
    server_module._ctx = original


def test_tool_refuses_when_not_initialized(unbound: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = git_init(tmp_path / "r")
    monkeypatch.chdir(repo)
    result = hgp_create_operation(op_type="hypothesis", agent_id="a")
    assert result["error"] == "HGP_NOT_INITIALIZED"
    assert "hgp init" in result["message"]
    assert not (repo / ".hgp").exists()


def test_non_git_dir_not_created(unbound: None, tmp_path: Path) -> None:
    assert hgp_query_operations()["error"] == "HGP_NOT_INITIALIZED"
    assert not (tmp_path / ".hgp").exists()


def test_existing_hgp_dir_stays_enabled(unbound: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = git_init(tmp_path / "r")
    (repo / ".hgp").mkdir()
    monkeypatch.chdir(repo)
    assert "op_id" in hgp_create_operation(op_type="hypothesis", agent_id="a")


def test_worktree_enabled_via_main_repo(
    unbound: None, linked: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, worktree = linked
    (repo / ".hgp").mkdir()
    monkeypatch.chdir(worktree)
    assert "op_id" in hgp_create_operation(op_type="hypothesis", agent_id="a")
    assert not (worktree / ".hgp").exists()


def test_enable_mid_session(unbound: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = git_init(tmp_path / "r")
    monkeypatch.chdir(repo)
    assert hgp_create_operation(op_type="hypothesis", agent_id="a")["error"] == "HGP_NOT_INITIALIZED"
    (repo / ".hgp").mkdir()
    assert "op_id" in hgp_create_operation(op_type="hypothesis", agent_id="a")


def test_global_mode_unaffected(unbound: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HGP_GLOBAL_MODE", "1")
    assert "op_id" in hgp_create_operation(op_type="hypothesis", agent_id="a")
    assert (home / ".hgp").is_dir()
