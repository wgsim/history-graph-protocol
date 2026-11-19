"""PreToolUse hook: warn when native Write/Edit is used instead of hgp_* tools.

Exit 0 = allow the tool call (non-blocking by default).
Print to stderr = message shown to the agent as a warning.
Set HGP_HOOK_BLOCK=1 to make the hook reject native file tool calls.
In a session that already used HGP tools it asks the user first (HGP_HOOK_ASK=0 disables).
"""
import json
import os
import sys
from pathlib import Path
from typing import Any, cast


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
    if os.environ.get("HGP_GLOBAL_MODE"):
        return True  # global mode records everywhere, as in the server
    for parent in [start, *start.parents]:  # no hgp_paths: walk up to the checkout root
        if (parent / ".git").exists():
            return (parent / ".hgp").is_dir()
    return (start / ".hgp").is_dir()  # outside git only this directory's store counts


def _paused(start: Path) -> bool:
    """Return whether HGP mode is advisory or off: HGP tools then do not write files.

    HGP_HOOK_BLOCK=1 always wins: an agent can write .hgp/mode, not this environment.
    """
    if os.environ.get("HGP_HOOK_BLOCK") == "1":
        return False
    mode = getattr(_hgp_paths(), "hgp_mode", None)
    return mode is not None and mode(start) in ("advisory", "off")


def _hgp_loaded(event: object) -> bool:
    """Return whether this session already loaded or used HGP tools.

    True once the transcript shows an ``mcp__hgp__*`` tool call, or a ToolSearch
    that loaded one. Fail-safe: a missing or unreadable transcript reads as False.
    """
    path = cast(dict[str, Any], event).get("transcript_path") if isinstance(event, dict) else None
    if not isinstance(path, str) or not path:
        return False
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if "mcp__hgp__" not in line:
                    continue
                try:
                    record: object = json.loads(line)
                except ValueError:
                    continue
                message = cast(dict[str, Any], record).get("message") if isinstance(record, dict) else None
                content = cast(dict[str, Any], message).get("content") if isinstance(message, dict) else None
                if not isinstance(content, list):
                    continue
                for block in cast(list[Any], content):
                    if not isinstance(block, dict) or cast(dict[str, Any], block).get("type") != "tool_use":
                        continue
                    name = cast(dict[str, Any], block).get("name", "")
                    if name.startswith("mcp__hgp__") or (
                        name == "ToolSearch"
                        and "mcp__hgp__" in json.dumps(cast(dict[str, Any], block).get("input", {}))
                    ):
                        return True
    except (OSError, UnicodeError):
        return False
    return False


def _event_cwd(event: object) -> Path:
    """HGP_PROJECT_ROOT when it names a directory (as in the server), else the event cwd."""
    env = os.environ.get("HGP_PROJECT_ROOT")
    if env and Path(env).is_dir():
        return Path(env).resolve()
    cwd = cast(dict[str, Any], event).get("cwd") if isinstance(event, dict) else None
    return Path(cwd) if isinstance(cwd, str) and cwd else Path.cwd()


HGP_TOOLS = {
    "Write": "hgp_write_file",
    "Edit": "hgp_edit_file",
    "MultiEdit": "hgp_edit_file",
}

def _resolve_block_mode(start: Path) -> bool:
    """Check HGP_HOOK_BLOCK env var, then fall back to .hgp/hook-policy file."""
    env = os.environ.get("HGP_HOOK_BLOCK")
    if env is not None:
        return env == "1"
    paths = _hgp_paths()

    def shared_hgp_dir(root: Path) -> Path:
        if paths is not None:
            return Path(paths.shared_hgp_dir(root))
        return root / ".hgp"  # partial install without hgp_paths.py

    def is_block(text: str) -> bool:
        if paths is not None:
            return paths.parse_hook_policy(text) is True
        stripped = text.strip()  # partial install without hgp_paths.py
        if stripped.startswith("{"):
            try:
                return json.loads(stripped).get("block_mode") is True
            except (ValueError, AttributeError):
                return False
        return stripped == "block"

    for parent in [start, *start.parents]:
        # Shared policy plus a checkout-local legacy one (written before storage
        # was shared): block if either says so, so upgrading never drops enforcement.
        candidates = dict.fromkeys([shared_hgp_dir(parent) / "hook-policy", parent / ".hgp" / "hook-policy"])
        found = [p for p in candidates if p.exists()]
        if found:
            return any(is_block(p.read_text()) for p in found)
        if (parent / ".git").exists():
            break
    return False


def _main() -> None:
    try:
        event = json.loads(sys.stdin.read())
    except json.JSONDecodeError:
        sys.exit(0)
    start = _event_cwd(event)  # one directory for the gate and the policy
    if not _enabled(start):
        sys.exit(0)

    tool_name = event.get("tool_name", "")
    if tool_name not in HGP_TOOLS:
        sys.exit(0)
    # Advising or blocking native tools is pointless while HGP tools do not write.
    if _paused(start):
        sys.exit(0)

    hgp_equiv = HGP_TOOLS[tool_name]
    msg = (
        f"[HGP] Native `{tool_name}` detected. "
        f"Use `{hgp_equiv}` instead to record this file operation in HGP history. "
        f"Set HGP_HOOK_BLOCK=1 or run `hgp hook-policy block` to enforce."
    )
    print(msg, file=sys.stderr)

    if _resolve_block_mode(start):
        # Exit 2 = block; Claude Code reads stderr (already printed above), ignores stdout
        sys.exit(2)

    # A session that uses HGP asks the user before a native write (HGP_HOOK_ASK=0 disables).
    if os.environ.get("HGP_HOOK_ASK", "1") == "1" and _hgp_loaded(event):
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": msg,
        }}))

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
