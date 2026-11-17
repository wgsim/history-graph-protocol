"""Tests for `hgp hook-policy` CLI subcommand."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HOOKS = Path(__file__).parents[1] / "src" / "hgp" / "hooks"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep the developer's own ~/.claude etc. hooks out of policy-format decisions."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("HGP_HOOK_BLOCK", raising=False)
    return home


def _run(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "hgp.server"] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
    )


def _make_git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    return path.resolve()


def _make_linked_worktree(tmp_path: Path) -> tuple[Path, Path]:
    repo = _make_git_repo(tmp_path / "repo")
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "tracked.txt").write_text("initial")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=repo, check=True)
    worktree = tmp_path / "worktree"
    subprocess.run(
        ["git", "worktree", "add", "-q", str(worktree), "-b", "feature"],
        cwd=repo,
        check=True,
    )
    return repo, worktree


# ---------------------------------------------------------------------------
# read policy
# ---------------------------------------------------------------------------


def test_default_policy_is_advisory(tmp_path: Path) -> None:
    repo = _make_git_repo(tmp_path / "repo")
    result = _run(["hook-policy"], cwd=repo)
    assert result.returncode == 0
    assert result.stdout.strip() == "advisory"


def test_read_policy_after_set(tmp_path: Path) -> None:
    repo = _make_git_repo(tmp_path / "repo")
    _run(["hook-policy", "block"], cwd=repo)
    result = _run(["hook-policy"], cwd=repo)
    assert result.returncode == 0
    assert result.stdout.strip() == "block"


def test_hook_reads_policy_from_main_repo_for_worktree(tmp_path: Path) -> None:
    repo, worktree = _make_linked_worktree(tmp_path)
    (repo / ".hgp").mkdir()
    (repo / ".hgp" / "hook-policy").write_text("block")
    hook = Path(__file__).parents[1] / "src" / "hgp" / "hooks" / "claude" / "pre_tool_use_hgp.py"
    result = subprocess.run(
        [sys.executable, str(hook)],
        input='{"tool_name":"Write"}',
        cwd=worktree,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "Native `Write` detected" in result.stderr


# ---------------------------------------------------------------------------
# set policy
# ---------------------------------------------------------------------------


def test_set_block_writes_policy_file(tmp_path: Path) -> None:
    repo = _make_git_repo(tmp_path / "repo")
    result = _run(["hook-policy", "block"], cwd=repo)
    assert result.returncode == 0
    assert json.loads((repo / ".hgp" / "hook-policy").read_text()) == {"block_mode": True}


def test_set_advisory_overwrites_block(tmp_path: Path) -> None:
    repo = _make_git_repo(tmp_path / "repo")
    _run(["hook-policy", "block"], cwd=repo)
    result = _run(["hook-policy", "advisory"], cwd=repo)
    assert result.returncode == 0
    assert json.loads((repo / ".hgp" / "hook-policy").read_text()) == {"block_mode": False}


def test_set_policy_keeps_other_json_keys(tmp_path: Path) -> None:
    repo = _make_git_repo(tmp_path / "repo")
    (repo / ".hgp").mkdir()
    (repo / ".hgp" / "hook-policy").write_text('{"block_mode": false, "telemetry_enabled": true}')
    assert _run(["hook-policy", "block"], cwd=repo).returncode == 0
    data = json.loads((repo / ".hgp" / "hook-policy").read_text())
    assert data == {"block_mode": True, "telemetry_enabled": True}
    assert _run(["hook-policy"], cwd=repo).stdout.strip() == "block"


@pytest.mark.parametrize("content,expected", [
    ("block", "block"),                       # legacy plain text
    ("advisory\n", "advisory"),
    ('{"block_mode": true}', "block"),
    ('{"block_mode": "yes"}', "advisory"),    # malformed value → advisory (fail-open)
    ("garbage", "advisory"),
])
def test_read_policy_json_and_legacy(tmp_path: Path, content: str, expected: str) -> None:
    repo = _make_git_repo(tmp_path / "repo")
    (repo / ".hgp").mkdir()
    (repo / ".hgp" / "hook-policy").write_text(content)
    assert _run(["hook-policy"], cwd=repo).stdout.strip() == expected


@pytest.mark.parametrize("content,blocked", [
    ('{"block_mode": true, "telemetry_enabled": true}', True),
    ('{"block_mode": false}', False),
    ("block", True),
    ("advisory", False),
])
def test_hook_reads_json_and_legacy_policy(tmp_path: Path, content: str, blocked: bool) -> None:
    repo = _make_git_repo(tmp_path / "repo")
    (repo / ".hgp").mkdir()
    (repo / ".hgp" / "hook-policy").write_text(content)
    hook = Path(__file__).parents[1] / "src" / "hgp" / "hooks" / "claude" / "pre_tool_use_hgp.py"
    env = {k: v for k, v in os.environ.items() if k != "HGP_HOOK_BLOCK"}
    result = subprocess.run(
        [sys.executable, str(hook)], input='{"tool_name":"Write"}',
        cwd=repo, capture_output=True, text=True, env=env,
    )
    assert (result.returncode == 2) is blocked


_JSON_UNAWARE_HOOK = (
    "def _resolve_block_mode():\n"
    "    return open('.hgp/hook-policy').read().strip() == 'block'\n"
)


@pytest.mark.parametrize("where", ["local", "global"])
def test_policy_written_as_legacy_text_while_json_unaware_hooks_installed(
    tmp_path: Path, _isolated_home: Path, where: str,
) -> None:
    repo = _make_git_repo(tmp_path / "repo")
    base = repo if where == "local" else _isolated_home
    hooks = base / ".claude" / "hooks"
    hooks.mkdir(parents=True)
    (hooks / "pre_tool_use_hgp.py").write_text(_JSON_UNAWARE_HOOK)

    result = _run(["hook-policy", "block"], cwd=repo)
    assert result.returncode == 0
    assert (repo / ".hgp" / "hook-policy").read_text().strip() == "block"
    assert "legacy plain-text format" in result.stderr

    (hooks / "pre_tool_use_hgp.py").write_bytes((HOOKS / "claude" / "pre_tool_use_hgp.py").read_bytes())
    _run(["hook-policy", "block"], cwd=repo)
    assert json.loads((repo / ".hgp" / "hook-policy").read_text()) == {"block_mode": True}


def _hook_blocks(hook: Path, cwd: Path) -> bool:
    result = subprocess.run(
        [sys.executable, str(hook)], input='{"tool_name":"Write"}',
        cwd=cwd, capture_output=True, text=True,
    )
    return result.returncode == 2


def test_hook_honors_legacy_worktree_local_block_policy(tmp_path: Path) -> None:
    repo, worktree = _make_linked_worktree(tmp_path)
    (repo / ".hgp").mkdir()
    (repo / ".hgp" / "hook-policy").write_text('{"block_mode": false}')
    (worktree / ".hgp").mkdir()
    (worktree / ".hgp" / "hook-policy").write_text("block")  # written before storage was shared
    for client in ("claude", "gemini"):
        hook = HOOKS / client / "pre_tool_use_hgp.py"
        if client == "gemini":
            r = subprocess.run(
                [sys.executable, str(hook)], input='{"tool_name":"write_file"}',
                cwd=worktree, capture_output=True, text=True,
            )
            assert '"deny"' in r.stdout
        else:
            assert _hook_blocks(hook, worktree)


def test_partial_install_hook_reads_json_policy(tmp_path: Path) -> None:
    repo = _make_git_repo(tmp_path / "repo")
    (repo / ".hgp").mkdir()
    (repo / ".hgp" / "hook-policy").write_text('{"block_mode": true}')
    lone = tmp_path / "installed"
    lone.mkdir()
    (lone / "pre_tool_use_hgp.py").write_bytes((HOOKS / "claude" / "pre_tool_use_hgp.py").read_bytes())
    assert _hook_blocks(lone / "pre_tool_use_hgp.py", repo)  # no hgp_paths.py beside it


def test_set_policy_from_worktree_updates_main_repo(tmp_path: Path) -> None:
    repo, worktree = _make_linked_worktree(tmp_path)
    assert _run(["hook-policy", "block"], cwd=worktree).returncode == 0
    assert json.loads((repo / ".hgp" / "hook-policy").read_text()) == {"block_mode": True}
    assert not (worktree / ".hgp").exists()
    assert _run(["hook-policy"], cwd=worktree).stdout.strip() == "block"


# ---------------------------------------------------------------------------
# error handling
# ---------------------------------------------------------------------------


def test_invalid_arg_exits_nonzero(tmp_path: Path) -> None:
    repo = _make_git_repo(tmp_path / "repo")
    result = _run(["hook-policy", "--bogus"], cwd=repo)
    assert result.returncode != 0
    assert "invalid argument" in result.stderr


def test_outside_repo_exits_nonzero(tmp_path: Path) -> None:
    no_repo = tmp_path / "no_repo"
    no_repo.mkdir()
    result = _run(["hook-policy"], cwd=no_repo)
    assert result.returncode != 0
    assert "no git repository" in result.stderr


# ---------------------------------------------------------------------------
# upgrade path
# ---------------------------------------------------------------------------


def test_stale_hook_triggers_warning(tmp_path: Path) -> None:
    repo = _make_git_repo(tmp_path / "repo")
    # install a "stale" hook without _resolve_block_mode
    claude_hooks = repo / ".claude" / "hooks"
    claude_hooks.mkdir(parents=True)
    (claude_hooks / "pre_tool_use_hgp.py").write_text(
        "# old hook without _resolve_block_mode\n"
        "import os\n"
        "BLOCK_MODE = os.environ.get('HGP_HOOK_BLOCK', '0') == '1'\n"
    )
    result = _run(["hook-policy", "block"], cwd=repo)
    assert result.returncode == 0
    assert "predate hook-policy support" in result.stderr
    assert "hgp install --claude --local" in result.stderr
    assert "hgp install-hooks" not in result.stderr


def test_stale_hook_comment_no_false_negative(tmp_path: Path) -> None:
    repo = _make_git_repo(tmp_path / "repo")
    # hook that mentions def _resolve_block_mode( only in a comment — still stale
    claude_hooks = repo / ".claude" / "hooks"
    claude_hooks.mkdir(parents=True)
    (claude_hooks / "pre_tool_use_hgp.py").write_text(
        "# old hook without def _resolve_block_mode( support\n"
        "import os\n"
        "BLOCK_MODE = os.environ.get('HGP_HOOK_BLOCK', '0') == '1'\n"
    )
    result = _run(["hook-policy", "block"], cwd=repo)
    assert result.returncode == 0
    assert "predate hook-policy support" in result.stderr


def test_stale_codex_hook_triggers_warning(tmp_path: Path) -> None:
    repo = _make_git_repo(tmp_path / "repo")
    # install a stale Codex hook without _resolve_block_mode
    codex_hooks = repo / ".codex" / "hooks"
    codex_hooks.mkdir(parents=True)
    (codex_hooks / "pre_tool_use_hgp.py").write_text(
        "# old codex hook without _resolve_block_mode\n"
        "import os\n"
        "BLOCK_MODE = os.environ.get('HGP_HOOK_BLOCK', '0') == '1'\n"
    )
    result = _run(["hook-policy", "block"], cwd=repo)
    assert result.returncode == 0
    assert "predate hook-policy support" in result.stderr
    assert ".codex/hooks/pre_tool_use_hgp.py" in result.stderr
    assert "hgp install --codex --local" in result.stderr
    assert "hgp install-hooks" not in result.stderr


def test_fresh_hook_no_warning(tmp_path: Path) -> None:
    repo = _make_git_repo(tmp_path / "repo")
    # install a current hook containing _resolve_block_mode
    claude_hooks = repo / ".claude" / "hooks"
    claude_hooks.mkdir(parents=True)
    (claude_hooks / "pre_tool_use_hgp.py").write_text(
        "def _resolve_block_mode(): return False\n"
        "BLOCK_MODE = _resolve_block_mode()\n"
    )
    result = _run(["hook-policy", "block"], cwd=repo)
    assert result.returncode == 0
    assert "predate" not in result.stderr


def test_missing_post_tool_use_warns_advisory_only(tmp_path: Path) -> None:
    """Missing post_tool_use_hgp.py warns about advisory gap, not policy enforcement."""
    repo = _make_git_repo(tmp_path / "repo")
    gemini_hooks = repo / ".gemini" / "hooks"
    gemini_hooks.mkdir(parents=True)
    # pre_tool_use present and current (has _resolve_block_mode)
    (gemini_hooks / "pre_tool_use_hgp.py").write_text(
        "def _resolve_block_mode(): return False\n"
        "BLOCK_MODE = _resolve_block_mode()\n"
    )
    # post_tool_use intentionally absent
    result = _run(["hook-policy", "block"], cwd=repo)
    assert result.returncode == 0
    # policy enforcement warning must NOT appear
    assert "predate hook-policy support" not in result.stderr
    assert "will not honor" not in result.stderr
    # advisory-gap warning must appear
    assert "post_tool_use_hgp.py is missing" in result.stderr
    assert "Advisory/block policy enforcement still works" in result.stderr


def test_missing_post_tool_use_no_warn_without_pre(tmp_path: Path) -> None:
    """No post_tool_use warning when Gemini pre_tool_use is also absent."""
    repo = _make_git_repo(tmp_path / "repo")
    # no .gemini/hooks at all
    result = _run(["hook-policy"], cwd=repo)
    assert result.returncode == 0
    assert "post_tool_use_hgp.py is missing" not in result.stderr


def test_stale_pre_and_missing_post_no_contradiction(tmp_path: Path) -> None:
    """Stale pre + missing post: no contradictory 'policy still works' sentence."""
    repo = _make_git_repo(tmp_path / "repo")
    gemini_hooks = repo / ".gemini" / "hooks"
    gemini_hooks.mkdir(parents=True)
    # stale pre hook (no _resolve_block_mode)
    (gemini_hooks / "pre_tool_use_hgp.py").write_text(
        "# old hook without def _resolve_block_mode( support\n"
    )
    # post_tool_use absent
    result = _run(["hook-policy", "advisory"], cwd=repo)
    assert result.returncode == 0
    # stale-policy warning must appear
    assert "predate hook-policy support" in result.stderr
    # post-hook absence still diagnosable
    assert "post_tool_use_hgp.py" in result.stderr
    # no contradictory claim
    assert "Advisory/block policy enforcement still works" not in result.stderr
