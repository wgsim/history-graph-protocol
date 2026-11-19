"""PreToolUse hook: warn when native Write/Edit is used instead of hgp_* tools.

Exit 0 = allow the tool call (non-blocking by default).
Print to stderr = message shown to the agent as a warning.
Set HGP_HOOK_BLOCK=1 to make the hook reject native file tool calls.
In a session that already used HGP tools it asks the user first (HGP_HOOK_ASK=0 disables).

The hook judges the file being written (``tool_input.file_path``), not the session's cwd.
Decision order, first match wins:
  1. The target has a path segment exactly `.hgp`: deny (exit 2), always (even paused, in
     advisory, with HGP_HOOK_ASK=0, or when HGP is not enabled): .hgp/ is HGP-internal and
     user-controlled.
  2. HGP is not enabled for the repository: silent.
  3. The target is outside the session's checkout (another repo, another worktree, /tmp): silent,
     since HGP could not have recorded it. Global mode (HGP_GLOBAL_MODE) has no such limit.
  4. HGP mode is advisory or off (and HGP_HOOK_BLOCK does not enforce): silent.
  5. The level is block or strict (policy file, or HGP_HOOK_BLOCK: 0 advisory, strict, any other
     value block): deny (exit 2). Claude has only file tools here, so block and strict agree.
  6. The session already used HGP: ask (HGP_HOOK_ASK=0 disables). Otherwise a warning only.
A relative target is read against both the agent's cwd and the checkout root; it is in scope
unless it is outside the checkout both ways (steps 1 and 3 use every reading).
A missing or non-string target falls back to the cwd-based behaviour (steps 2 and 4 to 6).
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

    HGP_HOOK_BLOCK wins unless it is 0: an agent can write .hgp/mode, not this environment.
    """
    env = os.environ.get("HGP_HOOK_BLOCK")
    if env is not None and env != "0":
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

_LEVELS = ("advisory", "block", "strict")


def _env_level() -> str | None:
    """The level HGP_HOOK_BLOCK forces (trusted: it wins over the policy file), or None when unset.

    0 -> advisory; "strict" -> strict; any other value (1, block, a typo, empty) -> block, so an
    unexpected value fails toward enforcing.
    """
    env = os.environ.get("HGP_HOOK_BLOCK")
    if env is None:
        return None
    if env == "0":
        return "advisory"
    return "strict" if env == "strict" else "block"


def _level_of(text: str) -> str:
    """The level a .hgp/hook-policy text says: advisory, block or strict (unrecognised: advisory)."""
    reader = getattr(_hgp_paths(), "hook_policy_level", None)
    if reader is not None:
        return reader(text) or "advisory"
    stripped = text.strip()  # partial install without hgp_paths.py
    if stripped.startswith("{"):
        try:
            data: object = json.loads(stripped)
        except ValueError:
            return "advisory"
        fields = cast(dict[str, Any], data) if isinstance(data, dict) else {}
        if fields.get("block_mode") is not True:
            return "advisory"
        return "strict" if fields.get("block_shell") is True else "block"
    return "block" if stripped == "block" else "advisory"


def _block_level(start: Path) -> str:
    """advisory, block (native file tools) or strict (the shell too): HGP_HOOK_BLOCK, then the policy file."""
    forced = _env_level()
    if forced is not None:
        return forced
    paths = _hgp_paths()

    def shared_hgp_dir(root: Path) -> Path:
        if paths is not None:
            return Path(paths.shared_hgp_dir(root))
        return root / ".hgp"  # partial install without hgp_paths.py

    for parent in [start, *start.parents]:
        # Shared policy plus a checkout-local legacy one (written before storage was shared):
        # the stricter of the two counts, so upgrading never drops enforcement.
        candidates = dict.fromkeys([shared_hgp_dir(parent) / "hook-policy", parent / ".hgp" / "hook-policy"])
        found = [p for p in candidates if p.exists()]
        if found:
            return max((_level_of(p.read_text()) for p in found), key=_LEVELS.index)
        if (parent / ".git").exists():
            break
    return "advisory"


def _resolve_block_mode(start: Path) -> bool:
    """Whether native file tools are blocked: the level is block or strict."""
    return _block_level(start) != "advisory"


