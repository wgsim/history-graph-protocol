"""Regression test: hgp.__version__ must not crash in bare source-tree imports."""

from __future__ import annotations

import importlib
from importlib.metadata import PackageNotFoundError
from unittest.mock import patch

import hgp


def test_version_fallback_without_metadata():
    """__version__ must fall back to 'unknown' when dist metadata is absent."""
    with patch(
        "importlib.metadata.version",
        side_effect=PackageNotFoundError("history-graph-protocol"),
    ):
        importlib.reload(hgp)
        assert hgp.__version__ == "unknown"

    # Restore the real version so other tests see a consistent state.
    importlib.reload(hgp)
