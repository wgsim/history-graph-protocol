"""PreToolUse hook for Codex: keep shell commands from changing HGP's controls, and track what they change.

The shell analysis is hgp_paths.shell_*, shared with the agy and Claude hooks. A command that runs HGP's
mode or hook-policy command, or may write .hgp/mode or .hgp/hook-policy, is denied at every level (even
paused, advisory, HGP_HOOK_BLOCK=0), and a read-only prefix or a quoting trick does not hide it.

Codex protocol (always exit 0):
  Block:        stdout JSON {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                             "permissionDecision": "deny", "permissionDecisionReason": "..."}}
  Warn:         stdout JSON {"systemMessage": "..."}
  Pass-through: no stdout output

When a mutating command is let through (not denied) in a git checkout, a ``git status`` baseline is
recorded so the PostToolUse hook can report what the command changed, not what was already changed.

Levels: block and strict both deny a mutating Bash command here. Codex's hooks see only the
shell (file edits go through the shell or apply_patch), so the shell is the only lever it has;
the files-only block of the other clients would leave Codex unguarded. HGP_HOOK_BLOCK works
as elsewhere: 0 advisory, strict strict, any other value block, and it wins over the policy file.

Note: Codex currently emits PreToolUse only for Bash commands. The apply_patch
tool does not trigger this hook (Codex bug). Hook will work correctly for Bash
once that is fixed upstream.

Baseline: <tmp>/hgp-codex-<uid>/bash-<tool_use_id>.json (the parent pid when the payload has no
tool_use_id; Codex PreToolUse and PostToolUse payloads both carry it), written 0600 in a per-user 0700
directory. Without hgp_paths.py beside the hook the guards still work through a coarse built-in
check, and there is no tracking.
"""
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


def _paused(start: Path) -> bool:
    """Return whether HGP mode is advisory or off: HGP tools then do not write files.

    HGP_HOOK_BLOCK wins unless it is 0: an agent can write .hgp/mode, not this environment.
    """
    env = os.environ.get("HGP_HOOK_BLOCK")
    if env is not None and env != "0":
        return False
    mode = getattr(_hgp_paths(), "hgp_mode", None)
    return mode is not None and mode(start) in ("advisory", "off")


def _event_cwd(event: object) -> Path:
    """HGP_PROJECT_ROOT when it names a directory (as in the server), else the event cwd."""
    env = os.environ.get("HGP_PROJECT_ROOT")
    if env and Path(env).is_dir():
        return Path(env).resolve()
    cwd = cast(dict[str, Any], event).get("cwd") if isinstance(event, dict) else None
    return Path(cwd) if isinstance(cwd, str) and cwd else Path.cwd()


_LEVELS = ("advisory", "block", "strict")


def _env_level() -> str | None:
    """The level HGP_HOOK_BLOCK forces (trusted: it wins over the policy file), or None when unset.

    0 -> advisory; "strict" -> strict; any other value (1, block, a typo, empty) -> block, so an
    unexpected value fails toward enforcing.
    """
    env = os.environ.get("HGP_HOOK_BLOCK")
    if env is None:
        return None
    if env == "0":
        return "advisory"
    return "strict" if env == "strict" else "block"


def _level_of(text: str) -> str:
    """The level a .hgp/hook-policy text says: advisory, block or strict (unrecognised: advisory)."""
    reader = getattr(_hgp_paths(), "hook_policy_level", None)
    if reader is not None:
        return reader(text) or "advisory"
    stripped = text.strip()  # partial install without hgp_paths.py
    if stripped.startswith("{"):
        try:
            data: object = json.loads(stripped)
        except ValueError:
            return "advisory"
        fields = cast(dict[str, Any], data) if isinstance(data, dict) else {}
        if fields.get("block_mode") is not True:
            return "advisory"
        return "strict" if fields.get("block_shell") is True else "block"
    return "block" if stripped == "block" else "advisory"


def _block_level(start: Path) -> str:
    """advisory, block (native file tools) or strict (the shell too): HGP_HOOK_BLOCK, then the policy file."""
    forced = _env_level()
    if forced is not None:
        return forced
    paths = _hgp_paths()

    def shared_hgp_dir(root: Path) -> Path:
        if paths is not None:
            return Path(paths.shared_hgp_dir(root))
        return root / ".hgp"  # partial install without hgp_paths.py

    for parent in [start, *start.parents]:
        # Shared policy plus a checkout-local legacy one (written before storage was shared):
        # the stricter of the two counts, so upgrading never drops enforcement.
        candidates = dict.fromkeys([shared_hgp_dir(parent) / "hook-policy", parent / ".hgp" / "hook-policy"])
        found = [p for p in candidates if p.exists()]
        if found:
            return max((_level_of(p.read_text()) for p in found), key=_LEVELS.index)
        if (parent / ".git").exists():
            break
    return "advisory"


def _resolve_block_mode(start: Path) -> bool:
    """Whether native file tools are blocked: the level is block or strict."""
    return _block_level(start) != "advisory"


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
    record("codex", paths.track_key(event), Path(root) if root is not None else start, pattern)


def _deny(reason: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        },
    }))


def _main() -> None:
    try:
        raw: object = json.loads(sys.stdin.read())
    except ValueError:
        sys.exit(0)
    if not isinstance(raw, dict):
        sys.exit(0)
    event = cast(dict[str, Any], raw)
    start = _event_cwd(event)  # one directory for the gate and the policy
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
        _deny(_MODE_DENY if violation == "mode" else _CONTROL_DENY)
        sys.exit(0)

    # Advising or blocking native tools is pointless while HGP tools do not write.
    if _paused(start):
        sys.exit(0)
    matched = _mutating_pattern(command)
    if matched is None:
        sys.exit(0)

    msg = (
        f"[HGP] Bash command may mutate files (matched: {matched!r}). "
        "If this writes or deletes tracked files, prefer hgp_* tools so the "
        "operation is recorded in HGP history."
    )

    if _resolve_block_mode(start):
        _deny(msg)
    else:
        # Let it through, and note the git status so PostToolUse can tell what it changed.
        _record_baseline(event, start, matched)
        print(json.dumps({"systemMessage": msg}))
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
