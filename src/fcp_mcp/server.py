"""Import-safe public boundary for the fcp-mcp server runtime."""

from __future__ import annotations

import importlib
import sys
from types import ModuleType
from typing import Any

from .platform_support import require_macos

_RUNTIME_MODULE = "fcp_mcp._server_runtime"
_runtime: ModuleType | None = None


def _load_runtime() -> ModuleType:
    global _runtime
    if _runtime is None:
        _runtime = importlib.import_module(_RUNTIME_MODULE)
    return _runtime


def main() -> None:
    """Run the MCP server after enforcing the platform boundary."""
    require_macos()
    _load_runtime().mcp.run()


def __getattr__(name: str) -> Any:
    if name.startswith("__"):
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(_load_runtime(), name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(dir(_load_runtime())))


class _ServerFacade(ModuleType):
    """Forward public test/runtime overrides to the lazy runtime module."""

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_") or name in self.__dict__:
            super().__setattr__(name, value)
            return
        runtime = _load_runtime()
        if hasattr(runtime, name):
            setattr(runtime, name, value)
            return
        super().__setattr__(name, value)

    def __delattr__(self, name: str) -> None:
        if name.startswith("_") or name in self.__dict__:
            super().__delattr__(name)
            return
        runtime = _load_runtime()
        if hasattr(runtime, name):
            delattr(runtime, name)
            return
        super().__delattr__(name)


sys.modules[__name__].__class__ = _ServerFacade


if __name__ == "__main__":
    main()
