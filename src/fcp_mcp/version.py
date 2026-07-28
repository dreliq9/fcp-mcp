from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

FALLBACK_VERSION = "0.3.0"


def distribution_version(name: str) -> str:
    try:
        return version(name)
    except PackageNotFoundError:
        return "unknown"


def package_version() -> str:
    installed = distribution_version("fcp-mcp")
    return FALLBACK_VERSION if installed == "unknown" else installed
