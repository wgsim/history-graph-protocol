"""Project root detection for HGP file-scoped operations."""

from __future__ import annotations

import logging
import os
import re
import tempfile
import uuid
from pathlib import Path

# Re-exported: single implementation shared with the standalone hook scripts.
from hgp.hooks.hgp_paths import find_storage_root as find_storage_root
from hgp.hooks.hgp_paths import parse_hook_policy as parse_hook_policy

_log = logging.getLogger(__name__)

_WORKTREE_ID_FILE = "hgp-worktree-id"
_WORKTREE_ID_RE = re.compile(r"[0-9a-f]{32}")


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
    """Read HGP's identifier for this Git worktree registration, if assigned.

    An unreadable or malformed marker reads as None: a damaged file in one
    registration must not stop HGP in every checkout of the repository.
    """
    try:
        value = (gitdir / _WORKTREE_ID_FILE).read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        return None
    return value if _WORKTREE_ID_RE.fullmatch(value) else None


def ensure_worktree_id(gitdir: Path) -> str | None:
    """Assign an ID that travels with the registration when Git moves the worktree.

    Publish a complete marker atomically. If two servers start together, both use
    the winner's ID. A deleted and recreated registration gets a different ID.
    Returns None, so operations keep path-only provenance, when the marker is
    damaged (it is never replaced) or cannot be written, e.g. a read-only gitdir.
    """
    if existing := read_worktree_id(gitdir):
        return existing
    marker = gitdir / _WORKTREE_ID_FILE
    if not (marker.exists() or marker.is_symlink()):
        try:
            _publish_worktree_id(marker)
        except OSError as exc:
            _log.warning("cannot assign worktree ID in %s: %s", gitdir, exc)
            return None
    assigned = read_worktree_id(gitdir)
    if assigned is None:
        _log.warning("ignoring damaged worktree ID marker %s", marker)
    return assigned


def _publish_worktree_id(marker: Path) -> None:
    value = uuid.uuid4().hex + "\n"
    with tempfile.NamedTemporaryFile(mode="w", encoding="ascii", dir=marker.parent, delete=False) as stream:
        temp = Path(stream.name)
        stream.write(value)
    try:
        try:
            marker.hardlink_to(temp)
        except FileExistsError:
            pass
        except OSError:
            # No hard links on this filesystem: create exclusively instead. A
            # concurrent reader may see a partial marker and fall back to None.
            try:
                fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            except FileExistsError:
                return
            with os.fdopen(fd, "w", encoding="ascii") as out:
                out.write(value)
    finally:
        temp.unlink(missing_ok=True)


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
