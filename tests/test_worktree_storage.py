"""Linked-worktree storage sharing: hooks, installed layout, and summary scoping."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

import hgp.server as server_module
from hgp import project
from hgp.cas import CAS
from hgp.db import Database
from hgp.hooks import hgp_paths
from hgp.lease import LeaseManager
from hgp.reconciler import Reconciler
from hgp.server import HGPContext, hgp_create_operation, hgp_get_context, hgp_set_context

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
