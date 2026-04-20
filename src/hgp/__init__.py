"""History Graph Protocol — crash-resilient semantic layer over MCP."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("history-graph-protocol")
except PackageNotFoundError:
    __version__ = "unknown"
