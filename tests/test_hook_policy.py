"""Tests for `hgp hook-policy` CLI subcommand."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


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


# ---------------------------------------------------------------------------
# set policy
# ---------------------------------------------------------------------------


def test_set_block_writes_policy_file(tmp_path: Path) -> None:
    repo = _make_git_repo(tmp_path / "repo")
    result = _run(["hook-policy", "block"], cwd=repo)
    assert result.returncode == 0
    assert (repo / ".hgp" / "hook-policy").read_text().strip() == "block"


def test_set_advisory_overwrites_block(tmp_path: Path) -> None:
    repo = _make_git_repo(tmp_path / "repo")
    _run(["hook-policy", "block"], cwd=repo)
    result = _run(["hook-policy", "advisory"], cwd=repo)
    assert result.returncode == 0
    assert (repo / ".hgp" / "hook-policy").read_text().strip() == "advisory"


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
    assert "hgp install-hooks" in result.stderr


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
