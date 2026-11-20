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

import pytest


@pytest.fixture(autouse=True)
def enabled_cwd(tmp_path, monkeypatch):
    repo = tmp_path / "enabled-repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / ".hgp").mkdir()
    monkeypatch.chdir(repo)


_SRC_HOOKS_DIR = Path(__file__).parent.parent / "src" / "hgp" / "hooks"
_PRE_HOOK = str(_SRC_HOOKS_DIR / "claude" / "pre_bash_hgp.py")
_POST_HOOK = str(_SRC_HOOKS_DIR / "claude" / "post_bash_hgp.py")
_GEMINI_PRE_HOOK = str(_SRC_HOOKS_DIR / "gemini" / "pre_bash_hgp.py")
_GEMINI_POST_HOOK = str(_SRC_HOOKS_DIR / "gemini" / "post_bash_hgp.py")

# Aliases kept for the mode-bypass tests (same paths, explicit names for clarity)
_SRC_PRE_HOOK_CLAUDE = _PRE_HOOK
_SRC_PRE_HOOK_GEMINI = _GEMINI_PRE_HOOK
_CODEX_PRE_HOOK = str(_SRC_HOOKS_DIR / "codex" / "pre_tool_use_hgp.py")
_CODEX_POST_HOOK = str(_SRC_HOOKS_DIR / "codex" / "post_tool_use_hgp.py")


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


