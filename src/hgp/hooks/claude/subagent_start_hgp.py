"""SubagentStart hook: inject HGP session root op_id into spawned subagents.

Reads .hgp/context-{session_id}.json written by hgp_set_context, then outputs
additionalContext so the subagent knows which parent_op_ids to use.
"""
from __future__ import annotations

import json
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
    for parent in [start, *start.parents]:  # no hgp_paths: walk up to the checkout root
        if (parent / ".git").exists():
            return (parent / ".hgp").is_dir()
    return (start / ".hgp").is_dir()  # outside git only this directory's store counts


def _event_cwd(event: object) -> Path:
    cwd = cast(dict[str, Any], event).get("cwd") if isinstance(event, dict) else None
    return Path(cwd) if isinstance(cwd, str) and cwd else Path.cwd()


def _find_hgp_dir(start: Path) -> Path | None:
    paths = _hgp_paths()
    if paths is not None:
        return paths.find_hgp_dir(start)
    # Fallback for partial installs without hgp_paths.py: worktree-local lookup.
    for parent in [start, *start.parents]:
        candidate = parent / ".hgp"
        if candidate.is_dir():
            return candidate
        if (parent / ".git").exists():
            return candidate  # return even if .hgp not yet created — caller handles missing
    return None


def _main() -> None:
    try:
        event = json.loads(sys.stdin.read())
    except json.JSONDecodeError:
        sys.exit(0)
    start = _event_cwd(event)  # one directory for the gate and the context lookup
    if not _enabled(start):
        sys.exit(0)

    if event.get("hook_event_name") != "SubagentStart":
        sys.exit(0)

    session_id = event.get("session_id", "")
    if not session_id:
        sys.exit(0)

    hgp_dir = _find_hgp_dir(start)
    if hgp_dir is None:
        sys.exit(0)

    context_path = hgp_dir / f"context-{session_id}.json"
    if not context_path.exists():
        sys.exit(0)

    try:
        data = json.loads(context_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        sys.exit(0)

    root_op_id = data.get("root_op_id", "")
    if not root_op_id:
        sys.exit(0)

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SubagentStart",
            "additionalContext": (
                f"[HGP] Session root op: {root_op_id}. "
                f"Use parent_op_ids=[\"{root_op_id}\"] for all hgp_* calls in this subagent."
            ),
        }
    }))


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
