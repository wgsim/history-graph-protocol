"""Linked-worktree storage sharing: hooks, installed layout, and summary scoping."""

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
    hgp_get_context,
    hgp_query_operations,
    hgp_set_context,
    hgp_write_file,
)

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
    # Old worktree-local behaviour: main-repo policy is not visible, but no crash.
    r = _run(dest / script.name, {"tool_name": "Write"}, cwd=worktree)
    assert r.returncode == 0
    r = _run(dest / START_HOOK.name, {"hook_event_name": "SubagentStart", "session_id": "x"}, cwd=worktree)
    assert r.returncode == 0 and r.stdout == ""


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
        assert meta["hgp_checkout"] == {"root": str(worktree), "branch": "feature"}
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

    assert ids(hgp_query_operations()) == {wt_id}
    assert ids(hgp_query_operations(scope="repo")) == {main_id, wt_id}
    server_module._ctx = ctx_for(repo)
    assert ids(hgp_query_operations()) == {main_id}
    assert ids(hgp_query_operations(scope="repo")) == {main_id, wt_id}
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
