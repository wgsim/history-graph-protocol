"""PostToolUse hook for Bash: detect and report actual file changes after Bash commands.

Only runs git status when the Pre-Bash hook wrote a marker file indicating a
potentially mutating command was about to execute. This avoids the overhead of
git status on every read-only Bash call.

Known limitation: .gitignore'd files won't appear in the report.

Marker file: /tmp/.hgp_bash_mutating_<ppid>
"""
import json
import os
import subprocess
import sys

_TIMEOUT_SECS = 2


def _marker_path() -> str:
    return f"/tmp/.hgp_bash_mutating_{os.getppid()}"


def _consume_marker() -> bool:
    """Return True and remove marker if it exists, False otherwise."""
    path = _marker_path()
    try:
        os.unlink(path)
        return True
    except FileNotFoundError:
        return False


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

    if not _consume_marker():
        # No marker → Pre hook didn't flag this as mutating; skip git status
        sys.exit(0)

    cwd = os.getcwd()
    changed = _git_changed_files(cwd)
    if not changed:
        sys.exit(0)

    lines_str = "\n  ".join(changed)
    print(
        f"[HGP] Bash command changed tracked files (use hgp_* tools for history):\n  {lines_str}",
        file=sys.stderr,
    )
    sys.exit(0)


if __name__ == "__main__":
    main()
