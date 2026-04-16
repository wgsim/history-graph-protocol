"""Tests for SubagentStart/SubagentStop hooks and hgp_set_context / hgp_get_context."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

import hgp.server as server_module
from hgp.cas import CAS
from hgp.db import Database
from hgp.lease import LeaseManager
from hgp.reconciler import Reconciler
from hgp.server import (
    HGPContext,
    hgp_create_operation,
    hgp_get_context,
    hgp_reconcile,
    hgp_set_context,
)

START_HOOK = Path(__file__).parent.parent / "src/hgp/hooks/claude/subagent_start_hgp.py"
STOP_HOOK = Path(__file__).parent.parent / "src/hgp/hooks/claude/subagent_stop_hgp.py"


@pytest.fixture
def server_components(tmp_path: Path):
    hgp_dir = tmp_path / ".hgp"
    hgp_dir.mkdir()
    content_dir = hgp_dir / ".hgp_content"
    content_dir.mkdir()

    db = Database(hgp_dir / "hgp.db")
    db.initialize()
    cas = CAS(content_dir)
    lease_mgr = LeaseManager(db)
    reconciler = Reconciler(db, cas, content_dir)

    orig_ctx = server_module._ctx
    server_module._ctx = HGPContext(
        db=db, cas=cas, lease_mgr=lease_mgr, reconciler=reconciler,
        project_root=tmp_path,
    )

    yield {"db": db, "hgp_dir": hgp_dir, "tmp_path": tmp_path}

    server_module._ctx = orig_ctx
    db.close()


# ── hgp_set_context / hgp_get_context ───────────────────────────────────────

def test_set_and_get_context(server_components):
    op = hgp_create_operation(op_type="hypothesis", agent_id="claude-code")
    result = hgp_set_context(root_op_id=op["op_id"], agent_id="claude-code", session_id="sess-test-1")
    assert result["status"] == "ok"
    assert result["root_op_id"] == op["op_id"]
    assert result["session_id"] == "sess-test-1"

    ctx = hgp_get_context(session_id="sess-test-1")
    assert ctx["root_op_id"] == op["op_id"]
    assert ctx["agent_id"] == "claude-code"
    assert ctx["session_id"] == "sess-test-1"
    assert ctx["age_seconds"] >= 0


def test_get_context_no_file(server_components):
    result = hgp_get_context(session_id="sess-does-not-exist")
    assert result == {"status": "no_context"}


def test_set_context_unknown_op(server_components):
    result = hgp_set_context(root_op_id="op-nonexistent", agent_id="a", session_id="sess-x")
    assert result["error"] == "OP_NOT_FOUND"


def test_concurrent_sessions_no_collision(server_components):
    op_a = hgp_create_operation(op_type="hypothesis", agent_id="a")
    op_b = hgp_create_operation(op_type="hypothesis", agent_id="b")

    hgp_set_context(root_op_id=op_a["op_id"], agent_id="a", session_id="sess-a")
    hgp_set_context(root_op_id=op_b["op_id"], agent_id="b", session_id="sess-b")

    assert hgp_get_context("sess-a")["root_op_id"] == op_a["op_id"]
    assert hgp_get_context("sess-b")["root_op_id"] == op_b["op_id"]


def test_reconcile_removes_stale_context(server_components):
    hgp_dir: Path = server_components["hgp_dir"]
    op = hgp_create_operation(op_type="hypothesis", agent_id="a")
    hgp_set_context(root_op_id=op["op_id"], agent_id="a", session_id="sess-stale")

    stale_path = hgp_dir / "context-sess-stale.json"
    assert stale_path.exists()

    # Backdate set_at beyond TTL
    data = json.loads(stale_path.read_text())
    data["set_at"] = time.time() - 90000
    stale_path.write_text(json.dumps(data))

    result = hgp_reconcile(dry_run=False)
    assert "context-sess-stale.json" in result.get("stale_context_files_removed", [])
    assert not stale_path.exists()


def test_reconcile_dry_run_does_not_remove(server_components):
    hgp_dir: Path = server_components["hgp_dir"]
    op = hgp_create_operation(op_type="hypothesis", agent_id="a")
    hgp_set_context(root_op_id=op["op_id"], agent_id="a", session_id="sess-dry")

    stale_path = hgp_dir / "context-sess-dry.json"
    data = json.loads(stale_path.read_text())
    data["set_at"] = time.time() - 90000
    stale_path.write_text(json.dumps(data))

    result = hgp_reconcile(dry_run=True)
    assert "context-sess-dry.json" in result.get("stale_context_files_removed", [])
    assert stale_path.exists()  # not deleted in dry_run


# ── SubagentStart hook script ────────────────────────────────────────────────

def _run_hook(event: dict, cwd: Path, script: Path | None = None) -> tuple[int, str, str]:
    if script is None:
        script = START_HOOK
    result = subprocess.run(
        [sys.executable, str(script)],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        cwd=str(cwd),
    )
    return result.returncode, result.stdout, result.stderr


def test_start_hook_injects_context(tmp_path):
    hgp_dir = tmp_path / ".hgp"
    hgp_dir.mkdir()
    (tmp_path / ".git").mkdir()

    root_op_id = "op-abc123"
    session_id = "sess-hook-test"
    context_path = hgp_dir / f"context-{session_id}.json"
    context_path.write_text(json.dumps({
        "root_op_id": root_op_id,
        "agent_id": "claude-code",
        "session_id": session_id,
        "set_at": time.time(),
    }))

    event = {"hook_event_name": "SubagentStart", "session_id": session_id}
    rc, stdout, _ = _run_hook(event, cwd=tmp_path)
    assert rc == 0
    output = json.loads(stdout)
    ctx_text = output["hookSpecificOutput"]["additionalContext"]
    assert root_op_id in ctx_text
    assert "parent_op_ids" in ctx_text


def test_hook_no_context_file_is_silent(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".hgp").mkdir()

    event = {"hook_event_name": "SubagentStart", "session_id": "sess-missing"}
    rc, stdout, _ = _run_hook(event, cwd=tmp_path)
    assert rc == 0
    assert stdout.strip() == ""


def test_hook_wrong_event_is_silent(tmp_path):
    event = {"hook_event_name": "PreToolUse", "session_id": "sess-x"}
    rc, stdout, _ = _run_hook(event, cwd=tmp_path)
    assert rc == 0
    assert stdout.strip() == ""


def test_hook_no_session_id_is_silent(tmp_path):
    event = {"hook_event_name": "SubagentStart"}
    rc, stdout, _ = _run_hook(event, cwd=tmp_path)
    assert rc == 0
    assert stdout.strip() == ""


# ── SubagentStop hook script ─────────────────────────────────────────────────

def _make_transcript(path: Path, hgp_op_names: list[str]) -> None:
    """Write a minimal transcript JSONL with the given mcp__hgp__ tool_use entries."""
    entries = []
    for name in hgp_op_names:
        entries.append(json.dumps({
            "type": "assistant",
            "message": {
                "content": [{"type": "tool_use", "name": name, "id": "x", "input": {}}]
            }
        }))
    path.write_text("\n".join(entries) + "\n", encoding="utf-8")


def test_stop_hook_writes_summary(tmp_path):
    hgp_dir = tmp_path / ".hgp"
    hgp_dir.mkdir()
    (tmp_path / ".git").mkdir()

    transcript = tmp_path / "agent.jsonl"
    _make_transcript(transcript, ["mcp__hgp__hgp_write_file", "mcp__hgp__hgp_create_operation"])

    event = {
        "hook_event_name": "SubagentStop",
        "session_id": "sess-stop-test",
        "agent_id": "agent-abc",
        "agent_type": "general-purpose",
        "agent_transcript_path": str(transcript),
        "cwd": str(tmp_path),
    }
    rc, stdout, _ = _run_hook(event, cwd=tmp_path, script=STOP_HOOK)
    assert rc == 0
    assert stdout.strip() == ""  # no additionalContext output

    summaries = list(hgp_dir.glob("subagent-summary-sess-stop-test-*.json"))
    assert len(summaries) == 1
    data = json.loads(summaries[0].read_text())
    assert data["hgp_op_count"] == 2
    assert data["agent_type"] == "general-purpose"
    assert data["session_id"] == "sess-stop-test"


def test_stop_hook_zero_ops(tmp_path):
    hgp_dir = tmp_path / ".hgp"
    hgp_dir.mkdir()
    (tmp_path / ".git").mkdir()

    transcript = tmp_path / "agent.jsonl"
    _make_transcript(transcript, [])  # no HGP ops

    event = {
        "hook_event_name": "SubagentStop",
        "session_id": "sess-zero",
        "agent_id": "agent-xyz",
        "agent_type": "Explore",
        "agent_transcript_path": str(transcript),
        "cwd": str(tmp_path),
    }
    rc, _, _ = _run_hook(event, cwd=tmp_path, script=STOP_HOOK)
    assert rc == 0
    summaries = list(hgp_dir.glob("subagent-summary-sess-zero-*.json"))
    assert len(summaries) == 1
    assert json.loads(summaries[0].read_text())["hgp_op_count"] == 0


def test_stop_hook_wrong_event_is_silent(tmp_path):
    event = {"hook_event_name": "SubagentStart", "session_id": "x", "cwd": str(tmp_path)}
    rc, stdout, _ = _run_hook(event, cwd=tmp_path, script=STOP_HOOK)
    assert rc == 0
    assert stdout.strip() == ""


def test_get_context_returns_summaries(server_components):
    hgp_dir: Path = server_components["hgp_dir"]
    op = hgp_create_operation(op_type="hypothesis", agent_id="a")
    hgp_set_context(root_op_id=op["op_id"], agent_id="a", session_id="sess-sum")

    # Plant a summary file as the hook would
    summary = {"agent_id": "ag1", "agent_type": "Explore", "session_id": "sess-sum",
               "hgp_op_count": 3, "completed_at": time.time()}
    (hgp_dir / "subagent-summary-sess-sum-1000.json").write_text(json.dumps(summary))

    result = hgp_get_context(session_id="sess-sum", consume_summaries=True)
    assert "subagent_summaries" in result
    assert result["subagent_summaries"][0]["hgp_op_count"] == 3
    # consumed — file should be gone
    assert not (hgp_dir / "subagent-summary-sess-sum-1000.json").exists()


def test_get_context_no_consume(server_components):
    hgp_dir: Path = server_components["hgp_dir"]
    op = hgp_create_operation(op_type="hypothesis", agent_id="a")
    hgp_set_context(root_op_id=op["op_id"], agent_id="a", session_id="sess-noconsume")

    summary = {"agent_id": "ag1", "agent_type": "Explore", "session_id": "sess-noconsume",
               "hgp_op_count": 1, "completed_at": time.time()}
    (hgp_dir / "subagent-summary-sess-noconsume-2000.json").write_text(json.dumps(summary))

    hgp_get_context(session_id="sess-noconsume", consume_summaries=False)
    assert (hgp_dir / "subagent-summary-sess-noconsume-2000.json").exists()


def test_reconcile_removes_stale_summary(server_components):
    hgp_dir: Path = server_components["hgp_dir"]
    op = hgp_create_operation(op_type="hypothesis", agent_id="a")
    hgp_set_context(root_op_id=op["op_id"], agent_id="a", session_id="sess-stale-sum")

    stale = hgp_dir / "subagent-summary-sess-stale-sum-9000.json"
    stale.write_text(json.dumps({"completed_at": time.time() - 90000}))

    result = hgp_reconcile(dry_run=False)
    assert stale.name in result.get("stale_context_files_removed", [])
    assert not stale.exists()
