"""Shared ``.hgp`` storage resolution for the HGP server and hook scripts.

Single source of truth for linked-worktree storage discovery. Stdlib only:
``hgp install`` copies this file next to the installed hook scripts, which load
it by file path, and ``hgp.project`` re-exports :func:`find_storage_root`.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, cast


def find_linked_worktree_gitdir(project_root: Path) -> Path | None:
    """Return the validated Git administrative directory of a linked worktree.

    Linked worktrees keep a ``.git`` pointer file in the checkout. Its gitdir
    contains a ``commondir`` entry pointing at the main repository's git
    directory. User file validation still uses ``project_root``; only HGP's
    internal storage is shared through this helper. Any inconsistency in the
    pointer chain returns None.
    """
    git_path = project_root / ".git"
    if not git_path.is_file():
        return None

    try:
        pointer = git_path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None
    if not pointer.startswith("gitdir:"):
        return None

    raw_gitdir = pointer[len("gitdir:"):].strip()
    if not raw_gitdir:
        return None
    gitdir = Path(raw_gitdir)
    if not gitdir.is_absolute():
        gitdir = project_root / gitdir
    try:
        gitdir = gitdir.resolve()
    except (OSError, RuntimeError):
        return None
    if not gitdir.is_dir():
        return None

    try:
        raw_common = (gitdir / "commondir").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None
    if not raw_common:
        return None

    common_git = Path(raw_common)
    if not common_git.is_absolute():
        common_git = gitdir / common_git
    try:
        common_git = common_git.resolve()
    except (OSError, RuntimeError):
        return None
    if not (
        common_git.is_dir()
        and (common_git / "HEAD").is_file()
        and (common_git / "config").is_file()
        and (common_git / "objects").is_dir()
        and (common_git / "worktrees").is_dir()
    ):
        return None

    worktrees_dir = common_git / "worktrees"
    try:
        if gitdir.parent != worktrees_dir:
            return None
        backpointer = gitdir / "gitdir"
        if not backpointer.is_file():
            return None
        raw_backpointer = backpointer.read_text(encoding="utf-8").strip()
        resolved_backpointer = Path(raw_backpointer)
        if not resolved_backpointer.is_absolute():
            resolved_backpointer = gitdir / resolved_backpointer
        resolved_backpointer = resolved_backpointer.resolve()
        resolved_git_path = git_path.resolve()
        if resolved_backpointer != resolved_git_path:
            return None

        storage_root = common_git.parent
        main_git = storage_root / ".git"
        if not main_git.is_dir() or main_git.resolve() != common_git:
            return None
        return gitdir
    except (OSError, UnicodeError, RuntimeError, ValueError):
        return None


def find_storage_root(project_root: Path) -> Path:
    """Return the root that owns shared HGP storage."""
    gitdir = find_linked_worktree_gitdir(project_root)
    return gitdir.parent.parent.parent if gitdir is not None else project_root


def checkout_value_from_metadata(raw: str | None, key: str) -> str | None:
    """Return a string field from an operation's checkout provenance."""
    import json

    try:
        meta: object = json.loads(raw or "null")
    except (TypeError, ValueError):
        return None
    if not isinstance(meta, dict):
        return None
    checkout = cast(dict[str, Any], meta).get("hgp_checkout")
    if not isinstance(checkout, dict):
        return None
    value = cast(dict[str, Any], checkout).get(key)
    return value if isinstance(value, str) else None


def parse_hook_policy(text: str) -> bool | None:
    """Return block mode from ``.hgp/hook-policy`` content, or None if unrecognised.

    Current format is JSON (``{"block_mode": true}``; other keys are ignored).
    Legacy plain-text files containing ``block`` / ``advisory`` are still read.
    """
    import json

    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            data: object = json.loads(stripped)
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
        block = cast(dict[str, Any], data).get("block_mode")
        return block if isinstance(block, bool) else None
    if stripped in ("block", "advisory"):
        return stripped == "block"
    return None


