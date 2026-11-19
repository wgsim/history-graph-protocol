"""Tests for `hgp install` — MCP registration, hooks, and instruction injection."""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Import the functions under test directly so we don't need the full CLI context.
from hgp.server import (
    _edit_codex_toml,
    _hook_backups_cmd,
    _inject_instructions,
    _install,
    _install_hooks_files,
    _install_mcp,
    _installed_policy_hooks,
    _toml_set_key,
    _update_agy_hooks_json,
    _update_hooks_settings,
)

# ── _inject_instructions ──────────────────────────────────────

def test_inject_instructions_new_file(tmp_path):
    md = tmp_path / "CLAUDE.md"
    result = _inject_instructions(md)
    assert result == "injected"
    content = md.read_text()
    assert "<!-- hgp-instructions-start -->" in content
    assert "<!-- hgp-instructions-end -->" in content


def test_instructions_mention_not_initialized(tmp_path):
    md = tmp_path / "CLAUDE.md"
    _inject_instructions(md)
    content = md.read_text()
    assert "HGP_NOT_INITIALIZED" in content
    assert "hgp init" in content


def test_inject_instructions_appends_to_existing(tmp_path):
    md = tmp_path / "CLAUDE.md"
    md.write_text("# My project\n\nSome existing content.\n")
    _inject_instructions(md)
    content = md.read_text()
    assert content.startswith("# My project")
    assert "<!-- hgp-instructions-start -->" in content


def test_inject_instructions_idempotent(tmp_path):
    md = tmp_path / "CLAUDE.md"
    _inject_instructions(md)
    first = md.read_text()
    result = _inject_instructions(md)
    assert result == "already_current"
    assert md.read_text() == first


def test_inject_instructions_updates_stale_block(tmp_path):
    md = tmp_path / "CLAUDE.md"
    md.write_text(
        "# Existing\n\n"
        "<!-- hgp-instructions-start -->\nOLD CONTENT\n<!-- hgp-instructions-end -->\n"
    )
    result = _inject_instructions(md)
    assert result == "updated"
    content = md.read_text()
    assert "OLD CONTENT" not in content
    assert "hgp_write_file" in content  # new block present


# ── _edit_codex_toml ──────────────────────────────────────────

def test_edit_codex_toml_creates_new(tmp_path):
    toml_path = tmp_path / ".codex" / "config.toml"
    result = _edit_codex_toml(toml_path, "/usr/bin/python3")
    assert result == "written"
    text = toml_path.read_text()
    assert "[mcp_servers.hgp]" in text
    assert 'command = "/usr/bin/python3"' in text
    assert '"-m"' in text
    # Hook activation flag must be present
    assert "codex_hooks = true" in text


def test_edit_codex_toml_appends_to_existing(tmp_path):
    toml_path = tmp_path / ".codex" / "config.toml"
    toml_path.parent.mkdir(parents=True)
    toml_path.write_text("[other_section]\nkey = true\n")
    result = _edit_codex_toml(toml_path, "/usr/bin/python3")
    assert result == "written"
    text = toml_path.read_text()
    assert "[other_section]" in text
    assert "[mcp_servers.hgp]" in text


def test_edit_codex_toml_idempotent(tmp_path):
    toml_path = tmp_path / ".codex" / "config.toml"
    _edit_codex_toml(toml_path, "/usr/bin/python3")
    first = toml_path.read_text()
    result = _edit_codex_toml(toml_path, "/usr/bin/python3")
    assert result == "already_current"
    assert toml_path.read_text() == first


def test_edit_codex_toml_updates_existing_section(tmp_path):
    toml_path = tmp_path / ".codex" / "config.toml"
    toml_path.parent.mkdir(parents=True)
    toml_path.write_text('[mcp_servers.hgp]\ncommand = "old_python"\nargs = ["-m", "hgp.server"]\n')
    result = _edit_codex_toml(toml_path, "/new/python3")
    assert result == "updated"
    assert 'command = "/new/python3"' in toml_path.read_text()


# ── _toml_set_key ─────────────────────────────────────────────

def test_toml_set_key_creates_section_and_key(tmp_path):
    toml_path = tmp_path / "config.toml"
    result = _toml_set_key(toml_path, "features", "codex_hooks", "true")
    assert result == "written"
    text = toml_path.read_text()
    assert "[features]" in text
    assert "codex_hooks = true" in text


def test_toml_set_key_appends_key_to_existing_section(tmp_path):
    toml_path = tmp_path / "config.toml"
    toml_path.write_text("[features]\nother = false\n")
    result = _toml_set_key(toml_path, "features", "codex_hooks", "true")
    assert result == "updated"
    text = toml_path.read_text()
    assert "other = false" in text
    assert "codex_hooks = true" in text


