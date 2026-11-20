"""PostToolUse hook for Antigravity CLI (agy): find what a tracked shell command changed.

PreToolUse records a ``git status`` baseline before a mutating ``run_command`` (see agy_state.py).
This hook diffs the status against it and keeps only the entries that are new, in
``post-<conversation>__<step>.json``; the PostInvocation hook delivers them to the model in one
message. Status-level only: a file that was already modified before the command shows no change.

agy protocol: stdin JSON, camelCase, the same ``stepIdx`` and ``toolCall.id`` as the matching
PreToolUse event (plus ``error``). stdout is always ``{}`` and the exit code 0: this hook never
decides anything, and any error means "nothing tracked".
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


def _track() -> None:
    raw: object = json.loads(sys.stdin.read())
    if not isinstance(raw, dict):
        return
    event = cast(dict[str, Any], raw)
    call = event.get("toolCall")
    if not isinstance(call, dict) or cast(dict[str, Any], call).get("name") != "run_command":
        return
    directory = agy_state.state_dir()
    if directory is None:
        return
    conversation, step = event.get("conversationId"), event.get("stepIdx")
    marker = agy_state.marker_path(directory, conversation, step)
    if marker is None:
        return
    data = agy_state.read_json(marker)
    agy_state.remove(marker)  # consumed, valid or not
    fields = cast(dict[str, Any], data) if isinstance(data, dict) else {}
    new = agy_state.new_entries(fields)
    if not new:
        return
    pattern = fields.get("pattern")
    path = agy_state.post_path(directory, conversation, step)
    if path is not None:
        agy_state.write_json(path, {
            "patterns": [pattern] if isinstance(pattern, str) else [],
            "changed": new[:agy_state.MAX_CHANGED],
            "truncated": len(new) > agy_state.MAX_CHANGED,
            "had_error": bool(event.get("error")),
        })


def main() -> None:
    """Fail-open entry point: whatever happens, print ``{}`` and exit 0."""
    try:
        _track()
    except Exception:
        pass
    print("{}")


if __name__ == "__main__":
    main()
