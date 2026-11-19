"""PreToolUse hook for Codex: warn when shell commands may mutate files outside HGP.

Codex protocol (always exit 0):
  Block:        stdout JSON {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                             "permissionDecision": "deny", "permissionDecisionReason": "..."}}
  Warn:         stdout JSON {"systemMessage": "..."}
  Pass-through: no stdout output

When a mutating pattern is detected, a marker file is written to /tmp so the
PostToolUse hook can run 'git status' to report actual changes.

Levels: block and strict both deny a mutating Bash command here. Codex's hooks see only the
shell (file edits go through the shell or apply_patch), so the shell is the only lever it has;
the files-only block of the other clients would leave Codex unguarded. HGP_HOOK_BLOCK works
as elsewhere: 0 advisory, strict strict, any other value block, and it wins over the policy file.

Note: Codex currently emits PreToolUse only for Bash commands. The apply_patch
tool does not trigger this hook (Codex bug). Hook will work correctly for Bash
once that is fixed upstream.

Marker file: /tmp/.hgp_bash_mutating_<ppid>
"""
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, cast

# ── Read-only command prefixes — skip pattern matching for these ──────────────
_READONLY_PREFIXES = (
    "git log",
    "git status",
    "git diff",
    "git show",
    "git branch",
    "git tag",
    "git remote",
    "git fetch",
    "git ls",
    "git stash list",
    "ls ",
    "ls\t",
    "head ",
    "tail ",
    "grep ",
    "rg ",
    "find ",
    "wc ",
    "diff ",
    "less ",
    "more ",
    "file ",
    "stat ",
    "pwd",
    "date",
    "which ",
    "type ",
    "uname",
)

# ── Mutating patterns (regex) ─────────────────────────────────────────────────
_HIGH_PATTERNS = [
    re.compile(r"\bcp\b"),
    re.compile(r"\bmv\b"),
    re.compile(r"\brm\b"),
    re.compile(r"\btee\b"),
    re.compile(r"\btouch\b"),
    re.compile(r"\binstall\b"),
    re.compile(r"\bmkdir\b"),
    re.compile(r"\brmdir\b"),
    re.compile(r"\bchmod\b"),
    re.compile(r"\bchown\b"),
    re.compile(r"\bln\b"),
    re.compile(r"\btruncate\b"),
    # git commands that rewrite working-tree files
    re.compile(r"\bgit\s+checkout\b"),
    re.compile(r"\bgit\s+restore\b"),
    re.compile(r"\bgit\s+switch\b"),
    re.compile(r"\bgit\s+apply\b"),
    re.compile(r"\bgit\s+revert\b"),
    re.compile(r"\bgit\s+merge\b"),
    re.compile(r"\bgit\s+rebase\b"),
    re.compile(r"\bgit\s+reset\b"),
    # patch tools
    re.compile(r"\bpatch\b"),
]

_MEDIUM_PATTERNS = [
    re.compile(r"(?<![|&])\s*>(?!>)"),  # stdout redirect (not >>)
    re.compile(r">>"),                   # append redirect
    re.compile(r"\bsed\s+-i\b"),
    re.compile(r"\bdd\b"),
    re.compile(r"\bawk\b.*>"),           # awk with redirect
]

# Detect agent attempts to change HGP mode via Bash
_HGP_MODE_PATTERN = re.compile(r"\bhgp\s+mode\b")


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


def _is_readonly(command: str) -> bool:
    stripped = command.lstrip()
    return any(stripped.startswith(p) for p in _READONLY_PREFIXES)


def _detect_mutating(command: str) -> str | None:
    """Return the first matched pattern description, or None if command looks safe."""
    for pat in _HIGH_PATTERNS:
        if pat.search(command):
            return pat.pattern
    for pat in _MEDIUM_PATTERNS:
        if pat.search(command):
            return pat.pattern
    return None


def _marker_path() -> str:
    return f"/tmp/.hgp_bash_mutating_{os.getppid()}"


def _main() -> None:
    try:
        event = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)
    start = _event_cwd(event)  # one directory for the gate and the policy
    if not _enabled(start):
        sys.exit(0)

    if event.get("tool_name") != "Bash":
        sys.exit(0)

    command: str = event.get("tool_input", {}).get("command", "")
    if not command or _is_readonly(command):
        sys.exit(0)

    # Block agent attempts to change HGP mode via Bash (mode control is user-only)
    if _HGP_MODE_PATTERN.search(command):
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    "[HGP] Blocked: `hgp mode` must be run by the user directly, not by the agent. "
                    "Use `!hgp mode <value>` in your terminal to change HGP mode."
                ),
            },
        }))
        sys.exit(0)

    # Advising or blocking native tools is pointless while HGP tools do not write.
    if _paused(start):
        sys.exit(0)
    matched = _detect_mutating(command)
    if matched is None:
        sys.exit(0)

    msg = (
        f"[HGP] Bash command may mutate files (matched: {matched!r}). "
        "If this writes or deletes tracked files, prefer hgp_* tools so the "
        "operation is recorded in HGP history."
    )

    if _resolve_block_mode(start):
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": msg,
            },
        }))
    else:
        # Write marker so PostToolUse hook knows to run git status
        try:
            with open(_marker_path(), "w") as f:
                f.write(matched)
        except OSError:
            pass  # /tmp not writable — skip marker, hook still warns
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
