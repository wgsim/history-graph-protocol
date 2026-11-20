"""Claude and Codex shell hooks at the agy hook's level: one shared analyser, before/after tracking.

* The shell analysis (read-only prefixes, self-mutating options, in-place editors, the mode and
  hook-policy guard, control-file writes) lives in ``hgp_paths`` and is used by the agy, Claude and
  Codex hooks. The agy corpora (tests/test_agy_hooks.py) run against it directly and through the
  Claude and Codex hooks.
* Claude's pre_bash denies a mutating command under ``strict`` and always denies the guards; it
  records a ``git status`` baseline for a mutating command it lets through. post_bash reports only
  the new entries, through ``additionalContext``. Codex does the same in its own output format.
* The marker is keyed by the payload's ``tool_use_id`` (the ppid when it is absent).
* git runs with a scrubbed environment and ``--no-optional-locks``.
"""

from __future__ import annotations

import importlib.util
import itertools
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import ModuleType

import pytest

from hgp.hooks import hgp_paths
from tests.test_agy_hooks import (
    COMMIT_MESSAGE_HIDING_ATTEMPTS,
    COMMIT_MESSAGE_LOOKALIKES,
    COMMIT_MESSAGES,
    COMPOUND_COMMANDS,
    CONTROL_FILE_READS,
    CONTROL_FILE_WRITES,
    CONTROL_GUARDS,
    IN_PLACE_EDITORS,
    MODE_CHANGE_BYPASSES,
    MUTATING_SHELL,
    READONLY_COMMANDS_MENTIONING_MUTATING_WORDS,
    SELF_MUTATING_READONLY,
)

HOOKS = Path(__file__).parents[1] / "src" / "hgp" / "hooks"
AGY_PRE = HOOKS / "agy" / "pre_tool_use_hgp.py"
CLAUDE_PRE = HOOKS / "claude" / "pre_bash_hgp.py"
CLAUDE_POST = HOOKS / "claude" / "post_bash_hgp.py"
CODEX_PRE = HOOKS / "codex" / "pre_tool_use_hgp.py"
CODEX_POST = HOOKS / "codex" / "post_tool_use_hgp.py"
PRE = {"agy": AGY_PRE, "claude": CLAUDE_PRE, "codex": CODEX_PRE}
POST = {"claude": CLAUDE_POST, "codex": CODEX_POST}

_ENV_KEYS = ("HGP_HOOK_BLOCK", "HGP_HOOK_ASK", "HGP_GLOBAL_MODE", "HGP_PROJECT_ROOT")
_M = "mo" + "de"  # keeps the literal command text out of this file
BLOCK = '{"block_mode": true, "block_shell": false}'
STRICT = '{"block_mode": true, "block_shell": true}'


# ── Helpers ──────────────────────────────────────────────────────────────────

def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _repo(tmp_path: Path, name: str = "r") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    (repo / "tracked.txt").write_text("one\n")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-qm", "seed")
    (repo / ".hgp").mkdir()
    return repo.resolve()


def _spawn(
    hook: Path, event: object, tmp_path: Path, *, flags: tuple[str, ...] = (), **env: str,
) -> subprocess.CompletedProcess[str]:
    """Run a hook as its client does: the event on stdin, a neutral cwd, TMPDIR and HOME inside tmp_path."""
    (tmp_path / "tmp").mkdir(exist_ok=True)
    (tmp_path / "home").mkdir(exist_ok=True)
    cwd = tmp_path / "hook-cwd"
    cwd.mkdir(exist_ok=True)
    base = {k: v for k, v in os.environ.items() if k not in _ENV_KEYS}
    base.update(TMPDIR=str(tmp_path / "tmp"), HOME=str(tmp_path / "home"))
    stdin = event if isinstance(event, str) else json.dumps(event)
    return subprocess.run(
        [sys.executable, *flags, str(hook)], input=stdin, cwd=cwd, capture_output=True, text=True, env={**base, **env},
    )


def _state_dir(tmp_path: Path, client: str) -> Path:
    return tmp_path / "tmp" / f"hgp-{client}-{os.getuid()}"


def _event(client: str, repo: Path, command: str, **kw: object) -> dict[str, object]:
    """A Pre event for ``command``. ``tool_use_id`` (Claude, Codex), or conv/step (agy), may be overridden."""
    if client == "agy":
        return {
            "hookEventName": "PreToolUse", "conversationId": kw.get("conv", "c1"), "stepIdx": kw.get("step", 4),
            "toolCall": {"id": "call_1", "name": "run_command", "args": {"CommandLine": command}},
            "cwd": str(repo), "workspacePaths": [str(repo)],
        }
    event: dict[str, object] = {
        "hook_event_name": "PreToolUse", "tool_name": kw.get("tool_name", "Bash"),
        "tool_input": {"command": command}, "cwd": str(repo), "session_id": "s1",
    }
    tool_use_id = kw["tool_use_id"] if "tool_use_id" in kw else _tool_id(client)
    if tool_use_id is not None:
        event["tool_use_id"] = tool_use_id
    return event


def _post_event(client: str, repo: Path, command: str = "cp a b", **kw: object) -> dict[str, object]:
    event = _event(client, repo, command, **kw)
    event["hook_event_name"] = "PostToolUse"
    event["tool_response"] = {"stdout": "", "stderr": "", "interrupted": False}
    return event


def _tool_id(client: str) -> str:
    return "toolu_01" if client == "claude" else "call_01"


def _verdict(client: str, r: subprocess.CompletedProcess[str]) -> tuple[str, str]:
    """('deny' | 'warn' | 'silent', the message) for a Pre hook's answer, in the client's own format."""
    assert "Traceback" not in r.stderr, r.stderr
    if client == "agy":
        assert r.returncode == 0 and r.stderr == ""
        if not r.stdout.strip():
            return "silent", ""
        out = json.loads(r.stdout)
        assert out["decision"] == "deny"
        return "deny", out["reason"]
    if client == "claude":
        assert r.stdout == ""
        if r.returncode == 2:
            return "deny", r.stderr
        assert r.returncode == 0
        return ("warn", r.stderr) if r.stderr else ("silent", "")
    assert (r.returncode, r.stderr) == (0, "")
    if not r.stdout.strip():
        return "silent", ""
    out = json.loads(r.stdout)
    if "hookSpecificOutput" in out:
        assert set(out) == {"hookSpecificOutput"}
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
        return "deny", out["hookSpecificOutput"]["permissionDecisionReason"]
    assert set(out) == {"systemMessage"}
    return "warn", out["systemMessage"]


