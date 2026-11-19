"""Tiered hook policy (advisory / block / strict): the level parser, the environment mapping, and
what each hook enforces at each level.

``.hgp/hook-policy`` is JSON ``{"block_mode": bool, "block_shell": bool}``: advisory is
(false, false), block is (true, false), strict is (true, true). ``HGP_HOOK_BLOCK`` is trusted and
wins over the file: unset -> the file, ``0`` -> advisory, ``strict`` -> strict, anything else
(``1``, ``block``, a typo, empty) -> block.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from hgp.hooks import hgp_paths

HOOKS = Path(__file__).parents[1] / "src" / "hgp" / "hooks"
HOOK_MODULES = [
    "hgp.hooks.claude.pre_tool_use_hgp", "hgp.hooks.agy.pre_tool_use_hgp", "hgp.hooks.codex.pre_tool_use_hgp",
]


def _git(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    return path.resolve()


def _repo(tmp_path: Path, policy: str | None = None) -> Path:
    repo = _git(tmp_path / "r")
    (repo / ".hgp").mkdir()
    if policy is not None:
        (repo / ".hgp" / "hook-policy").write_text(policy)
    return repo


# ── hgp_paths.hook_policy_level ──────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ('{"block_mode": false}', "advisory"),
    ('{"block_mode": false, "block_shell": false}', "advisory"),
    ('{"block_mode": false, "block_shell": true}', "advisory"),   # strict needs block_mode
    ('{"block_mode": true}', "block"),                            # pre-tier files read as block
    ('{"block_mode": true, "block_shell": false}', "block"),
    ('{"block_mode": true, "block_shell": true}', "strict"),
    ('{"block_mode": true, "block_shell": "yes"}', "block"),      # block_shell must be a real bool
    ('{"block_mode": true, "block_shell": 1}', "block"),
    ('{"block_mode": true, "telemetry_enabled": true}', "block"),  # other keys are ignored
    ('{"block_mode": true, "block_shell": true, "telemetry_enabled": true}', "strict"),
    ("block", "block"), ("block\n", "block"), (" block ", "block"),  # legacy plain text
    ("advisory", "advisory"), ("advisory\n", "advisory"),
    ('{"block_mode": "yes"}', None), ('{"block_mode": 1}', None), ("{}", None), ("[]", None), ("null", None),
    ("{not json", None), ("garbage", None), ("", None), ("strict", None), ("BLOCK", None),
])
def test_hook_policy_level(text: str, expected: str | None) -> None:
    assert hgp_paths.hook_policy_level(text) == expected


def test_parse_hook_policy_is_unchanged() -> None:
    assert hgp_paths.parse_hook_policy('{"block_mode": true, "block_shell": true}') is True
    assert hgp_paths.parse_hook_policy('{"block_mode": false, "block_shell": true}') is False
    assert hgp_paths.parse_hook_policy("block") is True
    assert hgp_paths.parse_hook_policy("strict") is None


def test_checkout_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HGP_PROJECT_ROOT", raising=False)
    repo = _git(tmp_path / "r")
    sub = repo / "a" / "b"
    sub.mkdir(parents=True)
    assert hgp_paths.checkout_root(sub) == repo
    assert hgp_paths.checkout_root(repo) == repo
    plain = tmp_path / "plain"
    plain.mkdir()
    assert hgp_paths.checkout_root(plain) is None
    monkeypatch.setenv("HGP_PROJECT_ROOT", str(plain))
    assert hgp_paths.checkout_root(sub) == plain.resolve()  # the server's override, as hgp_enabled reads it


# ── HGP_HOOK_BLOCK mapping, in every hook ────────────────────────────────────

def _module(name: str) -> ModuleType:
    return importlib.import_module(name)


@pytest.mark.parametrize("module", HOOK_MODULES)
@pytest.mark.parametrize("env,file,expected", [
    (None, None, "advisory"), (None, "block", "block"), (None, '{"block_mode": true}', "block"),
    (None, '{"block_mode": true, "block_shell": true}', "strict"), (None, '{"block_mode": false}', "advisory"),
    (None, "garbage", "advisory"),
    ("0", '{"block_mode": true, "block_shell": true}', "advisory"),
    ("1", None, "block"), ("block", None, "block"), ("yes", None, "block"), ("", None, "block"),
    ("false", None, "block"), ("Strict", None, "block"), ("STRICT", None, "block"),
    ("strict", None, "strict"), ("strict", '{"block_mode": false}', "strict"),
    ("1", '{"block_mode": true, "block_shell": true}', "block"),      # the env value wins, even downward
    ("block", '{"block_mode": true, "block_shell": true}', "block"),
])
def test_hook_block_level_from_env_and_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, module: str, env: str | None, file: str | None, expected: str,
) -> None:
    monkeypatch.delenv("HGP_PROJECT_ROOT", raising=False)
    monkeypatch.delenv("HGP_HOOK_BLOCK", raising=False)
    if env is not None:
        monkeypatch.setenv("HGP_HOOK_BLOCK", env)
    repo = _repo(tmp_path, file)
    assert _module(module)._block_level(repo) == expected  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize("module", HOOK_MODULES)
def test_unreadable_policy_raises_so_main_fails_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, module: str,
) -> None:
    monkeypatch.delenv("HGP_HOOK_BLOCK", raising=False)
    monkeypatch.delenv("HGP_PROJECT_ROOT", raising=False)
    repo = _repo(tmp_path)
    (repo / ".hgp" / "hook-policy").mkdir()  # a directory: read_text() raises, as before
    with pytest.raises(OSError):
        _module(module)._block_level(repo)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize("module", HOOK_MODULES)
@pytest.mark.parametrize("shared,local,expected", [
    ('{"block_mode": true}', '{"block_mode": true, "block_shell": true}', "strict"),
    ('{"block_mode": true, "block_shell": true}', '{"block_mode": false}', "strict"),
    ('{"block_mode": false}', "block", "block"),
    ('{"block_mode": false}', "advisory", "advisory"),
])
def test_the_stricter_of_the_shared_and_a_worktree_local_policy_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, module: str, shared: str, local: str, expected: str,
) -> None:
    """A policy left inside a linked worktree before storage was shared still counts: upgrading never relaxes."""
    monkeypatch.delenv("HGP_HOOK_BLOCK", raising=False)
    monkeypatch.delenv("HGP_PROJECT_ROOT", raising=False)
    repo = _repo(tmp_path, shared)
    for args in (["config", "user.email", "t@example.com"], ["config", "user.name", "T"]):
        subprocess.run(["git", *args], cwd=repo, check=True)
    (repo / "seed.txt").write_text("x")
    subprocess.run(["git", "add", "seed.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=repo, check=True)
    worktree = tmp_path / "wt"
    subprocess.run(["git", "worktree", "add", "-q", "-b", "wt", str(worktree)], cwd=repo, check=True)
    (worktree / ".hgp").mkdir()
    (worktree / ".hgp" / "hook-policy").write_text(local)
    assert _module(module)._block_level(worktree.resolve()) == expected  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize("module", HOOK_MODULES)
@pytest.mark.parametrize("env,paused", [
    (None, True), ("0", True), ("1", False), ("block", False), ("strict", False), ("yes", False), ("", False),
])
def test_env_block_wins_over_a_paused_mode_for_every_value_but_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, module: str, env: str | None, paused: bool,
) -> None:
    monkeypatch.delenv("HGP_PROJECT_ROOT", raising=False)
    monkeypatch.delenv("HGP_HOOK_BLOCK", raising=False)
    if env is not None:
        monkeypatch.setenv("HGP_HOOK_BLOCK", env)
    repo = _repo(tmp_path)
    (repo / ".hgp" / "mode").write_text("off\n")
    assert _module(module)._paused(repo) is paused  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize("module", HOOK_MODULES)
def test_hooks_read_the_level_through_hook_policy_level(module: str) -> None:
    """`hgp hook-policy` finds the level reader in an installed hook to tell it from a JSON-unaware one."""
    assert "hook_policy_level" in Path(_module(module).__file__ or "").read_text()


@pytest.mark.parametrize("module", [HOOK_MODULES[0], HOOK_MODULES[2]])
def test_resolve_block_mode_stays_for_the_policy_command_staleness_check(module: str) -> None:
    """`hgp hook-policy` looks for `def _resolve_block_mode(` in project-local claude and codex hooks."""
    source = Path(_module(module).__file__ or "").read_text()
    assert "def _resolve_block_mode(" in source
    assert _module(module)._resolve_block_mode  # pyright: ignore[reportPrivateUsage]


# ── Partial installs: no hgp_paths.py beside the hook ────────────────────────

@pytest.mark.parametrize("hook", ["claude", "agy", "codex"])
@pytest.mark.parametrize("file,expected", [
    ('{"block_mode": true, "block_shell": true}', "strict"), ('{"block_mode": true}', "block"),
    ("block", "block"), ('{"block_mode": false, "block_shell": true}', "advisory"), ("garbage", "advisory"),
])
def test_partial_install_reads_the_level_without_the_helper(
    tmp_path: Path, hook: str, file: str, expected: str,
) -> None:
    repo = _repo(tmp_path, file)
    lone = tmp_path / "installed" / "pre_tool_use_hgp.py"
    lone.parent.mkdir()
    lone.write_bytes((HOOKS / hook / "pre_tool_use_hgp.py").read_bytes())
    code = (
        "import importlib.util, sys; from pathlib import Path;"
        f"s = importlib.util.spec_from_file_location('h', {str(lone)!r}); m = importlib.util.module_from_spec(s);"
        "s.loader.exec_module(m);"
        f"print(m._block_level(Path({str(repo)!r})))"
    )
    env = {k: v for k, v in os.environ.items() if k != "HGP_HOOK_BLOCK"}
    # -S: neither hgp_paths beside the hook nor the hgp package is importable
    r = subprocess.run([sys.executable, "-S", "-c", code], capture_output=True, text=True, env=env, cwd=repo)
    assert (r.returncode, r.stdout.strip()) == (0, expected), r.stderr


# ── Codex: its hooks see only Bash, so block and strict both deny a mutating command ──

CODEX_HOOK = HOOKS / "codex" / "pre_tool_use_hgp.py"


def _codex(repo: Path, command: str, **env: str) -> subprocess.CompletedProcess[str]:
    base = {k: v for k, v in os.environ.items() if k != "HGP_HOOK_BLOCK"}
    event = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(repo)}
    return subprocess.run(
        [sys.executable, str(CODEX_HOOK)], input=json.dumps(event), cwd=repo,
        capture_output=True, text=True, env={**base, **env},
    )


@pytest.mark.parametrize("policy,denies", [
    ('{"block_mode": true, "block_shell": true}', True),    # strict
    ('{"block_mode": true, "block_shell": false}', True),   # block
    ('{"block_mode": true}', True), ("block", True),
    ('{"block_mode": false, "block_shell": false}', False), ("advisory", False), (None, False),
])
def test_codex_denies_mutating_bash_under_block_and_strict_alike(
    tmp_path: Path, policy: str | None, denies: bool,
) -> None:
    repo = _repo(tmp_path, policy)
    r = _codex(repo, "cp a b")
    assert r.returncode == 0
    assert ('"deny"' in r.stdout) is denies


@pytest.mark.parametrize("env,denies", [
    ("1", True), ("block", True), ("strict", True), ("yes", True), ("0", False),
])
def test_codex_env_levels(tmp_path: Path, env: str, denies: bool) -> None:
    repo = _repo(tmp_path, '{"block_mode": true, "block_shell": true}')
    r = _codex(repo, "cp a b", HGP_HOOK_BLOCK=env)
    assert ('"deny"' in r.stdout) is denies


def test_codex_paused_mode_still_yields_unless_the_env_enforces(tmp_path: Path) -> None:
    repo = _repo(tmp_path, '{"block_mode": true, "block_shell": true}')
    (repo / ".hgp" / "mode").write_text("off\n")
    assert _codex(repo, "cp a b").stdout == ""
    assert _codex(repo, "cp a b", HGP_HOOK_BLOCK="0").stdout == ""
    assert '"deny"' in _codex(repo, "cp a b", HGP_HOOK_BLOCK="strict").stdout
    assert '"deny"' in _codex(repo, "cp a b", HGP_HOOK_BLOCK="1").stdout
