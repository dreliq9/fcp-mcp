"""Typed results for editor-neutral source handoff imports."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from .common import ArtifactReference
from .fcpxml import TransactionReceiptResult


class YouTubeClipPlanGenerationResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    schema_version: Literal["1"] = "1"
    project: str
    selected_clip_count: int
    target_duration_seconds: float
    plan_revision: str | None
    materialization_revision: str | None
    hashes_verified: bool
    destination: ArtifactReference
    provenance: ArtifactReference
    receipt: TransactionReceiptResult
