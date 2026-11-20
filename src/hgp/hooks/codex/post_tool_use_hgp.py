"""PostToolUse hook for Codex: tell the agent what a shell command changed.

The PreToolUse hook records a ``git status`` baseline before a mutating command. This hook diffs the
status against it and reports only the entries that are new, so changes made before the command are not
blamed on it. Entries under .hgp/ are left out.

Codex protocol (always exit 0):
  Report:       stdout JSON {"hookSpecificOutput": {"hookEventName": "PostToolUse",
                             "additionalContext": "..."}}
  Pass-through: no stdout output

The report lists the new entries (at most 30, file names marked as data). When nothing is new the
advisory about the matched pattern is still sent, as before. Nothing is printed when there is no
usable marker or HGP is not enabled for the working directory.

Marker: <tmp>/hgp-codex-<uid>/bash-<tool_use_id>.json (the parent pid when the payload has no
tool_use_id; Codex PreToolUse and PostToolUse payloads both carry it), consumed here even when the
command left an enabled repository, so it cannot go stale. The shared code is hgp_paths (track_*);
without it beside the hook there is no tracking.

Known limit: status-level only, so a file that was already modified before the command shows no
change, and .gitignore'd files do not appear.
"""
import json
import os
import sys
from pathlib import Path
from typing import Any, cast


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


def _main() -> None:
    try:
        raw: object = json.loads(sys.stdin.read())
    except ValueError:
        sys.exit(0)
    if not isinstance(raw, dict):
        sys.exit(0)
    event = cast(dict[str, Any], raw)
    if event.get("tool_name") != "Bash":
        sys.exit(0)
    paths = _hgp_paths()
    report = getattr(paths, "track_report", None)
    if report is None:
        sys.exit(0)

    # Consume before the gate: a command that cd'd out of an enabled repo must
    # not leave a stale marker for a later, unrelated command.
    result = report("codex", paths.track_key(event))
    if result is None:
        # No usable marker: the PreToolUse hook didn't flag this as mutating; skip
        sys.exit(0)
    if not _enabled(_event_cwd(event)):
        sys.exit(0)

    if result.changed:
        context = paths.track_message("Bash command", result.changed, result.patterns, result.truncated, False)
    else:
        # Agent-facing advisory (additionalContext reaches the agent's reasoning), as before
        context = (
            f"[HGP] Bash command may mutate files (matched: {result.patterns[0]!r}). "
            "If this writes or deletes tracked files, prefer hgp_* tools so the "
            "operation is recorded in HGP history."
        )
    output: dict[str, Any] = {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": context}}
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