def _context(r: subprocess.CompletedProcess[str]) -> str | None:
    """The additionalContext of a Post hook's answer, or None when it says nothing."""
    assert "Traceback" not in r.stderr, r.stderr
    assert (r.returncode, r.stderr) == (0, "")
    if not r.stdout.strip():
        return None
    out = json.loads(r.stdout)
    assert list(out) == ["hookSpecificOutput"]
    assert list(out["hookSpecificOutput"]) == ["hookEventName", "additionalContext"]
    assert out["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    return out["hookSpecificOutput"]["additionalContext"]


# What a mutating command gets at each level, per client. paused = HGP mode off/advisory (the policy file
# yields; HGP_HOOK_BLOCK does not). Codex denies under block as well: its hooks see only the shell.
LEVELS = ["advisory", "block", "strict", "paused-strict", "env-strict", "paused-env-strict", "env-0", "env-1"]
MUTATING_VERDICT = {
    "agy": dict(zip(LEVELS, ["silent", "silent", "deny", "silent", "deny", "deny", "silent", "silent"], strict=True)),
    "claude": dict(zip(LEVELS, ["warn", "warn", "deny", "silent", "deny", "deny", "warn", "warn"], strict=True)),
    "codex": dict(zip(LEVELS, ["warn", "deny", "deny", "silent", "deny", "deny", "warn", "deny"], strict=True)),
}


def _apply_level(repo: Path, level: str) -> dict[str, str]:
    """Set up ``level`` in the repo and return the hook environment it needs."""
    policy = {"block": BLOCK, "strict": STRICT, "paused-strict": STRICT, "env-0": STRICT}.get(level)
    if policy is not None:
        (repo / ".hgp" / "hook-policy").write_text(policy)
    if level.startswith("paused"):
        (repo / ".hgp" / "mode").write_text("off\n")
    if level.startswith("env-") or level == "paused-env-strict":
        return {"HGP_HOOK_BLOCK": level.rsplit("-", 1)[1]}
    return {}


_COUNTER = itertools.count()


def _decide(client: str, tmp_path: Path, command: str, level: str = "advisory") -> tuple[str, str]:
    """What the client's Pre hook says about ``command`` at ``level``, in a fresh repository."""
    work = Path(tempfile.mkdtemp(dir=tmp_path, prefix=f"d{next(_COUNTER)}-"))
    repo = _repo(work)
    env = _apply_level(repo, level)
    return _verdict(client, _spawn(PRE[client], _event(client, repo, command), work, **env))


# ── The shared analyser, directly ────────────────────────────────────────────

NOT_READONLY = [*COMPOUND_COMMANDS, "ls ; pwd", "cp a b", "echo hi", "ls -la <(cp a b)", "git fetch origin",
                "git remote -v", "git remote show origin"]
READONLY = ["ls -la", "git status", "pwd", "grep -rn x .", "  git diff HEAD\n", "head -5 a.txt", "git log",
            "git show HEAD", "git diff", "git branch", "git ls-files", "find . -name x -type f"]
NOT_MUTATING = [
    "ls", "ls -la", "git status", "git status --short", "grep x f", "grep -rn x src", "find . -name x -print",
    "find . -type f -name '*.py'", "find . -name delete.txt", "find . -name exec", "git diff HEAD --stat",
    "git diff --output-indicator-new=+", "git log --oneline -5", "head -5 a.txt", "pwd", "uname -a", "git log",
    "git fetch origin", "git remote -v", "git ls-remote origin", "git diff --exit-code", "uv run pytest tests/ -q",
    "sed -n p f", "sed s/a/b/ f", "perl -ne print f", "perl -Mstrict -e 1", "git log -c --stat",
]


def test_the_analyser_lives_in_hgp_paths() -> None:
    for name in ("shell_normalise", "shell_is_readonly", "shell_detect_mutating", "shell_mutating_pattern",
                 "shell_changes_controls", "shell_writes_control_file", "shell_control_violation"):
        assert callable(getattr(hgp_paths, name)), name


@pytest.mark.parametrize("command", READONLY)
def test_readonly_commands_are_recognised(command: str) -> None:
    assert hgp_paths.shell_is_readonly(command), command


@pytest.mark.parametrize("command", NOT_READONLY)
def test_a_prefix_alone_is_not_readonly(command: str) -> None:
    assert not hgp_paths.shell_is_readonly(command), command


@pytest.mark.parametrize("command", [*COMPOUND_COMMANDS, *SELF_MUTATING_READONLY, *IN_PLACE_EDITORS, *MUTATING_SHELL])
def test_mutating_commands_are_detected(command: str) -> None:
    assert hgp_paths.shell_mutating_pattern(command) is not None, command


@pytest.mark.parametrize("command", [*NOT_MUTATING, *READONLY_COMMANDS_MENTIONING_MUTATING_WORDS])
def test_plain_reads_and_searches_are_not_mutating(command: str) -> None:
    assert hgp_paths.shell_mutating_pattern(command) is None, command


@pytest.mark.parametrize("command", [
    'c"p" a b', "r\\m -f a", "'mv' a b", 't""ee out.txt', "echo hi | 't'ee out.txt",
])
def test_quoting_does_not_hide_a_mutating_command(command: str) -> None:
    assert hgp_paths.shell_mutating_pattern(command) is not None


@pytest.mark.parametrize("command", [
    *MODE_CHANGE_BYPASSES, *COMMIT_MESSAGES, *COMMIT_MESSAGE_HIDING_ATTEMPTS, *COMMIT_MESSAGE_LOOKALIKES,
])
def test_mode_and_policy_changes_are_found(command: str) -> None:
    assert hgp_paths.shell_control_violation(command) == "mode", command


@pytest.mark.parametrize("command", CONTROL_FILE_WRITES)
def test_control_file_writes_are_found(command: str) -> None:
    assert hgp_paths.shell_control_violation(command) in ("mode", "control-file"), command


@pytest.mark.parametrize("command", [
    *CONTROL_FILE_READS, f"grep -rn {_M} src/hgp", "cat src/hgp/hooks/hgp_paths.py", "echo hgp", f"echo {_M}",
    f"git log --oneline --grep={_M} -- src/hgp", "python -m hgp.server --help", "git commit -F msg.txt",
    f"git commit -m 'fix paused {_M} handling'", "echo $HOME; grep -n hook-policy src/hgp/server.py",
])
def test_reading_or_mentioning_the_controls_is_not_a_violation(command: str) -> None:
    assert hgp_paths.shell_control_violation(command) is None, command


def test_the_building_blocks_agree_with_the_combined_calls() -> None:
    assert hgp_paths.shell_normalise("h\"g\"p mo'd'e") == "hgp mode"
    assert hgp_paths.shell_changes_controls("hgp " + _M + " off")
    assert hgp_paths.shell_writes_control_file(f"echo off > .hgp/{_M}")
    assert hgp_paths.shell_detect_mutating("cp a b") is not None
    assert hgp_paths.shell_detect_mutating("grep -rn install src", readonly=True) is None
    assert hgp_paths.shell_detect_mutating("find . -delete", readonly=True) is not None


# ── Shell levels, shared ─────────────────────────────────────────────────────

def test_the_level_helpers_are_shared(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HGP_HOOK_BLOCK", raising=False)
    monkeypatch.delenv("HGP_PROJECT_ROOT", raising=False)
    repo = _repo(tmp_path)
    assert hgp_paths.block_level(repo) == "advisory" and not hgp_paths.hook_paused(repo)
    (repo / ".hgp" / "hook-policy").write_text(STRICT)
    assert hgp_paths.block_level(repo) == "strict"
    (repo / ".hgp" / "mode").write_text("off\n")
    assert hgp_paths.hook_paused(repo)
    monkeypatch.setenv("HGP_HOOK_BLOCK", "1")
    assert hgp_paths.env_level() == "block" and hgp_paths.block_level(repo) == "block"
    assert not hgp_paths.hook_paused(repo)  # the environment is trusted: it does not yield to the mode file


def test_the_stricter_of_the_shared_and_the_local_policy_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("HGP_HOOK_BLOCK", raising=False)
    monkeypatch.delenv("HGP_PROJECT_ROOT", raising=False)
    repo = _repo(tmp_path)
    worktree = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", "-b", "wt", str(worktree))
    (worktree / ".hgp").mkdir()
    for shared, local, expected in ((BLOCK, STRICT, "strict"), (STRICT, BLOCK, "strict"), (BLOCK, "garbage", "block")):
        (repo / ".hgp" / "hook-policy").write_text(shared)
        (worktree / ".hgp" / "hook-policy").write_text(local)
        assert hgp_paths.block_level(worktree.resolve()) == expected, (shared, local)
        module = _load(CODEX_PRE, "_parity_codex_wt")
        assert module._block_level(worktree.resolve()) == expected  # pyright: ignore[reportPrivateUsage]


def test_the_policy_search_stops_at_the_checkout_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HGP_HOOK_BLOCK", raising=False)
    monkeypatch.delenv("HGP_PROJECT_ROOT", raising=False)
    outer = tmp_path / "outer"
    (outer / ".hgp").mkdir(parents=True)
    (outer / ".hgp" / "hook-policy").write_text(STRICT)  # above the checkout: not this repository's policy
    repo = _repo(outer)
    assert hgp_paths.block_level(repo) == "advisory"


@pytest.mark.parametrize("env,file", [
    (None, None), (None, "block"), (None, STRICT), (None, '{"block_mode": true}'), (None, "garbage"),
    ("0", STRICT), ("1", None), ("strict", None), ("yes", STRICT), ("", None),
])
def test_hgp_paths_levels_match_the_hooks_own_copies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env: str | None, file: str | None,
) -> None:
    monkeypatch.delenv("HGP_PROJECT_ROOT", raising=False)
    monkeypatch.delenv("HGP_HOOK_BLOCK", raising=False)
    if env is not None:
        monkeypatch.setenv("HGP_HOOK_BLOCK", env)
    repo = _repo(tmp_path)
    if file is not None:
        (repo / ".hgp" / "hook-policy").write_text(file)
    (repo / ".hgp" / "mode").write_text("off\n")
    for name in ("agy", "codex"):
        spec = importlib.util.spec_from_file_location(f"_parity_{name}", PRE[name])
        assert spec is not None and spec.loader is not None
        module: ModuleType = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        assert hgp_paths.block_level(repo) == module._block_level(repo), name  # pyright: ignore[reportPrivateUsage]
        assert hgp_paths.hook_paused(repo) is module._paused(repo), name  # pyright: ignore[reportPrivateUsage]


# ── Every hook, through the same corpora ─────────────────────────────────────

GUARD_COMMANDS = [*MODE_CHANGE_BYPASSES, *COMMIT_MESSAGES, *COMMIT_MESSAGE_HIDING_ATTEMPTS, *COMMIT_MESSAGE_LOOKALIKES]


@pytest.mark.parametrize("client", ["claude", "codex"])
@pytest.mark.parametrize("level", ["advisory", "paused-strict"])
@pytest.mark.parametrize("command", GUARD_COMMANDS)
def test_mode_and_policy_changes_are_denied(tmp_path: Path, client: str, level: str, command: str) -> None:
    verdict, text = _decide(client, tmp_path, command, level)
    assert verdict == "deny"
    assert "Blocked" in text and "must be run by the user" in text
    assert "git commit -F" in text  # the way out for a message that only mentions the words


@pytest.mark.parametrize("client", ["claude", "codex"])
@pytest.mark.parametrize("level", ["advisory", "paused-strict"])
@pytest.mark.parametrize("command", CONTROL_FILE_WRITES)
def test_shell_writes_to_control_files_are_denied(tmp_path: Path, client: str, level: str, command: str) -> None:
    verdict, text = _decide(client, tmp_path, command, level)
    assert verdict == "deny"
    assert ".hgp/mode" in text and "hook-policy" in text


@pytest.mark.parametrize("client", ["agy", "claude", "codex"])
@pytest.mark.parametrize("level", LEVELS)
@pytest.mark.parametrize("command", CONTROL_GUARDS)
def test_the_guards_deny_at_every_level(tmp_path: Path, client: str, level: str, command: str) -> None:
    assert _decide(client, tmp_path, command, level)[0] == "deny"


@pytest.mark.parametrize("client", ["agy", "claude", "codex"])
def test_the_guards_stay_out_of_a_repository_that_never_opted_in(tmp_path: Path, client: str) -> None:
    repo = _repo(tmp_path)
    (repo / ".hgp").rmdir()
    for command in (f"hgp {_M} off", f"echo off > .hgp/{_M}"):
        assert _verdict(client, _spawn(PRE[client], _event(client, repo, command), tmp_path)) == ("silent", "")


@pytest.mark.parametrize("client", ["claude", "codex"])
@pytest.mark.parametrize("command", [*CONTROL_FILE_READS, f"grep -rn {_M} src/hgp", f"echo {_M}", "echo hgp"])
def test_reading_the_controls_or_mentioning_them_is_not_denied(tmp_path: Path, client: str, command: str) -> None:
    assert _decide(client, tmp_path, command, "advisory")[0] != "deny"


@pytest.mark.parametrize("client", ["claude", "codex"])
@pytest.mark.parametrize("mode", ["advisory", "off"])
def test_a_paused_mode_silences_the_shell_hooks_unless_the_environment_enforces(
    tmp_path: Path, client: str, mode: str,
) -> None:
    repo = _repo(tmp_path)
    (repo / ".hgp" / "hook-policy").write_text(STRICT)
    (repo / ".hgp" / "mode").write_text(mode + "\n")
    event = _event(client, repo, "cp a b")
    assert _verdict(client, _spawn(PRE[client], event, tmp_path)) == ("silent", "")
    assert _verdict(client, _spawn(PRE[client], event, tmp_path, HGP_HOOK_BLOCK="0")) == ("silent", "")
    assert _verdict(client, _spawn(PRE[client], event, tmp_path, HGP_HOOK_BLOCK="strict"))[0] == "deny"
    assert not _state_dir(tmp_path, client).exists()


@pytest.mark.parametrize("client", ["claude", "codex"])
@pytest.mark.parametrize("level", LEVELS)
@pytest.mark.parametrize("command", ["cp a b", "mkdir x", "echo hi > out.txt", "sed -i s/a/b/ f", "ls && cp a b"])
def test_each_level_does_what_the_table_says(tmp_path: Path, client: str, level: str, command: str) -> None:
    assert _decide(client, tmp_path, command, level)[0] == MUTATING_VERDICT[client][level]


@pytest.mark.parametrize("client", ["claude", "codex"])
@pytest.mark.parametrize("command", [
    *COMPOUND_COMMANDS, *SELF_MUTATING_READONLY, *IN_PLACE_EDITORS, *MUTATING_SHELL,
    'c"p" a b', "r\\m -f a", "'mv' a b", "make install",
])
def test_strict_denies_what_the_analyser_calls_mutating(tmp_path: Path, client: str, command: str) -> None:
    verdict, text = _decide(client, tmp_path, command, "strict")
    assert verdict == "deny"
    assert "may mutate" in text
    assert _decide(client, tmp_path, command, "paused-strict")[0] == "silent"  # the policy file yields to a pause
    assert _decide(client, tmp_path, command, "paused-env-strict")[0] == "deny"  # the environment does not


@pytest.mark.parametrize("client", ["claude", "codex"])
@pytest.mark.parametrize("command", [*NOT_MUTATING, *READONLY_COMMANDS_MENTIONING_MUTATING_WORDS])
def test_strict_leaves_reads_and_searches_alone(tmp_path: Path, client: str, command: str) -> None:
    assert _decide(client, tmp_path, command, "strict")[0] == "silent"
    assert _decide(client, tmp_path, command, "env-strict")[0] == "silent"


@pytest.mark.parametrize("client", ["claude", "codex"])
def test_a_readonly_prefix_no_longer_hides_a_mode_change(tmp_path: Path, client: str) -> None:
    for command in (f"git status; hgp {_M} off", "ls -la && hgp hook-policy block", f"ls && echo off > .hgp/{_M}"):
        assert _decide(client, tmp_path, command)[0] == "deny", command


@pytest.mark.parametrize("client", ["claude", "codex"])
@pytest.mark.parametrize("event", [
    "", "not json{", "[]", "null", "42", {}, {"tool_name": "Bash"}, {"tool_name": "Bash", "tool_input": 3},
    {"tool_name": "Bash", "tool_input": {"command": 7}}, {"tool_name": "Bash", "tool_input": {}},
    {"tool_name": "Write", "tool_input": {"command": "cp a b"}}, {"tool_name": "Bash", "tool_input": []},
])
def test_pre_fails_open_on_malformed_events(tmp_path: Path, client: str, event: str | dict[str, object]) -> None:
    repo = _repo(tmp_path)
    (repo / ".hgp" / "hook-policy").write_text(STRICT)
    r = _spawn(PRE[client], {**event, "cwd": str(repo)} if isinstance(event, dict) else event, tmp_path)
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")


@pytest.mark.parametrize("client", ["claude", "codex"])
def test_pre_ignores_other_tools(tmp_path: Path, client: str) -> None:
    repo = _repo(tmp_path)
    (repo / ".hgp" / "hook-policy").write_text(STRICT)
    r = _spawn(PRE[client], _event(client, repo, "cp a b", tool_name="apply_patch"), tmp_path)
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")


# ── Strict in Claude ─────────────────────────────────────────────────────────

def test_claude_strict_denies_mkdir_and_block_and_advisory_do_not(tmp_path: Path) -> None:
    assert _decide("claude", tmp_path, "mkdir x", "strict")[0] == "deny"
    assert _decide("claude", tmp_path, "mkdir x", "block")[0] == "warn"
    assert _decide("claude", tmp_path, "mkdir x", "advisory")[0] == "warn"


def test_claude_strict_deny_exits_two_and_says_why(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / ".hgp" / "hook-policy").write_text(STRICT)
    r = _spawn(CLAUDE_PRE, _event("claude", repo, "mkdir x"), tmp_path)
    assert r.returncode == 2 and r.stdout == ""
    assert "[HGP]" in r.stderr and "may mutate" in r.stderr and "hgp_" in r.stderr


# ── Tracking: the baseline ───────────────────────────────────────────────────

CLIENTS = ["claude", "codex"]


def _marker(tmp_path: Path, client: str, name: str | None = None) -> Path:
    return _state_dir(tmp_path, client) / f"bash-{name or _tool_id(client).replace('_', '-')}.json"


def _track(tmp_path: Path, client: str, repo: Path, command: str = "cp a b", **kw: object) -> None:
    assert _verdict(client, _spawn(PRE[client], _event(client, repo, command, **kw), tmp_path))[0] == "warn"


@pytest.mark.parametrize("client", CLIENTS)
def test_a_mutating_command_records_a_baseline_keyed_by_tool_use_id(tmp_path: Path, client: str) -> None:
    repo = _repo(tmp_path)
    _track(tmp_path, client, repo)
    data = json.loads(_marker(tmp_path, client).read_text())
    assert set(data) == {"pattern", "baseline"}
    assert isinstance(data["pattern"], str) and data["pattern"]
    assert data["baseline"] == {str(repo): []}


@pytest.mark.parametrize("client", CLIENTS)
def test_the_baseline_holds_what_was_already_changed_and_leaves_hgp_out(tmp_path: Path, client: str) -> None:
    repo = _repo(tmp_path)
    (repo / "old.txt").write_text("x")
    (repo / "tracked.txt").write_text("changed\n")
    (repo / ".hgp" / "hgp.db").write_text("x")
    _track(tmp_path, client, repo)
    baseline = json.loads(_marker(tmp_path, client).read_text())["baseline"][str(repo)]
    assert sorted(baseline) == [" M tracked.txt", "?? old.txt"]


@pytest.mark.parametrize("client", CLIENTS)
def test_the_parent_pid_is_the_key_without_a_tool_use_id(tmp_path: Path, client: str) -> None:
    repo = _repo(tmp_path)
    for bad in (None, "", 7, ["x"]):
        _track(tmp_path, client, repo, tool_use_id=bad)
        assert [p.name for p in _state_dir(tmp_path, client).iterdir()] == [f"bash-ppid-{os.getpid()}.json"], bad
        _marker(tmp_path, client, f"ppid-{os.getpid()}").unlink()


@pytest.mark.parametrize("client", CLIENTS)
@pytest.mark.parametrize("tool_use_id,expected", [
    ("toolu_01AbC", "toolu-01AbC"), ("../../etc/passwd", "..-..-etc-passwd"), ("a b/c", "a-b-c"),
    ("x" * 200, "x" * 80),
])
def test_the_key_is_sanitised_into_the_file_name(tmp_path: Path, client: str, tool_use_id: str, expected: str) -> None:
    repo = _repo(tmp_path)
    _track(tmp_path, client, repo, tool_use_id=tool_use_id)
    assert [p.name for p in _state_dir(tmp_path, client).iterdir()] == [f"bash-{expected}.json"]
    assert not (tmp_path / "etc").exists()


@pytest.mark.parametrize("client", CLIENTS)
@pytest.mark.parametrize("setup", [
    "paused", "not-enabled", "non-git", "strict-denied", "readonly", "safe", "other-tool",
])
def test_no_marker_when_tracking_does_not_apply(tmp_path: Path, client: str, setup: str) -> None:
    repo = _repo(tmp_path)
    command, kw = "cp a b", {}
    if setup == "paused":
        (repo / ".hgp" / "mode").write_text("off\n")
    elif setup == "not-enabled":
        (repo / ".hgp").rmdir()
    elif setup == "non-git":
        plain = tmp_path / "plain"
        (plain / ".hgp").mkdir(parents=True)
        repo = plain.resolve()
    elif setup == "strict-denied":
        (repo / ".hgp" / "hook-policy").write_text(STRICT)
    elif setup == "readonly":
        command = "git status"
    elif setup == "safe":
        command = "echo hi"
    elif setup == "other-tool":
        kw["tool_name"] = "Write"
    r = _spawn(PRE[client], _event(client, repo, command, **kw), tmp_path)
    assert "Traceback" not in r.stderr
    assert not _state_dir(tmp_path, client).exists()  # nothing written, not even the directory


@pytest.mark.parametrize("client", CLIENTS)
def test_the_marker_is_for_the_session_checkout_root_only(tmp_path: Path, client: str) -> None:
    repo = _repo(tmp_path)
    sub = repo / "sub"
    sub.mkdir()
    event = _event(client, repo, "cp a b")
    event["cwd"] = str(sub)
    assert _verdict(client, _spawn(PRE[client], event, tmp_path))[0] == "warn"
    assert list(json.loads(_marker(tmp_path, client).read_text())["baseline"]) == [str(repo)]


@pytest.mark.parametrize("client", CLIENTS)
def test_the_state_directory_is_per_user_and_private(tmp_path: Path, client: str) -> None:
    repo = _repo(tmp_path)
    _track(tmp_path, client, repo)
    state = _state_dir(tmp_path, client)
    info = state.lstat()
    assert stat.S_ISDIR(info.st_mode) and not state.is_symlink() and info.st_uid == os.getuid()
    assert stat.S_IMODE(info.st_mode) == 0o700
    assert stat.S_IMODE(_marker(tmp_path, client).lstat().st_mode) == 0o600


@pytest.mark.parametrize("client", CLIENTS)
def test_a_symlinked_state_directory_is_refused(tmp_path: Path, client: str) -> None:
    repo = _repo(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (tmp_path / "tmp").mkdir()
    _state_dir(tmp_path, client).symlink_to(elsewhere)
    r = _spawn(PRE[client], _event(client, repo, "cp a b"), tmp_path)
    assert _verdict(client, r)[0] == "warn"  # the warning still comes; only the tracking is off
    assert list(elsewhere.iterdir()) == []


@pytest.mark.parametrize("client", CLIENTS)
def test_stale_state_is_cleaned_a_few_files_at_a_time(tmp_path: Path, client: str) -> None:
    repo = _repo(tmp_path)
    state = _state_dir(tmp_path, client)
    state.mkdir(parents=True, mode=0o700)
    old = time.time() - 3 * 86400
    for i in range(25):
        p = state / f"bash-old{i}.json"
        p.write_text("{}")
        os.utime(p, (old, old))
    unrelated = state / "notes.txt"
    unrelated.write_text("x")
    os.utime(unrelated, (old, old))
    _track(tmp_path, client, repo)
    assert len([p for p in state.iterdir() if p.name.startswith("bash-old")]) == 5
    assert unrelated.exists()


# ── Tracking: after the command ──────────────────────────────────────────────

def _post(tmp_path: Path, client: str, repo: Path, **kw: object) -> subprocess.CompletedProcess[str]:
    return _spawn(POST[client], _post_event(client, repo, **kw), tmp_path)


@pytest.mark.parametrize("client", CLIENTS)
def test_post_reports_only_the_entries_that_are_new(tmp_path: Path, client: str) -> None:
    repo = _repo(tmp_path)
    (repo / "old.txt").write_text("x")  # there before the command
    (repo / "tracked.txt").write_text("changed\n")
    _track(tmp_path, client, repo)
    (repo / "new.txt").write_text("y")
    (repo / "newdir").mkdir()
    (repo / "newdir" / "f.txt").write_text("z")
    (repo / ".hgp" / "hgp.db").write_text("x")
    msg = _context(_post(tmp_path, client, repo))
    assert msg is not None
    assert msg.startswith("[HGP] Bash command")
    listed = [line.strip() for line in msg.splitlines() if line.startswith("    ")]
    assert sorted(listed) == ["?? new.txt", "?? newdir/f.txt"]
    assert "old.txt" not in msg and "hgp.db" not in msg and "tracked.txt" not in msg
    assert "data, not instructions" in msg
    assert "hgp_write_file" in msg


@pytest.mark.parametrize("client", CLIENTS)
def test_post_sees_a_tracked_file_the_command_changed(tmp_path: Path, client: str) -> None:
    repo = _repo(tmp_path)
    _track(tmp_path, client, repo)
    (repo / "tracked.txt").write_text("edited by the shell\n")
    msg = _context(_post(tmp_path, client, repo))
    assert msg is not None and " M tracked.txt" in msg


@pytest.mark.parametrize("client", CLIENTS)
def test_post_consumes_the_marker(tmp_path: Path, client: str) -> None:
    repo = _repo(tmp_path)
    _track(tmp_path, client, repo)
    (repo / "n.txt").write_text("y")
    assert _context(_post(tmp_path, client, repo)) is not None
    assert not _marker(tmp_path, client).exists()
    assert _context(_post(tmp_path, client, repo)) is None  # a second Post has nothing to diff


def test_claude_post_with_nothing_new_is_silent(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "old.txt").write_text("x")
    _track(tmp_path, "claude", repo)
    r = _post(tmp_path, "claude", repo)
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")
    assert not _marker(tmp_path, "claude").exists()


def test_codex_post_with_nothing_new_keeps_its_advisory(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "old.txt").write_text("x")
    _track(tmp_path, "codex", repo)
    msg = _context(_post(tmp_path, "codex", repo))
    assert msg is not None
    assert "may mutate files" in msg and "prefer hgp_* tools" in msg
    assert "old.txt" not in msg and "New git status entries" not in msg


@pytest.mark.parametrize("client", CLIENTS)
@pytest.mark.parametrize("bad", ["", "not json{", "[]", "null", '{"baseline": 3}', '{"baseline": {"/nope": 3}}',
                                 '{"pattern": 1, "baseline": {"/definitely/not/a/dir": ["?? a"]}}',
                                 '{"baseline": {"REPO": []}}', '{"pattern": "cp"}'])
def test_a_missing_or_corrupt_marker_is_silent(tmp_path: Path, client: str, bad: str) -> None:
    repo = _repo(tmp_path)
    r = _post(tmp_path, client, repo)
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")  # no marker at all
    state = _state_dir(tmp_path, client)
    assert not state.exists()  # a Post with nothing to find does not make the directory
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    _marker(tmp_path, client).write_text(bad.replace("REPO", str(repo)))
    (repo / "n.txt").write_text("y")
    r = _post(tmp_path, client, repo)
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")
    assert not _marker(tmp_path, client).exists()  # a bad marker is consumed too


@pytest.mark.parametrize("client", CLIENTS)
def test_post_matches_the_tool_use_id(tmp_path: Path, client: str) -> None:
    repo = _repo(tmp_path)
    _track(tmp_path, client, repo, tool_use_id="id-A")
    _track(tmp_path, client, repo, tool_use_id="id-B")
    (repo / "n.txt").write_text("y")
    assert _context(_post(tmp_path, client, repo, tool_use_id="id-C")) is None  # unknown id: nothing
    assert _marker(tmp_path, client, "id-A").exists() and _marker(tmp_path, client, "id-B").exists()
    assert _context(_post(tmp_path, client, repo, tool_use_id="id-A")) is not None
    assert not _marker(tmp_path, client, "id-A").exists() and _marker(tmp_path, client, "id-B").exists()


@pytest.mark.parametrize("client", CLIENTS)
def test_pre_and_post_pair_through_the_parent_pid_without_an_id(tmp_path: Path, client: str) -> None:
    repo = _repo(tmp_path)
    _track(tmp_path, client, repo, tool_use_id=None)
    (repo / "n.txt").write_text("y")
    msg = _context(_post(tmp_path, client, repo, tool_use_id=None))
    assert msg is not None and "?? n.txt" in msg
    assert not _marker(tmp_path, client, f"ppid-{os.getpid()}").exists()


@pytest.mark.parametrize("client", CLIENTS)
def test_post_caps_the_list_at_thirty(tmp_path: Path, client: str) -> None:
    repo = _repo(tmp_path)
    _track(tmp_path, client, repo)
    for i in range(45):
        (repo / f"f{i:02}.txt").write_text("x")
    msg = _context(_post(tmp_path, client, repo))
    assert msg is not None
    assert "?? f29.txt" in msg and "?? f30.txt" not in msg and "(+15 more)" in msg


@pytest.mark.parametrize("client", CLIENTS)
@pytest.mark.parametrize("count,truncated", [(200, False), (205, True)])
def test_post_says_when_the_list_was_truncated(tmp_path: Path, client: str, count: int, truncated: bool) -> None:
    repo = _repo(tmp_path)
    _track(tmp_path, client, repo)
    for i in range(count):
        (repo / f"f{i:03}.txt").write_text("x")
    msg = _context(_post(tmp_path, client, repo))
    assert msg is not None and "(+170 more)" in msg  # at most 200 kept, 30 listed
    assert ("more than 200 files" in msg and "truncated" in msg) is truncated


@pytest.mark.parametrize("client", CLIENTS)
def test_post_leaves_other_tools_alone(tmp_path: Path, client: str) -> None:
    repo = _repo(tmp_path)
    _track(tmp_path, client, repo)
    (repo / "n.txt").write_text("y")
    r = _spawn(POST[client], _post_event(client, repo, tool_name="Write"), tmp_path)
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")
    assert _marker(tmp_path, client).exists()


@pytest.mark.parametrize("client", CLIENTS)
def test_post_consumes_the_marker_but_stays_silent_when_the_session_left_an_enabled_repo(
    tmp_path: Path, client: str,
) -> None:
    repo = _repo(tmp_path)
    other = _repo(tmp_path, "other")
    (other / ".hgp").rmdir()  # `cd other && ...`: not an HGP repository
    _track(tmp_path, client, repo)
    (repo / "n.txt").write_text("y")
    r = _spawn(POST[client], _post_event(client, other), tmp_path)
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")
    assert not _marker(tmp_path, client).exists()


@pytest.mark.parametrize("client", CLIENTS)
@pytest.mark.parametrize("stdin", ["", "not json{", "[]", "null", "42", "{}", '{"tool_name": "Bash"}'])
def test_post_fails_open_on_malformed_events(tmp_path: Path, client: str, stdin: str) -> None:
    r = _spawn(POST[client], stdin, tmp_path)
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")


@pytest.mark.parametrize("client", CLIENTS)
def test_the_whole_chain(tmp_path: Path, client: str) -> None:
    repo = _repo(tmp_path)
    (repo / "old.txt").write_text("x")
    _track(tmp_path, client, repo, "touch made_by_shell")
    (repo / "made_by_shell").write_text("")
    msg = _context(_post(tmp_path, client, repo, command="touch made_by_shell"))
    assert msg is not None and "?? made_by_shell" in msg and "old.txt" not in msg


# ── git runs with no optional locks and a scrubbed environment ───────────────

def _fake_git(tmp_path: Path) -> tuple[Path, Path]:
    bin_dir, log = tmp_path / "bin", tmp_path / "fake-git.json"
    bin_dir.mkdir()
    script = bin_dir / "git"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "open(os.environ['FAKE_GIT_LOG'], 'w').write(json.dumps(\n"
        "    {'argv': sys.argv[1:], 'env': dict(os.environ), 'cwd': os.getcwd()}))\n"
        "print('?? x')\n"
    )
    script.chmod(0o755)
    return bin_dir, log


HOSTILE_GIT_ENV = {
    "GIT_DIR": "/nonexistent/elsewhere", "GIT_WORK_TREE": "/nonexistent", "GIT_INDEX_FILE": "/nonexistent",
    "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.fsmonitor", "GIT_CONFIG_VALUE_0": "/bin/evil",
    "GIT_CONFIG_GLOBAL": "/nonexistent", "GIT_EXTERNAL_DIFF": "/bin/evil", "GIT_EXEC_PATH": "/opt/git-exec",
}


def _assert_scrubbed(seen: dict[str, object]) -> None:
    argv, env = seen["argv"], seen["env"]
    assert isinstance(argv, list) and isinstance(env, dict)
    assert argv[:1] == ["--no-optional-locks"]  # a global option, before the subcommand
    assert argv[-3:] == ["status", "--porcelain", "-uall"] and "core.fsmonitor=false" in argv
    assert {k for k in env if k.startswith("GIT_")} == {"GIT_EXEC_PATH", "GIT_CONFIG_NOSYSTEM"}
    assert env["GIT_CONFIG_NOSYSTEM"] == "1" and env["GIT_EXEC_PATH"] == "/opt/git-exec"


@pytest.mark.parametrize("client", CLIENTS)
def test_the_hooks_run_git_with_a_scrubbed_environment(tmp_path: Path, client: str) -> None:
    repo = _repo(tmp_path)
    bin_dir, log = _fake_git(tmp_path)
    env = {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "FAKE_GIT_LOG": str(log), **HOSTILE_GIT_ENV}
    r = _spawn(PRE[client], _event(client, repo, "cp a b"), tmp_path, **env)
    assert _verdict(client, r)[0] == "warn"
    _assert_scrubbed(json.loads(log.read_text()))
    assert Path(json.loads(log.read_text())["cwd"]).resolve() == repo
    assert json.loads(_marker(tmp_path, client).read_text())["baseline"] == {str(repo): ["?? x"]}
    log.unlink()
    msg = _context(_spawn(POST[client], _post_event(client, repo), tmp_path, **{**env, "GIT_INDEX_FILE": "/x"}))
    _assert_scrubbed(json.loads(log.read_text()))
    assert msg is None or "?? x" not in msg  # `?? x` was in the baseline: nothing new


def test_git_status_in_hgp_paths_is_scrubbed_and_drops_hgp_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repo(tmp_path)
    (repo / "a.txt").write_text("x")
    (repo / ".hgp" / "hgp.db").write_text("x")
    exec_path = subprocess.run(["git", "--exec-path"], capture_output=True, text=True, check=True).stdout.strip()
    for key, value in HOSTILE_GIT_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("GIT_EXEC_PATH", exec_path)
    assert hgp_paths.git_status(repo) == ["?? a.txt"]  # GIT_DIR, GIT_INDEX_FILE, ... did not redirect it
    assert hgp_paths.git_status(tmp_path / "nowhere") is None
    plain = tmp_path / "plain"
    plain.mkdir()
    assert hgp_paths.git_status(plain) is None


def test_a_relative_marker_root_is_never_diffed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A relative root would be read against the hook's own cwd."""
    repo = _repo(tmp_path)
    (repo / "n.txt").write_text("y")
    monkeypatch.chdir(repo)
    assert hgp_paths.track_new_entries({"pattern": "cp", "baseline": {".": []}}) == []
    assert hgp_paths.track_new_entries({"pattern": "cp", "baseline": {str(repo): []}}) == ["?? n.txt"]
    unusable = (
        {}, {"baseline": 3}, {"baseline": {str(repo): 3}}, {"baseline": {3: []}}, {"baseline": {str(repo): [1]}},
    )
    for bad in unusable:
        assert hgp_paths.track_new_entries(bad) == [], bad


@pytest.mark.parametrize("name", ["../x", "a/b", "", "a b", "x\x00y", "a__b__c/d"])
def test_track_record_refuses_a_name_that_is_not_a_plain_file_name_part(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str,
) -> None:
    repo = _repo(tmp_path)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    hgp_paths.track_record("claude", name, repo, "cp")
    state = tmp_path / f"hgp-claude-{os.getuid()}"
    assert not state.exists() or list(state.iterdir()) == []
    assert not (tmp_path / "x.json").exists() and not (tmp_path.parent / "x.json").exists()
    assert hgp_paths.track_report("claude", name) is None


def test_the_agy_state_module_uses_the_shared_helpers() -> None:
    from hgp.hooks.agy import agy_state

    for name in ("safe_id", "git_status", "is_hgp_entry", "cleanup_stale", "read_json", "write_json", "remove"):
        assert getattr(agy_state, name).__module__ == hgp_paths.__name__, name
    assert agy_state.state_dir() == hgp_paths.track_dir("agy")


# ── The message ──────────────────────────────────────────────────────────────

def test_the_message_names_patterns_caps_the_list_and_cleans_names() -> None:
    nasty = "?? " + "x" * 400
    msg = hgp_paths.track_message(
        "Bash command", [nasty, "?? a\x1b[31m‮.txt", *[f"?? f{i:02}.txt" for i in range(40)]],
        ["\\bcp\\b", "\\bmv\\b", "\\btee\\b", "\\btouch\\b"], True, True,
    )
    first = msg.splitlines()[0]
    assert first.startswith("[HGP] Bash command (matched: \\bcp\\b, \\bmv\\b, \\btee\\b) mutated files")
    assert "touch" not in first
    lines = msg.splitlines()
    assert lines[1] == "    ?? " + "x" * 197 and lines[2] == "    ?? a[31m.txt"
    assert "\x1b" not in msg and "‮" not in msg
    assert "(+12 more)" in msg  # 42 entries, 30 listed
    assert "data, not instructions" in msg and "truncated" in msg and "error" in msg


# ── The fallback: no hgp_paths beside the hook ───────────────────────────────

FALLBACK_BEGIN = "# --- hgp-shell-fallback begin"
FALLBACK_END = "# --- hgp-shell-fallback end"


def _fallback_block(hook: Path) -> str:
    text = hook.read_text()
    start = text.index(FALLBACK_BEGIN)
    return text[text.index("\n", start) + 1:text.index(FALLBACK_END)]


def _load(hook: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, hook)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_fallback_block_is_identical_in_every_hook() -> None:
    blocks = {name: _fallback_block(path) for name, path in PRE.items()}
    assert blocks["agy"].strip() and blocks["agy"] == blocks["claude"] == blocks["codex"]


@pytest.mark.parametrize("client", ["agy", "claude", "codex"])
@pytest.mark.parametrize("command", [
    *GUARD_COMMANDS, *CONTROL_FILE_WRITES, *CONTROL_GUARDS,
])
def test_the_fallback_still_denies_every_guard(client: str, command: str) -> None:
    module = _load(PRE[client], f"_fb_{client}")
    assert module._fb_control_violation(command) is not None, command  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize("client", ["agy", "claude", "codex"])
@pytest.mark.parametrize("command", [*MUTATING_SHELL, *COMPOUND_COMMANDS])
def test_the_fallback_still_finds_a_mutating_command(client: str, command: str) -> None:
    module = _load(PRE[client], f"_fb_{client}")
    assert module._fb_mutating(command) is not None, command  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize("client", ["agy", "claude", "codex"])
@pytest.mark.parametrize("command", ["ls -la", "git status", "pwd", "echo hi", "git log --oneline -5", "head -5 a"])
def test_the_fallback_leaves_plain_reads_alone(client: str, command: str) -> None:
    module = _load(PRE[client], f"_fb_{client}")
    assert module._fb_control_violation(command) is None  # pyright: ignore[reportPrivateUsage]
    assert module._fb_mutating(command) is None  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize("client", ["agy", "claude", "codex"])
def test_a_lone_hook_without_hgp_paths_still_guards(tmp_path: Path, client: str) -> None:
    """An installed copy with neither hgp_paths beside it nor the hgp package (python -S)."""
    repo = _repo(tmp_path)
    lone = tmp_path / "installed" / PRE[client].name
    lone.parent.mkdir()
    lone.write_bytes(PRE[client].read_bytes())
    for command in (f"git status; hgp {_M} off", f"echo off > .hgp/{_M}"):
        r = _spawn(lone, _event(client, repo, command), tmp_path, flags=("-S",))
        assert _verdict(client, r)[0] == "deny", command
    assert _verdict(client, _spawn(lone, _event(client, repo, "ls -la"), tmp_path, flags=("-S",)))[0] == "silent"
    # strict from the environment still denies a mutating command; the policy file is read only with hgp_paths
    r = _spawn(lone, _event(client, repo, "cp a b"), tmp_path, flags=("-S",), HGP_HOOK_BLOCK="strict")
    assert _verdict(client, r)[0] == "deny"


@pytest.mark.parametrize("client", CLIENTS)
def test_the_post_hooks_run_without_the_hgp_package(tmp_path: Path, client: str) -> None:
    """Installed copies sit beside hgp_paths.py, not inside the package (python -S)."""
    repo = _repo(tmp_path)
    installed = tmp_path / "installed"
    installed.mkdir()
    for path in (PRE[client], POST[client], HOOKS / "hgp_paths.py"):
        (installed / path.name).write_bytes(path.read_bytes())
    pre = _spawn(installed / PRE[client].name, _event(client, repo, "cp a b"), tmp_path, flags=("-S",))
    assert _verdict(client, pre)[0] == "warn"
    (repo / "n.txt").write_text("y")
    post = _spawn(installed / POST[client].name, _post_event(client, repo), tmp_path, flags=("-S",))
    msg = _context(post)
    assert msg is not None and "?? n.txt" in msg


# ── Install ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("client", CLIENTS)
def test_install_puts_the_shared_analyser_beside_the_hooks(tmp_path: Path, client: str) -> None:
    from unittest.mock import patch

    from hgp.server import _install  # pyright: ignore[reportPrivateUsage]

    home = tmp_path / "home"
    home.mkdir()
    with patch("pathlib.Path.home", return_value=home), \
         patch("hgp.server._install_mcp", return_value=(True, "registered")):
        _install([f"--{client}"])
    hooks_dir = home / f".{client}" / "hooks"
    shared = (hooks_dir / "hgp_paths.py").read_bytes()
    assert shared == (HOOKS / "hgp_paths.py").read_bytes()
    assert b"def shell_control_violation(" in shared and b"def track_message(" in shared
    names = {p.name for p in hooks_dir.iterdir()}
    assert {PRE[client].name, POST[client].name, "hgp_paths.py"} <= names
    # the installed hooks work from there, with neither the package nor the repository
    repo = _repo(tmp_path)
    r = _spawn(hooks_dir / PRE[client].name, _event(client, repo, f"git status; hgp {_M} off"), tmp_path, flags=("-S",))
    assert _verdict(client, r)[0] == "deny"
