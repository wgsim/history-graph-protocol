from pathlib import Path

import pytest

from hgp.project import (
    PathOutsideRootError,
    ProjectRootError,
    assert_within_root,
    find_project_root,
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


def test_path_within_root(tmp_path):
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    assert_within_root(tmp_path / "src" / "file.py", tmp_path)  # should not raise


def test_path_outside_root_rejected(tmp_path):
    with pytest.raises(PathOutsideRootError):
        assert_within_root(Path("/etc/passwd"), tmp_path)
