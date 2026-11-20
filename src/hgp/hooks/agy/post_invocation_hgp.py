"""PostInvocation hook for Antigravity CLI (agy): tell the model what shell commands changed.

Collects the ``post-<conversation>__<step>.json`` files that PostToolUse wrote (see agy_state.py),
merges them in step order without repeats, deletes them, and injects one system message:
``{"injectSteps": [{"systemMessage": {"systemMessage": "..."}}]}``. With nothing to report it
prints ``{}``. State is delivered once, then gone. Any error means ``{}`` and exit 0.

The entries are file names from ``git status``, so they are cut to printable text and a fixed
length before they reach the model, and the message says they are data.
"""
import json
import sys
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from hgp.hooks.agy import agy_state
else:
    try:
        import agy_state
    except ImportError:  # run from the hgp package, not from an installed copy
        try:
            from hgp.hooks.agy import agy_state
        except ImportError:
            print("{}")
            sys.exit(0)


def _collect() -> str | None:
    raw: object = json.loads(sys.stdin.read())
    if not isinstance(raw, dict):
        return None
    event = cast(dict[str, Any], raw)
    directory = agy_state.state_dir()
    if directory is None:
        return None
    changed: dict[str, None] = {}  # insertion-ordered set: each entry once, at its first appearance
    patterns: dict[str, None] = {}
    truncated = had_error = False
    for path in agy_state.post_files(directory, event.get("conversationId")):
        data = agy_state.read_json(path)
        agy_state.remove(path)  # delivered once; a corrupt file goes too
        fields = cast(dict[str, Any], data) if isinstance(data, dict) else {}
        entries = agy_state.str_list(fields.get("changed"))
        if not entries:
            continue
        changed.update(dict.fromkeys(entries))
        patterns.update(dict.fromkeys(agy_state.str_list(fields.get("patterns")) or []))
        truncated = truncated or fields.get("truncated") is True
        had_error = had_error or fields.get("had_error") is True
    if not changed:
        return None
    return agy_state.track_message("Shell command", list(changed), list(patterns), truncated, had_error)


def main() -> None:
    """Fail-open entry point: whatever happens, print ``{}`` unless there is something to inject."""
    try:
        message = _collect()
    except Exception:
        message = None
    if message is None:
        print("{}")
    else:
        print(json.dumps({"injectSteps": [{"systemMessage": {"systemMessage": message}}]}))


if __name__ == "__main__":
    main()
