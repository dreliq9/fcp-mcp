"""Typed result contracts for puppet inspection tools."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    FiniteFloat,
    model_validator,
)

from .common import ArtifactReference
from .fcpxml import TransactionReceiptResult

Vector2 = Annotated[
    list[FiniteFloat],
    Field(min_length=2, max_length=2),
]


class PuppetPresetParameterRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    default: int | float


class PuppetPresetRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    description: str
    parts: list[str]
    parameters: list[PuppetPresetParameterRecord]
    usage: str


class PuppetPresetListResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1"] = "1"
    presets: list[PuppetPresetRecord]


class PuppetPartRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    name: str
    image: str
    position: Vector2
    scale: FiniteFloat
    rotation: FiniteFloat
    anchor: Vector2
    z_order: int
    width: int
    height: int


class PuppetRigRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    name: str
    position: Vector2
    parts: list[PuppetPartRecord]


class PuppetKeyframeRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    time: str
    value: FiniteFloat | Vector2
    interp: Literal["smooth2", "linear", "hold"]


class PuppetAnimationRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    part_name: str
    property_name: Literal["position", "rotation", "scale"]
    keyframes: list[PuppetKeyframeRecord]


class PuppetRigResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    schema_version: Literal["1"] = "1"
    rig: PuppetRigRecord


class PuppetBuildResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    schema_version: Literal["1"] = "1"
    project: str
    rigs: list[PuppetRigRecord]
    animations: list[PuppetAnimationRecord]
    duration: str
    destination: ArtifactReference
    receipt: TransactionReceiptResult


class PuppetSceneArtifactRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    scene: str
    preset: Literal["idle", "walk", "talk", "wave", "bounce"]
    project: str
    rigs: list[PuppetRigRecord]
    animations: list[PuppetAnimationRecord]
    duration: str
    destination: ArtifactReference
    receipt: TransactionReceiptResult


class PuppetMultiSceneResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    schema_version: Literal["1"] = "1"
    scene_count: int
    artifacts: list[PuppetSceneArtifactRecord]

    @model_validator(mode="after")
    def scene_count_matches_artifacts(self) -> Self:
        if self.scene_count != len(self.artifacts):
            raise ValueError(
                "scene_count must match the verified artifact count"
            )
        return self
