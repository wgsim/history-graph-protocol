"""Git worktree support: shared storage, file identity, hooks, CLI and concurrency."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import hgp.server as server_module
from hgp import project
from hgp.cas import CAS
from hgp.db import Database
from hgp.hooks import hgp_paths
from hgp.lease import LeaseManager
from hgp.reconciler import Reconciler
from hgp.server import (
    HGPContext,
    hgp_create_operation,
    hgp_file_history,
    hgp_get_context,
    hgp_query_operations,
    hgp_set_context,
    hgp_write_file,
)

# ── resolver: mock git layouts (relative/absolute pointers, submodules) ─────

def _mock_main_repo(root: Path) -> Path:
    git = root / ".git"
    for d in ("objects", "worktrees", "modules"):
        (git / d).mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n")
    (git / "config").write_text("")
    return git


def _mock_worktree(main: Path, wt: Path, *, relative_gitdir: bool, relative_commondir: bool) -> None:
    git = main / ".git"
    admin = git / "worktrees" / wt.name
    admin.mkdir(parents=True)
    wt.mkdir(parents=True)
    (admin / "HEAD").write_text("ref: refs/heads/wt\n")
    (admin / "commondir").write_text("../..\n" if relative_commondir else f"{git}\n")
    (admin / "gitdir").write_text(f"{wt / '.git'}\n")
    pointer = os.path.relpath(admin, wt) if relative_gitdir else str(admin)
    (wt / ".git").write_text(f"gitdir: {pointer}\n")


@pytest.mark.parametrize("relative_gitdir", [False, True])
@pytest.mark.parametrize("relative_commondir", [False, True])
def test_mock_worktree_resolves_to_main_root(tmp_path, relative_gitdir, relative_commondir) -> None:
    main = tmp_path / "main"
    _mock_main_repo(main)
    wt = tmp_path / "wt"
    _mock_worktree(main, wt, relative_gitdir=relative_gitdir, relative_commondir=relative_commondir)
    assert project.find_storage_root(wt) == main.resolve()
    assert project.find_project_root(wt) == wt.resolve()  # write boundary stays the checkout
    assert project.current_branch(wt) == "wt"


def test_mock_worktree_with_mismatched_backpointer_is_not_shared(tmp_path) -> None:
    main = tmp_path / "main"
    _mock_main_repo(main)
    wt = tmp_path / "wt"
    _mock_worktree(main, wt, relative_gitdir=False, relative_commondir=True)
    (main / ".git" / "worktrees" / "wt" / "gitdir").write_text(f"{tmp_path / 'elsewhere' / '.git'}\n")
    assert project.find_storage_root(wt) == wt  # spoofed pointer chain → no sharing


def test_submodule_keeps_its_own_storage(tmp_path) -> None:
    main = tmp_path / "main"
    git = _mock_main_repo(main)
    module_git = git / "modules" / "sub"
    for d in ("objects", "refs"):
        (module_git / d).mkdir(parents=True)
    (module_git / "HEAD").write_text("ref: refs/heads/main\n")
    (module_git / "config").write_text("")
    sub = main / "sub"
    sub.mkdir()
    (sub / ".git").write_text("gitdir: ../.git/modules/sub\n")
    assert project.find_storage_root(sub) == sub
    assert hgp_paths.find_hgp_dir(sub / "deep") == sub / ".hgp"


# ── repo-relative file identity ─────────────────────────────────────────────

def test_file_history_converges_across_worktrees(shared_db) -> None:
    repo, worktree, db, ctx_for = shared_db
    (repo / "tracked.txt").write_text("main edit")
    server_module._ctx = ctx_for(repo)
    main_op = hgp_write_file(file_path=str(repo / "tracked.txt"), content="main edit", agent_id="a")
    server_module._ctx = ctx_for(worktree)
    wt_op = hgp_write_file(file_path=str(worktree / "tracked.txt"), content="wt edit", agent_id="a")

    for op_id in (main_op["op_id"], wt_op["op_id"]):
        assert db.get_operation(op_id)["file_path"] == "tracked.txt"

    history = hgp_file_history(file_path=str(worktree / "tracked.txt"))
    assert [o["op_id"] for o in history["operations"]] == [wt_op["op_id"], main_op["op_id"]]
    # Responses show absolute paths in the active checkout.
    assert {o["file_path"] for o in history["operations"]} == {str(worktree / "tracked.txt")}
    server_module._ctx = ctx_for(repo)
    from_main = hgp_file_history(file_path=str(repo / "tracked.txt"))
    assert len(from_main["operations"]) == 2
    queried = hgp_query_operations(file_path=str(repo / "tracked.txt"))["operations"]
    assert {o["file_path"] for o in queried} == {str(repo / "tracked.txt")}


def test_v6_migration_relativizes_paths_with_backup(tmp_path) -> None:
    root = tmp_path / "repo"
    wt = tmp_path / "wt"
    hgp_dir = root / ".hgp"
    hgp_dir.mkdir(parents=True)
    db = Database(hgp_dir / "hgp.db")
    db.initialize()
    db.execute("DELETE FROM _hgp_migrations WHERE name = 'v6_relative_file_keys'")  # simulate old DB
    rows = [
        ("op-main", str(root / "src" / "a.py"), None),
        ("op-wt", str(wt / "src" / "a.py"), {"hgp_checkout": {"root": str(wt), "branch": "f"}}),
        ("op-out", str(tmp_path / "outside.txt"), None),
        ("op-rel", "already/relative.txt", None),
    ]
    for i, (op_id, fp, meta) in enumerate(rows, start=1):
        db.insert_operation(
            op_id=op_id, op_type="artifact", agent_id="a", commit_seq=i,
            chain_hash="sha256:" + "a" * 64, file_path=fp,
            metadata=json.dumps(meta) if meta else None,
        )
    db.commit()

    backup = hgp_dir / "pre-v6.bak"
    assert db.relativize_file_paths(root, backup_path=backup) == 2
    assert backup.exists()
    paths = {op_id: db.get_operation(op_id)["file_path"] for op_id, _, _ in rows}
    assert paths == {
        "op-main": "src/a.py",
        "op-wt": "src/a.py",
        "op-out": str(tmp_path / "outside.txt"),  # outside its root: unchanged
        "op-rel": "already/relative.txt",
    }
    assert db.relativize_file_paths(root, backup_path=hgp_dir / "again.bak") == 0  # one-time
    assert not (hgp_dir / "again.bak").exists()
    db.close()


def test_reconcile_recovers_pending_op_with_relative_key(shared_db) -> None:
    repo, worktree, db, ctx_for = shared_db
    ctx = ctx_for(worktree)
    (worktree / "f.txt").write_text("content")
    obj_hash = ctx.cas.store(b"content")
    db.begin_immediate()
    seq = db.next_commit_seq()
    db.insert_operation(
        op_id="op-rel-pending", op_type="artifact", agent_id="a", commit_seq=seq,
        chain_hash="sha256:" + "d" * 64, status="PENDING", object_hash=obj_hash,
        metadata=json.dumps({"hgp_checkout": {"root": str(worktree), "id": ctx.reconciler.checkout_id}}),
        file_path="f.txt",
    )
    old_ts = (datetime.now(timezone.utc) - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    db.execute("UPDATE operations SET created_at = ? WHERE op_id = ?", (old_ts, "op-rel-pending"))
    db.commit()

    assert ctx_for(repo).reconciler.reconcile().pending_recovered == 0  # not main's op
    assert ctx.reconciler.reconcile().pending_recovered == 1
    assert db.get_operation("op-rel-pending")["status"] == "COMPLETED"

HOOKS = Path(__file__).parents[1] / "src" / "hgp" / "hooks"
START_HOOK = HOOKS / "claude" / "subagent_start_hgp.py"
STOP_HOOK = HOOKS / "claude" / "subagent_stop_hgp.py"

# (hook script, event for a native file write, predicate on a blocked result)
POLICY_HOOKS = [
    pytest.param(
        HOOKS / "claude" / "pre_tool_use_hgp.py",
        {"tool_name": "Write"},
        lambda r: r.returncode == 2,
        id="claude",
    ),
    pytest.param(
        HOOKS / "gemini" / "pre_tool_use_hgp.py",
        {"tool_name": "write_file"},
        lambda r: '"deny"' in r.stdout,
        id="gemini",
    ),
    pytest.param(
        HOOKS / "codex" / "pre_tool_use_hgp.py",
        {"tool_name": "Bash", "tool_input": {"command": "echo hi > out.txt"}},
        lambda r: '"deny"' in r.stdout,
        id="codex",
    ),
]


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def linked_worktree(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    (repo / "tracked.txt").write_text("initial")
    _git("add", "tracked.txt", cwd=repo)
    _git("commit", "-qm", "initial", cwd=repo)
    worktree = tmp_path / "worktree"
    _git("worktree", "add", "-q", str(worktree), "-b", "feature", cwd=repo)
    (repo / ".hgp").mkdir()
    return repo.resolve(), worktree.resolve()


def _run(script: Path, event: dict, cwd: Path) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != "HGP_HOOK_BLOCK"}
    return subprocess.run(
        [sys.executable, str(script)],
        input=json.dumps(event),
        cwd=cwd,
        capture_output=True,
        text=True,
        env=env,
    )


# ── single implementation / parity ──────────────────────────────────────────

def test_project_reexports_hook_helper() -> None:
    assert project.find_storage_root is hgp_paths.find_storage_root


def test_find_hgp_dir_matches_server_storage(linked_worktree) -> None:
    repo, worktree = linked_worktree
    nested = worktree / "a" / "b"
    nested.mkdir(parents=True)
    expected = project.find_storage_root(worktree) / ".hgp"
    assert expected == repo / ".hgp"
    assert hgp_paths.find_hgp_dir(nested) == expected
    assert hgp_paths.find_hgp_dir(repo) == repo / ".hgp"


def test_find_hgp_dir_prefers_shared_over_stale_local_dir(linked_worktree) -> None:
    repo, worktree = linked_worktree
    (worktree / ".hgp").mkdir()  # left over from before shared storage
    assert hgp_paths.find_hgp_dir(worktree) == repo / ".hgp"


# ── P0: subagent hooks in a linked worktree ─────────────────────────────────

def test_start_hook_reads_context_from_main_repo(linked_worktree) -> None:
    repo, worktree = linked_worktree
    (worktree / ".hgp").mkdir()
    (repo / ".hgp" / "context-sess-wt.json").write_text(
        json.dumps({"root_op_id": "op-root-wt", "agent_id": "a", "set_at": time.time()})
    )
    r = _run(START_HOOK, {"hook_event_name": "SubagentStart", "session_id": "sess-wt"}, cwd=worktree)
    assert r.returncode == 0
    assert "op-root-wt" in json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]


def test_stop_hook_writes_summary_to_main_repo(linked_worktree) -> None:
    repo, worktree = linked_worktree
    event = {
        "hook_event_name": "SubagentStop",
        "session_id": "sess-wt",
        "agent_id": "agent-1",
        "agent_type": "general-purpose",
        "cwd": str(worktree),
    }
    r = _run(STOP_HOOK, event, cwd=worktree)
    assert r.returncode == 0
    assert len(list((repo / ".hgp").glob("subagent-summary-sess-wt-*.json"))) == 1
    assert not (worktree / ".hgp").exists()


@pytest.mark.parametrize("script,event,blocked", POLICY_HOOKS)
def test_policy_hooks_read_main_repo_policy(linked_worktree, script, event, blocked) -> None:
    repo, worktree = linked_worktree
    (repo / ".hgp" / "hook-policy").write_text("block")
    assert blocked(_run(script, event, cwd=worktree))


# ── installed layout (hooks copied out of the package) ──────────────────────

def test_install_hooks_ships_shared_helper(linked_worktree) -> None:
    _, worktree = linked_worktree
    r = subprocess.run(
        [sys.executable, "-m", "hgp.server", "install-hooks"],
        cwd=worktree, capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    source = (HOOKS / "hgp_paths.py").read_bytes()
    for client in ("claude", "gemini"):
        assert (worktree / f".{client}" / "hooks" / "hgp_paths.py").read_bytes() == source


@pytest.mark.parametrize("script,event,blocked", POLICY_HOOKS)
def test_installed_hook_resolves_shared_policy(linked_worktree, tmp_path, script, event, blocked) -> None:
    repo, worktree = linked_worktree
    (repo / ".hgp" / "hook-policy").write_text("block")
    dest = tmp_path / "installed" / "hooks"
    dest.mkdir(parents=True)
    shutil.copy2(script, dest / script.name)
    shutil.copy2(HOOKS / "hgp_paths.py", dest / "hgp_paths.py")
    assert blocked(_run(dest / script.name, event, cwd=worktree))


def test_installed_hook_without_helper_degrades_safely(linked_worktree, tmp_path) -> None:
    repo, worktree = linked_worktree
    (repo / ".hgp" / "hook-policy").write_text("block")
    dest = tmp_path / "partial" / "hooks"
    dest.mkdir(parents=True)
    shutil.copy2(START_HOOK, dest / START_HOOK.name)
    script = HOOKS / "claude" / "pre_tool_use_hgp.py"
    shutil.copy2(script, dest / script.name)
    # -S: no site-packages, so neither hgp_paths beside the hooks nor the hgp package
    # is importable. Old worktree-local behaviour: main-repo policy is not visible, no crash.
    for name, event in (
        (script.name, {"tool_name": "Write"}),
        (START_HOOK.name, {"hook_event_name": "SubagentStart", "session_id": "x"}),
    ):
        r = subprocess.run(
            [sys.executable, "-S", str(dest / name)], input=json.dumps(event),
            cwd=worktree, capture_output=True, text=True,
        )
        assert r.returncode == 0 and r.stdout == "" and "Traceback" not in r.stderr


# ── summary scoping in a shared .hgp/ ───────────────────────────────────────

@pytest.fixture
def server_ctx(tmp_path: Path):
    hgp_dir = tmp_path / ".hgp"
    content_dir = hgp_dir / ".hgp_content"
    content_dir.mkdir(parents=True)
    db = Database(hgp_dir / "hgp.db")
    db.initialize()
    cas = CAS(content_dir)
    orig = server_module._ctx
    server_module._ctx = HGPContext(
        db=db, cas=cas, lease_mgr=LeaseManager(db),
        reconciler=Reconciler(db, cas, content_dir), project_root=tmp_path,
    )
    yield hgp_dir
    server_module._ctx = orig
    db.close()


def test_get_context_ignores_summaries_of_prefixed_session(server_ctx: Path) -> None:
    hgp_dir = server_ctx
    for sid in ("s1", "s1-x"):
        op = hgp_create_operation(op_type="hypothesis", agent_id=sid)
        hgp_set_context(root_op_id=op["op_id"], agent_id=sid, session_id=sid)
    own = hgp_dir / "subagent-summary-s1-1000-aaaaaaaa.json"
    other = hgp_dir / "subagent-summary-s1-x-1000-bbbbbbbb.json"
    own.write_text(json.dumps({"session_id": "s1"}))
    other.write_text(json.dumps({"session_id": "s1-x"}))

    result = hgp_get_context("s1")
    assert [s["session_id"] for s in result["subagent_summaries"]] == ["s1"]
    assert not own.exists()
    assert other.exists()
    assert [s["session_id"] for s in hgp_get_context("s1-x")["subagent_summaries"]] == ["s1-x"]


# ── P2: one shared DB seen from the main checkout and a linked worktree ─────

@pytest.fixture
def shared_db(linked_worktree):
    repo, worktree = linked_worktree
    content = repo / ".hgp" / ".hgp_content"
    content.mkdir(parents=True)
    db = Database(repo / ".hgp" / "hgp.db")
    db.initialize()
    cas = CAS(content)

    def ctx_for(root: Path) -> HGPContext:
        return HGPContext(
            db=db, cas=cas, lease_mgr=LeaseManager(db),
            reconciler=Reconciler(db, cas, content, project_root=root),
            project_root=root, storage_root=repo,
        )

    orig = server_module._ctx
    yield repo, worktree, db, ctx_for
    server_module._ctx = orig
    db.close()


def _git_branch(cwd: Path) -> str:
    return subprocess.run(
        ["git", "branch", "--show-current"], cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout.strip()


def test_current_branch_reads_head(linked_worktree) -> None:
    repo, worktree = linked_worktree
    assert project.current_branch(worktree) == "feature"
    assert project.current_branch(repo) == _git_branch(repo)
    _git("checkout", "-q", "--detach", cwd=worktree)
    assert project.current_branch(worktree) is None


def test_ops_from_worktree_are_tagged_with_checkout(shared_db) -> None:
    repo, worktree, db, ctx_for = shared_db
    server_module._ctx = ctx_for(worktree)
    op = hgp_create_operation(op_type="hypothesis", agent_id="a", metadata={"k": 1})
    written = hgp_write_file(file_path=str(worktree / "f.txt"), content="x", agent_id="a")
    for op_id in (op["op_id"], written["op_id"]):
        meta = json.loads(db.get_operation(op_id)["metadata"])
        assert meta["hgp_checkout"] == {
            "root": str(worktree),
            "branch": "feature",
            "id": server_module._get_context().reconciler.checkout_id,
        }
        assert meta["hgp_checkout"]["id"]
    assert json.loads(db.get_operation(op["op_id"])["metadata"])["k"] == 1

    server_module._ctx = ctx_for(repo)
    main_op = hgp_create_operation(op_type="hypothesis", agent_id="a")
    assert db.get_operation(main_op["op_id"])["metadata"] is None  # main checkout: untagged


def test_query_operations_scope(shared_db) -> None:
    repo, worktree, _, ctx_for = shared_db
    server_module._ctx = ctx_for(repo)
    main_id = hgp_create_operation(op_type="hypothesis", agent_id="main")["op_id"]
    server_module._ctx = ctx_for(worktree)
    wt_id = hgp_create_operation(op_type="hypothesis", agent_id="wt")["op_id"]

    def ids(result: dict) -> set[str]:
        return {o["op_id"] for o in result["operations"]}

    # Default: one shared history across worktrees; "checkout" narrows to this one.
    assert ids(hgp_query_operations()) == {main_id, wt_id}
    assert ids(hgp_query_operations(scope="checkout")) == {wt_id}
    server_module._ctx = ctx_for(repo)
    assert ids(hgp_query_operations()) == {main_id, wt_id}
    assert ids(hgp_query_operations(scope="checkout")) == {main_id}
    assert hgp_query_operations(scope="bogus")["error"] == "INVALID_SCOPE"


def _insert_old_pending(db: Database, op_id: str, file_path: Path, checkout_root: Path | None) -> None:
    meta: dict = {"reason": "test"}
    if checkout_root is not None:
        meta["hgp_checkout"] = {"root": str(checkout_root), "branch": "x"}
    db.begin_immediate()
    seq = db.next_commit_seq()
    db.insert_operation(
        op_id=op_id, op_type="artifact", agent_id="a", commit_seq=seq,
        chain_hash="sha256:" + "d" * 64, status="PENDING",
        metadata=json.dumps(meta), file_path=str(file_path),
    )
    old_ts = (datetime.now(timezone.utc) - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    db.execute("UPDATE operations SET created_at = ? WHERE op_id = ?", (old_ts, op_id))
    db.commit()


def test_reconcile_retires_ops_of_removed_worktree(shared_db, tmp_path) -> None:
    repo, worktree, db, ctx_for = shared_db
    gone = tmp_path / "removed-worktree"  # never created: worktree was deleted
    _insert_old_pending(db, "op-gone", gone / "f.txt", gone)
    _insert_old_pending(db, "op-live", worktree / "f.txt", worktree)
    reconciler = ctx_for(repo).reconciler

    dry = reconciler.reconcile(dry_run=True)
    assert dry.pending_orphaned == 1
    assert db.get_operation("op-gone")["status"] == "PENDING"

    report = reconciler.reconcile()
    assert report.pending_orphaned == 1
    assert db.get_operation("op-gone")["status"] == "STALE_PENDING"
    assert db.get_operation("op-live")["status"] == "PENDING"  # live worktree owns it


def test_reconcile_skips_main_ops_when_run_from_worktree(shared_db) -> None:
    repo, worktree, db, ctx_for = shared_db
    _insert_old_pending(db, "op-main", repo / "f.txt", None)
    report = ctx_for(worktree).reconciler.reconcile()
    assert report.pending_stale == 0 and report.pending_orphaned == 0
    assert db.get_operation("op-main")["status"] == "PENDING"


# ── P3: destructive CLI commands, installer, concurrency ────────────────────

def _cli(*args: str, cwd: Path, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "HGP_PROJECTS_DIR": str(tmp_path / "projects")}
    return subprocess.run(
        [sys.executable, "-m", "hgp.server", *args], cwd=cwd, capture_output=True, text=True, env=env,
    )


def test_restore_and_import_in_worktree_require_shared(linked_worktree, tmp_path) -> None:
    repo, worktree = linked_worktree
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / "hgp.db").write_bytes(b"")

    for args in (["restore"], ["import", str(snapshot)]):
        r = _cli(*args, cwd=worktree, tmp_path=tmp_path)
        assert r.returncode == 1
        assert "linked git worktree" in r.stderr and "--shared" in r.stderr
        r = _cli(*args, "--shared", cwd=worktree, tmp_path=tmp_path)
        assert "linked git worktree" not in r.stderr
        r = _cli(*args, cwd=repo, tmp_path=tmp_path)  # main checkout: no extra flag
        assert "linked git worktree" not in r.stderr


def test_install_hooks_backs_up_modified_hook(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    hooks = repo / ".claude" / "hooks"
    hooks.mkdir(parents=True)
    (hooks / "pre_tool_use_hgp.py").write_text("# locally customised hook\n")

    r = _cli("install-hooks", "--claude", cwd=repo, tmp_path=tmp_path)
    assert r.returncode == 0, r.stderr
    backups = list(hooks.glob("pre_tool_use_hgp.py.bak-*"))
    assert len(backups) == 1
    assert backups[0].read_text() == "# locally customised hook\n"
    assert "previous copy saved" in r.stderr
    bundled = HOOKS / "claude" / "pre_tool_use_hgp.py"
    assert (hooks / "pre_tool_use_hgp.py").read_bytes() == bundled.read_bytes()

    _cli("install-hooks", "--claude", cwd=repo, tmp_path=tmp_path)  # unchanged → no new backup
    assert len(list(hooks.glob("*.bak-*"))) == 1


def _make_backups(hooks: Path, names: list[str]) -> None:
    hooks.mkdir(parents=True, exist_ok=True)
    for name in names:
        (hooks / name).write_text(name)


def _hook_backups(*args: str, cwd: Path, home: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "HOME": str(home)}
    return subprocess.run(
        [sys.executable, "-m", "hgp.server", "hook-backups", *args],
        cwd=cwd, capture_output=True, text=True, env=env,
    )


def test_hook_backups_lists_only_bundled_hook_backups(tmp_path) -> None:
    hooks = tmp_path / "home" / ".claude" / "hooks"
    _make_backups(hooks, [
        "pre_tool_use_hgp.py.bak-20260101000000",
        "my_own_hook.py.bak-20260101000000",   # not an HGP hook → ignored
        "pre_tool_use_hgp.py.bak-manual",       # not our naming → ignored
    ])
    r = _hook_backups(cwd=tmp_path, home=tmp_path / "home")
    assert r.returncode == 0, r.stderr
    assert "pre_tool_use_hgp.py.bak-20260101000000" in r.stdout
    assert "my_own_hook" not in r.stdout and "bak-manual" not in r.stdout


def test_hook_backups_prune_keeps_newest(tmp_path) -> None:
    home = tmp_path / "home"
    hooks = home / ".gemini" / "hooks"
    names = [
        "pre_tool_use_hgp.py.bak-20260101000000",
        "pre_tool_use_hgp.py.bak-20260301000000",
        "pre_tool_use_hgp.py.bak-20260301000000-1",  # same second, later copy
        "post_bash_hgp.py.bak-20260101000000",
    ]
    _make_backups(hooks, names)

    r = _hook_backups("--prune", "--dry-run", cwd=tmp_path, home=home)
    assert r.returncode == 0, r.stderr
    assert "Would delete 2 of 4" in r.stdout
    assert all((hooks / n).exists() for n in names)

    r = _hook_backups("--prune", cwd=tmp_path, home=home)
    assert "Deleted 2 of 4" in r.stdout
    assert sorted(p.name for p in hooks.iterdir()) == [
        "post_bash_hgp.py.bak-20260101000000",
        "pre_tool_use_hgp.py.bak-20260301000000-1",
    ]

    r = _hook_backups("--prune", "--keep", "0", cwd=tmp_path, home=home)
    assert "Deleted 2 of 2" in r.stdout
    assert list(hooks.iterdir()) == []
    assert "No hook backups found." in _hook_backups(cwd=tmp_path, home=home).stdout


def test_hook_backups_local_scope(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    _make_backups(repo / ".claude" / "hooks", [
        "pre_tool_use_hgp.py.bak-20260101000000",
        "pre_tool_use_hgp.py.bak-20260201000000",
    ])
    global_hooks = tmp_path / "home" / ".claude" / "hooks"
    _make_backups(global_hooks, ["pre_tool_use_hgp.py.bak-20260101000000"])

    r = _hook_backups("--local", "--prune", cwd=repo, home=tmp_path / "home")
    assert "Deleted 1 of 2" in r.stdout
    assert (global_hooks / "pre_tool_use_hgp.py.bak-20260101000000").exists()  # global untouched


@pytest.mark.parametrize("args", [["--keep", "1"], ["--dry-run"], ["--prune", "--keep", "-1"], ["--bogus"]])
def test_hook_backups_rejects_bad_args(tmp_path, args) -> None:
    r = _hook_backups(*args, cwd=tmp_path, home=tmp_path)
    assert r.returncode == 1


def test_install_hints_prune_when_backups_pile_up(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    hooks = repo / ".claude" / "hooks"
    _make_backups(hooks, [f"pre_tool_use_hgp.py.bak-2026010{i}000000" for i in range(1, 4)])
    (hooks / "pre_tool_use_hgp.py").write_text("# customised\n")
    r = _cli("install-hooks", "--claude", cwd=repo, tmp_path=tmp_path)
    assert r.returncode == 0, r.stderr
    assert "hgp hook-backups --prune" in r.stderr


_WRITER = textwrap.dedent(
    """
    import sys
    from pathlib import Path
    import hgp.server as s
    from hgp.cas import CAS
    from hgp.db import Database
    from hgp.lease import LeaseManager
    from hgp.reconciler import Reconciler

    root, count, agent = Path(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
    content = root / ".hgp" / ".hgp_content"
    db = Database(root / ".hgp" / "hgp.db")
    db.initialize()
    cas = CAS(content)
    s._ctx = s.HGPContext(db=db, cas=cas, lease_mgr=LeaseManager(db),
                          reconciler=Reconciler(db, cas, content), project_root=root)
    for _ in range(count):
        result = s.hgp_create_operation(op_type="hypothesis", agent_id=agent)
        assert "op_id" in result, result
    db.close()
    """
)


def test_concurrent_server_processes_share_one_db(tmp_path) -> None:
    content = tmp_path / ".hgp" / ".hgp_content"
    content.mkdir(parents=True)
    db = Database(tmp_path / ".hgp" / "hgp.db")
    db.initialize()
    db.close()

    writers, per_writer = 3, 40
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _WRITER, str(tmp_path), str(per_writer), f"w{i}"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        for i in range(writers)
    ]
    for p in procs:
        _, err = p.communicate(timeout=120)
        assert p.returncode == 0, err

    db = Database(tmp_path / ".hgp" / "hgp.db")
    db.initialize()
    rows = db.execute("SELECT COUNT(*), COUNT(DISTINCT commit_seq) FROM operations").fetchone()
    db.close()
    assert tuple(rows) == (writers * per_writer, writers * per_writer)


@pytest.mark.parametrize("active_worktree", [False, True])
def test_move_invalidates_only_active_checkout_revision(shared_db, active_worktree) -> None:
    repo, worktree, db, ctx_for = shared_db
    active, other = (worktree, repo) if active_worktree else (repo, worktree)
    server_module._ctx = ctx_for(active)
    first = hgp_write_file(file_path=str(active / "tracked.txt"), content="older", agent_id="active")
    current = hgp_write_file(file_path=str(active / "tracked.txt"), content="current", agent_id="active")
    # File history must remain ordered by commit sequence, independently of memory tier.
    db.execute("UPDATE operations SET memory_tier = 'short_term' WHERE op_id = ?", (first["op_id"],))
    db.execute("UPDATE operations SET memory_tier = 'inactive' WHERE op_id = ?", (current["op_id"],))
    server_module._ctx = ctx_for(other)
    foreign = hgp_write_file(file_path=str(other / "tracked.txt"), content="other branch", agent_id="other")
    server_module._ctx = ctx_for(active)
    result = server_module.hgp_move_file(
        old_path=str(active / "tracked.txt"), new_path=str(active / "renamed.txt"), agent_id="active",
    )
    assert result["status"] == "COMPLETED"
    assert db.get_operation(current["op_id"])["status"] == "INVALIDATED"
    assert db.get_operation(first["op_id"])["status"] == "COMPLETED"
    assert db.get_operation(foreign["op_id"])["status"] == "COMPLETED"
    assert (other / "tracked.txt").read_text() == "other branch"
    assert (active / "renamed.txt").read_text() == "current"


def test_move_without_local_history_does_not_invalidate_other_checkout(shared_db) -> None:
    repo, worktree, db, ctx_for = shared_db
    server_module._ctx = ctx_for(worktree)
    other = hgp_write_file(file_path=str(worktree / "tracked.txt"), content="other", agent_id="other")
    server_module._ctx = ctx_for(repo)
    result = server_module.hgp_move_file(
        old_path=str(repo / "tracked.txt"), new_path=str(repo / "renamed.txt"), agent_id="main",
    )
    assert result["status"] == "COMPLETED"
    assert db.get_operation(other["op_id"])["status"] == "COMPLETED"
    assert db.get_invalidated_targets(result["invalidation_op_id"]) == []


@pytest.mark.parametrize("query", [hgp_file_history, hgp_query_operations])
@pytest.mark.parametrize("active_worktree", [False, True])
def test_file_queries_reject_foreign_repo_but_allow_shared_checkout(
    shared_db, tmp_path, query, active_worktree,
) -> None:
    repo, worktree, _, ctx_for = shared_db
    active, sibling = (worktree, repo) if active_worktree else (repo, worktree)
    server_module._ctx = ctx_for(active)
    own = hgp_write_file(file_path=str(active / "tracked.txt"), content="own", agent_id="active")
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    _git("init", "-q", cwd=foreign)
    result = query(file_path=str(foreign / "tracked.txt"))
    assert result.get("error") == "CROSS_REPO_OPERATION"
    assert "operations" not in result
    shared = query(file_path=str(sibling / "tracked.txt"))
    assert [op["op_id"] for op in shared["operations"]] == [own["op_id"]]


@pytest.mark.parametrize("query", [hgp_file_history, hgp_query_operations])
def test_global_file_queries_keep_absolute_repository_identity(shared_db, tmp_path, query) -> None:
    repo, _, _, ctx_for = shared_db
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    _git("init", "-q", cwd=foreign)
    ctx = ctx_for(repo)
    ctx.project_root = None
    ctx.storage_root = None
    server_module._ctx = ctx
    own = hgp_write_file(file_path=str(repo / "tracked.txt"), content="own", agent_id="a")
    other = hgp_write_file(file_path=str(foreign / "tracked.txt"), content="other", agent_id="a")
    for root, op in [(repo, own), (foreign, other)]:
        result = query(file_path=str(root / "tracked.txt"))
        assert [row["op_id"] for row in result["operations"]] == [op["op_id"]]
        assert result["operations"][0]["file_path"] == str(root / "tracked.txt")


def test_legacy_worktree_history_requires_archiving_before_switch(linked_worktree, tmp_path, monkeypatch):
    import sqlite3

    repo, wt = linked_worktree
    monkeypatch.chdir(wt)
    monkeypatch.setattr(server_module, "_ctx", None)
    local = wt / ".hgp"
    local.mkdir()
    with sqlite3.connect(local / "hgp.db") as db:
        db.execute("CREATE TABLE legacy (id INTEGER)")
        db.execute("INSERT INTO legacy VALUES (42)")
    content = local / ".hgp_content"
    content.mkdir()
    (content / "blob").write_bytes(b"history")
    with pytest.raises(server_module.LegacyWorktreeHistoryError, match="hgp export"):
        hgp_query_operations()
    assert not (repo / ".hgp" / "hgp.db").exists()

    snapshot = tmp_path / "snapshot"
    for args in [("export", str(snapshot)), ("backup",)]:
        result = _cli(*args, cwd=wt, tmp_path=tmp_path)
        assert result.returncode == 0, result.stderr
        assert "legacy worktree history" in result.stderr
    backup = tmp_path / "projects" / json.loads((local / "project-meta").read_text())["project_id"]
    for dest in (snapshot, backup):
        with sqlite3.connect(dest / "hgp.db") as db:
            assert db.execute("SELECT id FROM legacy").fetchone() == (42,)
        assert (dest / ".hgp_content" / "blob").read_bytes() == b"history"

    local.rename(tmp_path / "archived-history")
    assert hgp_query_operations()["operations"] == []
    assert (repo / ".hgp" / "hgp.db").exists()
    server_module._get_context().db.close()


@pytest.mark.parametrize("snapshot_command", ["export", "backup"])
def test_legacy_snapshot_file_history_is_portable(linked_worktree, tmp_path, snapshot_command):
    repo, worktree = linked_worktree
    local = worktree / ".hgp"
    local.mkdir()
    db = Database(local / "hgp.db")
    db.initialize()
    db.insert_operation(
        op_id="legacy", op_type="artifact", status="COMPLETED", agent_id="old",
        file_path=str(worktree / "tracked.txt"), commit_seq=1, chain_hash="sha256:" + "a" * 64,
    )
    db.close()

    snapshot = tmp_path / "snapshot"
    args = ("export", str(snapshot)) if snapshot_command == "export" else ("backup",)
    result = _cli(*args, cwd=worktree, tmp_path=tmp_path)
    assert result.returncode == 0, result.stderr
    if snapshot_command == "backup":
        project_id = json.loads((local / "project-meta").read_text())["project_id"]

    inspection = tmp_path / "inspection"
    _git("clone", str(repo), str(inspection), cwd=tmp_path)
    if snapshot_command == "backup":
        result = _cli("restore", "--project-id", project_id, "--force", cwd=inspection, tmp_path=tmp_path)
    else:
        result = _cli("import", str(snapshot), "--force", cwd=inspection, tmp_path=tmp_path)
    assert result.returncode == 0, result.stderr
    inspect = subprocess.run(
        [sys.executable, "-c",
         "import json; from hgp.server import hgp_file_history; print(json.dumps(hgp_file_history('tracked.txt')))"],
        cwd=inspection, check=True, capture_output=True, text=True,
    )
    records = json.loads(inspect.stdout)["operations"]
    assert [op["op_id"] for op in records] == ["legacy"]
    assert records[0]["file_path"] == str(inspection / "tracked.txt")

    db = Database(local / "hgp.db")
    db.initialize()
    assert db.get_operation("legacy")["file_path"] == str(worktree / "tracked.txt")
    db.close()


def test_reused_checkout_path_cannot_adopt_path_only_history(linked_worktree, monkeypatch):
    repo, wt = linked_worktree
    monkeypatch.setattr(server_module, "_ctx", None)
    monkeypatch.chdir(wt)
    previous = hgp_write_file("tracked.txt", "old checkout", "review")["op_id"]
    ctx = server_module._get_context()
    _insert_old_pending(ctx.db, "pending-old", wt / "tracked.txt", wt)
    ctx.db.execute("UPDATE operations SET metadata = json_remove(metadata, '$.hgp_checkout.id')")
    ctx.db.close()
    server_module._ctx = None

    monkeypatch.chdir(repo)
    _git("worktree", "remove", "--force", str(wt), cwd=repo)
    _git("worktree", "add", "-qb", "replacement", str(wt), cwd=repo)
    monkeypatch.chdir(wt)
    current = server_module._get_context()
    try:
        assert hgp_query_operations(scope="checkout")["operations"] == []
        assert current.db.get_operation("pending-old")["status"] == "PENDING"
        assert json.loads(current.db.get_operation(previous)["metadata"])["hgp_checkout"].get("id") is None
        assert previous in {row["op_id"] for row in hgp_file_history("tracked.txt")["operations"]}
        assert server_module.hgp_move_file("tracked.txt", "new.txt", "review")["status"] == "COMPLETED"
        assert current.db.get_operation(previous)["status"] == "COMPLETED"
    finally:
        current.db.close()


def test_completed_migration_does_not_request_a_write_lock(tmp_path):
    path = tmp_path / "hgp.db"
    writer, reader = Database(path), Database(path)
    writer.initialize()
    reader.initialize()
    try:
        assert writer.relativize_file_paths(tmp_path) == 0
        writer.begin_immediate()
        reader.execute("PRAGMA busy_timeout = 0")
        assert reader.relativize_file_paths(tmp_path) == 0
    finally:
        writer.close()
        reader.close()


def test_checkout_history_and_pending_recovery_follow_worktree_move(linked_worktree, tmp_path, monkeypatch):
    repo, wt = linked_worktree
    monkeypatch.setattr(server_module, "_ctx", None)
    monkeypatch.chdir(wt)
    old = hgp_write_file("tracked.txt", "first", "review")["op_id"]
    latest = hgp_write_file("tracked.txt", "current", "review")["op_id"]
    ctx = server_module._get_context()
    ctx.db.execute("UPDATE operations SET status='PENDING', created_at='2000-01-01T00:00:00Z' WHERE op_id=?", (latest,))
    ctx.db.close()
    server_module._ctx = None

    monkeypatch.chdir(repo)
    target = tmp_path / "renamed-worktree"
    _git("worktree", "move", str(wt), str(target), cwd=repo)
    # Reconcile from another checkout first. The old path is gone, but the
    # registration still exists and owns the pending operation.
    main = server_module._get_context()
    assert main.db.get_operation(latest)["status"] == "PENDING"
    main.db.close()
    server_module._ctx = None

    monkeypatch.chdir(target)
    current = server_module._get_context()
    assert current.db.get_operation(latest)["status"] == "COMPLETED"
    scoped = hgp_query_operations(scope="checkout", include_inactive=True)
    assert {op["op_id"] for op in scoped["operations"]} == {old, latest}
    move = server_module.hgp_move_file("tracked.txt", "renamed.txt", "review")
    assert move["status"] == "COMPLETED"
    assert current.db.get_operation(latest)["status"] == "INVALIDATED"
    current.db.close()
    server_module._ctx = None

    # A different checkout at the old path must not inherit the moved checkout's history.
    _git("worktree", "add", "-qb", "replacement", str(wt), cwd=repo)
    monkeypatch.chdir(wt)
    assert hgp_query_operations(scope="checkout", include_inactive=True)["operations"] == []
    server_module._get_context().db.close()
    server_module._ctx = None


def test_simultaneous_v6_migrations_preserve_original_backup(tmp_path, monkeypatch):
    import sqlite3
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    path = tmp_path / "hgp.db"
    backup = tmp_path / "pre-v6.bak"
    a, b = Database(path), Database(path)
    a.initialize()
    b.initialize()
    original_path = str(tmp_path / "tracked.txt")
    a.insert_operation(
        op_id="legacy", op_type="artifact", status="COMPLETED", agent_id="old",
        file_path=original_path, commit_seq=1, chain_hash="sha256:" + "a" * 64,
    )
    copying = Event()
    second_started = Event()
    connect = sqlite3.connect

    def synchronize_backup(path, *args, **kwargs):
        if str(path) == str(backup) and not copying.is_set():
            copying.set()
            assert second_started.wait(3)
        return connect(path, *args, **kwargs)

    def second_migration():
        assert b._conn is not None
        b._conn.set_trace_callback(
            lambda sql: second_started.set() if sql == "BEGIN IMMEDIATE" else None
        )
        return b.relativize_file_paths(tmp_path, backup_path=backup)

    monkeypatch.setattr(sqlite3, "connect", synchronize_backup)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(a.relativize_file_paths, tmp_path, backup)
            assert copying.wait(3)
            second = pool.submit(second_migration)
            assert sorted((first.result(timeout=6), second.result(timeout=6))) == [0, 1]
        with connect(backup) as snapshot:
            assert snapshot.execute("SELECT file_path FROM operations").fetchone() == (original_path,)
            assert snapshot.execute(
                "SELECT 1 FROM _hgp_migrations WHERE name = 'v6_relative_file_keys'"
            ).fetchone() is None
    finally:
        a.close()
        b.close()


def _insert_old_pending_with_id(db: Database, op_id: str, worktree: Path, checkout_id: str) -> None:
    _insert_old_pending(db, op_id, worktree / "f.txt", worktree)
    db.execute(
        "UPDATE operations SET metadata = json_set(metadata, '$.hgp_checkout.id', ?) WHERE op_id = ?",
        (checkout_id, op_id),
    )
    db.commit()


@pytest.mark.parametrize("locked", [False, True])
def test_reconcile_retires_ops_of_worktree_deleted_without_git(shared_db, tmp_path, locked) -> None:
    repo, worktree, db, ctx_for = shared_db
    checkout_id = ctx_for(worktree).reconciler.checkout_id
    assert checkout_id
    _insert_old_pending_with_id(db, "op-wt", worktree, checkout_id)
    if locked:  # e.g. on removable media: Git keeps it, so must HGP
        _git("worktree", "lock", str(worktree), cwd=repo)
    # Deleted outside Git: the registration stays behind as "prunable".
    worktree.rename(tmp_path / "elsewhere")
    assert (repo / ".git" / "worktrees" / worktree.name).is_dir()

    report = ctx_for(repo).reconciler.reconcile()
    assert report.pending_orphaned == (0 if locked else 1)
    assert db.get_operation("op-wt")["status"] == ("PENDING" if locked else "STALE_PENDING")


@pytest.mark.parametrize("content", [b"", b"\xff\xfe\n", b"not-an-id\n"])
def test_damaged_worktree_id_does_not_break_startup(shared_db, content) -> None:
    repo, worktree, db, ctx_for = shared_db
    marker = repo / ".git" / "worktrees" / worktree.name / "hgp-worktree-id"
    marker.write_bytes(content)
    _insert_old_pending(db, "op-live", worktree / "f.txt", worktree)

    # Every checkout reads every registration's marker; none may fail on it.
    ctx_for(repo).reconciler.reconcile()
    wt = ctx_for(worktree).reconciler
    assert wt.checkout_id is None  # path-only provenance, as before IDs
    wt.reconcile()
    assert marker.read_bytes() == content  # never silently replaced


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores permissions")
def test_read_only_worktree_registration_does_not_break_startup(shared_db) -> None:
    repo, worktree, _, ctx_for = shared_db
    admin = repo / ".git" / "worktrees" / worktree.name
    mode = admin.stat().st_mode
    admin.chmod(0o555)
    try:
        assert ctx_for(worktree).reconciler.checkout_id is None
    finally:
        admin.chmod(mode)


def test_worktree_id_without_hard_links(shared_db, monkeypatch) -> None:
    import errno

    repo, worktree, _, ctx_for = shared_db

    def no_links(self, target):
        raise OSError(errno.EPERM, "hard links not supported")

    monkeypatch.setattr(Path, "hardlink_to", no_links)
    checkout_id = ctx_for(worktree).reconciler.checkout_id
    assert checkout_id and len(checkout_id) == 32
    assert project.read_worktree_id(repo / ".git" / "worktrees" / worktree.name) == checkout_id
    assert [p.name for p in (repo / ".git" / "worktrees" / worktree.name).glob("tmp*")] == []


@pytest.mark.parametrize("unreadable_file", ["hgp-worktree-id", "gitdir"])
def test_inconclusive_registration_does_not_retire_pending_operations(
    shared_db, monkeypatch, caplog, unreadable_file,
):
    repo, worktree, db, ctx_for = shared_db
    checkout_id = ctx_for(worktree).reconciler.checkout_id
    assert checkout_id is not None
    _insert_old_pending_with_id(db, "op-wt", worktree, checkout_id)
    target = repo / ".git" / "worktrees" / worktree.name / unreadable_file
    read_text = Path.read_text

    def guarded_read(path, *args, **kwargs):
        if path == target:
            raise PermissionError("simulated unreadable worktree registration")
        return read_text(path, *args, **kwargs)

    main = ctx_for(repo).reconciler
    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", guarded_read)
        assert main.reconcile().pending_orphaned == 0
        assert db.get_operation("op-wt")["status"] == "PENDING"
        warnings = [record.message for record in caplog.records if record.levelname == "WARNING"]
        assert len(warnings) == 1
        assert "Orphan retirement deferred" in warnings[0]
        assert str(target.parent) in warnings[0]
        assert ("cannot read" if unreadable_file == "gitdir" else "worktree ID") in warnings[0]

    caplog.clear()
    assert main.reconcile().pending_orphaned == 0
    assert not caplog.records
    assert db.get_operation("op-wt")["status"] == "PENDING"


def test_worktree_never_opened_by_hgp_does_not_block_orphan_retirement(shared_db, tmp_path) -> None:
    repo, worktree, db, ctx_for = shared_db
    checkout_id = ctx_for(worktree).reconciler.checkout_id
    assert checkout_id
    _insert_old_pending_with_id(db, "op-wt", worktree, checkout_id)
    # A second worktree HGP never ran in: it has no ID marker, so it cannot own the op.
    _git("worktree", "add", "-q", str(tmp_path / "untouched"), "-b", "untouched", cwd=repo)
    worktree.rename(tmp_path / "elsewhere")  # the op's worktree is deleted outside Git

    report = ctx_for(repo).reconciler.reconcile()
    assert report.pending_orphaned == 1
    assert db.get_operation("op-wt")["status"] == "STALE_PENDING"
