"""SessionStart hook for Antigravity CLI (agy): tell the model its conversation id.

Subagent propagation keys on the ``session_id`` the agent gave ``hgp_set_context``, and PreToolUse
looks it up by the parent's ``conversationId``. The agent cannot know that id by itself, so when
HGP is enabled and not paused this injects one system message with it:
``{"injectSteps": [{"systemMessage": {"systemMessage": "..."}}]}``. Otherwise (HGP off, paused, no
usable id, bad input) it prints nothing. Exit code 0 always.
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
            sys.exit(0)


def _message() -> str | None:
    raw: object = json.loads(sys.stdin.read())
    if not isinstance(raw, dict):
        return None
    event = cast(dict[str, Any], raw)
    conversation = agy_state.conversation_id(event.get("conversationId"))
    start = agy_state.event_cwd(event)
    if conversation is None or not agy_state.enabled(start) or agy_state.paused(start):
        return None
    return (
        f"[HGP] This conversation's id is {conversation}. When you call hgp_set_context, pass it as "
        f'session_id (session_id="{conversation}"), so the subagents you start get this session\'s root '
        "operation as their parent_op_ids."
    )


def main() -> None:
    """Fail-open entry point: an unexpected error prints nothing."""
    try:
        message = _message()
    except Exception:
        return
    if message is not None:
        print(json.dumps({"injectSteps": [{"systemMessage": {"systemMessage": message}}]}))


if __name__ == "__main__":
    main()
