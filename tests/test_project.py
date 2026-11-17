import subprocess
from pathlib import Path

import pytest

from hgp.project import (
    PathOutsideRootError,
    ProjectRootError,
    assert_within_root,
    find_project_root,
    find_storage_root,
)


def test_find_root_via_git(tmp_path):
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    sub = tmp_path / "src" / "deep"
    sub.mkdir(parents=True)
    assert find_project_root(sub) == tmp_path


def test_find_root_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("HGP_PROJECT_ROOT", str(tmp_path))
    assert find_project_root(Path("/some/random/path")) == tmp_path


def test_find_root_env_invalid_path(tmp_path, monkeypatch):
    monkeypatch.setenv("HGP_PROJECT_ROOT", str(tmp_path / "nonexistent"))
    with pytest.raises(ProjectRootError):
        find_project_root(Path("/any/path"))


def test_find_root_not_found(tmp_path):
    with pytest.raises(ProjectRootError):
        find_project_root(tmp_path)


def test_find_storage_root_for_linked_worktree(tmp_path):
    main = tmp_path / "main"
    worktree = tmp_path / "worktree"
    main.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=main, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=main, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=main, check=True)
    (main / "tracked.txt").write_text("initial")
    subprocess.run(["git", "add", "tracked.txt"], cwd=main, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=main, check=True)
    subprocess.run(["git", "worktree", "add", "-q", str(worktree), "-b", "feature"], cwd=main, check=True)

    assert find_project_root(worktree) == worktree
    assert find_storage_root(worktree) == main


def test_find_storage_root_keeps_normal_repository(tmp_path):
    (tmp_path / ".git").mkdir()
    assert find_storage_root(tmp_path) == tmp_path


def test_find_storage_root_rejects_unverified_pointer(tmp_path):
    checkout = tmp_path / "checkout"
    fake_common = tmp_path / "not-a-repository"
    checkout.mkdir()
    (checkout / ".git").write_text(f"gitdir: {fake_common / 'worktrees' / 'fake'}\n")
    (fake_common / "worktrees" / "fake").mkdir(parents=True)
    (fake_common / "worktrees" / "fake" / "commondir").write_text("../..")
    (fake_common / "worktrees" / "fake" / "gitdir").write_text(str(checkout / ".git"))

    assert find_storage_root(checkout) == checkout


def test_find_storage_root_handles_symlink_loop(tmp_path):
    checkout = tmp_path / "checkout"
    loop = tmp_path / "loop"
    checkout.mkdir()
    loop.symlink_to(loop, target_is_directory=True)
    (checkout / ".git").write_text(f"gitdir: {loop}\n")

    assert find_storage_root(checkout) == checkout


def test_server_binds_worktree_files_to_shared_storage(tmp_path, monkeypatch):
    main = tmp_path / "main"
    worktree = tmp_path / "worktree"
    main.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=main, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=main, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=main, check=True)
    (main / "tracked.txt").write_text("initial")
    subprocess.run(["git", "add", "tracked.txt"], cwd=main, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=main, check=True)
    subprocess.run(["git", "worktree", "add", "-q", str(worktree), "-b", "feature"], cwd=main, check=True)

    import hgp.server as server

    original_context = server._ctx
    server._ctx = None
    monkeypatch.chdir(worktree)
    try:
        context = server._get_context()
        assert context.project_root == worktree
        assert context.storage_root == main
        assert context.db._path == main / ".hgp" / "hgp.db"
    finally:
        assert server._ctx is not None
        server._ctx.db.close()
        server._ctx = original_context


def test_path_within_root(tmp_path):
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    assert_within_root(tmp_path / "src" / "file.py", tmp_path)  # should not raise


def test_path_outside_root_rejected(tmp_path):
    with pytest.raises(PathOutsideRootError):
        assert_within_root(Path("/etc/passwd"), tmp_path)
