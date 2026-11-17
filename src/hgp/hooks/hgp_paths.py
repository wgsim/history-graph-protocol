"""Shared ``.hgp`` storage resolution for the HGP server and hook scripts.

Single source of truth for linked-worktree storage discovery. Stdlib only:
``hgp install`` copies this file next to the installed hook scripts, which load
it by file path, and ``hgp.project`` re-exports :func:`find_storage_root`.
"""
from __future__ import annotations

from pathlib import Path


def find_storage_root(project_root: Path) -> Path:
    """Return the root that owns shared HGP storage.

    Linked worktrees keep a ``.git`` pointer file in the checkout. Its gitdir
    contains a ``commondir`` entry pointing at the main repository's git
    directory. User file validation still uses ``project_root``; only HGP's
    internal storage is shared through this helper. Any inconsistency in the
    pointer chain falls back to ``project_root``.
    """
    git_path = project_root / ".git"
    if not git_path.is_file():
        return project_root

    try:
        pointer = git_path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return project_root
    if not pointer.startswith("gitdir:"):
        return project_root

    raw_gitdir = pointer[len("gitdir:"):].strip()
    if not raw_gitdir:
        return project_root
    gitdir = Path(raw_gitdir)
    if not gitdir.is_absolute():
        gitdir = project_root / gitdir
    try:
        gitdir = gitdir.resolve()
    except (OSError, RuntimeError):
        return project_root
    if not gitdir.is_dir():
        return project_root

    try:
        raw_common = (gitdir / "commondir").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return project_root
    if not raw_common:
        return project_root

    common_git = Path(raw_common)
    if not common_git.is_absolute():
        common_git = gitdir / common_git
    try:
        common_git = common_git.resolve()
    except (OSError, RuntimeError):
        return project_root
    if not (
        common_git.is_dir()
        and (common_git / "HEAD").is_file()
        and (common_git / "config").is_file()
        and (common_git / "objects").is_dir()
        and (common_git / "worktrees").is_dir()
    ):
        return project_root

    worktrees_dir = common_git / "worktrees"
    try:
        if gitdir.parent != worktrees_dir:
            return project_root
        backpointer = gitdir / "gitdir"
        if not backpointer.is_file():
            return project_root
        raw_backpointer = backpointer.read_text(encoding="utf-8").strip()
        resolved_backpointer = Path(raw_backpointer)
        if not resolved_backpointer.is_absolute():
            resolved_backpointer = gitdir / resolved_backpointer
        resolved_backpointer = resolved_backpointer.resolve()
        resolved_git_path = git_path.resolve()
        if resolved_backpointer != resolved_git_path:
            return project_root

        storage_root = common_git.parent
        main_git = storage_root / ".git"
        if not main_git.is_dir() or main_git.resolve() != common_git:
            return project_root
        return storage_root
    except (OSError, UnicodeError, RuntimeError, ValueError):
        return project_root


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
