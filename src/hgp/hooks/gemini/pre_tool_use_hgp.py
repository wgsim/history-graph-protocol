"""BeforeTool hook for Gemini CLI: warn/block when native file tools are used.

Gemini CLI protocol (all responses exit 0):
  Warn mode (default):   stdout JSON {"systemMessage": "..."}
  Block mode (HGP_HOOK_BLOCK=1): stdout JSON {"decision": "deny", "reason": "..."}
  Pass-through:          no stdout output

Set HGP_HOOK_BLOCK=1 to enforce blocking instead of warning.
"""
import json
import os
import sys

HGP_TOOLS = {
    "write_file": "hgp_write_file",
    "replace": "hgp_edit_file",
}

def _resolve_block_mode() -> bool:
    """Check HGP_HOOK_BLOCK env var, then fall back to .hgp/hook-policy file."""
    env = os.environ.get("HGP_HOOK_BLOCK")
    if env is not None:
        return env == "1"
    from pathlib import Path
    for parent in [Path.cwd(), *Path.cwd().parents]:
        policy_file = parent / ".hgp" / "hook-policy"
        if policy_file.exists():
            return policy_file.read_text().strip() == "block"
        if (parent / ".git").exists():
            break
    return False


BLOCK_MODE = _resolve_block_mode()


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
        f"Set HGP_HOOK_BLOCK=1 or run `hgp hook-policy block` to enforce."
    )

    if BLOCK_MODE:
        print(json.dumps({"decision": "deny", "reason": msg}))
    else:
        print(json.dumps({"systemMessage": msg}))

    sys.exit(0)


if __name__ == "__main__":
    main()
