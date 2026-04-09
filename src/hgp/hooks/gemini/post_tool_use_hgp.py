"""AfterTool hook for Gemini CLI: inject advisory warning into agent context after native file tools.

Gemini CLI protocol (always exit 0):
  Advisory:     stdout JSON {"hookSpecificOutput": {"additionalContext": "..."}}
  Pass-through: no stdout output

This hook fires after write_file or replace succeeds (i.e. was not blocked by
pre_tool_use_hgp.py). It appends a warning to the tool result so the agent
sees it and can prefer hgp_* tools on subsequent calls.

Block mode: pre_tool_use_hgp.py denies the tool before it runs, so this hook
never fires when blocking is active.
"""
import json
import sys

HGP_TOOLS = {
    "write_file": "hgp_write_file",
    "replace": "hgp_edit_file",
}


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
        f"[HGP] Native `{tool_name}` was used. "
        f"Prefer `{hgp_equiv}` to record this file operation in HGP history. "
        f"Run `hgp hook-policy block` to enforce this."
    )
    print(json.dumps({"hookSpecificOutput": {"additionalContext": msg}}))
    sys.exit(0)


if __name__ == "__main__":
    main()
