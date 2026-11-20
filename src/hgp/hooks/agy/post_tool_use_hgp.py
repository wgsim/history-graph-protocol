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
from pathlib import Path
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
    baseline = fields.get("baseline")
    if not isinstance(baseline, dict):
        return

    new: list[str] = []
    for root, before in cast(dict[object, object], baseline).items():
        seen = agy_state.str_list(before)
        if not isinstance(root, str) or not Path(root).is_absolute() or seen is None:
            continue
        now = agy_state.git_status(Path(root))
        if now is not None:
            before_set = set(seen)
            new.extend(line for line in now if line not in before_set)
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
