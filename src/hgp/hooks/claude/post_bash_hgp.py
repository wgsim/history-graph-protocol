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
from pathlib import Path
from typing import Any, cast

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
    for parent in [start, *start.parents]:  # no hgp_paths: walk up to the checkout root
        if (parent / ".hgp").is_dir():
            return True
        if (parent / ".git").exists():
            return False
    return False


def _event_cwd(event: object) -> Path:
    cwd = cast(dict[str, Any], event).get("cwd") if isinstance(event, dict) else None
    return Path(cwd) if isinstance(cwd, str) and cwd else Path.cwd()


def _main() -> None:
    try:
        event = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)
    start = _event_cwd(event)  # one directory for the gate and the git status
    if not _enabled(start):
        sys.exit(0)

    if event.get("tool_name") != "Bash":
        sys.exit(0)

    if not _consume_marker():
        # No marker → Pre hook didn't flag this as mutating; skip git status
        sys.exit(0)

    cwd = str(start)
    changed = _git_changed_files(cwd)
    if not changed:
        sys.exit(0)

    lines_str = "\n  ".join(changed)
    print(
        f"[HGP] Bash command changed tracked files (use hgp_* tools for history):\n  {lines_str}",
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