def test_toml_set_key_idempotent(tmp_path):
    toml_path = tmp_path / "config.toml"
    _toml_set_key(toml_path, "features", "codex_hooks", "true")
    first = toml_path.read_text()
    result = _toml_set_key(toml_path, "features", "codex_hooks", "true")
    assert result == "already_current"
    assert toml_path.read_text() == first


# ── _update_hooks_settings ────────────────────────────────────

def test_update_hooks_settings_claude_global_creates_file(tmp_path):
    settings = tmp_path / "settings.json"
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir()
    _update_hooks_settings("claude", settings, hooks_dir, "global")
    data = json.loads(settings.read_text())
    assert "hooks" in data
    # global scope → absolute path
    cmd = data["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert str(hooks_dir) in cmd


def test_update_hooks_settings_claude_includes_bash_hooks(tmp_path):
    settings = tmp_path / "settings.json"
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir()
    _update_hooks_settings("claude", settings, hooks_dir, "global")
    data = json.loads(settings.read_text())
    hook_events = set(data["hooks"].keys())
    # PreBash/PostBash removed; Bash hooks now use PreToolUse/PostToolUse with matcher
    assert "PreBash" not in hook_events
    assert "PostBash" not in hook_events
    assert "PreToolUse" in hook_events
    assert "PostToolUse" in hook_events
    # PreToolUse has two entries: one for Write/Edit (empty matcher), one for Bash
    pre_entries = data["hooks"]["PreToolUse"]
    bash_entry = next(e for e in pre_entries if e.get("matcher") == "Bash")
    assert "pre_bash_hgp.py" in bash_entry["hooks"][0]["command"]
    # PostToolUse has one Bash entry
    post_entries = data["hooks"]["PostToolUse"]
    assert post_entries[0]["matcher"] == "Bash"
    assert "post_bash_hgp.py" in post_entries[0]["hooks"][0]["command"]
    # SubagentStart and SubagentStop are registered
    assert "SubagentStart" in hook_events
    subagent_entries = data["hooks"]["SubagentStart"]
    assert any("subagent_start_hgp.py" in h["command"] for e in subagent_entries for h in e.get("hooks", []))
    assert "SubagentStop" in hook_events
    stop_entries = data["hooks"]["SubagentStop"]
    assert any("subagent_stop_hgp.py" in h["command"] for e in stop_entries for h in e.get("hooks", []))


def test_update_hooks_settings_claude_settings_match_installed_files(tmp_path):
    """Every generated Claude hook command must reference a file that _install_hooks_files copies."""
    import importlib.resources
    settings = tmp_path / "settings.json"
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir()
    _update_hooks_settings("claude", settings, hooks_dir, "global")

    # Collect basenames referenced in generated settings
    data = json.loads(settings.read_text())
    referenced = set()
    for entries in data["hooks"].values():
        for entry in entries:
            for h in entry.get("hooks", []):
                cmd = h.get("command", "")
                if "_hgp.py" in cmd:
                    referenced.add(cmd.split()[-1].split("/")[-1])

    # Collect basenames that _install_hooks_files would actually copy
    pkg_ref = importlib.resources.files("hgp.hooks.claude")
    installed = {
        item.name for item in pkg_ref.iterdir()
        if item.name.endswith(".py") and not item.name.startswith("__")
    }

    assert referenced <= installed, (
        f"Settings reference hook files not in the Claude package: {referenced - installed}"
    )


def test_update_hooks_settings_claude_local_uses_relative_path(tmp_path):
    settings = tmp_path / "settings.json"
    hooks_dir = tmp_path / ".claude" / "hooks"
    hooks_dir.mkdir(parents=True)
    _update_hooks_settings("claude", settings, hooks_dir, "local")
    data = json.loads(settings.read_text())
    cmd = data["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert cmd.startswith("python3 .claude/hooks/")


def test_update_hooks_settings_claude_cleans_up_legacy_prebash(tmp_path):
    """Re-running install removes deprecated PreBash/PostBash HGP entries."""
    settings = tmp_path / "settings.json"
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir()
    # Simulate old installation with PreBash/PostBash
    settings.write_text(json.dumps({
        "hooks": {
            "PreBash": [{"matcher": "", "hooks": [{"type": "command", "command": "/path/pre_bash_hgp.py"}]}],
            "PostBash": [{"matcher": "", "hooks": [{"type": "command", "command": "/path/post_bash_hgp.py"}]}],
        }
    }))
    _update_hooks_settings("claude", settings, hooks_dir, "global")
    data = json.loads(settings.read_text())
    hook_events = set(data["hooks"].keys())
    assert "PreBash" not in hook_events
    assert "PostBash" not in hook_events


def test_update_hooks_settings_claude_warns_on_mixed_legacy_prebash(tmp_path, capsys):
    """Reinstall warns when custom (non-HGP) entries remain under deprecated PreBash/PostBash."""
    settings = tmp_path / "settings.json"
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir()
    settings.write_text(json.dumps({
        "hooks": {
            "PreBash": [
                {"matcher": "", "hooks": [{"type": "command", "command": "my_custom_prebash.sh"}]},
                {"matcher": "", "hooks": [{"type": "command", "command": "/path/pre_bash_hgp.py"}]},
            ],
            "PostBash": [
                {"matcher": "", "hooks": [{"type": "command", "command": "my_custom_postbash.sh"}]},
                {"matcher": "", "hooks": [{"type": "command", "command": "/path/post_bash_hgp.py"}]},
            ],
        }
    }))
    _update_hooks_settings("claude", settings, hooks_dir, "global")
    data = json.loads(settings.read_text())
    hook_events = set(data["hooks"].keys())
    # deprecated keys preserved because custom entries remain
    assert "PreBash" in hook_events
    assert "PostBash" in hook_events
    # only custom entries survive (HGP entries removed)
    assert all("_hgp.py" not in h["command"] for e in data["hooks"]["PreBash"] for h in e["hooks"])
    assert all("_hgp.py" not in h["command"] for e in data["hooks"]["PostBash"] for h in e["hooks"])
    # warning emitted to stderr with correct destination event per deprecated event
    stderr = capsys.readouterr().err
    assert "PreBash" in stderr and "PreToolUse" in stderr
    assert "PostBash" in stderr and "PostToolUse" in stderr
    # PostBash must not incorrectly point to PreToolUse
    post_bash_line = next(line for line in stderr.splitlines() if "PostBash" in line)
    assert "PreToolUse" not in post_bash_line


def test_update_hooks_settings_preserves_non_hgp_hooks(tmp_path):
    settings = tmp_path / "settings.json"
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir()
    # Pre-populate with an existing non-HGP hook
    settings.write_text(json.dumps({
        "hooks": {
            "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "my_custom_hook.sh"}]}],
        }
    }))
    _update_hooks_settings("claude", settings, hooks_dir, "global")
    data = json.loads(settings.read_text())
    pre_tool_use = data["hooks"]["PreToolUse"]
    commands = [h["command"] for entry in pre_tool_use for h in entry.get("hooks", [])]
    assert any("my_custom_hook.sh" in c for c in commands), "existing hook was deleted"
    assert any("pre_tool_use_hgp.py" in c for c in commands), "HGP hook not added"


def test_update_hooks_settings_idempotent_no_duplicates(tmp_path):
    settings = tmp_path / "settings.json"
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir()
    _update_hooks_settings("claude", settings, hooks_dir, "global")
    first = json.loads(settings.read_text())
    _update_hooks_settings("claude", settings, hooks_dir, "global")
    second = json.loads(settings.read_text())
    # Running twice must not add duplicate HGP entries
    assert len(second["hooks"]["PreToolUse"]) == len(first["hooks"]["PreToolUse"])


def test_update_hooks_settings_merges_existing_keys(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"theme": "dark", "someOtherKey": 42}))
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir()
    _update_hooks_settings("claude", settings, hooks_dir, "global")
    data = json.loads(settings.read_text())
    assert data["theme"] == "dark"
    assert data["someOtherKey"] == 42
    assert "hooks" in data


