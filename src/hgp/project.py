"""Project root detection for HGP file-scoped operations."""

from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import Path

# Re-exported: single implementation shared with the standalone hook scripts.
from hgp.hooks.hgp_paths import find_storage_root as find_storage_root
from hgp.hooks.hgp_paths import parse_hook_policy as parse_hook_policy


class ProjectRootError(RuntimeError):
    """Raised when no project root can be determined."""


class PathOutsideRootError(ValueError):
    """Raised when a file_path resolves outside the project root."""


def find_project_root(start: Path) -> Path:
    """Return project root by env var override or .git traversal.

    Resolution order:
    1. HGP_PROJECT_ROOT environment variable (if set and is a directory)
    2. Walk up from `start` to find nearest .git directory
    3. Raise ProjectRootError if neither found
    """
    env = os.environ.get("HGP_PROJECT_ROOT")
    if env:
        p = Path(env).resolve()
        if p.is_dir():
            return p
        raise ProjectRootError(f"HGP_PROJECT_ROOT={env!r} is not a directory")

    current = start.resolve()
    for candidate in [current, *current.parents]:
        if (candidate / ".git").exists():
            return candidate

    raise ProjectRootError(
        f"No .git directory found above {current}. "
        "Set HGP_PROJECT_ROOT to specify the project root explicitly."
    )


def read_worktree_id(gitdir: Path) -> str | None:
    """Read HGP's identifier for this Git worktree registration, if assigned."""
    try:
        return (gitdir / "hgp-worktree-id").read_text(encoding="ascii").strip()
    except FileNotFoundError:
        return None


def ensure_worktree_id(gitdir: Path) -> str:
    """Assign an ID that travels with the registration when Git moves the worktree.

    Publish a complete marker atomically. If two servers start together, both use
    the winner's ID. A deleted and recreated registration gets a different ID.
    """
    if existing := read_worktree_id(gitdir):
        return existing
    with tempfile.NamedTemporaryFile(mode="w", encoding="ascii", dir=gitdir, delete=False) as stream:
        temp = Path(stream.name)
        stream.write(uuid.uuid4().hex + "\n")
    try:
        try:
            (gitdir / "hgp-worktree-id").hardlink_to(temp)
        except FileExistsError:
            pass
    finally:
        temp.unlink(missing_ok=True)
    assigned = read_worktree_id(gitdir)
    if not assigned:
        raise RuntimeError(f"Missing worktree ID after initialization: {gitdir}")
    return assigned


def current_branch(checkout_root: Path) -> str | None:
    """Return the branch checked out at ``checkout_root``.

    Reads ``HEAD`` directly (no git subprocess). Returns None for a detached
    HEAD or when the git metadata cannot be read.
    """
    git_path = checkout_root / ".git"
    try:
        if git_path.is_file():
            pointer = git_path.read_text(encoding="utf-8").strip()
            if not pointer.startswith("gitdir:"):
                return None
            gitdir = Path(pointer[len("gitdir:"):].strip())
            if not gitdir.is_absolute():
                gitdir = checkout_root / gitdir
        else:
            gitdir = git_path
        head = (gitdir / "HEAD").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None
    prefix = "ref: refs/heads/"
    return head[len(prefix):] if head.startswith(prefix) else None


def assert_within_root(file_path: Path, root: Path) -> None:
    """Raise PathOutsideRootError if file_path is not under root.

    Both paths are resolved to absolute form before comparison (symlinks followed).
    """
    resolved = file_path.resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError:
        raise PathOutsideRootError(
            f"{file_path} is outside project root {root}"
        )


def canonical_file_path(file_path: str | Path, root: Path) -> str:
    """Return the canonical absolute path string for storage and query.

    Resolves symlinks and normalizes . / .. so that the same file
    is always stored/queried under one key regardless of how the
    caller expressed the path.

    Raises PathOutsideRootError if the resolved path is outside root.
    """
    resolved = Path(file_path).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError:
        raise PathOutsideRootError(f"{file_path} is outside project root {root}")
    return str(resolved)
