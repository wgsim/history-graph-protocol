"""PreToolUse hook: warn when native Write/Edit is used instead of hgp_* tools.

Exit 0 = allow the tool call (non-blocking by default).
Print to stderr = message shown to the agent as a warning.
Set HGP_HOOK_BLOCK=1 to make the hook reject native file tool calls.
"""
import json
import os
import sys

HGP_TOOLS = {
    "Write": "hgp_write_file",
    "Edit": "hgp_edit_file",
    "MultiEdit": "hgp_edit_file",
}

BLOCK_MODE = os.environ.get("HGP_HOOK_BLOCK", "0") == "1"


def main() -> None:
    try:
        event = json.loads(sys.stdin.read())
    except json.JSONDecodeError:
        sys.exit(0)

    tool_name = event.get("tool_name", "")
    if tool_name not in HGP_TOOLS:
        sys.exit(0)

    hgp_equiv = HGP_TOOLS[tool_name]
    msg = (
        f"[HGP] Native `{tool_name}` detected. "
        f"Use `{hgp_equiv}` instead to record this file operation in HGP history. "
        f"Set HGP_HOOK_BLOCK=1 to enforce this as an error."
    )
    print(msg, file=sys.stderr)

    if BLOCK_MODE:
        # Exit 2 = block; Claude Code reads stderr (already printed above), ignores stdout
        sys.exit(2)

    sys.exit(0)


if __name__ == "__main__":
    main()
