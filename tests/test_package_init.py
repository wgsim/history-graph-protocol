"""Regression test: hgp.__version__ must not crash in bare source-tree imports."""

from __future__ import annotations

import importlib
import sys
from importlib.metadata import PackageNotFoundError
from unittest.mock import patch


def test_version_fallback_without_metadata():
    """__version__ falls back to 'unknown' on a true first import without dist metadata."""
    # Remove any cached hgp module so the import below is a genuine first import.
    sys.modules.pop("hgp", None)
    try:
        with patch(
            "importlib.metadata.version",
            side_effect=PackageNotFoundError("history-graph-protocol"),
        ):
            import hgp  # noqa: PLC0415
            assert hgp.__version__ == "unknown"
    finally:
        # Always restore a clean hgp so later tests see the real version.
        sys.modules.pop("hgp", None)
        import hgp  # noqa: PLC0415
        importlib.reload(hgp)
