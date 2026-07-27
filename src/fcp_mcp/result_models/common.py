"""Common SDK-neutral result types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict

ResultT = TypeVar("ResultT", bound=BaseModel)


class LegacyTextResult(BaseModel):
    """Temporary structured channel for handlers that still return plain text."""

    model_config = ConfigDict(frozen=True)

    schema_version: str = "1"
    result: str


class ArtifactReference(BaseModel):
    """A committed artifact whose identity is bound to its bytes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    media_type: Literal["application/vnd.apple.fcpxml+xml"]
    sha256: str
    size_bytes: int


@dataclass(frozen=True)
class ToolOutcome(Generic[ResultT]):
    """One tool result represented on both MCP output channels."""

    text: str
    structured: ResultT

    def __str__(self) -> str:
        """Preserve the direct-call text view while carrying structured data."""
        return self.text
