"""Reusable MCP tool annotation presets for the public catalog."""

from __future__ import annotations

from mcp.types import ToolAnnotations

OFFLINE_READ = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)

OFFLINE_WRITE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=False,
    openWorldHint=False,
)

LIVE_READ = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)

LIVE_WRITE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=False,
    openWorldHint=True,
)

STATEFUL_WRITE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=False,
)

DIAGNOSTIC = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)


__all__ = [
    "DIAGNOSTIC",
    "LIVE_READ",
    "LIVE_WRITE",
    "OFFLINE_READ",
    "OFFLINE_WRITE",
    "STATEFUL_WRITE",
]
