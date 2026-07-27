"""Typed result contracts for media inspection tools."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

RawSummary = Annotated[str, Field(max_length=2000)]


class MediaStreamRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    index: int
    codec_type: str
    codec: str | None
    codec_long_name: str | None
    language: str
    duration_seconds: float | None
    bitrate_kbps: float | None
    raw_summary: RawSummary | None = None


class VideoStreamRecord(MediaStreamRecord):
    codec_type: Literal["video"]
    width: int | None
    height: int | None
    frame_rate: float | None
    pixel_format: str | None


class AudioStreamRecord(MediaStreamRecord):
    codec_type: Literal["audio"]
    sample_rate_hz: int | None
    channels: int | None
    channel_layout: str | None


class SubtitleStreamRecord(MediaStreamRecord):
    codec_type: Literal["subtitle"]
    title: str | None


StreamRecord = Annotated[
    VideoStreamRecord | AudioStreamRecord | SubtitleStreamRecord,
    Field(discriminator="codec_type"),
]


class MediaInfoResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1"] = "1"
    filename: str | None
    format: str | None
    duration_seconds: float | None
    size_mb: float | None
    bitrate_kbps: float | None
    streams: list[StreamRecord]
    raw_summary: RawSummary | None = None


class SilenceRangeRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    start_seconds: float
    end_seconds: float
    duration_seconds: float


class SilenceDetectionResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1"] = "1"
    noise_threshold: str
    minimum_duration_seconds: float
    ranges: list[SilenceRangeRecord]


class BeatRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    seconds: float
    timecode: str


class BeatCadenceRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    intervals_seconds: list[float]
    average_interval_seconds: float | None
    estimated_bpm: float | None


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
    message: str


class LoudnessResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1"] = "1"
    integrated_lufs: float | None
    loudness_range_lu: float | None
    true_peak_dbfs: float | None
    warnings: list[ParserWarningRecord]
    raw_summary: RawSummary | None = None


class StreamListResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1"] = "1"
    streams: list[StreamRecord]


class SceneChangeRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    time_seconds: float
    timecode: str
    score: float | None


class SceneDetectionResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1"] = "1"
    threshold: float
    scene_count: int
    scene_changes: list[SceneChangeRecord]
    warnings: list[ParserWarningRecord]
    raw_summary: RawSummary | None = None

