"""Tests for `hgp install` — MCP registration, hooks, and instruction injection."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Import the functions under test directly so we don't need the full CLI context.
from hgp.server import (
    _edit_codex_toml,
    _inject_instructions,
    _install,
    _install_hooks_files,
    _install_mcp,
    _update_hooks_settings,
    _HGP_INSTRUCTIONS_BLOCK,
)


# ── _inject_instructions ──────────────────────────────────────

def test_inject_instructions_new_file(tmp_path):
    md = tmp_path / "CLAUDE.md"
    result = _inject_instructions(md)
    assert result == "injected"
    content = md.read_text()
    assert "<!-- hgp-instructions-start -->" in content
    assert "<!-- hgp-instructions-end -->" in content


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
    assert "PreBash" in hook_events
    assert "PostBash" in hook_events
    pre_bash_cmd = data["hooks"]["PreBash"][0]["hooks"][0]["command"]
    assert "pre_bash_hgp.py" in pre_bash_cmd


def test_update_hooks_settings_claude_local_uses_relative_path(tmp_path):
    settings = tmp_path / "settings.json"
    hooks_dir = tmp_path / ".claude" / "hooks"
    hooks_dir.mkdir(parents=True)
    _update_hooks_settings("claude", settings, hooks_dir, "local")
    data = json.loads(settings.read_text())
    cmd = data["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert cmd.startswith("python3 .claude/hooks/")


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
    assert "BeforeShell" in data["hooks"]
    assert "AfterShell" in data["hooks"]


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
         patch("hgp.server._inject_instructions", return_value="injected"):
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
