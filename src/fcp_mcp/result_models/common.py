"""Common SDK-neutral result types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict

ResultT = TypeVar("ResultT", bound=BaseModel)


class LegacyTextResult(BaseModel):
    """Temporary structured channel for handlers that still return plain text."""

    model_config = ConfigDict(frozen=True)

    schema_version: str = "1"
    result: str


@dataclass(frozen=True)
class ToolOutcome(Generic[ResultT]):
    """One tool result represented on both MCP output channels."""

    text: str
    structured: ResultT
