"""hktvmall-mcp: MCP access to an HKTVmall (Hong Kong) basket: search, basket edits with read-back; never pays."""

from importlib.metadata import PackageNotFoundError, version

try:
    # The release tool bumps pyproject.toml only; read the installed distribution.
    __version__ = version("hktvmall-mcp")
except PackageNotFoundError:  # pragma: no cover - source tree without an install
    __version__ = "0+unknown"
