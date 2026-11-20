"""Shared helpers for the agy shell-change tracking and subagent hooks. Stdlib only.

``hgp install --agy`` copies this file next to the hook scripts, which import it by name (and fall
back to the hgp package). The three tracking hooks pass state to each other through small JSON
files in a per-user directory, ``<tmp>/hgp-agy-<uid>`` (mode 0700), never a bare /tmp, so other
users can neither read nor plant them:

  bash-<conversation>__<step>.json   a ``git status`` baseline, written by PreToolUse before a
                                     mutating shell command and consumed by PostToolUse
  post-<conversation>__<step>.json   the new status entries PostToolUse found, collected (and
                                     deleted) by PostInvocation into one message

Conversation ids and steps are sanitised into the file name (``[A-Za-z0-9.-]``, at most 80
characters). Tracking is status-level only: it cannot see a re-edit of a file that was already
modified before the command.

Subagent propagation reads ``.hgp/context-<conversationId>.json`` (written by ``hgp_set_context``).
The agent can write that file, so the conversation id is validated before it names a file, the
file must resolve inside the ``.hgp`` directory, and the root op id is validated before it is
put into a prompt.
"""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import tempfile
import time
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import Any, cast

MAX_CHANGED = 200  # status entries kept per command
STALE_SECONDS = 6 * 3600  # state older than this belongs to a command that never finished
MAX_CLEANUP = 20  # stale files removed per run
GIT_TIMEOUT = 6.0  # seconds; the hooks themselves time out at 10 to 15

ROOT_PREFIX = "[HGP] Session root op:"
SKIP_TYPES = frozenset({"research", "browser", "image-generator"})  # subagents that never use HGP
SKIP_WORKSPACES = frozenset({"branch", "share"})  # they work in another checkout

_UNSAFE = re.compile(r"[^A-Za-z0-9.-]")
_STATE_FILE = re.compile(r"(?:bash|post)-[A-Za-z0-9.-]+__[A-Za-z0-9.-]+\.json(?:\.\d+\.tmp)?")
_CONVERSATION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
_OP_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}")


# ── HGP storage and mode (the hooks are standalone, so these mirror pre_tool_use_hgp.py) ──

def load_hgp_paths() -> Any:
    """Return hgp_paths: installed beside the hooks, else from the hgp package, else None."""
    try:
        import hgp_paths  # pyright: ignore[reportMissingImports]
    except ImportError:
        try:
            from hgp.hooks import hgp_paths
        except ImportError:
            return None
    return hgp_paths


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
    reader = getattr(load_hgp_paths(), "checkout_root", None)
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


def enabled(start: Path) -> bool:
    """Whether HGP is enabled for ``start`` (its storage .hgp/ exists, or global mode)."""
    check = getattr(load_hgp_paths(), "hgp_enabled", None)
    if check is not None:
        return bool(check(start))
    if os.environ.get("HGP_GLOBAL_MODE"):
        return True
    for parent in [start, *start.parents]:  # no hgp_paths: walk up to the checkout root
        if (parent / ".git").exists():
            return (parent / ".hgp").is_dir()
    return (start / ".hgp").is_dir()


def paused(start: Path) -> bool:
    """Whether HGP mode is advisory or off (HGP tools then write nothing).

    HGP_HOOK_BLOCK wins unless it is 0: an agent can write .hgp/mode, not the hook's environment.
    """
    env = os.environ.get("HGP_HOOK_BLOCK")
    if env is not None and env != "0":
        return False
    mode = getattr(load_hgp_paths(), "hgp_mode", None)
    return mode is not None and mode(start) in ("advisory", "off")


def hgp_dirs(root: Path) -> list[Path]:
    """The .hgp directories that may hold a session context: the checkout's shared store, then
    ``~/.hgp`` in global mode (the server records there when HGP_GLOBAL_MODE is set)."""
    shared = getattr(load_hgp_paths(), "shared_hgp_dir", None)
    dirs = [Path(shared(root)) if shared is not None else root / ".hgp"]
    if os.environ.get("HGP_GLOBAL_MODE"):
        dirs.append(Path.home() / ".hgp")
    return dirs


# ── Ids and the per-user state directory ──