def test_update_hooks_settings_gemini_global(tmp_path):
    settings = tmp_path / "settings.json"
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir()
    _update_hooks_settings("gemini", settings, hooks_dir, "global")
    data = json.loads(settings.read_text())
    assert "BeforeTool" in data["hooks"]
    assert "AfterTool" in data["hooks"]
    assert "BeforeShell" not in data["hooks"]
    assert "AfterShell" not in data["hooks"]
    before_cmds = [h["command"] for h in data["hooks"]["BeforeTool"][0]["hooks"]]
    assert any("pre_tool_use_hgp.py" in c for c in before_cmds)
    assert any("pre_bash_hgp.py" in c for c in before_cmds)
    after_cmds = [h["command"] for h in data["hooks"]["AfterTool"][0]["hooks"]]
    assert any("post_tool_use_hgp.py" in c for c in after_cmds)
    assert any("post_bash_hgp.py" in c for c in after_cmds)


def test_update_hooks_settings_gemini_removes_stale_shell_events(tmp_path):
    settings = tmp_path / "settings.json"
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir()
    # Simulate stale settings written by an older hgp version
    stale = {
        "hooks": {
            "BeforeShell": [{"matcher": "", "hooks": [{"type": "command", "command": "python3 /x/pre_bash_hgp.py"}]}],
            "AfterShell": [{"matcher": "", "hooks": [{"type": "command", "command": "python3 /x/post_bash_hgp.py"}]}],
        }
    }
    settings.write_text(json.dumps(stale))
    _update_hooks_settings("gemini", settings, hooks_dir, "global")
    data = json.loads(settings.read_text())
    assert "BeforeShell" not in data["hooks"]
    assert "AfterShell" not in data["hooks"]
    assert "BeforeTool" in data["hooks"]
    assert "AfterTool" in data["hooks"]


