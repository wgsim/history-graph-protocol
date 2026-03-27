"""Project root detection for HGP file-scoped operations."""

from __future__ import annotations

import os
from pathlib import Path


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
