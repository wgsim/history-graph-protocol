"""Antigravity CLI (agy) PreToolUse hook: deny/ask native file writes in sessions that use HGP.

The payloads mirror what agy 1.5.x sends a PreToolUse hook on stdin (camelCase JSON:
``toolCall.{id,name,args}``, ``cwd``, ``workspacePaths``, ``transcriptPath``). The hook
answers with ``{"decision": "ask"|"deny", "reason": ...}`` or with empty stdout, which
agy treats as "no opinion". It must never answer "allow": that auto-approves the call.
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
def test_asks_once_session_used_hgp(
    tmp_path: Path, call: dict[str, object], name: str, args: dict[str, object], equiv: str,
) -> None:
    repo = _repo(tmp_path)
    r = _run(repo, name, args, _transcript(tmp_path, OTHER_SERVER, call))
    assert (r.returncode, r.stderr) == (0, "")
    out = json.loads(r.stdout)
    assert set(out) == {"decision", "reason"}
    assert out["decision"] == "ask"
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
    assert _decision(_run(repo, "write_file", FILE_TOOLS["write_file"][0], path)) == "ask"


def test_hgp_hook_ask_0_disables_ask(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    r = _run(repo, "write_file", FILE_TOOLS["write_file"][0], _transcript(tmp_path, HGP_CALL), HGP_HOOK_ASK="0")
    assert _silent(r)


def test_workspace_path_used_when_cwd_absent(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    event = _event(repo, "write_file", FILE_TOOLS["write_file"][0], _transcript(tmp_path, HGP_CALL))
    del event["cwd"]
    assert _decision(_run_raw(json.dumps(event), tmp_path)) == "ask"
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
    assert _decision(_run_raw(event, tmp_path, HGP_PROJECT_ROOT=str(repo))) == "ask"


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
    assert _decision(r) == "ask"


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
                           ("run_command", {"CommandLine": MODE_CMD}), ("run_command", {"CommandLine": "ls"})]:
            for transcript in transcripts.values():
                for env in ({}, {"HGP_HOOK_BLOCK": "1"}, {"HGP_HOOK_ASK": "0"}, {"HGP_GLOBAL_MODE": "1"}):
                    r = _run(repo, tool, args, transcript, **env)
                    assert r.returncode == 0
                    seen.add(_decision(r))
    assert seen == {None, "ask", "deny"}
