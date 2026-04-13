"""PostToolUse hook for Codex: detect and report actual file changes after shell commands.

Only runs git status when the PreToolUse hook wrote a marker file indicating a
potentially mutating command was about to execute. This avoids the overhead of
git status on every read-only shell call.

Codex protocol (always exit 0):
  Report:       stdout JSON {"hookSpecificOutput": {"hookEventName": "PostToolUse",
                             "additionalContext": "..."}}
  Pass-through: no stdout output

Known limitation: .gitignore'd files won't appear in the report.

Marker file: /tmp/.hgp_bash_mutating_<ppid>
"""
import json
import os
import subprocess
import sys
from typing import Any

_TIMEOUT_SECS = 2


def _marker_path() -> str:
    return f"/tmp/.hgp_bash_mutating_{os.getppid()}"


def _consume_marker() -> str | None:
    """Return marker contents (matched pattern) and remove marker, or None if absent."""
    path = _marker_path()
    try:
        content = open(path).read().strip()
        os.unlink(path)
        return content or ""
    except FileNotFoundError:
        return None


def _git_changed_files(cwd: str) -> list[str]:
    """Run git status --porcelain and return list of changed file entries."""
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECS,
        )
        if result.returncode != 0:
            return []
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        return lines
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return []


def main() -> None:
    try:
        event = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)

    if event.get("tool_name") != "Bash":
        sys.exit(0)

    matched = _consume_marker()
    if matched is None:
        # No marker → PreToolUse hook didn't flag this as mutating; skip
        sys.exit(0)

    cwd = os.getcwd()
    changed = _git_changed_files(cwd)

    # Build agent-facing advisory (additionalContext — reaches agent reasoning)
    parts = [
        f"[HGP] Bash command may mutate files (matched: {matched!r}). "
        "If this writes or deletes tracked files, prefer hgp_* tools so the "
        "operation is recorded in HGP history."
    ]
    if changed:
        lines_str = "\n  ".join(changed)
        parts.append(f"Changed tracked files:\n  {lines_str}")
    additional_context = "\n".join(parts)

    output: dict[str, Any] = {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": additional_context,
        },
    }
    print(json.dumps(output))
    sys.exit(0)


if __name__ == "__main__":
    main()
