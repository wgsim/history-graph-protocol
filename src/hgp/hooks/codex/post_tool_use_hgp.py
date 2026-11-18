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
from pathlib import Path
from typing import Any, cast

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


def _main() -> None:
    try:
        event = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)
    start = _event_cwd(event)  # one directory for the gate and the git status
    if event.get("tool_name") != "Bash":
        sys.exit(0)

    # Consume before the gate: a command that cd'd out of an enabled repo must
    # not leave a stale marker for a later, unrelated command.
    matched = _consume_marker()
    if matched is None:
        # No marker → PreToolUse hook didn't flag this as mutating; skip
        sys.exit(0)
    if not _enabled(start):
        sys.exit(0)

    cwd = str(start)
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