def _real_repo(path: Path) -> Path:
    """An HGP-enabled git repository with one tracked file, committed."""
    path.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "t@t.com"], ["config", "user.name", "T"]):
        subprocess.run(["git", *args], cwd=path, check=True, capture_output=True)
    (path / ".hgp").mkdir()
    (path / "tracked.txt").write_text("original")
    subprocess.run(["git", "add", "tracked.txt"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True, capture_output=True)
    return path.resolve()


def _run_isolated(hook: str, payload: dict, tmp_path: Path, cwd: Path) -> subprocess.CompletedProcess:
    """Run a hook with TMPDIR inside tmp_path, so the per-user marker directory is the test's own."""
    (tmp_path / "tmp").mkdir(exist_ok=True)
    return subprocess.run(
        [sys.executable, hook], input=json.dumps(payload), capture_output=True, text=True, cwd=cwd,
        env={**os.environ, "TMPDIR": str(tmp_path / "tmp")},
    )


def _state_dir(tmp_path: Path, client: str) -> Path:
    return tmp_path / "tmp" / f"hgp-{client}-{os.getuid()}"


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


def test_pre_bash_writes_marker_on_mutating_command(tmp_path):
    """Pre hook records a git status baseline when a mutating command is detected.

    The marker now lives in a per-user directory under TMPDIR (hgp-claude-<uid>), keyed by the
    payload's tool_use_id, and holds the baseline: the old empty /tmp/.hgp_bash_mutating_<ppid> file
    could not tell what the command changed.
    """
    repo = _real_repo(tmp_path / "repo")
    event = {**_bash_event("rm old_file.txt"), "tool_use_id": "toolu_01", "cwd": str(repo)}
    result = _run_isolated(_PRE_HOOK, event, tmp_path, repo)
    assert result.returncode == 0
    assert "[HGP]" in result.stderr
    marker = _state_dir(tmp_path, "claude") / "bash-toolu-01.json"
    assert marker.exists(), f"Pre hook must write marker file {marker} for mutating commands"
    assert json.loads(marker.read_text())["baseline"] == {str(repo): []}


# ── Post-Bash hook (Claude) ───────────────────────────────────────────────────

def test_post_bash_no_marker_silent():
    """No marker file → post hook runs silently without invoking git status."""
    # Ensure no stale marker exists
    marker = Path(f"/tmp/.hgp_bash_mutating_{os.getpid()}")
    marker.unlink(missing_ok=True)

    result = _run_hook(_POST_HOOK, _bash_event("echo done"))
    assert result.returncode == 0
    assert result.stderr == ""


def test_post_bash_no_git_repo(tmp_path):
    """Running post hook outside a git repo does not crash (exit 0), and consumes the marker."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # A marker whose baseline root is not a git repository: the hook proceeds to git status
        state = _state_dir(tmp_path, "claude")
        state.mkdir(parents=True, mode=0o700)
        marker = state / f"bash-ppid-{os.getpid()}.json"
        marker.write_text(json.dumps({"pattern": "touch", "baseline": {tmpdir: []}}))
        result = _run_isolated(_POST_HOOK, _bash_event("touch foo"), tmp_path, Path(tmpdir))
        assert result.returncode == 0
        assert result.stdout == "" and result.stderr == ""
        assert not marker.exists()


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


def test_gemini_post_bash_marker_no_changes_emits_additional_context_only(tmp_path):
    """Marker present but no tracked-file changes → additionalContext only, no systemMessage."""
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    (tmp_path / ".hgp").mkdir()  # HGP is opt-in: hooks only speak in enabled repos

    marker = Path(f"/tmp/.hgp_bash_mutating_{os.getpid()}")
    marker.write_text(r"\bcp\b")
    try:
        result = _run_hook(_GEMINI_POST_HOOK, _gemini_shell_event("echo done"), cwd=str(tmp_path))
        assert result.returncode == 0
        assert result.stdout.strip(), "Expected JSON output"
        data = json.loads(result.stdout.strip())
        assert "hookSpecificOutput" in data
        assert "[HGP]" in data["hookSpecificOutput"]["additionalContext"]
        assert "systemMessage" not in data
    finally:
        marker.unlink(missing_ok=True)


def test_gemini_post_bash_reports_changes_as_json(tmp_path):
    """Marker present + tracked-file changes → both additionalContext and systemMessage."""
    # Init a minimal git repo with a tracked file
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    (tmp_path / ".hgp").mkdir()  # HGP is opt-in: hooks only speak in enabled repos
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
    marker.write_text(r"\bcp\b")
    try:
        result = _run_hook(_GEMINI_POST_HOOK, _gemini_shell_event("echo done"), cwd=str(tmp_path))
        assert result.returncode == 0
        assert result.stdout.strip(), "Expected JSON output"
        data = json.loads(result.stdout.strip())
        # agent channel
        assert "hookSpecificOutput" in data
        assert "[HGP]" in data["hookSpecificOutput"]["additionalContext"]
        assert "tracked.txt" in data["hookSpecificOutput"]["additionalContext"]
        # terminal channel
        assert "systemMessage" in data
        assert "tracked.txt" in data["systemMessage"]
    finally:
        marker.unlink(missing_ok=True)


# ── hgp mode bypass detection ─────────────────────────────────

def test_pre_bash_blocks_hgp_mode_claude():
    """Claude pre_bash blocks `hgp mode` via exit 2 so agents cannot change mode."""
    result = _run_hook(_SRC_PRE_HOOK_CLAUDE, _bash_event("hgp mode off"))
    assert result.returncode == 2
    assert "[HGP]" in result.stderr
    assert "Blocked" in result.stderr


def test_pre_bash_blocks_hgp_mode_gemini():
    """Gemini pre_bash blocks `hgp mode` via decision:deny so agents cannot change mode."""
    result = _run_hook(_SRC_PRE_HOOK_GEMINI, _gemini_shell_event("hgp mode off"))
    assert result.returncode == 0
    assert result.stdout.strip(), "Expected JSON output"
    data = json.loads(result.stdout.strip())
    assert data.get("decision") == "deny"
    assert "Blocked" in data.get("reason", "")


# ── Codex PreToolUse hook ─────────────────────────────────────

def test_codex_pre_tool_use_detects_mutating():
    """Codex PreToolUse hook warns on mutating commands via JSON systemMessage."""
    result = _run_hook(_CODEX_PRE_HOOK, _bash_event("cp foo bar"))
    assert result.returncode == 0
    assert result.stdout.strip(), "Expected JSON output"
    data = json.loads(result.stdout.strip())
    assert "systemMessage" in data
    assert "[HGP]" in data["systemMessage"]


def test_codex_pre_tool_use_passthrough_readonly():
    """Codex PreToolUse hook is silent for read-only commands."""
    result = _run_hook(_CODEX_PRE_HOOK, _bash_event("git status"))
    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_codex_pre_tool_use_blocks_hgp_mode():
    """Codex PreToolUse blocks `hgp mode` via permissionDecision:deny."""
    result = _run_hook(_CODEX_PRE_HOOK, _bash_event("hgp mode off"))
    assert result.returncode == 0
    assert result.stdout.strip(), "Expected JSON output"
    data = json.loads(result.stdout.strip())
    output = data.get("hookSpecificOutput", {})
    assert output.get("permissionDecision") == "deny"
    assert "Blocked" in output.get("permissionDecisionReason", "")


def test_codex_pre_tool_use_ignores_non_bash():
    """Codex PreToolUse hook ignores non-Bash tool events."""
    result = _run_hook(_CODEX_PRE_HOOK, {"tool_name": "apply_patch", "tool_input": {"command": "rm -rf /"}})
    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_codex_pre_tool_use_block_mode_denies_mutating(tmp_path):
    """Codex PreToolUse returns permissionDecision:deny in block mode."""
    (tmp_path / ".git").mkdir()
    policy_dir = tmp_path / ".hgp"
    policy_dir.mkdir()
    (policy_dir / "hook-policy").write_text("block")

    result = _run_hook(_CODEX_PRE_HOOK, _bash_event("cp foo bar"), cwd=str(tmp_path))
    assert result.returncode == 0
    data = json.loads(result.stdout.strip())
    output = data.get("hookSpecificOutput", {})
    assert output.get("permissionDecision") == "deny"
    assert "[HGP]" in output.get("permissionDecisionReason", "")


def test_codex_pre_tool_use_block_mode_does_not_write_marker(tmp_path):
    """Codex PreToolUse must NOT write a marker file when denying — denied commands
    don't execute, so a marker would cause a false PostToolUse advisory on the
    next Bash call. (The marker moved to a per-user directory under TMPDIR.)"""
    repo = _real_repo(tmp_path / "repo")
    (repo / ".hgp" / "hook-policy").write_text("block")

    result = _run_isolated(_CODEX_PRE_HOOK, {**_bash_event("cp foo bar"), "cwd": str(repo)}, tmp_path, repo)

    assert '"deny"' in result.stdout
    assert not _state_dir(tmp_path, "codex").exists(), "marker must not be written when command is denied"


def test_codex_pre_tool_use_advisory_mode_warns_mutating(tmp_path):
    """Codex PreToolUse returns systemMessage in advisory mode."""
    (tmp_path / ".git").mkdir()
    policy_dir = tmp_path / ".hgp"
    policy_dir.mkdir()
    (policy_dir / "hook-policy").write_text("advisory")

    result = _run_hook(_CODEX_PRE_HOOK, _bash_event("cp foo bar"), cwd=str(tmp_path))
    assert result.returncode == 0
    data = json.loads(result.stdout.strip())
    assert "systemMessage" in data
    assert "hookSpecificOutput" not in data


# ── Codex PostToolUse hook ────────────────────────────────────

def test_codex_post_tool_use_no_marker_passthrough():
    """PostToolUse hook is silent when no marker file exists."""
    # Ensure no stale marker from earlier pre-hook tests
    import os
    Path(f"/tmp/.hgp_bash_mutating_{os.getpid()}").unlink(missing_ok=True)
    result = _run_hook(_CODEX_POST_HOOK, _bash_event("echo done"))
    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_codex_post_tool_use_reports_changed_files(tmp_path):
    """PostToolUse hook reports changed files via hookSpecificOutput.additionalContext.

    Only what changed since the PreToolUse baseline is reported (it used to be the whole git status),
    so the PreToolUse hook runs first and the file is modified after it.
    """
    repo = _real_repo(tmp_path / "repo")
    pre = _run_isolated(_CODEX_PRE_HOOK, {**_bash_event("cp foo bar"), "cwd": str(repo)}, tmp_path, repo)
    assert pre.returncode == 0 and "systemMessage" in pre.stdout
    (repo / "tracked.txt").write_text("modified")

    result = _run_isolated(_CODEX_POST_HOOK, {**_bash_event("echo done"), "cwd": str(repo)}, tmp_path, repo)
    assert result.returncode == 0
    assert result.stdout.strip(), "Expected JSON output"
    data = json.loads(result.stdout.strip())
    output = data.get("hookSpecificOutput", {})
    assert output.get("hookEventName") == "PostToolUse"
    assert "[HGP]" in output.get("additionalContext", "")
    assert "tracked.txt" in output.get("additionalContext", "")
