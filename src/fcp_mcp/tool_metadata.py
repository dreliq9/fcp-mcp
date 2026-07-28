"""SDK-neutral safety metadata for the public tool catalog."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SafetyHints:
    read_only: bool
    destructive: bool
    idempotent: bool
    open_world: bool


OFFLINE_READ = SafetyHints(
    read_only=True,
    destructive=False,
    idempotent=True,
    open_world=False,
)

OFFLINE_WRITE = SafetyHints(
    read_only=False,
    destructive=True,
    idempotent=False,
    open_world=False,
)

LIVE_READ = SafetyHints(
    read_only=True,
    destructive=False,
    idempotent=True,
    open_world=True,
)

LIVE_WRITE = SafetyHints(
    read_only=False,
    destructive=True,
    idempotent=False,
    open_world=True,
)

STATEFUL_WRITE = SafetyHints(
    read_only=False,
    destructive=False,
    idempotent=False,
    open_world=False,
)

DIAGNOSTIC = SafetyHints(
    read_only=False,
    destructive=False,
    idempotent=True,
    open_world=False,
)


__all__ = [
    "DIAGNOSTIC",
    "LIVE_READ",
    "LIVE_WRITE",
    "OFFLINE_READ",
    "OFFLINE_WRITE",
    "STATEFUL_WRITE",
    "SafetyHints",
]
