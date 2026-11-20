"""PreToolUse hook for Bash: keep shell commands from changing HGP's controls, and track what they change.

Decision order, first match wins (the analysis is hgp_paths.shell_*, shared with the agy and Codex hooks):
  1. HGP is not enabled for the repository: silent.
  2. The command runs HGP's mode or hook-policy command, or may write .hgp/mode or .hgp/hook-policy:
     deny (exit 2), always (even paused, in advisory, with HGP_HOOK_BLOCK=0). Those controls are
     user-only, and a read-only prefix or a quoting trick does not hide them.
  3. HGP mode is advisory or off (and HGP_HOOK_BLOCK does not enforce): silent.
  4. The command does not look file-mutating: silent.
  5. The level is strict (policy file `block_shell`, or HGP_HOOK_BLOCK=strict): deny (exit 2).
     block only stops native file tools and leaves shell work alone.
  6. Otherwise warn on stderr, and record a ``git status`` baseline so the PostToolUse hook can
     tell the agent what the command changed (post_bash_hgp.py reports only the new entries).

Baseline: <tmp>/hgp-claude-<uid>/bash-<tool_use_id>.json (the parent pid when the payload has no
tool_use_id), written 0600 in a per-user 0700 directory. Without hgp_paths.py beside the hook (a
broken install) the guards still work through a coarse built-in check, and neither the policy file
nor tracking is available.

Fails open: an unexpected error never blocks the agent's tool call.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any, cast

# --- hgp-shell-fallback begin: keep this block identical in the agy, Claude and Codex hooks (a test checks) ---
# The shell analysis lives in hgp_paths (shell_*), installed beside every hook. This block is what a hook
# still has when hgp_paths.py is missing (a broken install): the always-deny guards and a coarse mutating
# check. It may deny more than hgp_paths does, never less than the guards. The messages are shared.
_MODE_DENY = (
    "[HGP] Blocked: `hgp mode` and `hgp hook-policy` must be run by the user directly, not by the agent. "
    "Use `!hgp mode <value>` or `!hgp hook-policy <value>` in your terminal. "
    "If this is only text, such as a commit message, write it to a file and run `git commit -F <file>`."
)
_CONTROL_DENY = (
    "[HGP] Blocked: `.hgp/mode` and `.hgp/hook-policy` are user-controlled settings; the agent "
    "must not write them from the shell. Ask the user to run `!hgp mode <value>` or "
    "`!hgp hook-policy <value>` instead."
)
_FB_QUOTES = str.maketrans("", "", "'\"\\")
_FB_MODE = re.compile(r"\bhgp(?:\.server)?\s+(?:mode|hook-policy)\b")
_FB_HGP_WORD = re.compile(r"(?:(?<![\w./-])|(?<=/bin/))hgp(?:\.server)?(?![\w./-])")
_FB_CONTROL_WORD = re.compile(r"(?<![\w./-])(?:mode|hook-policy)(?![\w./-])")
_FB_CONTROL_PATH = re.compile(r"\.hgp/(?:mode|hook-policy)(?![\w.-])")
_FB_WRITER = re.compile(
    r"\b(?:cp|mv|rm|tee|touch|install|mkdir|rmdir|chmod|chown|ln|truncate|patch|dd|sed|perl|git|find|rg|awk)\b|>"
)
_FB_MUTATING = re.compile(
    r"\b(?:cp|mv|rm|tee|touch|install|mkdir|rmdir|chmod|chown|ln|truncate|patch|dd|sed|perl)\b"
    r"|\bgit\s+(?:checkout|restore|switch|apply|revert|merge|rebase|reset)\b|>"
)


def _fb_control_violation(command: str) -> str | None:
    text = command.translate(_FB_QUOTES)
    if _FB_MODE.search(text):
        return "mode"
    if ("$" in text or "`" in text) and _FB_HGP_WORD.search(text) and _FB_CONTROL_WORD.search(text):
        return "mode"
    # A control path next to any writer word or redirect: fail closed (operands are not resolved).
    return "control-file" if _FB_CONTROL_PATH.search(text) and _FB_WRITER.search(text) else None


def _fb_mutating(command: str) -> str | None:
    found = _FB_MUTATING.search(command.translate(_FB_QUOTES))
    return found.group(0) if found else None


# --- hgp-shell-fallback end ---


def _hgp_paths() -> Any:
    """Return hgp_paths: installed beside this hook, else from the hgp package, else None."""
    try:
        import hgp_paths  # pyright: ignore[reportMissingImports]
    except ImportError:
        try:
            from hgp.hooks import hgp_paths
        except ImportError:
            return None
    return hgp_paths


def _enabled(start: Path) -> bool:
    """Return whether HGP is enabled for ``start`` (its storage .hgp/ exists)."""
    check = getattr(_hgp_paths(), "hgp_enabled", None)
    if check is not None:
        return bool(check(start))
    if os.environ.get("HGP_GLOBAL_MODE"):
        return True  # global mode records everywhere, as in the server
    for parent in [start, *start.parents]:  # no hgp_paths: walk up to the checkout root
        if (parent / ".git").exists():
            return (parent / ".hgp").is_dir()
    return (start / ".hgp").is_dir()  # outside git only this directory's store counts


def _event_cwd(event: object) -> Path:
    """HGP_PROJECT_ROOT when it names a directory (as in the server), else the event cwd."""
    env = os.environ.get("HGP_PROJECT_ROOT")
    if env and Path(env).is_dir():
        return Path(env).resolve()
    cwd = cast(dict[str, Any], event).get("cwd") if isinstance(event, dict) else None
    return Path(cwd) if isinstance(cwd, str) and cwd else Path.cwd()


def _paused(start: Path) -> bool:
    """Whether HGP mode is advisory or off: HGP tools then do not write files.

    HGP_HOOK_BLOCK wins unless it is 0: an agent can write .hgp/mode, not this environment.
    Without hgp_paths the mode file is not read, so nothing is paused.
    """
    check = getattr(_hgp_paths(), "hook_paused", None)
    return bool(check(start)) if check is not None else False


def _block_level(start: Path) -> str:
    """advisory, block or strict: HGP_HOOK_BLOCK, then the policy file. Without hgp_paths only the
    environment is read (the trusted channel)."""
    check = getattr(_hgp_paths(), "block_level", None)
    if check is not None:
        return str(check(start))
    env = os.environ.get("HGP_HOOK_BLOCK")
    if env is None or env == "0":
        return "advisory"
    return "strict" if env == "strict" else "block"


def _control_violation(command: str) -> str | None:
    """"mode" for a command that changes HGP's mode or hook policy, "control-file" for a shell write to
    .hgp/mode or .hgp/hook-policy, else None. Both are user-only (hgp_paths.shell_control_violation)."""
    check = getattr(_hgp_paths(), "shell_control_violation", None)
    return check(command) if check is not None else _fb_control_violation(command)


def _mutating_pattern(command: str) -> str | None:
    """The pattern that makes the command look file-mutating, or None (hgp_paths.shell_mutating_pattern)."""
    check = getattr(_hgp_paths(), "shell_mutating_pattern", None)
    return check(command) if check is not None else _fb_mutating(command)


def _record_baseline(event: dict[str, Any], start: Path, pattern: str) -> None:
    """Note ``git status`` before the command so PostToolUse can tell what it changed (needs hgp_paths)."""
    paths = _hgp_paths()
    record = getattr(paths, "track_record", None)
    if record is None:
        return
    root = paths.checkout_root(start)
    record("claude", paths.track_key(event), Path(root) if root is not None else start, pattern)


def _main() -> None:
    try:
        raw: object = json.loads(sys.stdin.read())
    except ValueError:
        sys.exit(0)
    if not isinstance(raw, dict):
        sys.exit(0)
    event = cast(dict[str, Any], raw)
    start = _event_cwd(event)
    if not _enabled(start):
        sys.exit(0)
    if event.get("tool_name") != "Bash":
        sys.exit(0)

    tool_input = event.get("tool_input")
    command = cast(dict[str, Any], tool_input).get("command", "") if isinstance(tool_input, dict) else ""
    if not isinstance(command, str) or not command:
        sys.exit(0)

    # Mode and policy control is user-only, so this runs before the paused and read-only exits:
    # a leading read-only command must not smuggle a change through, and neither may pausing HGP.
    violation = _control_violation(command)
    if violation is not None:
        print(_MODE_DENY if violation == "mode" else _CONTROL_DENY, file=sys.stderr)
        sys.exit(2)
    # Advising or blocking is pointless while HGP tools do not write.
    if _paused(start):
        sys.exit(0)
    matched = _mutating_pattern(command)
    if matched is None:
        sys.exit(0)
    # Only strict blocks the shell: block stops native file tools and leaves shell work alone.
    if _block_level(start) == "strict":
        print(
            f"[HGP] Blocked: Bash command may mutate files (matched: {matched!r}) and the hook level is strict. "
            "Use hgp_* tools so the operation is recorded in HGP history.",
            file=sys.stderr,
        )
        sys.exit(2)

    _record_baseline(event, start, matched)
    print(
        f"[HGP] Bash command may mutate files (matched: {matched!r}). "
        "If this writes or deletes tracked files, prefer hgp_* tools so the "
        "operation is recorded in HGP history.",
        file=sys.stderr,
    )
    sys.exit(0)


def main() -> None:
    """Fail-open entry point: an unexpected error never blocks the agent's tool call."""
    try:
        _main()
    except SystemExit:
        raise
    except Exception:
        sys.exit(0)


if __name__ == "__main__":
    main()
