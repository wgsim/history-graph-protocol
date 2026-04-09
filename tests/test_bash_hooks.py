"""Subprocess-based tests for Pre/Post Bash HGP hooks.

Hooks are invoked by piping JSON to their stdin, mimicking the Claude Code
and Gemini CLI hook protocols.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_HOOKS_DIR = Path(__file__).parent.parent / ".claude" / "hooks"
_GEMINI_HOOKS_DIR = Path(__file__).parent.parent / ".gemini" / "hooks"
_PRE_HOOK = str(_HOOKS_DIR / "pre_bash_hgp.py")
_POST_HOOK = str(_HOOKS_DIR / "post_bash_hgp.py")
_GEMINI_PRE_HOOK = str(_GEMINI_HOOKS_DIR / "pre_bash_hgp.py")
_GEMINI_POST_HOOK = str(_GEMINI_HOOKS_DIR / "post_bash_hgp.py")


def _run_hook(hook_path: str, payload: dict, cwd: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, hook_path],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        cwd=cwd,
    )


def _bash_event(command: str) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": command}}


# ── Pre-Bash hook (Claude) ────────────────────────────────────────────────────

def test_pre_bash_detects_cp():
    """cp command triggers a HGP warning on stderr."""
    result = _run_hook(_PRE_HOOK, _bash_event("cp foo bar"))
    assert result.returncode == 0
    assert "[HGP]" in result.stderr
    assert "cp" in result.stderr


def test_pre_bash_detects_redirect():
    """Stdout redirect (>) triggers a warning."""
    result = _run_hook(_PRE_HOOK, _bash_event("echo hello > output.txt"))
    assert result.returncode == 0
    assert "[HGP]" in result.stderr


def test_pre_bash_detects_append_redirect():
    """Append redirect (>>) triggers a warning."""
    result = _run_hook(_PRE_HOOK, _bash_event("echo more >> log.txt"))
    assert result.returncode == 0
    assert "[HGP]" in result.stderr


def test_pre_bash_detects_sed_inplace():
    """sed -i triggers a warning."""
    result = _run_hook(_PRE_HOOK, _bash_event("sed -i 's/a/b/' file.txt"))
    assert result.returncode == 0
    assert "[HGP]" in result.stderr


def test_pre_bash_ignores_git_status():
    """git status is read-only — no warning."""
    result = _run_hook(_PRE_HOOK, _bash_event("git status"))
    assert result.returncode == 0
    assert result.stderr == ""


def test_pre_bash_ignores_git_log():
    """git log is read-only — no warning."""
    result = _run_hook(_PRE_HOOK, _bash_event("git log --oneline -5"))
    assert result.returncode == 0
    assert result.stderr == ""


def test_pre_bash_ignores_uv_run_pytest():
    """uv run pytest is not in the readonly prefix list but contains no mutating patterns."""
    result = _run_hook(_PRE_HOOK, _bash_event("uv run pytest tests/ -q"))
    assert result.returncode == 0
    # No mutating pattern matched — should be silent
    assert result.stderr == ""


def test_pre_bash_invalid_json():
    """Garbage stdin must not crash the hook (exit 0, no output)."""
    proc = subprocess.run(
        [sys.executable, _PRE_HOOK],
        input="not json at all!!!",
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0


def test_pre_bash_writes_marker_on_mutating_command():
    """Pre hook writes /tmp marker when a mutating command is detected."""
    result = _run_hook(_PRE_HOOK, _bash_event("rm old_file.txt"))
    assert result.returncode == 0
    assert "[HGP]" in result.stderr
    # Marker file: /tmp/.hgp_bash_mutating_<ppid-of-hook-process>
    # The hook uses os.getppid() which will be our test process pid
    marker = Path(f"/tmp/.hgp_bash_mutating_{os.getpid()}")
    # Task 5.6: assert the marker was actually written before cleaning it up
    assert marker.exists(), (
        f"Pre hook must write marker file {marker} for mutating commands"
    )
    marker.unlink(missing_ok=True)


# ── Post-Bash hook (Claude) ───────────────────────────────────────────────────

def test_post_bash_no_marker_silent():
    """No marker file → post hook runs silently without invoking git status."""
    # Ensure no stale marker exists
    marker = Path(f"/tmp/.hgp_bash_mutating_{os.getpid()}")
    marker.unlink(missing_ok=True)

    result = _run_hook(_POST_HOOK, _bash_event("echo done"))
    assert result.returncode == 0
    assert result.stderr == ""


def test_post_bash_no_git_repo():
    """Running post hook outside a git repo does not crash (exit 0)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Write marker so hook proceeds to git status
        marker = Path(f"/tmp/.hgp_bash_mutating_{os.getpid()}")
        marker.write_text("")
        try:
            result = _run_hook(_POST_HOOK, _bash_event("touch foo"), cwd=tmpdir)
            assert result.returncode == 0
        finally:
            marker.unlink(missing_ok=True)


# ── Gemini Pre-Bash hook ──────────────────────────────────────────────────────

def _gemini_shell_event(command: str) -> dict:
    return {"tool_name": "run_shell_command", "tool_input": {"command": command}}


