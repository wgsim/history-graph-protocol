"""agy-specific helpers for the shell-change tracking and subagent hooks. Stdlib only.

``hgp install --agy`` copies this file next to the hook scripts, which import it by name (and fall
back to the hgp package). It builds on ``hgp_paths`` (installed beside it), where the parts shared
with the Claude and Codex hooks live: the per-user state directory, the ``git status`` baseline and
diff, the scrubbed git environment and the message. The three agy tracking hooks pass state to each
other through files in ``<tmp>/hgp-agy-<uid>`` (mode 0700):

  bash-<conversation>__<step>.json   a ``git status`` baseline, written by PreToolUse before a
                                     mutating shell command and consumed by PostToolUse
  post-<conversation>__<step>.json   the new status entries PostToolUse found, collected (and
                                     deleted) by PostInvocation into one message

Tracking is status-level only: it cannot see a re-edit of a file that was already modified before
the command.

Subagent propagation reads ``.hgp/context-<conversationId>.json`` (written by ``hgp_set_context``).
The agent can write that file, so the conversation id is validated before it names a file, the
file must resolve inside the ``.hgp`` directory, and the root op id is validated before it is
put into a prompt.
"""
from __future__ import annotations

import os
import re
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from hgp.hooks import hgp_paths
else:
    try:
        import hgp_paths
    except ImportError:  # run from the hgp package, not from an installed copy
        from hgp.hooks import hgp_paths

MAX_CHANGED = hgp_paths.TRACK_MAX_CHANGED  # status entries kept per command
ROOT_PREFIX = "[HGP] Session root op:"
SKIP_TYPES = frozenset({"research", "browser", "image-generator"})  # subagents that never use HGP
SKIP_WORKSPACES = frozenset({"branch", "share"})  # they work in another checkout

_CONVERSATION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
_OP_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}")

# The shared helpers, under the names the agy hooks use.
safe_id = hgp_paths.safe_id
str_list = hgp_paths.str_list
read_json = hgp_paths.track_read_json
write_json = hgp_paths.track_write_json
remove = hgp_paths.track_remove
cleanup_stale = hgp_paths.track_cleanup
is_hgp_entry = hgp_paths.is_hgp_entry
git_status = hgp_paths.git_status
track_message = hgp_paths.track_message
new_entries = hgp_paths.track_new_entries


# ── HGP storage and mode ──

def event_cwd(event: dict[str, Any]) -> Path:
    """HGP_PROJECT_ROOT when it names a directory (as in the server), else the event's cwd."""
    env = os.environ.get("HGP_PROJECT_ROOT")
    if env and Path(env).is_dir():
        return Path(env).resolve()
    cwd = event.get("cwd")
    if isinstance(cwd, str) and cwd:
        return Path(cwd)
    workspaces = event.get("workspacePaths")
    if isinstance(workspaces, list) and workspaces:
        first = cast(list[Any], workspaces)[0]
        if isinstance(first, str) and first:
            return Path(first)
    return Path.cwd()


def session_root(start: Path) -> Path:
    """The checkout root the server treats as the project, else ``start`` itself (outside git)."""
    root = hgp_paths.checkout_root(start)
    return Path(root) if root is not None else start


def enabled(start: Path) -> bool:
    """Whether HGP is enabled for ``start`` (its storage .hgp/ exists, or global mode)."""
    return hgp_paths.hgp_enabled(start)


def paused(start: Path) -> bool:
    """Whether HGP mode is advisory or off (HGP tools then write nothing).

    HGP_HOOK_BLOCK wins unless it is 0: an agent can write .hgp/mode, not the hook's environment.
    """
    return hgp_paths.hook_paused(start)


def hgp_dirs(root: Path) -> list[Path]:
    """The .hgp directories that may hold a session context: the checkout's shared store, then
    ``~/.hgp`` in global mode (the server records there when HGP_GLOBAL_MODE is set)."""
    dirs = [Path(hgp_paths.shared_hgp_dir(root))]
    if os.environ.get("HGP_GLOBAL_MODE"):
        dirs.append(Path.home() / ".hgp")
    return dirs


# ── Ids and the per-user state directory ──

