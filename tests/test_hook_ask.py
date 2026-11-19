"""Claude PreToolUse hook: ask before a native write in sessions that use HGP."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).parents[1] / "src" / "hgp" / "hooks" / "claude" / "pre_tool_use_hgp.py"


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / ".hgp").mkdir()
    return repo


def _transcript(tmp_path: Path, *blocks: dict[str, object]) -> Path:
    path = tmp_path / "transcript.jsonl"
    lines = [json.dumps({"type": "user", "message": {"content": "hi"}})]
    lines += [json.dumps({"type": "assistant", "message": {"content": [block]}}) for block in blocks]
    path.write_text("\n".join(lines) + "\n")
    return path


HGP_CALL = {"type": "tool_use", "name": "mcp__hgp__hgp_create_operation", "input": {}}
HGP_SEARCH = {"type": "tool_use", "name": "ToolSearch", "input": {"query": "select:mcp__hgp__hgp_write_file"}}
OTHER_CALL = {"type": "tool_use", "name": "Read", "input": {"file_path": "mcp__hgp__ in a path"}}


def _run(repo: Path, transcript: Path | None, **env: str) -> subprocess.CompletedProcess[str]:
    event: dict[str, object] = {"tool_name": "Write", "cwd": str(repo)}
    if transcript is not None:
        event["transcript_path"] = str(transcript)
    base = {k: v for k, v in os.environ.items() if k not in ("HGP_HOOK_BLOCK", "HGP_HOOK_ASK")}
    return subprocess.run(
        [sys.executable, str(HOOK)], input=json.dumps(event), cwd=repo,
        capture_output=True, text=True, env={**base, **env},
    )


def _asked(r: subprocess.CompletedProcess[str]) -> bool:
    if not r.stdout.strip():
        return False
    return json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"] == "ask"


@pytest.mark.parametrize("block", [HGP_CALL, HGP_SEARCH], ids=["hgp-call", "toolsearch"])
def test_asks_by_default_once_session_used_hgp(tmp_path: Path, block: dict[str, object]) -> None:
    repo = _repo(tmp_path)
    r = _run(repo, _transcript(tmp_path, block))
    assert r.returncode == 0
    assert _asked(r)
    assert "hgp_write_file" in json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"]


def test_no_ask_when_session_never_used_hgp(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    r = _run(repo, _transcript(tmp_path, OTHER_CALL))
    assert (r.returncode, _asked(r)) == (0, False)
    assert "Native `Write` detected" in r.stderr


@pytest.mark.parametrize("transcript", ["missing", "absent-key"])
def test_no_ask_without_readable_transcript(tmp_path: Path, transcript: str) -> None:
    repo = _repo(tmp_path)
    path = tmp_path / "nope.jsonl" if transcript == "missing" else None
    r = _run(repo, path)
    assert (r.returncode, _asked(r)) == (0, False)


def test_hgp_hook_ask_0_disables(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    r = _run(repo, _transcript(tmp_path, HGP_CALL), HGP_HOOK_ASK="0")
    assert (r.returncode, _asked(r)) == (0, False)


def test_block_policy_wins_over_ask(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    r = _run(repo, _transcript(tmp_path, HGP_CALL), HGP_HOOK_BLOCK="1")
    assert r.returncode == 2
    assert not _asked(r)


@pytest.mark.parametrize("setup", ["not-initialized", "paused"])
def test_no_ask_when_hgp_is_not_recording(tmp_path: Path, setup: str) -> None:
    repo = _repo(tmp_path)
    if setup == "not-initialized":
        (repo / ".hgp").rmdir()
    else:
        (repo / ".hgp" / "mode").write_text("off\n")
    r = _run(repo, _transcript(tmp_path, HGP_CALL))
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")


# ── Target-path scoping, the .hgp deny and the levels ────────────────────────
# The hook judges the file being written (tool_input.file_path), not the session's cwd: only a write
# HGP could have recorded (inside the session's checkout) is asked about or blocked, and a write
# into .hgp/ is always denied.

WRITE_TOOLS = ["Write", "Edit", "MultiEdit"]


def _commit(repo: Path) -> None:
    for args in (["config", "user.email", "t@example.com"], ["config", "user.name", "T"]):
        subprocess.run(["git", *args], cwd=repo, check=True)
    (repo / "seed.txt").write_text("x")
    subprocess.run(["git", "add", "seed.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=repo, check=True)


def _run_write(
    repo: Path, file_path: object, *, transcript: Path | None = None, tool: str = "Write",
    event_cwd: Path | None = None, tool_input: dict[str, object] | None = None, **env: str,
) -> subprocess.CompletedProcess[str]:
    event: dict[str, object] = {"tool_name": tool, "cwd": str(event_cwd or repo)}
    shown = str(file_path) if isinstance(file_path, Path) else file_path
    event["tool_input"] = tool_input if tool_input is not None else {"file_path": shown}
    if transcript is not None:
        event["transcript_path"] = str(transcript)
    base = {k: v for k, v in os.environ.items() if k not in ("HGP_HOOK_BLOCK", "HGP_HOOK_ASK", "HGP_GLOBAL_MODE")}
    # The hook process runs outside the repo: only the event's cwd says where the session is.
    return subprocess.run(
        [sys.executable, str(HOOK)], input=json.dumps(event), cwd=repo.parent,
        capture_output=True, text=True, env={**base, **env},
    )


def _set_policy(repo: Path, text: str) -> None:
    (repo / ".hgp" / "hook-policy").write_text(text)


BLOCK = '{"block_mode": true, "block_shell": false}'
STRICT = '{"block_mode": true, "block_shell": true}'


@pytest.fixture
def outside_targets(tmp_path: Path) -> dict[str, Path]:
    """Places HGP could not have recorded a write to: a plain dir, another repo, another worktree."""
    main = _repo(tmp_path)
    _commit(main)
    sibling_repo = tmp_path / "sibling-repo"
    sibling_repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=sibling_repo, check=True)
    (sibling_repo / ".hgp").mkdir()  # enabled too: it is still another checkout
    worktree = tmp_path / "sibling-worktree"
    subprocess.run(["git", "worktree", "add", "-q", "-b", "wt", str(worktree)], cwd=main, check=True)
    plain = tmp_path / "plain"
    plain.mkdir()
    return {
        "plain-dir": plain / "x.txt", "sibling-repo": sibling_repo / "x.txt", "sibling-worktree": worktree / "x.txt",
        "parent-of-repo": tmp_path / "x.txt",
    }


@pytest.mark.parametrize("tool", WRITE_TOOLS)
@pytest.mark.parametrize("place", ["plain-dir", "sibling-repo", "sibling-worktree", "parent-of-repo"])
@pytest.mark.parametrize("setup", ["block-policy", "strict-policy", "env-block", "env-strict", "hgp-session"])
def test_writes_outside_the_checkout_are_not_judged(
    tmp_path: Path, outside_targets: dict[str, Path], tool: str, place: str, setup: str,
) -> None:
    repo = tmp_path / "r"
    env: dict[str, str] = {}
    transcript = _transcript(tmp_path, HGP_CALL)  # the session used HGP in every case
    if setup == "block-policy":
        _set_policy(repo, BLOCK)
    elif setup == "strict-policy":
        _set_policy(repo, STRICT)
    elif setup == "env-block":
        env["HGP_HOOK_BLOCK"] = "1"
    elif setup == "env-strict":
        env["HGP_HOOK_BLOCK"] = "strict"
    r = _run_write(repo, outside_targets[place], transcript=transcript, tool=tool, **env)
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")


def test_a_write_in_the_main_checkout_is_outside_a_worktree_session(
    tmp_path: Path, outside_targets: dict[str, Path],
) -> None:
    repo = tmp_path / "r"
    worktree = outside_targets["sibling-worktree"].parent
    _set_policy(repo, BLOCK)  # the store is shared, so the worktree session sees block
    r = _run_write(worktree, repo / "x.txt", transcript=_transcript(tmp_path, HGP_CALL))
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")
    assert _run_write(worktree, worktree / "x.txt").returncode == 2  # its own checkout is judged


@pytest.mark.parametrize("tool", WRITE_TOOLS)
def test_a_write_inside_the_checkout_is_judged_as_before(tmp_path: Path, tool: str) -> None:
    repo = _repo(tmp_path)
    target = repo / "src" / "x.py"
    transcript = _transcript(tmp_path, HGP_CALL)
    r = _run_write(repo, target, tool=tool)  # warning only
    assert (r.returncode, r.stdout) == (0, "")
    assert f"Native `{tool}` detected" in r.stderr
    assert _asked(_run_write(repo, target, tool=tool, transcript=transcript))
    assert not _asked(_run_write(repo, target, tool=tool, transcript=transcript, HGP_HOOK_ASK="0"))
    _set_policy(repo, BLOCK)
    assert _run_write(repo, target, tool=tool, transcript=transcript).returncode == 2


@pytest.mark.parametrize("how", ["policy-block", "policy-strict", "env-1", "env-block", "env-strict", "env-yes"])
def test_block_and_strict_both_stop_native_writes(tmp_path: Path, how: str) -> None:
    repo = _repo(tmp_path)
    env: dict[str, str] = {}
    if how.startswith("policy"):
        _set_policy(repo, STRICT if how == "policy-strict" else BLOCK)
    else:
        env["HGP_HOOK_BLOCK"] = how.removeprefix("env-")
    assert _run_write(repo, repo / "x.txt", **env).returncode == 2


def test_env_zero_overrides_a_block_policy_file(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _set_policy(repo, STRICT)
    r = _run_write(repo, repo / "x.txt", HGP_HOOK_BLOCK="0")
    assert r.returncode == 0
    assert "Native `Write` detected" in r.stderr


# A write into .hgp/ is denied whatever the policy: the directory is HGP-internal and user-controlled.
HGP_TARGETS = [
    ".hgp/mode", ".hgp/hook-policy", "sub/.hgp/x", ".hgp/hgp.db", "./.hgp/mode", "sub/../.hgp/mode", ".hgp/a/b/c",
    ".HGP/mode",
]


@pytest.mark.parametrize("tool", WRITE_TOOLS)
@pytest.mark.parametrize("relative", HGP_TARGETS)
@pytest.mark.parametrize("setup", ["default", "paused-off", "paused-advisory", "env-zero", "ask-zero", "not-enabled"])
def test_writes_into_dot_hgp_are_always_denied(tmp_path: Path, tool: str, relative: str, setup: str) -> None:
    repo = _repo(tmp_path)
    env: dict[str, str] = {}
    if setup in ("paused-off", "paused-advisory"):
        (repo / ".hgp" / "mode").write_text(setup.removeprefix("paused-") + "\n")
    elif setup == "env-zero":
        env["HGP_HOOK_BLOCK"] = "0"
    elif setup == "ask-zero":
        env["HGP_HOOK_ASK"] = "0"
    elif setup == "not-enabled":
        (repo / ".hgp").rmdir()
    r = _run_write(repo, repo / relative, tool=tool, **env)
    assert r.returncode == 2
    assert ".hgp" in r.stderr
    assert "HGP-internal" in r.stderr
    assert r.stdout == ""


@pytest.mark.parametrize("target", ["{repo}/.hgp/mode", "{tmp}/elsewhere/.hgp/db", "{tmp}/sibling/.hgp/x"])
def test_dot_hgp_is_denied_even_outside_the_checkout_and_in_global_mode(tmp_path: Path, target: str) -> None:
    repo = _repo(tmp_path)
    path = target.format(repo=repo, tmp=tmp_path)
    assert _run_write(repo, path).returncode == 2
    assert _run_write(repo, path, HGP_GLOBAL_MODE="1").returncode == 2


@pytest.mark.parametrize("relative", [".hgpx/mode", "hgp/mode", "x.hgp", "a.hgp/b", ".hgp.bak/x", "dot.hgp"])
def test_only_an_exact_dot_hgp_segment_is_internal(tmp_path: Path, relative: str) -> None:
    repo = _repo(tmp_path)
    r = _run_write(repo, repo / relative)
    assert (r.returncode, r.stdout) == (0, "")  # judged like any other file: a warning, no deny


def test_dot_hgp_through_a_symlink_is_denied(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "link").symlink_to(repo / ".hgp")
    assert _run_write(repo, repo / "link" / "mode").returncode == 2


def test_a_relative_target_is_judged_unless_it_is_outside_from_both_bases(tmp_path: Path) -> None:
    """The tool resolves a relative path against the agent's cwd, the server against the checkout root.
    The hook keeps the target in scope unless BOTH resolutions leave the checkout."""
    repo = _repo(tmp_path)
    sub = repo / "sub"
    sub.mkdir()
    _set_policy(repo, BLOCK)
    transcript = _transcript(tmp_path, HGP_CALL)
    for target in ("x.txt", "../x.txt", "sub/x.txt", "../sub/x.txt", "../sibling/x.txt", "./y/../z.txt"):
        # cwd=<root>/sub: `../x.txt` really writes <root>/x.txt, so it must not slip through as <root>/../x.txt
        assert _run_write(repo, target, event_cwd=sub).returncode == 2, target
        assert _run_write(repo, target, transcript=transcript, event_cwd=sub).returncode == 2, target
    for target in ("../../x.txt", "../../elsewhere/x", "../../../elsewhere/x"):
        r = _run_write(repo, target, transcript=transcript, event_cwd=sub)  # outside both ways
        assert (r.returncode, r.stdout, r.stderr) == (0, "", ""), target
    for target in ("../x.txt", "../elsewhere/x", "../sibling/x.txt", "../../elsewhere/x"):
        r = _run_write(repo, target, transcript=transcript, event_cwd=repo)  # cwd=<root>
        assert (r.returncode, r.stdout, r.stderr) == (0, "", ""), target
    for target in ("x.txt", "sub/x.txt", "a/../b.txt"):
        assert _run_write(repo, target, event_cwd=repo).returncode == 2, target


@pytest.mark.parametrize("tool", WRITE_TOOLS)
def test_every_write_tool_judges_a_relative_target_from_the_cwd_too(tmp_path: Path, tool: str) -> None:
    repo = _repo(tmp_path)
    sub = repo / "sub"
    sub.mkdir()
    _set_policy(repo, BLOCK)
    assert _run_write(repo, "../x", tool=tool, event_cwd=sub).returncode == 2
    assert _run_write(repo, "x", tool=tool, event_cwd=sub).returncode == 2
    r = _run_write(repo, "../../elsewhere/x", tool=tool, event_cwd=repo)
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")


def test_a_relative_target_without_a_cwd_in_the_event_uses_the_session_root(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _set_policy(repo, BLOCK)
    event = {"tool_name": "Write", "tool_input": {"file_path": "x.txt"}}  # no cwd: the process cwd is the session
    r = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(event), cwd=repo, capture_output=True, text=True)
    assert r.returncode == 2
    event["tool_input"] = {"file_path": "../x.txt"}
    r = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(event), cwd=repo, capture_output=True, text=True)
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")


def test_a_dot_hgp_target_is_found_from_either_base(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    sub = repo / "sub"
    sub.mkdir()
    for target in ("../.hgp/mode", ".hgp/mode", "../../r/.hgp/hook-policy", "../sub/../.hgp/x"):
        assert _run_write(repo, target, event_cwd=sub).returncode == 2, target


def test_a_tilde_target_is_the_home_directory(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    home = tmp_path / "home"  # outside the checkout
    home.mkdir()
    _set_policy(repo, BLOCK)
    transcript = _transcript(tmp_path, HGP_CALL)
    for target in ("~/x", "~/notes/x.md", "~"):
        r = _run_write(repo, target, transcript=transcript, HOME=str(home))
        assert (r.returncode, r.stdout, r.stderr) == (0, "", ""), target
        r = _run_write(repo, target, transcript=transcript, event_cwd=repo / "sub", HOME=str(home))
        assert (r.returncode, r.stdout, r.stderr) == (0, "", ""), target
    inside = repo / "home"  # a home inside the checkout is in scope
    inside.mkdir()
    assert _run_write(repo, "~/x", HOME=str(inside)).returncode == 2
    assert _run_write(repo, "~/.hgp/mode", HOME=str(home)).returncode == 2  # a .hgp segment anywhere
    assert _run_write(repo, "~/x", HOME=str(home), HGP_GLOBAL_MODE="1", HGP_HOOK_BLOCK="1").returncode == 2


@pytest.mark.parametrize("tool_input", [
    {}, {"file_path": None}, {"file_path": 7}, {"file_path": ""}, {"file_path": ["a"]}, {"path": "elsewhere"},
    {"file_path": {"x": 1}},
])
def test_a_missing_or_malformed_target_keeps_the_cwd_behaviour(tmp_path: Path, tool_input: dict[str, object]) -> None:
    repo = _repo(tmp_path)
    transcript = _transcript(tmp_path, HGP_CALL)
    assert _asked(_run_write(repo, None, transcript=transcript, tool_input=tool_input))
    _set_policy(repo, BLOCK)
    assert _run_write(repo, None, tool_input=tool_input).returncode == 2


def test_a_missing_tool_input_is_not_an_error(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    event = {"tool_name": "Write", "cwd": str(repo)}
    r = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(event), cwd=repo, capture_output=True, text=True)
    assert r.returncode == 0
    assert "Traceback" not in r.stderr
    assert "Native `Write` detected" in r.stderr


def test_global_mode_has_no_checkout_restriction(tmp_path: Path, outside_targets: dict[str, Path]) -> None:
    repo = tmp_path / "r"
    transcript = _transcript(tmp_path, HGP_CALL)
    outside = outside_targets["plain-dir"]
    assert _asked(_run_write(repo, outside, transcript=transcript, HGP_GLOBAL_MODE="1"))
    r = _run_write(repo, outside, HGP_GLOBAL_MODE="1", HGP_HOOK_BLOCK="1")
    assert r.returncode == 2
    (repo / ".hgp").rmdir()  # global mode needs no .hgp/ in the repo
    assert _run_write(repo, outside, HGP_GLOBAL_MODE="1", HGP_HOOK_BLOCK="strict").returncode == 2


def test_hgp_project_root_is_the_session_root(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=other, check=True)
    (other / "x.txt").write_text("")
    # cwd is `other`, but the server's project root is `repo`: a write in `repo` is judged, one in `other` is not
    assert _run_write(other, repo / "x.txt", HGP_PROJECT_ROOT=str(repo), HGP_HOOK_BLOCK="1").returncode == 2
    r = _run_write(other, other / "x.txt", HGP_PROJECT_ROOT=str(repo), HGP_HOOK_BLOCK="1")
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")


@pytest.mark.parametrize("setup", ["paused-off", "paused-advisory"])
def test_a_paused_session_stays_silent_for_files_in_the_checkout_unless_the_env_enforces(
    tmp_path: Path, setup: str,
) -> None:
    repo = _repo(tmp_path)
    (repo / ".hgp" / "mode").write_text(setup.removeprefix("paused-") + "\n")
    _set_policy(repo, STRICT)
    r = _run_write(repo, repo / "x.txt")
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")
    assert _run_write(repo, repo / "x.txt", HGP_HOOK_BLOCK="0").returncode == 0
    assert _run_write(repo, repo / "x.txt", HGP_HOOK_BLOCK="strict").returncode == 2
    assert _run_write(repo, repo / "x.txt", HGP_HOOK_BLOCK="1").returncode == 2


def test_the_hook_never_emits_allow(tmp_path: Path, outside_targets: dict[str, Path]) -> None:
    repo = tmp_path / "r"
    transcript = _transcript(tmp_path, HGP_CALL)
    targets = [repo / "x.txt", repo / ".hgp/mode", outside_targets["plain-dir"], "rel.txt", None]
    for target in targets:
        for env in ({}, {"HGP_HOOK_BLOCK": "1"}, {"HGP_HOOK_BLOCK": "strict"}, {"HGP_HOOK_BLOCK": "0"},
                    {"HGP_HOOK_ASK": "0"}, {"HGP_GLOBAL_MODE": "1"}):
            r = _run_write(repo, target, transcript=transcript, **env)
            assert '"allow"' not in r.stdout


@pytest.mark.parametrize("tool", WRITE_TOOLS)
def test_the_whole_checkout_is_in_scope_from_a_subdirectory_session(tmp_path: Path, tool: str) -> None:
    """The root is the checkout, not the cwd: a session in repo/sub still judges repo/other/x."""
    repo = _repo(tmp_path)
    (repo / "sub").mkdir()
    (repo / "other").mkdir()
    _set_policy(repo, BLOCK)
    sub = repo / "sub"
    assert _run_write(repo, repo / "other" / "x.txt", tool=tool, event_cwd=sub).returncode == 2
    assert _run_write(repo, sub / "x.txt", tool=tool, event_cwd=sub).returncode == 2
    r = _run_write(repo, tmp_path / "x.txt", tool=tool, event_cwd=sub)
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")


def test_a_relative_target_from_a_cwd_outside_the_checkout_is_still_judged(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _set_policy(repo, BLOCK)
    env = {"HGP_PROJECT_ROOT": str(repo)}
    assert _run_write(repo, "x.txt", event_cwd=elsewhere, **env).returncode == 2  # <root>/x.txt by the root reading
    r = _run_write(repo, "../../outside/x", event_cwd=elsewhere, **env)
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")


def test_a_relative_write_from_a_cwd_inside_dot_hgp_is_denied(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    for target in ("mode", "hook-policy", "sub/x", "../.hgp/mode"):
        assert _run_write(repo, target, event_cwd=repo / ".hgp").returncode == 2, target
    r = _run_write(repo, "../../x", event_cwd=repo / ".hgp")  # outside both ways
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")


def test_the_agent_cwd_counts_even_when_hgp_project_root_is_set(tmp_path: Path) -> None:
    """HGP_PROJECT_ROOT picks the repository; the tool still resolves `..` from the event's cwd."""
    repo = _repo(tmp_path)
    sub = repo / "sub"
    sub.mkdir()
    _set_policy(repo, BLOCK)
    env = {"HGP_PROJECT_ROOT": str(repo)}
    assert _run_write(repo, "../x", event_cwd=sub, **env).returncode == 2  # really <root>/x
    r = _run_write(repo, "../../x", event_cwd=sub, **env)                   # outside both ways
    assert (r.returncode, r.stdout, r.stderr) == (0, "", "")
