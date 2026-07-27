"""Typed result contracts for media inspection tools."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, model_validator

from .common import ArtifactReference

RawSummary = Annotated[str, Field(max_length=2000)]
BoundedText = Annotated[str, Field(max_length=2000)]


class MediaStreamRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    index: int | None
    codec_type: str
    codec: str | None
    codec_long_name: str | None
    language: str
    duration_seconds: FiniteFloat | None
    bitrate_kbps: FiniteFloat | None
    raw_summary: RawSummary | None = None


class VideoStreamRecord(MediaStreamRecord):
    codec_type: Literal["video"]
    width: int | None
    height: int | None
    frame_rate: FiniteFloat | None
    pixel_format: str | None


class AudioStreamRecord(MediaStreamRecord):
    codec_type: Literal["audio"]
    sample_rate_hz: int | None
    channels: int | None
    channel_layout: str | None


class SubtitleStreamRecord(MediaStreamRecord):
    codec_type: Literal["subtitle"]
    title: str | None


class UnsupportedStreamRecord(MediaStreamRecord):
    codec_type: Literal["data", "attachment", "unknown"]
    reason: Literal[
        "unsupported_codec_type",
        "unrecognized_codec_type",
        "missing_codec_type",
        "malformed_codec_type",
    ]


StreamRecord = Annotated[
    (
        VideoStreamRecord
        | AudioStreamRecord
        | SubtitleStreamRecord
        | UnsupportedStreamRecord
    ),
    Field(discriminator="codec_type"),
]


class MediaInfoResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1"] = "1"
    filename: str | None
    format: str | None
    duration_seconds: FiniteFloat | None
    size_mb: FiniteFloat | None
    bitrate_kbps: FiniteFloat | None
    streams: list[StreamRecord]
    raw_summary: RawSummary | None = None


class SilenceRangeRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    start_seconds: FiniteFloat
    end_seconds: FiniteFloat
    duration_seconds: FiniteFloat


class SilenceDetectionResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1"] = "1"
    noise_threshold: str
    minimum_duration_seconds: FiniteFloat
    ranges: list[SilenceRangeRecord]


class BeatRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    seconds: FiniteFloat
    timecode: str


class BeatCadenceRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    intervals_seconds: list[FiniteFloat]
    average_interval_seconds: FiniteFloat | None
    estimated_bpm: FiniteFloat | None


class BeatDetectionResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1"] = "1"
    beat_count: int
    beats: list[BeatRecord]
    cadence: BeatCadenceRecord


class ParserWarningRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["malformed_value", "command_warning"]
    field: str | None
    message: BoundedText


class LoudnessResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1"] = "1"
    integrated_lufs: FiniteFloat | None
    loudness_range_lu: FiniteFloat | None
    true_peak_dbfs: FiniteFloat | None
    warnings: list[ParserWarningRecord]
    raw_summary: RawSummary | None = None


class StreamListResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1"] = "1"
    streams: list[StreamRecord]


class SceneChangeRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    time_seconds: FiniteFloat | None
    timecode: str | None
    score: FiniteFloat | None


class SceneDetectionResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1"] = "1"
    threshold: FiniteFloat
    scene_count: int
    scene_changes: list[SceneChangeRecord]
    warnings: list[ParserWarningRecord]
    raw_summary: RawSummary | None = None


class MediaArtifactResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    schema_version: Literal["1"] = "1"
    operation: Literal[
        "extract_thumbnail",
        "extract_audio",
        "audio_to_midi",
    ]
    source: ArtifactReference
    artifact: ArtifactReference

    @model_validator(mode="after")
    def artifact_type_matches_operation(self) -> Self:
        expected_prefix = {
            "extract_thumbnail": "image/",
            "extract_audio": "audio/",
            "audio_to_midi": "audio/midi",
        }[self.operation]
        if (
            self.artifact.media_type != expected_prefix
            if self.operation == "audio_to_midi"
            else not self.artifact.media_type.startswith(expected_prefix)
        ):
            raise ValueError(
                "artifact media_type must match the requested operation"
            )
        return self


class MediaArtifactListResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    schema_version: Literal["1"] = "1"
    operation: Literal["extract_thumbnails"]
    source: ArtifactReference
    requested_count: int
    artifacts: list[ArtifactReference]

    @model_validator(mode="after")
    def requested_count_matches_artifacts(self) -> Self:
        if self.requested_count != len(self.artifacts):
            raise ValueError(
                "requested_count must match the verified artifact count"
            )
        if self.requested_count <= 0:
            raise ValueError("requested_count must be positive")
        if any(
            not artifact.media_type.startswith("image/")
            for artifact in self.artifacts
        ):
            raise ValueError(
                "extract_thumbnails artifacts must be images"
            )
        if len({artifact.path for artifact in self.artifacts}) != len(
            self.artifacts
        ):
            raise ValueError("thumbnail artifact paths must be unique")
        return self