def _session_root(start: Path) -> Path:
    """The session's checkout root, as the server derives its project root.

    HGP_PROJECT_ROOT when it names a directory, else the checkout containing ``start``, else
    ``start`` itself (outside git the server's store is the working directory's).
    """
    reader = getattr(_hgp_paths(), "checkout_root", None)
    if reader is not None:
        root = reader(start)
        return Path(root) if root is not None else start
    env = os.environ.get("HGP_PROJECT_ROOT")  # partial install without hgp_paths.py
    if env and Path(env).is_dir():
        return Path(env).resolve()
    for parent in [start, *start.parents]:
        if (parent / ".git").exists():
            return parent
    return start


def _in_hgp_dir(target: Path) -> bool:
    """Whether ``target`` has a path segment exactly `.hgp` (the server's HGP_INTERNAL_PATH rule).

    Checked on the lexically normalised path and on the resolved one, so `a/../.hgp/x` and a
    symlink into .hgp count. The comparison ignores case: a case-insensitive filesystem would
    otherwise let `.HGP/mode` reach the same directory.
    """
    candidates = [Path(os.path.normpath(target))]
    try:
        candidates.append(target.resolve())
    except (OSError, RuntimeError, ValueError):
        pass
    return any(part.casefold() == ".hgp" for path in candidates for part in path.parts)


def _inside(target: Path, root: Path) -> bool:
    """Whether ``target`` is ``root`` or below it, after resolving symlinks (the tail may not exist yet)."""
    try:
        resolved, base = target.resolve(), root.resolve()
    except (OSError, RuntimeError, ValueError):
        return True  # cannot tell: act as if it were in scope
    return resolved == base or base in resolved.parents


def _targets(event: object, root: Path, cwd: Path) -> list[Path]:
    """The file(s) a native tool may write, from tool_input.file_path; empty when absent or not a string.

    A leading `~` is the home directory. An absolute path is one target. A relative path has two
    readings: the tool writes it against the agent's ``cwd``, the server resolves it against
    ``root``; the hook keeps both, so `../x` from <root>/sub (really <root>/x) is not mistaken
    for <root>/../x.
    """
    tool_input = cast(dict[str, Any], event).get("tool_input") if isinstance(event, dict) else None
    raw = cast(dict[str, Any], tool_input).get("file_path") if isinstance(tool_input, dict) else None
    if not isinstance(raw, str) or not raw:
        return []
    path = Path(os.path.expanduser(raw))
    if path.is_absolute():
        return [path]
    return list(dict.fromkeys([cwd / path, root / path]))


def _event_dir(event: dict[str, Any], start: Path) -> Path:
    """The agent's cwd, where its tools resolve a relative path; ``start`` when the event has none."""
    cwd = event.get("cwd")
    return Path(cwd) if isinstance(cwd, str) and cwd and Path(cwd).is_absolute() else start


def _main() -> None:
    try:
        raw: object = json.loads(sys.stdin.read())
    except json.JSONDecodeError:
        sys.exit(0)
    if not isinstance(raw, dict):
        sys.exit(0)
    event = cast(dict[str, Any], raw)
    start = _event_cwd(event)  # one directory for the gate and the policy
    tool_name = event.get("tool_name", "")
    if tool_name not in HGP_TOOLS:
        sys.exit(0)

    root = _session_root(start)
    targets = _targets(event, root, _event_dir(event, start))
    if any(_in_hgp_dir(t) for t in targets):
        print(
            f"[HGP] Blocked: `{tool_name}` targets `.hgp/`, which is HGP-internal and user-controlled "
            "(database, mode, hook-policy). Agents must not write it, with a native tool or an HGP one; "
            "the user changes HGP settings with the `hgp` CLI in their own terminal.",
            file=sys.stderr,
        )
        sys.exit(2)
    if not _enabled(start):
        sys.exit(0)
    # HGP records only files under the session's checkout; anywhere else the hook has no say.
    # In scope unless every reading of the target leaves the checkout.
    if targets and not os.environ.get("HGP_GLOBAL_MODE") and not any(_inside(t, root) for t in targets):
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
