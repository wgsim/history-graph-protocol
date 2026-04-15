"""Tests for `hgp backup/restore/export/import` CLI subcommands."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(
    args: list[str],
    cwd: Path,
    projects_dir: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if projects_dir is not None:
        env["HGP_PROJECTS_DIR"] = str(projects_dir)
    return subprocess.run(
        [sys.executable, "-m", "hgp.server"] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
        env=env,
    )


def _make_git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    return path.resolve()


def _seed_hgp(repo: Path) -> None:
    """Create a minimal .hgp/ with a valid SQLite database and CAS dir."""
    hgp = repo / ".hgp"
    hgp.mkdir(exist_ok=True)
    db_path = hgp / "hgp.db"
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS ops (id TEXT PRIMARY KEY, data TEXT)")
        conn.execute("INSERT INTO ops VALUES ('test-op', 'hello')")
        conn.commit()
    (hgp / ".hgp_content").mkdir(exist_ok=True)
    (hgp / ".hgp_content" / "blob.txt").write_text("cas content")
    (hgp / "mode").write_text("on")
    (hgp / "hook-policy").write_text("advisory")


# ---------------------------------------------------------------------------
# backup
# ---------------------------------------------------------------------------


def test_backup_creates_snapshot(tmp_path: Path) -> None:
    repo = _make_git_repo(tmp_path / "repo")
    projects = tmp_path / "projects"
    _seed_hgp(repo)

    result = _run(["backup"], cwd=repo, projects_dir=projects)
    assert result.returncode == 0, result.stderr

    # project-meta must have been created
    meta = json.loads((repo / ".hgp" / "project-meta").read_text())
    pid = meta["project_id"]

    backup_dir = projects / pid
    assert backup_dir.is_dir()
    assert (backup_dir / "hgp.db").exists()
    assert (backup_dir / ".hgp_content").is_dir()
    assert (backup_dir / "project-meta").exists()


def test_backup_writes_project_meta(tmp_path: Path) -> None:
    repo = _make_git_repo(tmp_path / "repo")
    projects = tmp_path / "projects"
    _seed_hgp(repo)

    _run(["backup"], cwd=repo, projects_dir=projects)

    meta = json.loads((repo / ".hgp" / "project-meta").read_text())
    assert "project_id" in meta
    assert "repo_name" in meta
    assert "hgp_version" in meta


def test_backup_excludes_operational_files(tmp_path: Path) -> None:
    repo = _make_git_repo(tmp_path / "repo")
    projects = tmp_path / "projects"
    _seed_hgp(repo)

    _run(["backup"], cwd=repo, projects_dir=projects)

    meta = json.loads((repo / ".hgp" / "project-meta").read_text())
    pid = meta["project_id"]
    backup_dir = projects / pid

    assert not (backup_dir / "mode").exists()
    assert not (backup_dir / "hook-policy").exists()


def test_backup_uses_sqlite_api(tmp_path: Path) -> None:
    """Backup DB must be a valid readable SQLite file (not a partial WAL copy)."""
    repo = _make_git_repo(tmp_path / "repo")
    projects = tmp_path / "projects"
    _seed_hgp(repo)

    _run(["backup"], cwd=repo, projects_dir=projects)

    meta = json.loads((repo / ".hgp" / "project-meta").read_text())
    backup_db = projects / meta["project_id"] / "hgp.db"

    with sqlite3.connect(str(backup_db)) as conn:
        rows = conn.execute("SELECT id FROM ops").fetchall()
    assert ("test-op",) in rows


def test_backup_force_overwrites(tmp_path: Path) -> None:
    repo = _make_git_repo(tmp_path / "repo")
    projects = tmp_path / "projects"
    _seed_hgp(repo)

    _run(["backup"], cwd=repo, projects_dir=projects)
    result = _run(["backup"], cwd=repo, projects_dir=projects)
    assert result.returncode != 0
    assert "already exists" in result.stderr

    result = _run(["backup", "--force"], cwd=repo, projects_dir=projects)
    assert result.returncode == 0


def test_backup_outside_repo_exits_nonzero(tmp_path: Path) -> None:
    no_repo = tmp_path / "no_repo"
    no_repo.mkdir()
    projects = tmp_path / "projects"
    result = _run(["backup"], cwd=no_repo, projects_dir=projects)
    assert result.returncode != 0
    assert "no git repository" in result.stderr


# ---------------------------------------------------------------------------
# restore
# ---------------------------------------------------------------------------


def _make_backup(repo: Path, projects: Path) -> str:
    """Seed .hgp/, run backup, return project_id."""
    _seed_hgp(repo)
    _run(["backup"], cwd=repo, projects_dir=projects)
    meta = json.loads((repo / ".hgp" / "project-meta").read_text())
    return meta["project_id"]


def test_restore_compatible(tmp_path: Path) -> None:
    """Restore into a fresh repo that has the same git remote as the backup."""
    repo = _make_git_repo(tmp_path / "repo")
    projects = tmp_path / "projects"
    _make_backup(repo, projects)

    # Remove .hgp/ to simulate a clean restore target
    shutil.rmtree(repo / ".hgp")

    # Without a remote, compatibility is "unverifiable" — use --force
    result = _run(["restore", "--force"], cwd=repo, projects_dir=projects)
    assert result.returncode == 0, result.stderr
    assert (repo / ".hgp" / "hgp.db").exists()


def test_restore_by_project_id(tmp_path: Path) -> None:
    repo = _make_git_repo(tmp_path / "repo")
    projects = tmp_path / "projects"
    pid = _make_backup(repo, projects)

    shutil.rmtree(repo / ".hgp")

    result = _run(["restore", "--project-id", pid, "--force"], cwd=repo, projects_dir=projects)
    assert result.returncode == 0, result.stderr
    assert (repo / ".hgp" / "hgp.db").exists()


def test_restore_overwrites_existing_requires_force(tmp_path: Path) -> None:
    repo = _make_git_repo(tmp_path / "repo")
    projects = tmp_path / "projects"
    _make_backup(repo, projects)

    result = _run(["restore"], cwd=repo, projects_dir=projects)
    assert result.returncode != 0
    assert "already exists" in result.stderr


def test_restore_unverifiable_requires_force(tmp_path: Path) -> None:
    """No git remote → unverifiable → requires --force."""
    repo = _make_git_repo(tmp_path / "repo")
    projects = tmp_path / "projects"
    _make_backup(repo, projects)

    shutil.rmtree(repo / ".hgp")

    result = _run(["restore"], cwd=repo, projects_dir=projects)
    assert result.returncode != 0
    assert "force" in result.stderr.lower()


def test_restore_mismatch_requires_force(tmp_path: Path) -> None:
    """Backup from repo-A restored into repo-B (different git remote) → mismatch."""
    repo_a = _make_git_repo(tmp_path / "repo_a")
    repo_b = _make_git_repo(tmp_path / "repo_b")
    projects = tmp_path / "projects"

    _seed_hgp(repo_a)
    # Give repo_a a fake remote so the meta has a git_remote
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/org/repo-a.git"],
        cwd=repo_a, capture_output=True,
    )
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/org/repo-b.git"],
        cwd=repo_b, capture_output=True,
    )
    # Write project-meta for repo_a manually with the remote set
    meta_a = {
        "project_id": "aaaaaaaa-0000-0000-0000-000000000000",
        "git_remote": "https://github.com/org/repo-a.git",
        "repo_name": "repo-a",
        "hgp_version": "test",
    }
    (repo_a / ".hgp").mkdir(exist_ok=True)
    (repo_a / ".hgp" / "project-meta").write_text(json.dumps(meta_a))

    _run(["backup", "--force"], cwd=repo_a, projects_dir=projects)

    # Try restoring repo_a's backup into repo_b — remotes differ
    result = _run(
        ["restore", "--project-id", "aaaaaaaa-0000-0000-0000-000000000000"],
        cwd=repo_b, projects_dir=projects,
    )
    assert result.returncode != 0
    assert "mismatch" in result.stderr.lower()

    # With --force it should succeed
    result = _run(
        ["restore", "--project-id", "aaaaaaaa-0000-0000-0000-000000000000", "--force"],
        cwd=repo_b, projects_dir=projects,
    )
    assert result.returncode == 0


def test_restore_no_hgp_auto_discovery(tmp_path: Path) -> None:
    """When .hgp/ is absent and project-meta has project_id, auto-find backup."""
    repo = _make_git_repo(tmp_path / "repo")
    projects = tmp_path / "projects"
    pid = _make_backup(repo, projects)

    shutil.rmtree(repo / ".hgp")

    # project_id is unknown but backup exists; auto-discovery via project_id
    # in the unverifiable case, --force is still required
    result = _run(["restore", "--project-id", pid, "--force"], cwd=repo, projects_dir=projects)
    assert result.returncode == 0
    assert (repo / ".hgp" / "hgp.db").exists()


def test_restore_no_hgp_no_backup_exits(tmp_path: Path) -> None:
    repo = _make_git_repo(tmp_path / "repo")
    projects = tmp_path / "projects"
    # No backup ever made
    result = _run(["restore"], cwd=repo, projects_dir=projects)
    assert result.returncode != 0
    assert "no backup" in result.stderr.lower()


def test_restore_preserves_operational_files(tmp_path: Path) -> None:
    """After restore, existing mode/hook-policy must not be overwritten."""
    repo = _make_git_repo(tmp_path / "repo")
    projects = tmp_path / "projects"
    _make_backup(repo, projects)

    # Change operational files in the current .hgp/
    (repo / ".hgp" / "mode").write_text("advisory")
    (repo / ".hgp" / "hook-policy").write_text("block")

    result = _run(["restore", "--force"], cwd=repo, projects_dir=projects)
    assert result.returncode == 0

    assert (repo / ".hgp" / "mode").read_text() == "advisory"
    assert (repo / ".hgp" / "hook-policy").read_text() == "block"


def test_restore_atomic_on_existing_hgp(tmp_path: Path) -> None:
    """After successful restore, no .hgp_old or .hgp_restore_tmp left behind."""
    repo = _make_git_repo(tmp_path / "repo")
    projects = tmp_path / "projects"
    _make_backup(repo, projects)

    _run(["restore", "--force"], cwd=repo, projects_dir=projects)

    assert not (repo / ".hgp_old").exists()
    assert not (repo / ".hgp_restore_tmp").exists()


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------


def test_export_creates_at_dest(tmp_path: Path) -> None:
    repo = _make_git_repo(tmp_path / "repo")
    _seed_hgp(repo)
    dest = tmp_path / "export_out"

    result = _run(["export", str(dest)], cwd=repo)
    assert result.returncode == 0, result.stderr
    assert (dest / "hgp.db").exists()
    assert (dest / ".hgp_content").is_dir()
    assert (dest / "project-meta").exists()


def test_export_excludes_operational_files(tmp_path: Path) -> None:
    repo = _make_git_repo(tmp_path / "repo")
    _seed_hgp(repo)
    dest = tmp_path / "export_out"

    _run(["export", str(dest)], cwd=repo)

    assert not (dest / "mode").exists()
    assert not (dest / "hook-policy").exists()


def test_export_force_overwrites(tmp_path: Path) -> None:
    repo = _make_git_repo(tmp_path / "repo")
    _seed_hgp(repo)
    dest = tmp_path / "export_out"

    _run(["export", str(dest)], cwd=repo)
    result = _run(["export", str(dest)], cwd=repo)
    assert result.returncode != 0
    assert "already exists" in result.stderr

    result = _run(["export", str(dest), "--force"], cwd=repo)
    assert result.returncode == 0


def test_export_outside_repo_exits_nonzero(tmp_path: Path) -> None:
    no_repo = tmp_path / "no_repo"
    no_repo.mkdir()
    dest = tmp_path / "out"
    result = _run(["export", str(dest)], cwd=no_repo)
    assert result.returncode != 0
    assert "no git repository" in result.stderr


# ---------------------------------------------------------------------------
# import
# ---------------------------------------------------------------------------


def test_import_compatible(tmp_path: Path) -> None:
    """Export from repo → import into a fresh same-remote repo (forced for no-remote case)."""
    repo = _make_git_repo(tmp_path / "repo")
    _seed_hgp(repo)
    dest = tmp_path / "snapshot"

    _run(["export", str(dest)], cwd=repo)

    shutil.rmtree(repo / ".hgp")

    result = _run(["import", str(dest), "--force"], cwd=repo)
    assert result.returncode == 0, result.stderr
    assert (repo / ".hgp" / "hgp.db").exists()


def test_import_mismatch_requires_force(tmp_path: Path) -> None:
    repo_a = _make_git_repo(tmp_path / "repo_a")
    repo_b = _make_git_repo(tmp_path / "repo_b")

    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/org/repo-a.git"],
        cwd=repo_a, capture_output=True,
    )
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/org/repo-b.git"],
        cwd=repo_b, capture_output=True,
    )

    _seed_hgp(repo_a)
    snapshot = tmp_path / "snap"
    _run(["export", str(snapshot)], cwd=repo_a)

    result = _run(["import", str(snapshot)], cwd=repo_b)
    assert result.returncode != 0
    assert "mismatch" in result.stderr.lower()

    result = _run(["import", str(snapshot), "--force"], cwd=repo_b)
    assert result.returncode == 0


def test_import_no_meta_requires_force(tmp_path: Path) -> None:
    """Snapshot without project-meta → unverifiable → requires --force."""
    repo = _make_git_repo(tmp_path / "repo")
    _seed_hgp(repo)
    snapshot = tmp_path / "snap"

    _run(["export", str(snapshot)], cwd=repo)
    # Remove project-meta from snapshot to simulate manual/old snapshot
    (snapshot / "project-meta").unlink()

    shutil.rmtree(repo / ".hgp")

    result = _run(["import", str(snapshot)], cwd=repo)
    assert result.returncode != 0
    assert "force" in result.stderr.lower()

    result = _run(["import", str(snapshot), "--force"], cwd=repo)
    assert result.returncode == 0


def test_import_preserves_operational_files(tmp_path: Path) -> None:
    repo = _make_git_repo(tmp_path / "repo")
    _seed_hgp(repo)
    snapshot = tmp_path / "snap"

    _run(["export", str(snapshot)], cwd=repo)

    (repo / ".hgp" / "mode").write_text("advisory")
    (repo / ".hgp" / "hook-policy").write_text("block")

    result = _run(["import", str(snapshot), "--force"], cwd=repo)
    assert result.returncode == 0

    assert (repo / ".hgp" / "mode").read_text() == "advisory"
    assert (repo / ".hgp" / "hook-policy").read_text() == "block"
