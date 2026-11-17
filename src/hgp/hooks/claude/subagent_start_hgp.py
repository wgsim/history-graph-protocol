"""SubagentStart hook: inject HGP session root op_id into spawned subagents.

Reads .hgp/context-{session_id}.json written by hgp_set_context, then outputs
additionalContext so the subagent knows which parent_op_ids to use.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


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


def _find_hgp_dir() -> Path | None:
    paths = _load_hgp_paths()
    if paths is not None:
        return paths.find_hgp_dir(Path.cwd())
    # Fallback for partial installs without hgp_paths.py: worktree-local lookup.
    for parent in [Path.cwd(), *Path.cwd().parents]:
        candidate = parent / ".hgp"
        if candidate.is_dir():
            return candidate
        if (parent / ".git").exists():
            return candidate  # return even if .hgp not yet created — caller handles missing
    return None


def main() -> None:
    try:
        event = json.loads(sys.stdin.read())
    except json.JSONDecodeError:
        sys.exit(0)

    if event.get("hook_event_name") != "SubagentStart":
        sys.exit(0)

    session_id = event.get("session_id", "")
    if not session_id:
        sys.exit(0)

    hgp_dir = _find_hgp_dir()
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


if __name__ == "__main__":
    main()