def safe_id(value: object) -> str | None:
    """A conversation id or step as a file-name part: ``[A-Za-z0-9.-]``, at most 80 characters.

    Anything else becomes ``-``, so an id can never name another directory. None for a value that
    is not a non-empty string or an integer.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        value = str(value)
    if not isinstance(value, str) or not value:
        return None
    return _UNSAFE.sub("-", value)[:80]


def conversation_id(value: object) -> str | None:
    """A conversation id that is safe to echo and to use in a file name as it is, else None.

    Letters, digits, ``.``, ``_`` and ``-``, starting with a letter or digit, at most 100 characters,
    and no ``..``. agy's ids are UUIDs.
    """
    if isinstance(value, str) and _CONVERSATION_ID.fullmatch(value) and ".." not in value:
        return value
    return None


def state_dir() -> Path | None:
    """The per-user state directory, created 0700; None when it cannot be made safely.

    Refused when the path is a symlink, is not ours, or cannot be tightened to 0700.
    """
    uid = os.getuid() if hasattr(os, "getuid") else None
    path = Path(tempfile.gettempdir()) / f"hgp-agy-{uid if uid is not None else 'user'}"
    try:
        path.mkdir(mode=0o700, exist_ok=True)
        info = path.lstat()
    except OSError:
        return None
    if not stat.S_ISDIR(info.st_mode) or (uid is not None and info.st_uid != uid):
        return None
    if info.st_mode & 0o077:
        try:
            path.chmod(0o700)
        except OSError:
            return None
    return path


def marker_path(directory: Path, conversation: object, step: object) -> Path | None:
    conv, number = safe_id(conversation), safe_id(step)
    return None if conv is None or number is None else directory / f"bash-{conv}__{number}.json"


def post_path(directory: Path, conversation: object, step: object) -> Path | None:
    conv, number = safe_id(conversation), safe_id(step)
    return None if conv is None or number is None else directory / f"post-{conv}__{number}.json"


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


def write_json(path: Path, data: object) -> bool:
    """Write ``data`` as JSON, mode 0600, atomically; never through a symlink."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        return False
    return True


def read_json(path: Path) -> object:
    """The JSON in ``path``, or None when it is missing, not a regular file, too large or not JSON."""
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > 4_000_000:
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return None


def remove(path: Path) -> None:
    """Delete a state file (a symlink is removed, never followed); a missing file is fine."""
    try:
        path.unlink()
    except OSError:
        pass


def cleanup_stale(directory: Path) -> None:
    """Delete state files older than STALE_SECONDS, at most MAX_CLEANUP per run."""
    limit = time.time() - STALE_SECONDS
    removed = 0
    try:
        names = list(directory.iterdir())
    except OSError:
        return
    for path in names:
        if removed >= MAX_CLEANUP:
            return
        try:
            if _STATE_FILE.fullmatch(path.name) and path.lstat().st_mtime < limit:
                path.unlink()
                removed += 1
        except OSError:
            continue


def str_list(value: object) -> list[str] | None:
    """``value`` when it is a list of strings, else None (state files are untrusted input)."""
    if not isinstance(value, list):
        return None
    items = cast(list[Any], value)
    return cast(list[str], items) if all(isinstance(item, str) for item in items) else None


# ── git status ──

def is_hgp_entry(line: str) -> bool:
    """Whether a ``git status --porcelain`` line is about .hgp/ (HGP's own storage)."""
    paths = [part.strip().strip('"') for part in line[3:].split(" -> ")]
    return any(segment.casefold() == ".hgp" for path in paths for segment in PurePosixPath(path).parts)


def git_status(root: Path) -> list[str] | None:
    """``git status --porcelain -uall`` lines for the checkout at ``root``, without .hgp entries.

    None when git fails or times out (the hooks then track nothing). Optional locks and the
    fsmonitor hook are off: a hook must not touch the index or run repository-configured programs.
    """
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
    try:
        result = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "status", "--porcelain", "-uall"],
            cwd=root, capture_output=True, text=True, timeout=GIT_TIMEOUT, check=False, env=env,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return None
    if result.returncode != 0:
        return None
    return [line for line in result.stdout.splitlines() if line and not is_hgp_entry(line)]


def write_baseline(conversation: object, step: object, root: Path, pattern: str) -> None:
    """Record ``root``'s git status as the baseline for the shell command at this step.

    Does nothing without a usable conversation id and step, outside a git checkout, or when the
    per-user state directory is unavailable.
    """
    if safe_id(conversation) is None or safe_id(step) is None:
        return
    baseline = git_status(root)
    if baseline is None:
        return
    directory = state_dir()
    if directory is None:
        return
    cleanup_stale(directory)
    path = marker_path(directory, conversation, step)
    if path is not None:
        write_json(path, {"pattern": pattern, "baseline": {str(root): baseline}})


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