def conversation_id(value: object) -> str | None:
    """A conversation id that is safe to echo and to use in a file name as it is, else None.

    Letters, digits, ``.``, ``_`` and ``-``, starting with a letter or digit, at most 100 characters,
    and no ``..``. agy's ids are UUIDs.
    """
    if isinstance(value, str) and _CONVERSATION_ID.fullmatch(value) and ".." not in value:
        return value
    return None


def state_dir() -> Path | None:
    """The per-user state directory ``<tmp>/hgp-agy-<uid>``, created 0700; None when it cannot be used."""
    return hgp_paths.track_dir("agy")


def _name(conversation: object, step: object) -> str | None:
    conv, number = safe_id(conversation), safe_id(step)
    return None if conv is None or number is None else f"{conv}__{number}"


def marker_path(directory: Path, conversation: object, step: object) -> Path | None:
    name = _name(conversation, step)
    return None if name is None else directory / f"bash-{name}.json"


def post_path(directory: Path, conversation: object, step: object) -> Path | None:
    name = _name(conversation, step)
    return None if name is None else directory / f"post-{name}.json"


def post_files(directory: Path, conversation: object) -> list[Path]:
    """The conversation's post-state files, in step order (numeric, so step 3 comes before 12)."""
    conv = safe_id(conversation)
    if conv is None:
        return []
    found: list[tuple[int, str, Path]] = []
    prefix = f"post-{conv}__"
    try:
        names = list(directory.iterdir())
    except OSError:
        return []
    for path in names:
        name = path.name
        if not name.startswith(prefix) or not name.endswith(".json"):
            continue
        step = name[len(prefix):-len(".json")]
        found.append((int(step) if step.isdigit() else 1 << 62, step, path))
    return [path for _, _, path in sorted(found)]


def write_baseline(conversation: object, step: object, root: Path, pattern: str) -> None:
    """Record ``root``'s git status as the baseline for the shell command at this step.

    Does nothing without a usable conversation id and step, outside a git checkout, or when the
    per-user state directory is unavailable.
    """
    name = _name(conversation, step)
    if name is not None:
        hgp_paths.track_record("agy", name, root, pattern)


# ── Subagents ──

def _context_root_op(hgp_dir: Path, conversation: str) -> str | None:
    """The root op id in ``hgp_dir/context-<conversation>.json``, or None.

    The file must resolve to somewhere inside ``hgp_dir`` (a symlink out of it is ignored), and
    the id must look like an op id: the agent can write this file, so it is not trusted as text.
    """
    try:
        base = hgp_dir.resolve()
        path = (hgp_dir / f"context-{conversation}.json").resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    if base not in path.parents:
        return None
    data = read_json(path)
    value = cast(dict[str, Any], data).get("root_op_id") if isinstance(data, dict) else None
    return value if isinstance(value, str) and _OP_ID.fullmatch(value) else None


def subagent_prompt(args: dict[str, Any], conversation: object, dirs: Iterable[Path]) -> str | None:
    """The subagent's ``Prompt`` with the parent session's root op id in front, or None to leave it.

    None when there is no usable prompt, the prompt already starts with the prefix, the subagent
    type or workspace never uses HGP (SKIP_TYPES, SKIP_WORKSPACES), the conversation id is not safe
    to look up, or no context file holds a valid root op id.
    """
    prompt = args.get("Prompt")
    if not isinstance(prompt, str) or not prompt.strip() or prompt.lstrip().startswith(ROOT_PREFIX):
        return None
    type_name, workspace = args.get("TypeName"), args.get("Workspace")
    if isinstance(type_name, str) and type_name.casefold() in SKIP_TYPES:
        return None
    if isinstance(workspace, str) and workspace.casefold() in SKIP_WORKSPACES:
        return None
    conv = conversation_id(conversation)
    if conv is None:
        return None
    for hgp_dir in dirs:
        op = _context_root_op(hgp_dir, conv)
        if op is not None:
            return (
                f'{ROOT_PREFIX} {op}. Use parent_op_ids=["{op}"] for all hgp_* calls in this subagent.\n\n{prompt}'
            )
    return None
