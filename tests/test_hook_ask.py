"""Claude PreToolUse hook: ask before a native write in sessions that use HGP."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).parents[1] / "src" / "hgp" / "hooks" / "claude" / "pre_tool_use_hgp.py"


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / ".hgp").mkdir()
    return repo


def _transcript(tmp_path: Path, *blocks: dict[str, object]) -> Path:
    path = tmp_path / "transcript.jsonl"
    lines = [json.dumps({"type": "user", "message": {"content": "hi"}})]
    lines += [json.dumps({"type": "assistant", "message": {"content": [block]}}) for block in blocks]
    path.write_text("\n".join(lines) + "\n")
    return path


HGP_CALL = {"type": "tool_use", "name": "mcp__hgp__hgp_create_operation", "input": {}}
HGP_SEARCH = {"type": "tool_use", "name": "ToolSearch", "input": {"query": "select:mcp__hgp__hgp_write_file"}}
OTHER_CALL = {"type": "tool_use", "name": "Read", "input": {"file_path": "mcp__hgp__ in a path"}}


def _run(repo: Path, transcript: Path | None, **env: str) -> subprocess.CompletedProcess[str]:
    event: dict[str, object] = {"tool_name": "Write", "cwd": str(repo)}
    if transcript is not None:
        event["transcript_path"] = str(transcript)
    base = {k: v for k, v in os.environ.items() if k not in ("HGP_HOOK_BLOCK", "HGP_HOOK_ASK")}
    return subprocess.run(
        [sys.executable, str(HOOK)], input=json.dumps(event), cwd=repo,
        capture_output=True, text=True, env={**base, **env},
    )


def _asked(r: subprocess.CompletedProcess[str]) -> bool:
    if not r.stdout.strip():
        return False
    return json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"] == "ask"


@pytest.mark.parametrize("block", [HGP_CALL, HGP_SEARCH], ids=["hgp-call", "toolsearch"])
def test_asks_by_default_once_session_used_hgp(tmp_path: Path, block: dict[str, object]) -> None:
    repo = _repo(tmp_path)
    r = _run(repo, _transcript(tmp_path, block))
    assert r.returncode == 0
    assert _asked(r)
    assert "hgp_write_file" in json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"]


def test_no_ask_when_session_never_used_hgp(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    r = _run(repo, _transcript(tmp_path, OTHER_CALL))
    assert (r.returncode, _asked(r)) == (0, False)
    assert "Native `Write` detected" in r.stderr


@pytest.mark.parametrize("transcript", ["missing", "absent-key"])
def test_no_ask_without_readable_transcript(tmp_path: Path, transcript: str) -> None:
    repo = _repo(tmp_path)
    path = tmp_path / "nope.jsonl" if transcript == "missing" else None
    r = _run(repo, path)
    assert (r.returncode, _asked(r)) == (0, False)


def test_hgp_hook_ask_0_disables(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    r = _run(repo, _transcript(tmp_path, HGP_CALL), HGP_HOOK_ASK="0")
    assert (r.returncode, _asked(r)) == (0, False)


def test_block_policy_wins_over_ask(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    r = _run(repo, _transcript(tmp_path, HGP_CALL), HGP_HOOK_BLOCK="1")
    assert r.returncode == 2
    assert not _asked(r)


@pytest.mark.parametrize("setup", ["not-initialized", "paused"])
def test_no_ask_when_hgp_is_not_recording(tmp_path: Path, setup: str) -> None:
    repo = _repo(tmp_path)
    if setup == "not-initialized":
        (repo / ".hgp").rmdir()
    else:
        (repo / ".hgp" / "mode").write_text("off\n")
    r = _run(repo, _transcript(tmp_path, HGP_CALL))
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")
