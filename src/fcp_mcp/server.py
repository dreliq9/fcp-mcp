"""Import-safe public boundary for the fcp-mcp server runtime."""

from __future__ import annotations

import importlib
import sys
from types import ModuleType
from typing import Any

from .platform_support import require_macos

_RUNTIME_MODULE = "fcp_mcp._server_runtime"
_runtime: ModuleType | None = None
_FACADE_OWNED_NAMES = frozenset(
    {
        "_FACADE_OWNED_NAMES",
        "_RUNTIME_MODULE",
        "_ServerFacade",
        "_load_runtime",
        "_runtime",
        "Any",
        "ModuleType",
        "importlib",
        "main",
        "require_macos",
        "sys",
    }
)


def _load_runtime() -> ModuleType:
    global _runtime
    require_macos()
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
    names = set(globals())
    if _runtime is not None:
        names.update(dir(_runtime))
    return sorted(names)


class _ServerFacade(ModuleType):
    """Forward public test/runtime overrides to the lazy runtime module."""

    def __setattr__(self, name: str, value: Any) -> None:
        if name in _FACADE_OWNED_NAMES or (
            name.startswith("__") and name.endswith("__")
        ):
            super().__setattr__(name, value)
            return
        runtime = _load_runtime()
        setattr(runtime, name, value)

    def __delattr__(self, name: str) -> None:
        if name in _FACADE_OWNED_NAMES or (
            name.startswith("__") and name.endswith("__")
        ):
            super().__delattr__(name)
            return
        runtime = _load_runtime()
        delattr(runtime, name)


sys.modules[__name__].__class__ = _ServerFacade


if __name__ == "__main__":
    main()
