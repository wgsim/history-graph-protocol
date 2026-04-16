"""SubagentStop hook: record subagent HGP activity summary to a file.

additionalContext from SubagentStop does not reach the main agent (verified
2026-04-16). Instead, write a summary file that the main agent can read via
hgp_get_context(session_id, include_summaries=True).

Summary file: .hgp/subagent-summary-{session_id}-{timestamp}.json
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, cast


def _find_hgp_dir(cwd: str) -> Path | None:
    start = Path(cwd)
    for parent in [start, *start.parents]:
        candidate = parent / ".hgp"
        if candidate.is_dir():
            return candidate
        if (parent / ".git").exists():
            return candidate
    return None


def _count_hgp_ops(transcript_path: str) -> int:
    """Count tool_use blocks whose name starts with mcp__hgp__ in the transcript."""
    try:
        count = 0
        with open(transcript_path, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                obj = cast(dict[str, Any], json.loads(line))
                raw_msg = obj.get("message")
                if not isinstance(raw_msg, dict):
                    continue
                msg = cast(dict[str, Any], raw_msg)
                content = cast(list[dict[str, Any]], msg.get("content") or [])
                for block in content:
                    if (
                        block.get("type") == "tool_use"
                        and str(block.get("name", "")).startswith("mcp__hgp__")
                    ):
                        count += 1
        return count
    except (OSError, json.JSONDecodeError):
        return -1  # -1 = unreadable, distinct from 0 (readable but no ops)


def main() -> None:
    try:
        event = json.loads(sys.stdin.read())
    except json.JSONDecodeError:
        sys.exit(0)

    if event.get("hook_event_name") != "SubagentStop":
        sys.exit(0)

    session_id = event.get("session_id", "")
    agent_id = event.get("agent_id", "unknown")
    agent_type = event.get("agent_type", "unknown")
    transcript_path = event.get("agent_transcript_path", "")
    cwd = event.get("cwd", "")

    hgp_dir = _find_hgp_dir(cwd)
    if hgp_dir is None:
        sys.exit(0)

    hgp_op_count = _count_hgp_ops(transcript_path) if transcript_path else -1

    summary = {
        "agent_id": agent_id,
        "agent_type": agent_type,
        "session_id": session_id,
        "hgp_op_count": hgp_op_count,
        "completed_at": time.time(),
    }

    ts = int(time.time())
    summary_path = hgp_dir / f"subagent-summary-{session_id}-{ts}.json"
    try:
        summary_path.write_text(json.dumps(summary), encoding="utf-8")
    except OSError:
        pass


if __name__ == "__main__":
    main()