def hook_policy_level(text: str) -> str | None:
    """Return the hook level from ``.hgp/hook-policy`` content, or None if unrecognised.

    The level is "advisory", "block" (native file tools are blocked) or "strict" (the shell is
    blocked as well). JSON ``{"block_mode": bool, "block_shell": bool}``: ``block_mode`` false is
    advisory (``block_shell`` alone does nothing); true is strict when ``block_shell`` is true
    and block otherwise, so a file written before the levels reads as block. Legacy plain text
    ``block`` / ``advisory`` is still read. :func:`parse_hook_policy` keeps returning
    ``block_mode`` for the hooks that do not know the levels.
    """
    import json

    block = parse_hook_policy(text)
    if block is None:
        return None
    if not block:
        return "advisory"
    stripped = text.strip()
    if stripped.startswith("{"):
        data: object = json.loads(stripped)  # parse_hook_policy accepted it, so it is a JSON object
        if isinstance(data, dict) and cast(dict[str, Any], data).get("block_shell") is True:
            return "strict"
    return "block"


def shared_hgp_dir(root: Path) -> Path:
    """Return the ``.hgp`` directory HGP uses for a checkout rooted at ``root``."""
    return find_storage_root(root) / ".hgp"


def find_hgp_dir(start: Path) -> Path | None:
    """Return the nearest ``.hgp`` directory for hooks, walking up from ``start``.

    A git checkout root (``.git`` dir or worktree pointer file) maps to its
    shared storage, so a linked worktree resolves to the main repository's
    ``.hgp``. A bare ``.hgp`` directory below the checkout root still wins, as
    before. The returned directory may not exist yet; callers handle that.
    """
    for parent in [start, *start.parents]:
        if (parent / ".git").exists():
            return shared_hgp_dir(parent)
        candidate = parent / ".hgp"
        if candidate.is_dir():
            return candidate
    return None


def hgp_enabled(start: Path) -> bool:
    """Return whether HGP is enabled for ``start``.

    Enabled when its storage ``.hgp/`` exists, or when the checkout itself has a
    legacy ``.hgp/`` (a linked worktree's store from before storage was shared),
    so upgrading never silently drops a worktree's policy. Always enabled in
    global mode (``HGP_GLOBAL_MODE``), as in the server. Outside git only
    ``start/.hgp`` counts, matching the server's cwd-local store: an ancestor's
    ``.hgp`` (such as ``~/.hgp``) belongs to another directory. Like the
    server, ``HGP_PROJECT_ROOT`` (when it names a directory) replaces ``start``
    as the project root, with no walk up.
    """
    if os.environ.get("HGP_GLOBAL_MODE"):
        return True  # the server records to ~/.hgp in every directory
    root = checkout_root(start)
    if root is None:
        return (start / ".hgp").is_dir()
    return shared_hgp_dir(root).is_dir() or (root / ".hgp").is_dir()


def checkout_root(start: Path) -> Path | None:
    """HGP_PROJECT_ROOT when it names a directory, else the enclosing checkout root (None outside git).

    This is the root the server treats as the project: the files HGP can record live under it.
    """
    env = os.environ.get("HGP_PROJECT_ROOT")
    if env and Path(env).is_dir():
        return Path(env).resolve()
    for parent in [start, *start.parents]:
        if (parent / ".git").exists():
            return parent
    return None


def hgp_mode(start: Path) -> str:
    """Return the server's HGP mode for ``start``: "on", "advisory" or "off".

    Read from the storage ``.hgp/mode`` like the server; global mode is always "on".
    In "advisory" and "off" HGP tools return without writing files.
    """
    if os.environ.get("HGP_GLOBAL_MODE"):
        return "on"
    root = checkout_root(start)
    mode_file = (shared_hgp_dir(root) if root is not None else start / ".hgp") / "mode"
    try:
        value = mode_file.read_text().strip()
    except (OSError, UnicodeError):
        return "on"
    return value if value in ("on", "advisory", "off") else "on"
