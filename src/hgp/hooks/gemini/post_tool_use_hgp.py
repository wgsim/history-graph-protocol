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
from pathlib import Path
from typing import Any, cast

HGP_TOOLS = {
    "write_file": "hgp_write_file",
    "replace": "hgp_edit_file",
}


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


def _main() -> None:
    try:
        event = json.loads(sys.stdin.read())
    except json.JSONDecodeError:
        sys.exit(0)
    if not _enabled(_event_cwd(event)):
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
