"""Common SDK-neutral result types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, StringConstraints

ResultT = TypeVar("ResultT", bound=BaseModel)
MediaType = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=3,
        max_length=127,
        pattern=(
            r"^[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,62}/"
            r"[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,62}$"
        ),
    ),
]


class LegacyTextResult(BaseModel):
    """Temporary structured channel for handlers that still return plain text."""

    model_config = ConfigDict(frozen=True)

    schema_version: str = "1"
    result: str


class ArtifactReference(BaseModel):
    """A committed artifact whose identity is bound to its bytes."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    path: str
    media_type: MediaType
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
