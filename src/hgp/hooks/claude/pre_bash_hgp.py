"""PreToolUse hook for Bash: warn when shell commands may mutate files outside HGP.

This hook is non-blocking (always exit 0). It prints a warning to stderr when
it detects shell patterns that typically write or delete files, reminding the
agent to use hgp_* tools for tracked file operations.

When a mutating pattern is detected, a marker file is written to /tmp so the
Post-Bash hook can run 'git status' to report actual changes.

Marker file: /tmp/.hgp_bash_mutating_<ppid>
"""
import json
import os
import re
import sys

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


def main() -> None:
    try:
        event = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)

    if event.get("tool_name") != "Bash":
        sys.exit(0)

    command: str = event.get("tool_input", {}).get("command", "")
    if not command or _is_readonly(command):
        sys.exit(0)

    # Warn if agent tries to change HGP mode via Bash (mode control is user-only)
    if _HGP_MODE_PATTERN.search(command):
        print(
            "[HGP] Agent attempted to change HGP mode via Bash. "
            "Mode control is user-only (`hgp mode` in your terminal). "
            "The command will run but is noted.",
            file=sys.stderr,
        )

    matched = _detect_mutating(command)
    if matched is None:
        sys.exit(0)

    # Write marker so Post-Bash hook knows to run git status
    try:
        open(_marker_path(), "w").close()
    except OSError:
        pass  # /tmp not writable — skip gating, hook still warns

    print(
        f"[HGP] Bash command may mutate files (matched: {matched!r}). "
        "If this writes or deletes tracked files, prefer hgp_* tools so the "
        "operation is recorded in HGP history.",
        file=sys.stderr,
    )
    sys.exit(0)


if __name__ == "__main__":
    main()
