"""PreToolUse hook for Codex: warn when shell commands may mutate files outside HGP.

Codex protocol (always exit 0):
  Block:        stdout JSON {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                             "permissionDecision": "deny", "permissionDecisionReason": "..."}}
  Warn:         stdout JSON {"systemMessage": "..."}
  Pass-through: no stdout output

When a mutating pattern is detected, a marker file is written to /tmp so the
PostToolUse hook can run 'git status' to report actual changes.

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
from typing import Any

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


def _load_hgp_paths() -> Any:
    """Load hgp_paths.py installed beside this hook (or from the package tree)."""
    import importlib.util

    here = Path(__file__).resolve().parent
    for candidate in (here / "hgp_paths.py", here.parent / "hgp_paths.py"):
        if not candidate.is_file():
            continue
        try:
            spec = importlib.util.spec_from_file_location("_hgp_paths", candidate)
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
        except Exception:
            continue
    return None


def _resolve_block_mode() -> bool:
    """Check HGP_HOOK_BLOCK env var, then fall back to .hgp/hook-policy file."""
    env = os.environ.get("HGP_HOOK_BLOCK")
    if env is not None:
        return env == "1"
    paths = _load_hgp_paths()

    def shared_hgp_dir(root: Path) -> Path:
        if paths is not None:
            return Path(paths.shared_hgp_dir(root))
        return root / ".hgp"  # partial install without hgp_paths.py

    for parent in [Path.cwd(), *Path.cwd().parents]:
        policy_file = shared_hgp_dir(parent) / "hook-policy"
        if policy_file.exists():
            text = policy_file.read_text()
            if paths is not None:
                return paths.parse_hook_policy(text) is True
            return text.strip() == "block"  # legacy reader for partial installs
        if (parent / ".git").exists():
            break
    return False


BLOCK_MODE = _resolve_block_mode()


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

    matched = _detect_mutating(command)
    if matched is None:
        sys.exit(0)

    msg = (
        f"[HGP] Bash command may mutate files (matched: {matched!r}). "
        "If this writes or deletes tracked files, prefer hgp_* tools so the "
        "operation is recorded in HGP history."
    )

    if BLOCK_MODE:
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


if __name__ == "__main__":
    main()