def test_gemini_pre_bash_json_output_on_mutating():
    """Gemini pre hook outputs JSON systemMessage on stdout for mutating commands."""
    result = _run_hook(_GEMINI_PRE_HOOK, _gemini_shell_event("cp source dest"))
    assert result.returncode == 0
    assert result.stdout.strip(), "Expected JSON on stdout"
    data = json.loads(result.stdout.strip())
    assert "systemMessage" in data
    assert "[HGP]" in data["systemMessage"]


def test_gemini_pre_bash_silent_on_readonly():
    """Gemini pre hook produces no stdout for read-only commands."""
    result = _run_hook(_GEMINI_PRE_HOOK, _gemini_shell_event("git status"))
    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_pre_bash_detects_git_checkout():
    """git checkout (branch switch) triggers a warning — rewrites working tree."""
    result = _run_hook(_PRE_HOOK, _bash_event("git checkout some-branch"))
    assert result.returncode == 0
    assert "[HGP]" in result.stderr


def test_pre_bash_detects_git_restore():
    """git restore (file restore) triggers a warning — overwrites tracked files."""
    result = _run_hook(_PRE_HOOK, _bash_event("git restore src/hgp/server.py"))
    assert result.returncode == 0
    assert "[HGP]" in result.stderr


def test_pre_bash_detects_patch():
    """patch command triggers a warning — applies diffs that modify files."""
    result = _run_hook(_PRE_HOOK, _bash_event("patch -p1 < fix.diff"))
    assert result.returncode == 0
    assert "[HGP]" in result.stderr


def test_pre_bash_git_status_still_silent():
    """git status must remain silent even after adding new git mutating patterns."""
    result = _run_hook(_PRE_HOOK, _bash_event("git status --porcelain"))
    assert result.returncode == 0
    assert result.stderr == ""


def test_pre_bash_git_log_still_silent():
    """git log must remain silent."""
    result = _run_hook(_PRE_HOOK, _bash_event("git log --oneline -10"))
    assert result.returncode == 0
    assert result.stderr == ""


def test_gemini_pre_bash_wrong_tool_name_ignored():
    """Gemini pre hook ignores events with tool_name != 'shell'."""
    result = _run_hook(_GEMINI_PRE_HOOK, {"tool_name": "Bash", "tool_input": {"command": "rm x"}})
    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_gemini_pre_bash_detects_git_checkout():
    """Gemini pre hook emits JSON systemMessage for git checkout."""
    result = _run_hook(_GEMINI_PRE_HOOK, _gemini_shell_event("git checkout some-branch"))
    assert result.returncode == 0
    data = json.loads(result.stdout.strip())
    assert "[HGP]" in data["systemMessage"]


def test_gemini_pre_bash_detects_git_restore():
    """Gemini pre hook emits JSON systemMessage for git restore."""
    result = _run_hook(_GEMINI_PRE_HOOK, _gemini_shell_event("git restore src/hgp/server.py"))
    assert result.returncode == 0
    data = json.loads(result.stdout.strip())
    assert "[HGP]" in data["systemMessage"]


def test_gemini_pre_bash_detects_patch():
    """Gemini pre hook emits JSON systemMessage for patch command."""
    result = _run_hook(_GEMINI_PRE_HOOK, _gemini_shell_event("patch -p1 < fix.diff"))
    assert result.returncode == 0
    data = json.loads(result.stdout.strip())
    assert "[HGP]" in data["systemMessage"]


def test_gemini_pre_bash_git_status_silent():
    """Gemini pre hook is silent for git status after adding new patterns."""
    result = _run_hook(_GEMINI_PRE_HOOK, _gemini_shell_event("git status --porcelain"))
    assert result.returncode == 0
    assert result.stdout.strip() == ""


# ── Gemini Post-Bash hook ─────────────────────────────────────────────────────


def test_gemini_post_bash_no_marker_silent():
    """No marker → Gemini post hook runs silently."""
    marker = Path(f"/tmp/.hgp_bash_mutating_{os.getpid()}")
    marker.unlink(missing_ok=True)

    result = _run_hook(_GEMINI_POST_HOOK, _gemini_shell_event("echo done"))
    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_gemini_post_bash_reports_changes_as_json(tmp_path):
    """Gemini post hook emits JSON systemMessage when marker exists and files changed."""
    # Init a minimal git repo with a tracked file
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t.com"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "T"],
                   check=True, capture_output=True)
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("original")
    subprocess.run(["git", "-C", str(tmp_path), "add", "tracked.txt"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-m", "init"],
                   check=True, capture_output=True)
    # Modify so git status --porcelain reports it
    tracked.write_text("modified")

    marker = Path(f"/tmp/.hgp_bash_mutating_{os.getpid()}")
    marker.write_text("")
    try:
        result = _run_hook(_GEMINI_POST_HOOK, _gemini_shell_event("echo done"), cwd=str(tmp_path))
        assert result.returncode == 0
        assert result.stdout.strip(), "Expected JSON output"
        data = json.loads(result.stdout.strip())
        assert "systemMessage" in data
        assert "[HGP]" in data["systemMessage"]
        assert "tracked.txt" in data["systemMessage"]
    finally:
        marker.unlink(missing_ok=True)
