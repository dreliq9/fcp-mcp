"""Typed result contracts for puppet inspection tools."""

from __future__ import annotations

import re
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    FiniteFloat,
    StringConstraints,
    field_validator,
    model_validator,
)

from .common import ArtifactReference
from .fcpxml import TransactionReceiptResult

Vector2 = Annotated[
    list[FiniteFloat],
    Field(min_length=2, max_length=2),
]
NonEmptyString = Annotated[
    str,
    StringConstraints(strict=True, min_length=1),
]


def _fcpxml_time_value(value: str, *, positive: bool) -> str:
    match = re.fullmatch(r"(-?\d+)(?:/(\d+))?s", value)
    if match is None:
        raise ValueError("must be a valid FCPXML time")
    numerator = int(match.group(1))
    denominator = int(match.group(2) or "1")
    if denominator == 0 or numerator < 0 or (positive and numerator == 0):
        raise ValueError("must be a nonnegative FCPXML time")
    return value


def _write_evidence_is_consistent(
    destination: ArtifactReference,
    receipt: TransactionReceiptResult,
) -> None:
    if destination.media_type != "application/vnd.apple.fcpxml+xml":
        raise ValueError("destination must identify an FCPXML artifact")
    if destination.path != receipt.destination:
        raise ValueError("destination path must match the receipt")
    if destination.sha256 != receipt.output_sha256:
        raise ValueError("destination hash must match the receipt")
    if receipt.elapsed_ms < 0:
        raise ValueError("receipt elapsed_ms must be nonnegative")


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

    name: NonEmptyString
    image: NonEmptyString
    position: Vector2
    scale: FiniteFloat
    rotation: FiniteFloat
    anchor: Vector2
    z_order: int
    width: int
    height: int


class PuppetRigRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    name: NonEmptyString
    position: Vector2
    parts: Annotated[list[PuppetPartRecord], Field(min_length=1)]


class PuppetKeyframeRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    time: str
    value: FiniteFloat | Vector2
    interp: Literal["smooth2", "linear", "hold"]

    @field_validator("time")
    @classmethod
    def time_is_valid(cls, value: str) -> str:
        return _fcpxml_time_value(value, positive=False)


class PuppetAnimationRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    rig_name: NonEmptyString
    part_name: NonEmptyString
    property_name: Literal["position", "rotation", "scale"]
    keyframes: Annotated[list[PuppetKeyframeRecord], Field(min_length=1)]

    @model_validator(mode="after")
    def value_shape_matches_property(self) -> Self:
        expects_vector = self.property_name in {"position", "scale"}
        if any(
            isinstance(keyframe.value, list) != expects_vector
            for keyframe in self.keyframes
        ):
            raise ValueError(
                "keyframe value shape must match the animated property"
            )
        return self


class PuppetRigResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    schema_version: Literal["1"] = "1"
    rig: PuppetRigRecord


class PuppetBuildResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    schema_version: Literal["1"] = "1"
    operation: Literal["build_scene", "animate", "preset_motion"]
    project: NonEmptyString
    rigs: Annotated[list[PuppetRigRecord], Field(min_length=1)]
    animations: list[PuppetAnimationRecord]
    duration: str
    destination: ArtifactReference
    receipt: TransactionReceiptResult

    @field_validator("duration")
    @classmethod
    def duration_is_valid(cls, value: str) -> str:
        return _fcpxml_time_value(value, positive=True)

    @model_validator(mode="after")
    def operation_and_write_evidence_are_consistent(self) -> Self:
        if self.operation == "build_scene" and self.animations:
            raise ValueError("build_scene must not report animations")
        if self.operation != "build_scene" and not self.animations:
            raise ValueError(
                "animation operations must report animation evidence"
            )
        _write_evidence_is_consistent(self.destination, self.receipt)
        return self


class PuppetSceneArtifactRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    operation: Literal["multi_scene"]
    scene: NonEmptyString
    preset: Literal["idle", "walk", "talk", "wave", "bounce"]
    project: NonEmptyString
    rigs: Annotated[list[PuppetRigRecord], Field(min_length=1)]
    animations: Annotated[
        list[PuppetAnimationRecord],
        Field(min_length=1),
    ]
    duration: str
    destination: ArtifactReference
    receipt: TransactionReceiptResult

    @field_validator("duration")
    @classmethod
    def duration_is_valid(cls, value: str) -> str:
        return _fcpxml_time_value(value, positive=True)

    @model_validator(mode="after")
    def write_evidence_is_consistent(self) -> Self:
        _write_evidence_is_consistent(self.destination, self.receipt)
        return self


class PuppetMultiSceneResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    schema_version: Literal["1"] = "1"
    scene_count: int
    artifacts: Annotated[
        list[PuppetSceneArtifactRecord],
        Field(min_length=1),
    ]

    @model_validator(mode="after")
    def scene_count_matches_artifacts(self) -> Self:
        if self.scene_count != len(self.artifacts):
            raise ValueError(
                "scene_count must match the verified artifact count"
            )
        paths = [artifact.destination.path for artifact in self.artifacts]
        if len(set(paths)) != len(paths):
            raise ValueError("scene artifact paths must be unique")
        return self
