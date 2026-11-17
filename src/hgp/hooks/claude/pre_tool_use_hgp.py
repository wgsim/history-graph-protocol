"""PreToolUse hook: warn when native Write/Edit is used instead of hgp_* tools.

Exit 0 = allow the tool call (non-blocking by default).
Print to stderr = message shown to the agent as a warning.
Set HGP_HOOK_BLOCK=1 to make the hook reject native file tool calls.
"""
import json
import os
import sys
from typing import Any


def _load_hgp_paths() -> Any:
    """Load hgp_paths.py installed beside this hook (or from the package tree)."""
    import importlib.util
    from pathlib import Path

    here = Path(__file__).resolve().parent
    for candidate in (here / "hgp_paths.py", here.parent / "hgp_paths.py"):
        if not candidate.is_file():
            continue
        try:
            spec = importlib.util.spec_from_file_location("_hgp_paths", candidate)
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
        except Exception:
            continue
    return None


HGP_TOOLS = {
    "Write": "hgp_write_file",
    "Edit": "hgp_edit_file",
    "MultiEdit": "hgp_edit_file",
}

def _resolve_block_mode() -> bool:
    """Check HGP_HOOK_BLOCK env var, then fall back to .hgp/hook-policy file."""
    env = os.environ.get("HGP_HOOK_BLOCK")
    if env is not None:
        return env == "1"
    from pathlib import Path

    paths = _load_hgp_paths()

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

    for parent in [Path.cwd(), *Path.cwd().parents]:
        # Shared policy plus a checkout-local legacy one (written before storage
        # was shared): block if either says so, so upgrading never drops enforcement.
        candidates = dict.fromkeys([shared_hgp_dir(parent) / "hook-policy", parent / ".hgp" / "hook-policy"])
        found = [p for p in candidates if p.exists()]
        if found:
            return any(is_block(p.read_text()) for p in found)
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
    print(msg, file=sys.stderr)

    if BLOCK_MODE:
        # Exit 2 = block; Claude Code reads stderr (already printed above), ignores stdout
        sys.exit(2)

    sys.exit(0)


if __name__ == "__main__":
    main()
