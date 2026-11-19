"""Antigravity CLI (agy) PreToolUse hook: deny/ask native file writes in sessions that use HGP.

The payloads mirror what agy 1.5.x sends a PreToolUse hook on stdin (camelCase JSON:
``toolCall.{id,name,args}``, ``cwd``, ``workspacePaths``, ``transcriptPath``). The hook
answers with ``{"decision": "force_ask"|"ask"|"deny", "reason": ...}`` or with empty stdout,
which agy treats as "no opinion". It must never answer "allow": that auto-approves the call.
A native write in a session that used HGP gets ``force_ask`` by default (agy's plain ``ask``
honours its always-allow cache and mode, so ``--mode accept-edits`` approved it unseen).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).parents[1] / "src" / "hgp" / "hooks" / "agy" / "pre_tool_use_hgp.py"

# Native file tools agy emits (older agents: write_to_file & co.; agy 1.5.x: write_file/edit_file)
# -> the HGP tool the hook points the agent to.
FILE_TOOLS = {
    "write_to_file": ({"TargetFile": "a.txt", "CodeContent": "x", "Overwrite": True}, "hgp_write_file"),
    "write_file": ({"Path": "a.txt", "Content": "x"}, "hgp_write_file"),
    "replace_file_content": (
        {"TargetFile": "a.txt", "TargetContent": "x", "ReplacementContent": "y"}, "hgp_edit_file",
    ),
    "multi_replace_file_content": ({"TargetFile": "a.txt", "ReplacementChunks": []}, "hgp_edit_file"),
    "edit_file": ({"Path": "a.txt", "Target": "x", "Replacement": "y"}, "hgp_edit_file"),
}
FILE_TOOL_CASES = [pytest.param(name, *spec, id=name) for name, spec in FILE_TOOLS.items()]

# What agy records in transcript_full.jsonl for an HGP MCP call (plain strings) ...
HGP_CALL = {"name": "call_mcp_tool", "args": {
    "ServerName": "hgp", "ToolName": "hgp_write_file", "Arguments": {"file_path": "a.txt"}}}
# ... and in transcript.jsonl (every value JSON-encoded).
HGP_CALL_ENCODED = {"name": "call_mcp_tool", "args": {
    "ServerName": '"hgp"', "ToolName": '"hgp_write_file"', "Arguments": '{"file_path": "a.txt"}'}}
OTHER_SERVER = {"name": "call_mcp_tool", "args": {"ServerName": "other", "ToolName": "hgp_write_file"}}
LOOKALIKE_SERVER = {"name": "call_mcp_tool", "args": {"ServerName": "hgp-other", "ToolName": "x"}}
MENTION_ONLY = {"name": "run_command", "args": {"CommandLine": 'echo \'"ServerName": "hgp"\''}}

MODE_CMD = "hgp " + "mode off"  # split so this file never trips command-line guards


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / ".hgp").mkdir()
    return repo


def _transcript(tmp_path: Path, *calls: dict[str, object]) -> Path:
    path = tmp_path / "transcript_full.jsonl"
    records: list[dict[str, object]] = [
        {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "talk about hgp, no tools"},
    ]
    records += [
        {"step_index": i, "source": "MODEL", "type": "PLANNER_RESPONSE", "tool_calls": [call]}
        for i, call in enumerate(calls, 1)
    ]
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return path


_ENV_KEYS = ("HGP_HOOK_BLOCK", "HGP_HOOK_ASK", "HGP_GLOBAL_MODE", "HGP_PROJECT_ROOT")


def _event(
    repo: Path, name: str, args: dict[str, object] | None, transcript: Path | None = None,
) -> dict[str, object]:
    event: dict[str, object] = {
        "hookEventName": "PreToolUse", "conversationId": "c1", "stepIdx": 4,
        "toolCall": {"id": "call_1", "name": name, "args": args if args is not None else {}},
        "cwd": str(repo), "workspacePaths": [str(repo)],
    }
    if transcript is not None:
        event["transcriptPath"] = str(transcript)
    return event


def _run_raw(
    stdin: str, cwd: Path, hook: Path = HOOK, flags: tuple[str, ...] = (), **env: str,
) -> subprocess.CompletedProcess[str]:
    base = {k: v for k, v in os.environ.items() if k not in _ENV_KEYS}
    return subprocess.run(
        [sys.executable, *flags, str(hook)], input=stdin, cwd=cwd,
        capture_output=True, text=True, env={**base, **env},
    )


def _run(
    repo: Path, name: str, args: dict[str, object] | None = None, transcript: Path | None = None,
    hook: Path = HOOK, flags: tuple[str, ...] = (), **env: str,
) -> subprocess.CompletedProcess[str]:
    # agy runs hook commands with the hooks.json directory as cwd, not the agent's cwd.
    hook_cwd = repo.parent / "hooks-json-dir"
    hook_cwd.mkdir(exist_ok=True)
    return _run_raw(json.dumps(_event(repo, name, args, transcript)), hook_cwd, hook, flags, **env)


def _decision(r: subprocess.CompletedProcess[str]) -> str | None:
    return json.loads(r.stdout)["decision"] if r.stdout.strip() else None


def _silent(r: subprocess.CompletedProcess[str]) -> bool:
    return (r.returncode, r.stdout, r.stderr) == (0, "", "")


def _set_block(repo: Path, text: str = '{"block_mode": true}') -> None:
    (repo / ".hgp" / "hook-policy").write_text(text)


def _lone_copy(tmp_path: Path) -> Path:
    """An installed hook with neither hgp_paths beside it nor the hgp package (run with -S)."""
    lone = tmp_path / "installed"
    lone.mkdir()
    hook = lone / "pre_tool_use_hgp.py"
    hook.write_bytes(HOOK.read_bytes())
    return hook


# ── Gate: HGP must be enabled ────────────────────────────────────────────────

@pytest.mark.parametrize("name,args,_equiv", FILE_TOOL_CASES)
def test_silent_when_hgp_not_enabled(tmp_path: Path, name: str, args: dict[str, object], _equiv: str) -> None:
    repo = _repo(tmp_path)
    (repo / ".hgp").rmdir()
    # even a block request and an HGP session stay out of a repository that never opted in
    r = _run(repo, name, args, _transcript(tmp_path, HGP_CALL), HGP_HOOK_BLOCK="1")
    assert _silent(r)
    assert not (repo / ".hgp").exists()


def test_silent_for_run_command_when_hgp_not_enabled(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / ".hgp").rmdir()
    assert _silent(_run(repo, "run_command", {"CommandLine": MODE_CMD}))
    assert _silent(_run(repo, "run_command", {"CommandLine": "cp a b"}, HGP_HOOK_BLOCK="1"))


# ── Native file tools: ask once the session used HGP ─────────────────────────

@pytest.mark.parametrize("name,args,_equiv", FILE_TOOL_CASES)
def test_no_output_for_write_in_session_that_never_used_hgp(
    tmp_path: Path, name: str, args: dict[str, object], _equiv: str,
) -> None:
    repo = _repo(tmp_path)
    for calls in ((), (OTHER_SERVER,), (LOOKALIKE_SERVER,), (MENTION_ONLY,)):
        assert _silent(_run(repo, name, args, _transcript(tmp_path, *calls))), calls


@pytest.mark.parametrize("call", [HGP_CALL, HGP_CALL_ENCODED], ids=["plain", "json-encoded"])
@pytest.mark.parametrize("name,args,equiv", FILE_TOOL_CASES)
def test_force_asks_once_session_used_hgp(
    tmp_path: Path, call: dict[str, object], name: str, args: dict[str, object], equiv: str,
) -> None:
    repo = _repo(tmp_path)
    r = _run(repo, name, args, _transcript(tmp_path, OTHER_SERVER, call))
    assert (r.returncode, r.stderr) == (0, "")
    out = json.loads(r.stdout)
    assert set(out) == {"decision", "reason"}
    assert out["decision"] == "force_ask"
    assert out["reason"].startswith("[HGP]")
    assert f"`{name}`" in out["reason"]
    assert f"`{equiv}`" in out["reason"]


@pytest.mark.parametrize("transcript", ["missing", "absent-key", "empty-key", "wrong-type"])
def test_no_ask_without_readable_transcript(tmp_path: Path, transcript: str) -> None:
    repo = _repo(tmp_path)
    args = FILE_TOOLS["write_file"][0]
    if transcript == "absent-key":
        r = _run(repo, "write_file", args)
    else:
        event = _event(repo, "write_file", args)
        event["transcriptPath"] = {
            "missing": str(tmp_path / "nope.jsonl"), "empty-key": "", "wrong-type": 7,
        }[transcript]
        r = _run_raw(json.dumps(event), tmp_path)
    assert _silent(r)


def test_ask_survives_garbage_lines_in_transcript(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    path = _transcript(tmp_path, HGP_CALL)
    path.write_text("not json hgp\n" + path.read_text() + "\xff\xfe broken hgp line\n", errors="replace")
    assert _decision(_run(repo, "write_file", FILE_TOOLS["write_file"][0], path)) == "force_ask"


def test_hgp_hook_ask_0_disables_ask(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    r = _run(repo, "write_file", FILE_TOOLS["write_file"][0], _transcript(tmp_path, HGP_CALL), HGP_HOOK_ASK="0")
    assert _silent(r)


# HGP_HOOK_ASK picks the decision for a native write in a session that used HGP: unset, 1 and force
# give force_ask (always prompts, unlike agy's ask, which honours the always-allow cache and the
# accept-edits mode); ask gives ask; 0 gives no prompt; any other value fails toward prompting.
ASK_VALUES = [
    pytest.param(None, "force_ask", id="unset"), pytest.param("1", "force_ask", id="1"),
    pytest.param("force", "force_ask", id="force"), pytest.param("ask", "ask", id="ask"),
    pytest.param("0", None, id="0"), pytest.param("", "force_ask", id="empty"),
    pytest.param("2", "force_ask", id="2"), pytest.param("yes", "force_ask", id="yes"),
    pytest.param("false", "force_ask", id="false"), pytest.param("no", "force_ask", id="no"),
    pytest.param("ASK", "force_ask", id="ASK-is-case-sensitive"), pytest.param("Force", "force_ask", id="Force"),
    pytest.param(" 0", "force_ask", id="space-0"),
]


def _ask_env(value: str | None) -> dict[str, str]:
    return {} if value is None else {"HGP_HOOK_ASK": value}


@pytest.mark.parametrize("value,expected", ASK_VALUES)
@pytest.mark.parametrize("name,args,equiv", FILE_TOOL_CASES)
def test_hgp_hook_ask_value_picks_the_decision(
    tmp_path: Path, name: str, args: dict[str, object], equiv: str, value: str | None, expected: str | None,
) -> None:
    repo = _repo(tmp_path)
    r = _run(repo, name, args, _transcript(tmp_path, HGP_CALL), **_ask_env(value))
    assert (r.returncode, r.stderr) == (0, "")
    assert '"allow"' not in r.stdout
    if expected is None:
        assert r.stdout == ""
        return
    out = json.loads(r.stdout)
    assert set(out) == {"decision", "reason"}
    assert out["decision"] == expected
    assert f"`{name}`" in out["reason"]
    assert f"`{equiv}`" in out["reason"]


@pytest.mark.parametrize("value,_expected", ASK_VALUES)
def test_block_still_denies_whatever_hgp_hook_ask_says(
    tmp_path: Path, value: str | None, _expected: str | None,
) -> None:
    repo = _repo(tmp_path)
    transcript = _transcript(tmp_path, HGP_CALL)
    args = FILE_TOOLS["write_file"][0]
    assert _decision(_run(repo, "write_file", args, transcript, HGP_HOOK_BLOCK="1", **_ask_env(value))) == "deny"
    _set_block(repo)
    assert _decision(_run(repo, "write_file", args, transcript, **_ask_env(value))) == "deny"


@pytest.mark.parametrize("value,_expected", ASK_VALUES)
def test_no_prompt_outside_a_session_that_used_hgp_whatever_hgp_hook_ask_says(
    tmp_path: Path, value: str | None, _expected: str | None,
) -> None:
    repo = _repo(tmp_path)
    args = FILE_TOOLS["write_file"][0]
    for calls in ((), (OTHER_SERVER,), (MENTION_ONLY,)):
        assert _silent(_run(repo, "write_file", args, _transcript(tmp_path, *calls), **_ask_env(value)))
    assert _silent(_run(repo, "write_file", args, **_ask_env(value)))  # no transcript at all
    (repo / ".hgp").rmdir()  # HGP not enabled: silent even for a session that used it
    assert _silent(_run(repo, "write_file", args, _transcript(tmp_path, HGP_CALL), **_ask_env(value)))


@pytest.mark.parametrize("value,_expected", ASK_VALUES)
def test_paused_hgp_stays_silent_whatever_hgp_hook_ask_says(
    tmp_path: Path, value: str | None, _expected: str | None,
) -> None:
    repo = _repo(tmp_path)
    (repo / ".hgp" / "mode").write_text("off\n")
    r = _run(repo, "write_file", FILE_TOOLS["write_file"][0], _transcript(tmp_path, HGP_CALL), **_ask_env(value))
    assert _silent(r)


def test_respond_emits_force_ask_ask_and_deny_but_never_allow(capsys: pytest.CaptureFixture[str]) -> None:
    from hgp.hooks.agy import pre_tool_use_hgp as agy_hook

    for decision in ("force_ask", "ask", "deny"):
        agy_hook._respond(decision, "why")  # pyright: ignore[reportPrivateUsage]
        assert json.loads(capsys.readouterr().out) == {"decision": decision, "reason": "why"}
    for decision in ("allow", "ALLOW", "", "force_allow", "bogus"):
        agy_hook._respond(decision, "why")  # pyright: ignore[reportPrivateUsage]
        assert capsys.readouterr().out == ""


def test_ask_value_helper_maps_the_documented_values() -> None:
    import os
    from unittest import mock

    from hgp.hooks.agy import pre_tool_use_hgp as agy_hook

    for value, expected in [(p.values[0], p.values[1]) for p in ASK_VALUES]:
        env = {k: v for k, v in os.environ.items() if k != "HGP_HOOK_ASK"}
        if value is not None:
            env["HGP_HOOK_ASK"] = value
        with mock.patch.dict(os.environ, env, clear=True):
            assert agy_hook._ask_decision() == expected  # pyright: ignore[reportPrivateUsage]


def test_workspace_path_used_when_cwd_absent(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    event = _event(repo, "write_file", FILE_TOOLS["write_file"][0], _transcript(tmp_path, HGP_CALL))
    del event["cwd"]
    assert _decision(_run_raw(json.dumps(event), tmp_path)) == "force_ask"
    other = tmp_path / "other"
    other.mkdir()  # neither cwd nor workspacePaths: the hook's own cwd decides (not enabled here)
    del event["workspacePaths"]
    assert _silent(_run_raw(json.dumps(event), other))


def test_hgp_project_root_env_decides_like_the_server(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=elsewhere, check=True)  # not enabled
    transcript = _transcript(tmp_path, HGP_CALL)
    event = json.dumps(_event(elsewhere, "write_file", FILE_TOOLS["write_file"][0], transcript))
    assert _silent(_run_raw(event, tmp_path))
    assert _decision(_run_raw(event, tmp_path, HGP_PROJECT_ROOT=str(repo))) == "force_ask"


# ── Block policy ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name,args,equiv", FILE_TOOL_CASES)
@pytest.mark.parametrize("how", ["policy-file", "legacy-policy-file", "env"])
def test_block_denies_native_file_tools(
    tmp_path: Path, name: str, args: dict[str, object], equiv: str, how: str,
) -> None:
    repo = _repo(tmp_path)
    env: dict[str, str] = {}
    if how == "env":
        env["HGP_HOOK_BLOCK"] = "1"
    else:
        _set_block(repo, "block\n" if how == "legacy-policy-file" else '{"block_mode": true}')
    # block applies with or without an HGP session, and wins over ask
    for calls in ((), (HGP_CALL,)):
        r = _run(repo, name, args, _transcript(tmp_path, *calls), **env)
        assert (r.returncode, r.stderr) == (0, "")
        out = json.loads(r.stdout)
        assert out["decision"] == "deny"
        assert f"`{equiv}`" in out["reason"]


def test_block_denies_without_toolcall_args(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    event = _event(repo, "write_file", None)
    del event["toolCall"]["args"]  # type: ignore[attr-defined]
    assert _decision(_run_raw(json.dumps(event), tmp_path, HGP_HOOK_BLOCK="1")) == "deny"


def test_env_block_0_overrides_block_policy_file(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _set_block(repo)
    assert _silent(_run(repo, "write_file", FILE_TOOLS["write_file"][0], HGP_HOOK_BLOCK="0"))


def test_block_policy_in_ancestor_store_outside_git_does_not_block(tmp_path: Path) -> None:
    (tmp_path / ".hgp").mkdir()
    (tmp_path / ".hgp" / "hook-policy").write_text('{"block_mode": true}')
    plain = tmp_path / "plain"
    plain.mkdir()
    r = _run(plain, "write_file", FILE_TOOLS["write_file"][0], _transcript(tmp_path, HGP_CALL))
    assert _silent(r)


# ── Paused HGP (mode advisory/off): HGP tools do not write, so stay out of the way ──

@pytest.mark.parametrize("mode", ["advisory", "off"])
@pytest.mark.parametrize("name,args,_equiv", FILE_TOOL_CASES)
def test_silent_while_paused(tmp_path: Path, name: str, args: dict[str, object], _equiv: str, mode: str) -> None:
    repo = _repo(tmp_path)
    (repo / ".hgp" / "mode").write_text(mode + "\n")
    _set_block(repo)
    r = _run(repo, name, args, _transcript(tmp_path, HGP_CALL))  # would be ask, and block would be deny
    assert _silent(r)


@pytest.mark.parametrize("mode", ["advisory", "off"])
@pytest.mark.parametrize("name,args,_equiv", FILE_TOOL_CASES)
def test_env_block_wins_while_paused(
    tmp_path: Path, name: str, args: dict[str, object], _equiv: str, mode: str,
) -> None:
    # An agent can write .hgp/mode; it cannot change the hook's environment.
    repo = _repo(tmp_path)
    (repo / ".hgp" / "mode").write_text(mode + "\n")
    assert _decision(_run(repo, name, args, HGP_HOOK_BLOCK="1")) == "deny"


# ── run_command ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("mode", ["on", "advisory", "off"])
@pytest.mark.parametrize("command", [
    MODE_CMD, f"  {MODE_CMD}", "hgp  " + "mode on", f"ls && {MODE_CMD}", f"echo hi; {MODE_CMD}",
    "cd /tmp && hgp " + "mode advisory",
])
def test_mode_change_command_is_always_denied(tmp_path: Path, command: str, mode: str) -> None:
    repo = _repo(tmp_path)
    (repo / ".hgp" / "mode").write_text(mode + "\n")
    r = _run(repo, "run_command", {"CommandLine": command, "Cwd": str(repo)})
    assert (r.returncode, r.stderr) == (0, "")
    out = json.loads(r.stdout)
    assert out["decision"] == "deny"
    assert "must be run by the user" in out["reason"]


def test_mode_change_command_denied_without_block_and_with_ask_disabled(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    r = _run(repo, "run_command", {"CommandLine": MODE_CMD}, HGP_HOOK_ASK="0", HGP_HOOK_BLOCK="0")
    assert _decision(r) == "deny"


@pytest.mark.parametrize("command", ["cp a b", "rm -f a", "echo hi > out.txt", "git checkout -- a", "sed -i s/a/b/ f"])
def test_mutating_command_is_denied_only_under_block(tmp_path: Path, command: str) -> None:
    repo = _repo(tmp_path)
    args = {"CommandLine": command}
    transcript = _transcript(tmp_path, HGP_CALL)
    assert _silent(_run(repo, "run_command", args, transcript))  # advisory: no ask for shell, as in Claude
    _set_block(repo)
    r = _run(repo, "run_command", args, transcript)
    assert (r.returncode, r.stderr) == (0, "")
    assert json.loads(r.stdout)["decision"] == "deny"
    assert "hgp_" in json.loads(r.stdout)["reason"]
    assert _decision(_run(repo, "run_command", args, HGP_HOOK_BLOCK="1")) == "deny"


@pytest.mark.parametrize("command", ["ls -la", "git status", "git diff HEAD", "pwd", "grep -rn x .", "uname -a"])
def test_readonly_and_other_commands_never_denied(tmp_path: Path, command: str) -> None:
    repo = _repo(tmp_path)
    _set_block(repo)
    assert _silent(_run(repo, "run_command", {"CommandLine": command}))


# A read-only prefix must not exempt a command that chains, substitutes or redirects.
COMPOUND_COMMANDS = [
    "ls && cp a b", "git status; echo x > f", "ls -la | tee out.txt", "ls -la\ncp a b", "git diff $(cp a b)",
    "grep x `rm f`", "ls -la > out.txt", "git log; rm -f a", "pwd && mv a b", "find . -exec rm {} \\;",
    "head -1 a || touch b", "ls -la & cp a b", "git status >> log.txt", "uname -a;rm a",
]


@pytest.mark.parametrize("command", COMPOUND_COMMANDS)
def test_compound_command_with_readonly_prefix_is_still_checked_for_mutation(tmp_path: Path, command: str) -> None:
    repo = _repo(tmp_path)
    args = {"CommandLine": command}
    assert _silent(_run(repo, "run_command", args))  # advisory policy: never denied
    _set_block(repo)
    r = _run(repo, "run_command", args)
    assert (r.returncode, r.stderr) == (0, "")
    assert json.loads(r.stdout)["decision"] == "deny"
    assert _decision(_run(repo, "run_command", args, HGP_HOOK_BLOCK="1")) == "deny"
    (repo / ".hgp" / "mode").write_text("off\n")  # paused: the policy file yields, the env var does not
    assert _silent(_run(repo, "run_command", args))
    assert _decision(_run(repo, "run_command", args, HGP_HOOK_BLOCK="1")) == "deny"


@pytest.mark.parametrize("command", [
    "ls -la", "git status", "git diff HEAD~1 -- src", "git log --oneline -5", "pwd", "grep -rn foo src",
    "find . -name x -type f", "head -5 a.txt", "  git diff HEAD  ", "wc -l a.txt",
])
def test_plain_readonly_commands_stay_silent_under_block(tmp_path: Path, command: str) -> None:
    repo = _repo(tmp_path)
    _set_block(repo)
    assert _silent(_run(repo, "run_command", {"CommandLine": command}, HGP_HOOK_BLOCK="1"))


@pytest.mark.parametrize("command", ['c"p" a b', "r\\m -f a", "'mv' a b", 't""ee out.txt', "echo hi | 't'ee out.txt"])
def test_quoting_does_not_hide_a_mutating_command(tmp_path: Path, command: str) -> None:
    repo = _repo(tmp_path)
    args = {"CommandLine": command}
    assert _silent(_run(repo, "run_command", args))
    _set_block(repo)
    assert _decision(_run(repo, "run_command", args)) == "deny"


def test_is_readonly_needs_a_prefix_and_no_control_operators() -> None:
    from hgp.hooks.agy import pre_tool_use_hgp as agy_hook

    for command in ("ls -la", "git status", "pwd", "grep -rn x .", "  git diff HEAD\n", "head -5 a.txt"):
        assert agy_hook._is_readonly(command), command  # pyright: ignore[reportPrivateUsage]
    for command in (*COMPOUND_COMMANDS, "ls ; pwd", "cp a b", "echo hi", "ls -la <(cp a b)"):
        assert not agy_hook._is_readonly(command), command  # pyright: ignore[reportPrivateUsage]


# Mode and policy are user-only. Quoting, backslashes and variable expansion must not hide the command.
_M = "mo" + "de"  # keeps the literal command text out of this file
MODE_CHANGE_BYPASSES = [
    f"'hgp' {_M} off", f'"hgp" "{_M}" off', "h\"g\"p mo'd'e off", "hg\\p m\\ode on", f"hgp \\{_M} off",
    f"/usr/local/bin/hgp {_M} off", f"uv run hgp {_M} advisory", f"HGP_X=1 hgp {_M} off", f"sh -c 'hgp {_M} off'",
    f"H=hgp; $H {_M} off", f"M={_M}; hgp $M off", f"$(echo hgp) {_M} off", f"H=hgp && ${{H}} {_M} on",
    f"M={_M}; ~/.venv/bin/hgp $M off",
    "hgp hook-policy block", "hgp 'hook-policy' advisory", "hgp hook\\-policy advisory",
    "X=hook-policy; hgp $X advisory", "hgp hook-policy", f"ls -la && hgp hook-policy block; echo {_M}",
]


@pytest.mark.parametrize("mode", ["on", "off"])
@pytest.mark.parametrize("command", MODE_CHANGE_BYPASSES)
def test_mode_and_policy_changes_are_denied_through_quoting_and_expansion(
    tmp_path: Path, command: str, mode: str,
) -> None:
    repo = _repo(tmp_path)
    (repo / ".hgp" / "mode").write_text(mode + "\n")
    r = _run(repo, "run_command", {"CommandLine": command}, HGP_HOOK_ASK="0")
    assert (r.returncode, r.stderr) == (0, "")
    out = json.loads(r.stdout)
    assert out["decision"] == "deny"
    assert "must be run by the user" in out["reason"]


@pytest.mark.parametrize("command", [
    f"grep -rn {_M} src/hgp", "cat src/hgp/hooks/hgp_paths.py", f'git commit -m "fix(hgp): paused {_M} handling"',
    "python -m hgp.server --help", f"echo {_M}", "echo hgp", f"uv run pytest tests/test_{_M}.py -q",
    f"git log --oneline --grep={_M} -- src/hgp", f"cat .hgp/{_M}", "ls -la .hgp/", "grep -n block .hgp/hook-policy",
    # with `$` the words only need to appear, but a path component or file name is not the word hgp
    f"cd $(git rev-parse --show-toplevel) && grep -rn {_M} src/hgp", f"grep -n {_M} $HOME/proj/hgp_paths.py",
    "echo $HOME; grep -n hook-policy src/hgp/server.py",
])
def test_commands_that_only_mention_hgp_or_mode_are_not_denied(tmp_path: Path, command: str) -> None:
    repo = _repo(tmp_path)
    _set_block(repo)
    assert _silent(_run(repo, "run_command", {"CommandLine": command}))


# The agent can write .hgp/mode and .hgp/hook-policy itself, so a shell write to them is denied too.
CONTROL_FILE_WRITES = [
    f"echo off > .hgp/{_M}", 'echo \'{"block_mode": false}\' > .hgp/hook-policy', f"printf off | tee .hgp/{_M}",
    f"sed -i s/on/off/ .hgp/{_M}", "mv /tmp/x .hgp/hook-policy", f"cp /tmp/x /repo/.hgp/{_M}",
    f"echo advisory >> .hgp/{_M}", f'echo off > ".hgp/{_M}"', "echo off > .hgp/mo\\de", "rm .hgp/hook-policy",
    f"ls -la && echo off > .hgp/{_M}",
    f"echo off 1>.hgp/{_M}", f"echo off 2> .hgp/{_M}", f"echo off &> .hgp/{_M}", f"echo off >| .hgp/{_M}",
    f"cat > .hgp/{_M} <<EOF\noff\nEOF", f"dd if=/dev/null of=.hgp/{_M}", f"truncate -s 0 .hgp/{_M}",
    f"touch .hgp/{_M}", f"ln -sf /dev/null .hgp/{_M}", "sed -i.bak s/a/b/ .hgp/hook-policy", f"sudo tee .hgp/{_M}",
    f"/bin/cp x .hgp/{_M}", f"echo off | tee -a .hgp/{_M} > /dev/null", f"find . -exec cp x .hgp/{_M} \\;",
    "mv .hgp/hook-policy /tmp/policy.bak",  # moving the policy away drops it
    # a redirect after the target must not hide the operand that is written
    f"cp /tmp/x .hgp/{_M} > /dev/null", f"cp /tmp/x .hgp/{_M} 2>/dev/null", f"cp /tmp/x .hgp/{_M} 2>&1",
    f"rm .hgp/{_M} 2>/dev/null", f"tee .hgp/{_M} < /tmp/x",
    # fail closed: a control path together with a writer word anywhere in the command is a deny,
    # whichever operand the word acts on (operands are not resolved)
    "git checkout -- .hgp/hook-policy", f"git checkout -- .hgp/{_M}", "git restore .hgp/hook-policy",
    f"git reset .hgp/{_M}", f"git apply p .hgp/{_M}", f"patch .hgp/{_M} < p", "patch .hgp/hook-policy < p",
    f"cp .hgp/{_M} /tmp/x", f"cp .hgp/{_M} /tmp/backup", "cat .hgp/hook-policy | tee log.txt",
    f"cat .hgp/{_M} && tee log.txt < x", f"grep -c on .hgp/{_M}; touch other.txt", f"ln -s .hgp/{_M} link",
    "tee log.txt < .hgp/hook-policy", f"chmod 600 .hgp/{_M}", f"install -m 600 x .hgp/{_M}",
    f"sed -i s/a/b/ x; cat .hgp/{_M}", f"dd if=x of=y; cat .hgp/{_M}", f"rm x; cat .hgp/{_M}",
    f"echo off | tee x .hgp/{_M}", f"mkdir d && cat .hgp/{_M}", f"find . -path .hgp/{_M} -delete",
    f"git diff --output=.hgp/{_M}", f"find . -name {_M} -exec echo .hgp/{_M} {{}} +",
    # redirects are found wherever they occur, not only after the first `>`
    f"echo off 2> err.log > .hgp/{_M}", f"echo a >> x.log; echo off > .hgp/{_M}", f"cat x > y 2>&1 >.hgp/{_M}",
    f"echo off >&.hgp/{_M}", f"true > a > .hgp/{_M}",
    # in-place editors: any sed or perl next to a control path is a write, whatever its options
    f"sed -Ei 's/.*/off/' .hgp/{_M}", f"sed -i.bak s/a/b/ .hgp/{_M}", f"sed --in-place s/a/b/ .hgp/{_M}",
    "sed --in-place=.bak s/a/b/ .hgp/hook-policy", f"sed -ni p .hgp/{_M}", f"sed -E -i s/a/b/ .hgp/{_M}",
    f"sed --i s/a/b/ .hgp/{_M}", f"sed s/a/b/ .hgp/{_M} -i", f"perl -pi -e 's/on/off/' .hgp/{_M}",
    "perl -i.bak -pe s/a/b/ .hgp/hook-policy", f"perl -0pi -e x .hgp/{_M}", f"perl -ne print .hgp/{_M}",
    f"sed -n p .hgp/{_M}", f"sed s/a/b/ .hgp/{_M} > out.txt", "/usr/bin/perl -pi -e x .hgp/hook-policy",
]


@pytest.mark.parametrize("mode", ["on", "off"])
@pytest.mark.parametrize("command", CONTROL_FILE_WRITES)
def test_shell_writes_to_hgp_control_files_are_denied(tmp_path: Path, command: str, mode: str) -> None:
    repo = _repo(tmp_path)
    (repo / ".hgp" / "mode").write_text(mode + "\n")
    # whatever the policy: no block policy, ask disabled, paused (mode off) or not
    r = _run(repo, "run_command", {"CommandLine": command}, HGP_HOOK_ASK="0")
    assert (r.returncode, r.stderr) == (0, "")
    out = json.loads(r.stdout)
    assert out["decision"] == "deny"
    assert ".hgp/mode" in out["reason"]
    assert "hook-policy" in out["reason"]


def test_control_file_write_check_needs_an_enabled_repo(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / ".hgp").rmdir()
    assert _silent(_run(repo, "run_command", {"CommandLine": f"echo off > .hgp/{_M}"}))


# Allowed: no writer word at all, and the only writes are redirects to some other file. Everything
# else that mentions a control path next to a writer word is denied (see CONTROL_FILE_WRITES).
CONTROL_FILE_READS = [
    f"grep x .hgp/{_M} > out.txt", f"head -1 .hgp/{_M} 2>/dev/null", f"diff .hgp/{_M} other.txt",
    f"echo hi > .hgp/{_M}.bak", f"cat .hgp/{_M} .hgp/hook-policy | wc -l",
    f"grep -c on .hgp/{_M}", f"cat .hgp/{_M} > /tmp/copy.txt", "grep x .hgp/hook-policy 2> err.log",
    "cat .hgp/hook-policy &> out.txt", f"echo $(cat .hgp/{_M}) > out.txt", f"cat .hgp/{_M} >> log.txt 2>&1",
    f"cat .hgp/{_M}", "ls -la .hgp/", f"git log --oneline -- .hgp/{_M}",
]


@pytest.mark.parametrize("mode", ["on", "off"])
@pytest.mark.parametrize("command", CONTROL_FILE_READS)
def test_reading_a_control_file_or_redirecting_elsewhere_is_not_a_control_write(
    tmp_path: Path, command: str, mode: str,
) -> None:
    repo = _repo(tmp_path)
    (repo / ".hgp" / "mode").write_text(mode + "\n")
    assert _silent(_run(repo, "run_command", {"CommandLine": command}, HGP_HOOK_ASK="0"))


def test_unrelated_redirect_is_an_ordinary_mutation_not_a_control_write(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _set_block(repo)
    out = json.loads(_run(repo, "run_command", {"CommandLine": f"grep x .hgp/{_M} > out.txt"}).stdout)
    assert out["decision"] == "deny"
    assert "may mutate" in out["reason"]
    assert "user-controlled" not in out["reason"]


# ── A read-only prefix never hides a self-mutating option ────────────────────

SELF_MUTATING_READONLY = [
    "find . -name '*.tmp' -delete", "find . -name x -exec chmod 600 {} +", "git diff --output=patch.txt",
    "find . -name x -exec echo {} +", "find . -name x -execdir echo {} +", "find . -name x -ok echo {} +",
    "find . -name x -okdir echo {} +", "find . -name x -fprint out.txt", "find . -name x -fprint0 out.txt",
    "find . -name x -fprintf out.txt %p", "find . -name x -fls out.txt", "git log --output out.txt",
    "git show --output=x HEAD", "ls -la; find . -delete",
    # options that run another command, with and without a mutating word in it
    "rg --pre chmod x .", "rg --pre ./clean.sh x .", "rg --pre=./clean.sh x .", "find . -name x -exec rm {} +",
    # git options that run a program (also through git's unambiguous abbreviations)
    "git fetch --upload-pack='sh -c x' origin", "git fetch --upload-pack=cmd origin",
    "git ls-remote --upload-pack=cmd origin", "git ls-remote --uploadpack=cmd origin", "git ls-remote --up=cmd origin",
    "git ls-remote --upl=cmd origin", "git ls-remote --exec=cmd origin", "git ls-remote --exe=cmd origin",
    "git push --receive-pack=cmd origin", "git push --rec=cmd origin", "git push --rece=cmd origin",
    "git diff --ext-diff", "git diff --ext x", "git diff --ext-d", "git log --ext-diff -p", "git show --ext-diff HEAD",
    "git -c core.pager=less log", "git -c core.fsmonitor=cmd status", "git -ccore.pager=x log",
    "git -c alias.x='!cmd' x", "git --config-env=core.pager=PAGER log", "git ls-remote --upload-pack x origin",
]


@pytest.mark.parametrize("command", SELF_MUTATING_READONLY)
def test_self_mutating_option_behind_a_readonly_prefix_is_denied_under_block(tmp_path: Path, command: str) -> None:
    repo = _repo(tmp_path)
    args = {"CommandLine": command}
    assert _silent(_run(repo, "run_command", args))  # advisory policy: never denied
    _set_block(repo)
    r = _run(repo, "run_command", args)
    assert (r.returncode, r.stderr) == (0, "")
    assert json.loads(r.stdout)["decision"] == "deny"
    assert _decision(_run(repo, "run_command", args, HGP_HOOK_BLOCK="1")) == "deny"
    (repo / ".hgp" / "mode").write_text("off\n")  # paused: the policy file yields, the env var does not
    assert _silent(_run(repo, "run_command", args))
    assert _decision(_run(repo, "run_command", args, HGP_HOOK_BLOCK="1")) == "deny"


def test_git_fetch_and_remote_are_not_readonly_prefixes() -> None:
    from hgp.hooks.agy import pre_tool_use_hgp as agy_hook

    for command in (
        "git fetch origin", "git fetch --upload-pack=cmd origin", "git remote -v", "git remote show origin",
    ):
        assert not agy_hook._is_readonly(command), command  # pyright: ignore[reportPrivateUsage]
    for command in ("git log", "git show HEAD", "git diff", "git status", "git branch", "git ls-files", "ls -la"):
        assert agy_hook._is_readonly(command), command  # pyright: ignore[reportPrivateUsage]


# In-place editors outside the control files: sed with any -...i cluster or --in-place, perl with -...i.
IN_PLACE_EDITORS = [
    "sed -i s/a/b/ src/x.py", "sed -Ei s/a/b/ f", "sed -i.bak s/a/b/ f", "sed --in-place s/a/b/ f",
    "sed --in-place=.bak s/a/b/ f", "sed s/a/b/ -i f", "sed -ni p f", "sed -E -i s/a/b/ f", "sed --i s/a/b/ f",
    "sed 's/a;b/c/' -i f", "perl -pi -e s/a/b/ f", "perl -i.bak -pe s/a/b/ f", "perl -0pi -e x f",
    "perl -pi -e 's/a/b/' f", "/usr/bin/perl -pi -e x f", "perl -Mstrict -pi -e x f",
]


@pytest.mark.parametrize("command", IN_PLACE_EDITORS)
def test_in_place_editors_are_denied_under_block(tmp_path: Path, command: str) -> None:
    repo = _repo(tmp_path)
    args = {"CommandLine": command}
    assert _silent(_run(repo, "run_command", args))  # advisory policy: never denied
    _set_block(repo)
    assert _decision(_run(repo, "run_command", args)) == "deny"
    assert _decision(_run(repo, "run_command", args, HGP_HOOK_BLOCK="1")) == "deny"
    (repo / ".hgp" / "mode").write_text("off\n")  # paused: the policy file yields, the env var does not
    assert _silent(_run(repo, "run_command", args))
    assert _decision(_run(repo, "run_command", args, HGP_HOOK_BLOCK="1")) == "deny"


@pytest.mark.parametrize("command", [
    "sed -n p f", "sed s/a/b/ f", "sed -e s/a/b/ f", "sed -E s/a/b/ f", "sed -s p f", "sed --expression=s/a/b/ f",
    "cat f | sed s/a/b/ | grep -i x", "sed s/a/b/ f | sort -u", "perl -ne print f", "perl -Mstrict -e 1",
    "perl -Ilib x.pl",
    "perl -MData::Dumper -e 1", "perl -wle print", "perl -mwarnings -e 1", "perl -ne print f | grep -i x",
    "perl -wle print | grep -in y", "perl -e 1 && ls -i", "sed -n p f | grep -i x", "sed -n p f && ls -i",
    # read-only commands: sed and perl in their arguments are text
    "grep -n 'sed -i' src", "grep -n 'sed -Ei' src", "rg 'perl -pi' src", "grep -rn in-place src", "git log --grep=sed",
])
def test_non_inplace_sed_and_perl_are_not_denied_under_block(tmp_path: Path, command: str) -> None:
    repo = _repo(tmp_path)
    _set_block(repo)
    assert _silent(_run(repo, "run_command", {"CommandLine": command}))
    assert _silent(_run(repo, "run_command", {"CommandLine": command}, HGP_HOOK_BLOCK="1"))


@pytest.mark.parametrize("command", [
    "git log", "git show HEAD", "git diff", "git diff HEAD~1 -- src", "git status",
    "git log --extended-regexp --grep=x",
    "git log -E --grep=x", "git ls-files --exclude-standard", "git ls-files --recurse-submodules", "git log -c --stat",
    "git diff -c", "git status --untracked-files=no", "git log --pretty=format:%h -5", "git fetch origin",
    "git remote -v", "git remote show origin", "git ls-remote origin", "git diff --exit-code", "git log --exclude=x",
    "git fetch --update-head-ok origin",
])
def test_plain_git_reads_are_not_denied_under_block(tmp_path: Path, command: str) -> None:
    repo = _repo(tmp_path)
    _set_block(repo)
    assert _silent(_run(repo, "run_command", {"CommandLine": command}))
    assert _silent(_run(repo, "run_command", {"CommandLine": command}, HGP_HOOK_BLOCK="1"))


# A read-only command only runs the option-based checks: a mutating word in its arguments is text.
READONLY_COMMANDS_MENTIONING_MUTATING_WORDS = [
    "grep -rn install src", "grep -rn 'rm' src", "rg chmod", "git log --grep=cp", "git show HEAD:install.sh",
    "ls rm-me", "grep -rn mkdir src", "git log --oneline --grep=patch", "grep -n tee a.txt", "grep -n 'sed -i' src",
    "grep -n dd a.txt", "find . -name rm-me", "find . -name 'cp*' -type f", "git diff HEAD -- touch.py",
    "rg --pre-glob '*.pdf' foo .", "git status --short mv.txt", "head -3 ln.txt", "wc -l truncate.py",
]


@pytest.mark.parametrize("command", READONLY_COMMANDS_MENTIONING_MUTATING_WORDS)
def test_readonly_search_mentioning_a_mutating_word_is_not_denied_under_block(tmp_path: Path, command: str) -> None:
    repo = _repo(tmp_path)
    _set_block(repo)
    args = {"CommandLine": command}
    assert _silent(_run(repo, "run_command", args))
    assert _silent(_run(repo, "run_command", args, HGP_HOOK_BLOCK="1"))


@pytest.mark.parametrize("command", [
    "echo cp a b", "cat a.txt | grep install", "ls src && grep -rn install src", "grep -rn install src | head",
    "grep -rn install src; pwd", "make install",
])
def test_commands_that_are_not_readonly_keep_the_full_pattern_set(tmp_path: Path, command: str) -> None:
    repo = _repo(tmp_path)
    _set_block(repo)
    assert _decision(_run(repo, "run_command", {"CommandLine": command})) == "deny"


@pytest.mark.parametrize("command", [
    "ls", "ls -la", "git status", "git status --short", "grep x f", "grep -rn x src", "find . -name x -print",
    "find . -type f -name '*.py'", "find . -name delete.txt", "find . -name exec", "git diff HEAD --stat",
    "git diff --output-indicator-new=+", "git log --oneline -5", "head -5 a.txt", "pwd", "uname -a",
])
def test_plain_readonly_commands_stay_silent_under_block_after_pattern_changes(tmp_path: Path, command: str) -> None:
    repo = _repo(tmp_path)
    _set_block(repo)
    assert _silent(_run(repo, "run_command", {"CommandLine": command}))
    assert _silent(_run(repo, "run_command", {"CommandLine": command}, HGP_HOOK_BLOCK="1"))


# ── Commit messages: no text is skipped, because a regex is not a shell parser ──
# A heredoc or quoted message that mentions both words is a false deny. Skipping it would let a
# crafted heredoc tag or quote mix hide a real mode change, so the workaround is `git commit -F`.

def _message_heredoc(body: str, opener: str = "<<EOF", tag: str = "EOF") -> str:
    return f'git commit -m "$(cat {opener}\n{body}\n{tag}\n)"'


COMMIT_MESSAGES = [
    _message_heredoc(f"fix(hgp): handle paused {_M}"),
    _message_heredoc(f"fix(hgp): handle paused {_M}", "<<'EOF'"),
    _message_heredoc(f"fix(hgp): handle paused {_M}", '<<"EOF"'),
    _message_heredoc(f"fix(hgp): handle paused {_M}", "<<-EOF"),
    _message_heredoc(f"subject\n\ndocs: explain hgp {_M} off and hgp hook-policy\n\nmore", "<<'EOF'"),
    _message_heredoc(f"docs: explain hgp {_M} off"),  # unquoted delimiter, but nothing is expanded in the body
    _message_heredoc(f"docs: hgp {_M}", "<<'END'", "END"),
    f"git commit -am 'explain hgp {_M} off'", f'git commit -m "docs: hgp {_M}"',
    f'git tag -a v1 --message="hgp {_M} notes"',
    f"git commit -m x -m 'hgp {_M} y'",
]


@pytest.mark.parametrize("command", COMMIT_MESSAGES)
def test_commit_message_mentioning_both_words_is_denied_with_a_file_hint(tmp_path: Path, command: str) -> None:
    repo = _repo(tmp_path)
    out = json.loads(_run(repo, "run_command", {"CommandLine": command}, HGP_HOOK_ASK="0").stdout)
    assert out["decision"] == "deny"
    assert "must be run by the user" in out["reason"]
    assert "git commit -F" in out["reason"]  # the way out


@pytest.mark.parametrize("command", [
    "git commit -F msg.txt", "git commit -a -F /tmp/msg.txt", "git commit --file=msg.txt", "git commit -F - < msg.txt",
    "git tag -a v1 -F msg.txt", f"git commit -m 'fix paused {_M} handling'", f'git commit -m "fix(hgp): paused {_M}"',
])
def test_commit_message_from_a_file_or_without_both_words_is_not_denied(tmp_path: Path, command: str) -> None:
    repo = _repo(tmp_path)
    _set_block(repo)
    assert _silent(_run(repo, "run_command", {"CommandLine": command}))
    assert _silent(_run(repo, "run_command", {"CommandLine": command}, HGP_HOOK_BLOCK="1"))


# Crafted heredocs and quote mixes: whatever hides a command in a message is still seen.
COMMIT_MESSAGE_HIDING_ATTEMPTS = [
    _message_heredoc(f"ok\nEOF\nhgp {_M} off\nEOF"),  # a lazy match on the tag ends at the first EOF
    _message_heredoc(f"ok\nEOF\nhgp {_M} off", "<<'EOF'"),
    f"git commit -m 'x'\"'\"'; hgp {_M} off; echo '\"'\"'y'",  # 'x'"'"'...'"'"'y' concatenation
    f"git commit -m 'a' -m \"b\"; hgp {_M} off", f"git commit -m 'a'; git commit -m 'b'; hgp {_M} off",
    f"git commit -m \"a\" && H=hgp && $H {_M} off", f"git commit -m 'a'\"b\" hgp {_M} off",
]


@pytest.mark.parametrize("command", COMMIT_MESSAGE_HIDING_ATTEMPTS)
def test_nothing_hides_a_mode_change_inside_a_commit_message_shape(tmp_path: Path, command: str) -> None:
    repo = _repo(tmp_path)
    out = json.loads(_run(repo, "run_command", {"CommandLine": command}, HGP_HOOK_ASK="0").stdout)
    assert out["decision"] == "deny"
    assert "must be run by the user" in out["reason"]


# Text that is expanded or fed to a shell is a command, not a message.
COMMIT_MESSAGE_LOOKALIKES = [
    _message_heredoc(f"$(hgp {_M} off)"),  # unquoted delimiter: the body is expanded
    _message_heredoc(f"`hgp {_M} off`"),
    f"git commit -m \"$(printf 'hgp {_M}')\"",  # substitution inside the message: not told apart from a command
    f"git commit -m \"$(printf '%s' hgp) {_M} off\"",
    f"sh <<'EOF'\nhgp {_M} off\nEOF", f"bash <<EOF\nhgp {_M} off\nEOF", f"cat <<EOF | sh\nhgp {_M} off\nEOF",
    f"git status; sh <<EOF\nhgp {_M} off\nEOF", f'git commit -m "$(cat <<EOF | sh\nhgp {_M} off\nEOF\n)"',
    f"echo -m 'hgp {_M} off' | sh", f'git commit -m "x"; hgp {_M} off', f"git commit -m \"x\" && sh -c 'hgp {_M} off'",
    f"git commit -m \"$(cat <<'EOF'\nok\nEOF\n)\" && H=hgp; $H {_M} off",
    # the same shape outside a git command
    f"tool -m \"$(cat <<'EOF'\nhgp {_M} off\nEOF\n)\"", f"git status | tool -m \"$(cat <<EOF\nhgp {_M} off\nEOF\n)\"",
    f"ls; tool --message=\"$(cat <<'EOF'\nhgp {_M} off\nEOF\n)\"",
    f"python -m hgp.server {_M} off", "python3 -m hgp.server hook-policy block",
]


@pytest.mark.parametrize("command", COMMIT_MESSAGE_LOOKALIKES)
def test_expanded_or_executed_text_is_still_a_mode_change(tmp_path: Path, command: str) -> None:
    repo = _repo(tmp_path)
    r = _run(repo, "run_command", {"CommandLine": command}, HGP_HOOK_ASK="0")
    assert (r.returncode, r.stderr) == (0, "")
    out = json.loads(r.stdout)
    assert out["decision"] == "deny"
    assert "must be run by the user" in out["reason"]


# ── Reading the transcript ───────────────────────────────────────────────────

def test_transcript_scan_parses_only_lines_that_could_hold_an_hgp_mcp_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hgp.hooks.agy import pre_tool_use_hgp as agy_hook

    noise_call = {"name": "run_command", "args": {"CommandLine": "ls src/hgp"}}
    noise = {"content": "talk about hgp", "tool_calls": [noise_call]}
    lines = [json.dumps({"step_index": i, **noise}) for i in range(50)]
    lines.append(json.dumps({"step_index": 99, "tool_calls": [HGP_CALL]}))
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(lines) + "\n")
    real_loads = json.loads
    parsed: list[str] = []

    def counting_loads(text: str, *args: object, **kwargs: object) -> object:
        parsed.append(text)
        return real_loads(text, *args, **kwargs)

    monkeypatch.setattr(agy_hook.json, "loads", counting_loads)
    assert agy_hook._hgp_loaded({"transcriptPath": str(path)})  # pyright: ignore[reportPrivateUsage]
    assert [text.strip() for text in parsed] == [lines[-1]]  # the 50 lines that only mention hgp were never decoded


@pytest.mark.parametrize("mode", ["advisory", "off"])
def test_mutating_command_not_denied_while_paused_unless_env_block(tmp_path: Path, mode: str) -> None:
    repo = _repo(tmp_path)
    (repo / ".hgp" / "mode").write_text(mode + "\n")
    _set_block(repo)
    args = {"CommandLine": "cp a b"}
    assert _silent(_run(repo, "run_command", args))
    assert _decision(_run(repo, "run_command", args, HGP_HOOK_BLOCK="1")) == "deny"


@pytest.mark.parametrize("args", [None, {}, {"CommandLine": ""}, {"CommandLine": 7}, {"Command": "cp a b"}])
def test_run_command_without_command_line_is_ignored(tmp_path: Path, args: dict[str, object] | None) -> None:
    repo = _repo(tmp_path)
    assert _silent(_run(repo, "run_command", args, HGP_HOOK_BLOCK="1"))


# ── Tools the hook must leave alone ──────────────────────────────────────────

@pytest.mark.parametrize("name", [
    "call_mcp_tool", "read_file", "view_file", "list_dir", "grep_search", "ask_permission", "manage_task", "",
])
def test_other_tools_are_never_touched(tmp_path: Path, name: str) -> None:
    repo = _repo(tmp_path)
    _set_block(repo)
    args = {"ServerName": "hgp", "ToolName": "hgp_write_file", "Arguments": {"file_path": "a.txt"}}
    # HGP's own write goes through call_mcp_tool: blocking it would lock the agent out of HGP.
    assert _silent(_run(repo, name, args, _transcript(tmp_path, HGP_CALL), HGP_HOOK_BLOCK="1"))


# ── Global mode, partial installs ────────────────────────────────────────────

@pytest.mark.parametrize("flags", [(), ("-S",)], ids=["with-helper", "fallback"])
def test_enforces_in_global_mode(tmp_path: Path, flags: tuple[str, ...]) -> None:
    repo = _repo(tmp_path)
    (repo / ".hgp").rmdir()  # the server still records to ~/.hgp in global mode
    hook = _lone_copy(tmp_path) if flags else HOOK
    r = _run(repo, "write_file", FILE_TOOLS["write_file"][0], hook=hook, flags=flags,
             HGP_GLOBAL_MODE="1", HGP_HOOK_BLOCK="1")
    assert _decision(r) == "deny"
    r = _run(repo, "write_file", FILE_TOOLS["write_file"][0], _transcript(tmp_path, HGP_CALL),
             hook=hook, flags=flags, HGP_GLOBAL_MODE="1")
    assert _decision(r) == "force_ask"


def test_partial_install_reads_policy_and_gates_without_helper(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    hook = _lone_copy(tmp_path)
    args = FILE_TOOLS["write_file"][0]
    assert _silent(_run(repo, "write_file", args, hook=hook, flags=("-S",)))
    _set_block(repo)
    assert _decision(_run(repo, "write_file", args, hook=hook, flags=("-S",))) == "deny"
    _set_block(repo, "block")
    assert _decision(_run(repo, "write_file", args, hook=hook, flags=("-S",))) == "deny"
    (repo / ".hgp").rename(tmp_path / "gone")
    assert _silent(_run(repo, "write_file", args, hook=hook, flags=("-S",)))


# ── Fail-open ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("stdin", [
    "", "not json{", "[]", "null", "42", '"str"', "{}", '{"toolCall": 3}', '{"toolCall": []}',
])
def test_fail_open_on_bad_input(tmp_path: Path, stdin: str) -> None:
    repo = _repo(tmp_path)
    _set_block(repo)
    r = _run_raw(stdin, repo)
    assert _silent(r)


@pytest.mark.parametrize("event", [
    {"toolCall": {"name": 5, "args": {}}, "cwd": "{repo}"},
    {"toolCall": {"name": "run_command", "args": "cp a b"}, "cwd": "{repo}"},
    {"toolCall": {"name": "write_file", "args": {}}, "cwd": 7, "workspacePaths": "x"},
])
def test_fail_open_on_malformed_events(tmp_path: Path, event: dict[str, object]) -> None:
    repo = _repo(tmp_path)
    stdin = json.dumps(event).replace("{repo}", str(repo))
    r = _run_raw(stdin, repo)
    assert r.returncode == 0
    assert "Traceback" not in r.stderr
    assert "allow" not in r.stdout


def test_fail_open_on_unreadable_policy(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / ".hgp" / "hook-policy").mkdir()  # a directory: read_text() raises
    r = _run(repo, "write_file", FILE_TOOLS["write_file"][0])
    assert (r.returncode, r.stdout) == (0, "")
    assert "Traceback" not in r.stderr


# ── The hook must never auto-approve ─────────────────────────────────────────

def test_source_never_writes_an_allow_decision() -> None:
    source = HOOK.read_text()
    assert '"allow"' not in source
    assert "'allow'" not in source


def test_never_emits_allow_across_scenarios(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    transcripts = {"none": None, "hgp": _transcript(tmp_path, HGP_CALL)}
    seen: set[str | None] = set()
    for setup in ("on", "advisory", "off", "block-file"):
        if setup == "block-file":
            (repo / ".hgp" / "mode").unlink(missing_ok=True)
            _set_block(repo)
        else:
            (repo / ".hgp" / "mode").write_text(setup + "\n")
        for tool, args in [*((n, a) for n, (a, _) in FILE_TOOLS.items()), ("run_command", {"CommandLine": "cp a b"}),
                           ("run_command", {"CommandLine": MODE_CMD}), ("run_command", {"CommandLine": "ls"}),
                           ("run_command", {"CommandLine": "ls && cp a b"}),
                           ("run_command", {"CommandLine": CONTROL_FILE_WRITES[0]})]:
            for transcript in transcripts.values():
                for env in (
                    {}, {"HGP_HOOK_BLOCK": "1"}, {"HGP_HOOK_ASK": "0"}, {"HGP_HOOK_ASK": "ask"},
                    {"HGP_HOOK_ASK": "force"}, {"HGP_HOOK_ASK": "weird"}, {"HGP_GLOBAL_MODE": "1"},
                ):
                    r = _run(repo, tool, args, transcript, **env)
                    assert r.returncode == 0
                    assert '"allow"' not in r.stdout
                    seen.add(_decision(r))
    assert seen == {None, "force_ask", "ask", "deny"}