def test_update_hooks_settings_codex_global(tmp_path):
    settings = tmp_path / "hooks.json"
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir()
    _update_hooks_settings("codex", settings, hooks_dir, "global")
    data = json.loads(settings.read_text())
    assert "PreToolUse" in data["hooks"]
    assert "PostToolUse" in data["hooks"]
    pre_cmd = data["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert "pre_tool_use_hgp.py" in pre_cmd


def test_update_hooks_settings_codex_settings_match_installed_files(tmp_path):
    """Every generated Codex hook command must reference a file that _install_hooks_files copies."""
    import importlib.resources
    settings = tmp_path / "hooks.json"
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir()
    _update_hooks_settings("codex", settings, hooks_dir, "global")

    data = json.loads(settings.read_text())
    referenced = set()
    for entries in data["hooks"].values():
        for entry in entries:
            for h in entry.get("hooks", []):
                cmd = h.get("command", "")
                if "_hgp.py" in cmd:
                    referenced.add(cmd.split()[-1].split("/")[-1])

    pkg_ref = importlib.resources.files("hgp.hooks.codex")
    installed = {
        item.name for item in pkg_ref.iterdir()
        if item.name.endswith(".py") and not item.name.startswith("__")
    }

    assert referenced <= installed, (
        f"Settings reference hook files not in the Codex package: {referenced - installed}"
    )


# ── _install_mcp ──────────────────────────────────────────────

def _mock_subprocess_ok(*_args, **_kwargs):
    m = MagicMock()
    m.returncode = 0
    m.stderr = ""
    return m


def _mock_subprocess_fail(*_args, **_kwargs):
    m = MagicMock()
    m.returncode = 1
    m.stderr = "command failed"
    return m


def test_install_mcp_claude_global(tmp_path):
    with patch("shutil.which", return_value="/usr/bin/claude"), \
         patch("subprocess.run", side_effect=_mock_subprocess_ok) as mock_run:
        ok, msg = _install_mcp("claude", "global", "/usr/bin/python3")
    assert ok
    cmd = mock_run.call_args[0][0]
    assert "--scope=user" in cmd
    assert "hgp" in cmd


def test_install_mcp_claude_local(tmp_path):
    with patch("shutil.which", return_value="/usr/bin/claude"), \
         patch("subprocess.run", side_effect=_mock_subprocess_ok) as mock_run:
        ok, _ = _install_mcp("claude", "local", "/usr/bin/python3")
    assert ok
    cmd = mock_run.call_args[0][0]
    assert "--scope=local" in cmd


def test_install_mcp_cli_missing():
    with patch("shutil.which", return_value=None):
        ok, msg = _install_mcp("claude", "global", "/usr/bin/python3")
    assert not ok
    assert "not found" in msg


def test_install_mcp_cli_error():
    with patch("shutil.which", return_value="/usr/bin/claude"), \
         patch("subprocess.run", side_effect=_mock_subprocess_fail):
        ok, msg = _install_mcp("claude", "global", "/usr/bin/python3")
    assert not ok
    assert "CLI error" in msg


def test_install_mcp_gemini_local():
    with patch("shutil.which", return_value="/usr/bin/gemini"), \
         patch("subprocess.run", side_effect=_mock_subprocess_ok) as mock_run:
        ok, _ = _install_mcp("gemini", "local", "/usr/bin/python3")
    assert ok
    cmd = mock_run.call_args[0][0]
    assert "--scope=project" in cmd


# ── _install (top-level dispatcher) ──────────────────────────

def _make_git_repo(tmp_path: Path) -> Path:
    """Create a minimal git repo in tmp_path and return it."""
    (tmp_path / ".git").mkdir()
    return tmp_path


def test_install_unknown_flag(capsys):
    with pytest.raises(SystemExit) as exc_info:
        _install(["--unknown"])
    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "unknown flag" in captured.err


def test_install_claude_only_skips_others(tmp_path, capsys):
    repo = _make_git_repo(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    with patch("hgp.server.find_project_root", return_value=repo), \
         patch("pathlib.Path.home", return_value=home), \
         patch("hgp.server._install_mcp", return_value=(True, "registered")) as mock_mcp, \
         patch("hgp.server._install_hooks_files", return_value=[]), \
         patch("hgp.server._update_hooks_settings"), \
         patch("hgp.server._inject_instructions", return_value="injected"):
        _install(["--claude"])

    # Only claude calls should have been made
    clients_called = [call[0][0] for call in mock_mcp.call_args_list]
    assert clients_called == ["claude"]


def test_install_local_uses_project_paths(tmp_path, capsys):
    repo = _make_git_repo(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    with patch("hgp.server.find_project_root", return_value=repo), \
         patch("pathlib.Path.home", return_value=home), \
         patch("hgp.server._install_mcp", return_value=(True, "registered")), \
         patch("hgp.server._install_hooks_files", return_value=[]), \
         patch("hgp.server._update_hooks_settings") as mock_settings, \
         patch("hgp.server._inject_instructions", return_value="injected") as mock_instr:
        _install(["--claude", "--local"])

    # settings path should be inside the repo, not home
    settings_path_arg = mock_settings.call_args[0][1]
    assert str(settings_path_arg).startswith(str(repo))

    # instruction path should be project CLAUDE.md
    instr_path_arg = mock_instr.call_args[0][0]
    assert instr_path_arg == repo / "CLAUDE.md"


def test_install_all_global(tmp_path, capsys):
    repo = _make_git_repo(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    with patch("hgp.server.find_project_root", return_value=repo), \
         patch("pathlib.Path.home", return_value=home), \
         patch("hgp.server._install_mcp", return_value=(True, "registered")) as mock_mcp, \
         patch("hgp.server._install_hooks_files", return_value=[]), \
         patch("hgp.server._update_hooks_settings"), \
         patch("hgp.server._inject_instructions", return_value="injected"), \
         patch("shutil.which", return_value=None):  # no agy on PATH: it is not installed unasked
        _install([])

    clients_called = [call[0][0] for call in mock_mcp.call_args_list]
    assert set(clients_called) == {"claude", "gemini", "codex"}


def test_install_codex_local_uses_toml(tmp_path, capsys):
    repo = _make_git_repo(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    with patch("hgp.server.find_project_root", return_value=repo), \
         patch("pathlib.Path.home", return_value=home), \
         patch("hgp.server._install_mcp") as mock_mcp, \
         patch("hgp.server._edit_codex_toml", return_value="written") as mock_toml, \
         patch("hgp.server._inject_instructions", return_value="injected"):
        _install(["--codex", "--local"])

    # should use TOML, not CLI mcp install
    mock_mcp.assert_not_called()
    mock_toml.assert_called_once()


# ── Codex local install — E2E artifact check ─────────────────

def test_install_codex_local_artifact_tree(tmp_path, capsys):
    """_install --codex --local must produce all expected artifacts with correct content."""
    repo = _make_git_repo(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    with patch("hgp.server.find_project_root", return_value=repo), \
         patch("pathlib.Path.home", return_value=home):
        _install(["--codex", "--local"])

    # config.toml — MCP section + feature flag
    toml_path = repo / ".codex" / "config.toml"
    assert toml_path.exists(), "config.toml not created"
    toml_text = toml_path.read_text()
    assert "[mcp_servers.hgp]" in toml_text
    assert "codex_hooks = true" in toml_text

    # hooks directory — at least one hook file copied
    hooks_dir = repo / ".codex" / "hooks"
    assert hooks_dir.is_dir(), ".codex/hooks/ not created"
    hook_files = list(hooks_dir.glob("*_hgp.py"))
    assert hook_files, "no hook files installed into .codex/hooks/"

    # hooks.json — PreToolUse and PostToolUse registered
    hooks_json = repo / ".codex" / "hooks.json"
    assert hooks_json.exists(), "hooks.json not created"
    data = json.loads(hooks_json.read_text())
    assert "PreToolUse" in data["hooks"], "PreToolUse missing from hooks.json"
    assert "PostToolUse" in data["hooks"], "PostToolUse missing from hooks.json"

    # global home must NOT be touched for local install
    assert not (home / ".codex").exists(), "global ~/.codex written during --local install"


# ── install-hooks deprecation notice ─────────────────────────

def test_install_hooks_deprecation_warning(capsys):
    """hgp install-hooks should print a deprecation warning before running."""
    with patch("sys.argv", ["hgp", "install-hooks", "--claude"]), \
         patch("hgp.server._install_hooks") as mock_ih:
        from hgp.server import run
        run()
    captured = capsys.readouterr()
    assert "deprecated" in captured.err.lower()
    mock_ih.assert_called_once_with(["--claude"])


@pytest.mark.parametrize("client,event,matcher", [
    ("claude", "PreToolUse", "Bash"),
    ("codex", "PreToolUse", ""),
])
def test_update_hooks_settings_keeps_custom_hooks_sharing_an_hgp_entry(tmp_path, client, event, matcher):
    """A user hook grouped with an HGP hook in one entry must survive reinstalling."""
    settings = tmp_path / "settings.json"
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir()
    custom = {"type": "command", "command": "python3 /home/u/.claude/hooks/block_rm.py", "onFailure": "block"}
    stale = {"type": "command", "command": f"python3 {hooks_dir}/old_location/pre_bash_hgp.py"}
    settings.write_text(json.dumps({"hooks": {event: [{"matcher": matcher, "hooks": [custom, stale]}]}}))

    for _ in range(2):  # idempotent
        _update_hooks_settings(client, settings, hooks_dir, "global")
    entries = json.loads(settings.read_text())["hooks"][event]
    commands = [h["command"] for e in entries for h in e["hooks"]]
    assert commands.count(custom["command"]) == 1
    assert any(e["hooks"] == [custom] and e["matcher"] == matcher for e in entries)
    assert stale["command"] not in commands  # the HGP hook itself is still replaced
    assert len([c for c in commands if c.endswith("_hgp.py")]) == len(set(c for c in commands if c.endswith("_hgp.py")))


def test_update_hooks_settings_deprecated_event_keeps_custom_hook_in_shared_entry(tmp_path):
    settings = tmp_path / "settings.json"
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir()
    custom = {"type": "command", "command": "python3 /home/u/mine.py"}
    hgp = {"type": "command", "command": "python3 /x/pre_bash_hgp.py"}
    settings.write_text(json.dumps({"hooks": {"PreBash": [{"matcher": "", "hooks": [custom, hgp]}]}}))
    _update_hooks_settings("claude", settings, hooks_dir, "global")
    assert json.loads(settings.read_text())["hooks"]["PreBash"] == [{"matcher": "", "hooks": [custom]}]


# ── agy (Antigravity CLI) ─────────────────────────────────────

AGY_MATCHER = "write_to_file|replace_file_content|multi_replace_file_content|write_file|edit_file|run_command"


def _agy_paths(home: Path) -> tuple[Path, Path]:
    config = home / ".gemini" / "config"
    return config / "hooks.json", config / "hooks"


def test_update_agy_hooks_json_creates_file(tmp_path):
    hooks_json, hooks_dir = _agy_paths(tmp_path)
    assert _update_agy_hooks_json(hooks_json, hooks_dir) == "written"
    data = json.loads(hooks_json.read_text())
    assert list(data) == ["hgp"]
    assert list(data["hgp"]) == ["PreToolUse"]  # no PostToolUse/PreInvocation, and not disabled
    (group,) = data["hgp"]["PreToolUse"]
    assert group["matcher"] == AGY_MATCHER
    (hook,) = group["hooks"]
    assert hook == {"type": "command", "command": f"python3 {hooks_dir / 'pre_tool_use_hgp.py'}", "timeout": 10}


def test_agy_hooks_json_matcher_covers_exactly_the_hook_tools(tmp_path):
    import re

    from hgp.hooks.agy import pre_tool_use_hgp as agy_hook

    hooks_json, hooks_dir = _agy_paths(tmp_path)
    _update_agy_hooks_json(hooks_json, hooks_dir)
    matcher = json.loads(hooks_json.read_text())["hgp"]["PreToolUse"][0]["matcher"]
    assert set(matcher.split("|")) == {*agy_hook.HGP_TOOLS, "run_command"}
    for name in (*agy_hook.HGP_TOOLS, "run_command"):
        assert re.fullmatch(matcher, name)
    for name in ("call_mcp_tool", "read_file", "view_file", "write_file_x", ""):
        assert not re.fullmatch(matcher, name)  # HGP's own writes go through call_mcp_tool


def test_update_agy_hooks_json_command_survives_spaces_in_path(tmp_path):
    hooks_json, hooks_dir = _agy_paths(tmp_path / "my home")
    _update_agy_hooks_json(hooks_json, hooks_dir)
    command = json.loads(hooks_json.read_text())["hgp"]["PreToolUse"][0]["hooks"][0]["command"]
    assert shlex.split(command) == ["python3", str(hooks_dir / "pre_tool_use_hgp.py")]


def test_update_agy_hooks_json_keeps_other_named_hooks(tmp_path):
    hooks_json, hooks_dir = _agy_paths(tmp_path)
    hooks_json.parent.mkdir(parents=True)
    lint = {"PostToolUse": [{"matcher": "run_command", "hooks": [{"command": "./lint.sh", "timeout": 5}]}]}
    gate = {"enabled": False, "PreToolUse": [{"matcher": "run_command", "hooks": [{"command": "./gate.sh"}]}]}
    hooks_json.write_text(json.dumps({"lint": lint, "gate": gate}))
    assert _update_agy_hooks_json(hooks_json, hooks_dir) == "updated"
    data = json.loads(hooks_json.read_text())
    assert data["lint"] == lint
    assert data["gate"] == gate
    assert set(data) == {"lint", "gate", "hgp"}


def test_update_agy_hooks_json_replaces_stale_hgp_entry_in_place(tmp_path):
    hooks_json, hooks_dir = _agy_paths(tmp_path)
    hooks_json.parent.mkdir(parents=True)
    stale = {
        "enabled": False,
        "PreToolUse": [{"matcher": "old", "hooks": [{"type": "command", "command": "python3 /old/hook.py"}]}],
        "PostToolUse": [{"matcher": "", "hooks": [{"command": "./old.sh"}]}],
    }
    hooks_json.write_text(json.dumps({"a": {"Stop": [{"command": "a.sh"}]}, "hgp": stale, "z": {"Stop": []}}))
    _update_agy_hooks_json(hooks_json, hooks_dir)
    data = json.loads(hooks_json.read_text())
    assert list(data) == ["a", "hgp", "z"]  # replaced where it was, neighbours untouched
    assert list(data["hgp"]) == ["PreToolUse"]
    assert data["hgp"]["PreToolUse"][0]["matcher"] == AGY_MATCHER
    assert "/old/" not in hooks_json.read_text()
    assert data["a"] == {"Stop": [{"command": "a.sh"}]}


def test_update_agy_hooks_json_idempotent(tmp_path):
    hooks_json, hooks_dir = _agy_paths(tmp_path)
    hooks_json.parent.mkdir(parents=True)
    hooks_json.write_text(json.dumps({"other": {"Stop": [{"command": "x.sh"}]}}, indent=4))
    _update_agy_hooks_json(hooks_json, hooks_dir)
    first = hooks_json.read_bytes()
    assert _update_agy_hooks_json(hooks_json, hooks_dir) == "already_current"
    assert hooks_json.read_bytes() == first
    assert hooks_json.read_text().endswith("\n")


@pytest.mark.parametrize("content", ["{not json", "[]", '"hgp"', "null", "42"])
def test_update_agy_hooks_json_refuses_to_overwrite_unreadable_file(tmp_path, content):
    hooks_json, hooks_dir = _agy_paths(tmp_path)
    hooks_json.parent.mkdir(parents=True)
    hooks_json.write_text(content)
    with pytest.raises(ValueError, match="hooks.json"):
        _update_agy_hooks_json(hooks_json, hooks_dir)
    assert hooks_json.read_text() == content


def test_update_agy_hooks_json_treats_blank_file_as_empty(tmp_path):
    hooks_json, hooks_dir = _agy_paths(tmp_path)
    hooks_json.parent.mkdir(parents=True)
    hooks_json.write_text("\n")
    assert _update_agy_hooks_json(hooks_json, hooks_dir) == "updated"
    assert list(json.loads(hooks_json.read_text())) == ["hgp"]


def test_install_mcp_agy_is_global_only():
    with patch("shutil.which", return_value="/opt/agy"), patch("subprocess.run") as mock_run:
        ok, msg = _install_mcp("agy", "local", "/usr/bin/python3")
    assert not ok
    assert "global" in msg
    mock_run.assert_not_called()


def test_install_mcp_agy_global():
    with patch("shutil.which", return_value="/opt/agy") as mock_which, \
         patch("subprocess.run", side_effect=_mock_subprocess_ok) as mock_run:
        ok, msg = _install_mcp("agy", "global", "/usr/bin/python3")
    assert (ok, msg) == (True, "registered")
    mock_which.assert_called_with("agy")
    assert mock_run.call_args[0][0] == ["/opt/agy", "mcp", "add", "hgp", "/usr/bin/python3", "-m", "hgp.server"]


def test_install_mcp_agy_cli_missing_and_error():
    with patch("shutil.which", return_value=None):
        ok, msg = _install_mcp("agy", "global", "/usr/bin/python3")
    assert not ok
    assert "agy CLI not found" in msg
    with patch("shutil.which", return_value="/opt/agy"), patch("subprocess.run", side_effect=_mock_subprocess_fail):
        ok, msg = _install_mcp("agy", "global", "/usr/bin/python3")
    assert not ok
    assert "CLI error" in msg


def test_install_usage_mentions_agy(capsys):
    with pytest.raises(SystemExit):
        _install(["--bogus"])
    assert "--agy" in capsys.readouterr().err


def test_install_agy_installs_hook_files_hooks_json_and_instructions(tmp_path, capsys):
    home = tmp_path / "home"
    home.mkdir()
    with patch("pathlib.Path.home", return_value=home), \
         patch("hgp.server._install_mcp", return_value=(True, "registered")) as mock_mcp:
        _install(["--agy"])

    assert [call[0][0] for call in mock_mcp.call_args_list] == ["agy"]
    hooks_json, hooks_dir = _agy_paths(home)
    assert {p.name for p in hooks_dir.iterdir()} == {"pre_tool_use_hgp.py", "hgp_paths.py"}
    data = json.loads(hooks_json.read_text())
    assert data["hgp"]["PreToolUse"][0]["hooks"][0]["command"] == f"python3 {hooks_dir / 'pre_tool_use_hgp.py'}"
    assert "<!-- hgp-instructions-start -->" in (home / ".gemini" / "GEMINI.md").read_text()
    out = capsys.readouterr().out
    assert "agy" in out
    assert "✗" not in out
    # Gemini CLI's own files, and the other clients, are not touched
    assert not (home / ".gemini" / "hooks").exists()
    assert not (home / ".gemini" / "settings.json").exists()
    assert not (home / ".claude").exists()
    assert not (home / ".codex").exists()


def test_installed_agy_hook_is_self_contained(tmp_path):
    """The installed copy finds hgp_paths.py beside it (no hgp package): mode off yields to block policy."""
    home = tmp_path / "home"
    home.mkdir()
    with patch("pathlib.Path.home", return_value=home), \
         patch("hgp.server._install_mcp", return_value=(True, "registered")):
        _install(["--agy"])
    hook = _agy_paths(home)[1] / "pre_tool_use_hgp.py"
    repo = tmp_path / "repo"
    (repo / ".hgp").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / ".hgp" / "hook-policy").write_text('{"block_mode": true}')
    event = json.dumps({"toolCall": {"name": "write_file", "args": {}}, "cwd": str(repo)})

    def run() -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, "-S", str(hook)], input=event, cwd=tmp_path,
                              capture_output=True, text=True)

    assert json.loads(run().stdout)["decision"] == "deny"
    (repo / ".hgp" / "mode").write_text("off\n")  # only hgp_paths.hgp_mode knows this
    assert run().stdout == ""


def test_install_agy_local_is_skipped_with_message(tmp_path, capsys):
    repo = _make_git_repo(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    with patch("hgp.server.find_project_root", return_value=repo), \
         patch("pathlib.Path.home", return_value=home), \
         patch("hgp.server._install_mcp") as mock_mcp:
        _install(["--agy", "--local"])

    mock_mcp.assert_not_called()
    out = capsys.readouterr().out
    assert "agy" in out
    assert "not supported" in out
    assert not (home / ".gemini").exists()
    assert not (repo / ".gemini").exists()


@pytest.mark.parametrize("flag", ["--claude", "--gemini", "--codex"])
def test_install_client_flag_leaves_agy_out_even_when_on_path(tmp_path, flag):
    repo = _make_git_repo(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    with patch("hgp.server.find_project_root", return_value=repo), \
         patch("pathlib.Path.home", return_value=home), \
         patch("shutil.which", return_value="/opt/agy"), \
         patch("hgp.server._install_mcp", return_value=(True, "registered")) as mock_mcp, \
         patch("hgp.server._install_hooks_files", return_value=[]), \
         patch("hgp.server._update_hooks_settings"), \
         patch("hgp.server._inject_instructions", return_value="injected"):
        _install([flag])

    assert "agy" not in [call[0][0] for call in mock_mcp.call_args_list]
    assert not (home / ".gemini" / "config").exists()


def test_install_all_global_includes_agy_only_when_found(tmp_path):
    repo = _make_git_repo(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    with patch("hgp.server.find_project_root", return_value=repo), \
         patch("pathlib.Path.home", return_value=home), \
         patch("shutil.which", return_value="/opt/agy"), \
         patch("hgp.server._install_mcp", return_value=(True, "registered")) as mock_mcp, \
         patch("hgp.server._install_hooks_files", return_value=[]), \
         patch("hgp.server._update_hooks_settings"), \
         patch("hgp.server._inject_instructions", return_value="injected"):
        _install([])

    assert {call[0][0] for call in mock_mcp.call_args_list} == {"claude", "gemini", "codex", "agy"}
    assert _agy_paths(home)[0].is_file()


def test_install_hooks_files_agy_backs_up_modified_hook(tmp_path):
    dest = tmp_path / "hooks"
    dest.mkdir()
    (dest / "pre_tool_use_hgp.py").write_text("# customised\n")
    _install_hooks_files("agy", dest)
    (backup,) = [p for p in dest.iterdir() if ".bak-" in p.name]
    assert backup.name.startswith("pre_tool_use_hgp.py.bak-")
    assert backup.read_text() == "# customised\n"
    assert (dest / "hgp_paths.py").is_file()


def test_policy_hook_scan_and_backups_cover_agy_hooks_dir(tmp_path, capsys):
    repo = _make_git_repo(tmp_path)
    home = tmp_path / "home"
    _, agy_dir = _agy_paths(home)
    agy_dir.mkdir(parents=True)
    backup = agy_dir / "pre_tool_use_hgp.py.bak-20260101000000"
    backup.write_text("old")
    with patch("pathlib.Path.home", return_value=home):
        hooks = _installed_policy_hooks(repo)
        assert agy_dir / "pre_tool_use_hgp.py" in hooks
        assert home / ".gemini" / "hooks" / "pre_tool_use_hgp.py" in hooks  # Gemini CLI's stays scanned
        _hook_backups_cmd([])
        assert str(backup) in capsys.readouterr().out
        _hook_backups_cmd(["--prune", "--keep", "0"])
    assert not backup.exists()
