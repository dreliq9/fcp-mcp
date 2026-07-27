"""Typed result contracts for FCPXML analysis tools."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class ProjectSummaryRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    has_sequence: bool
    clip_count: int
    duration: str


class FCPXMLSummaryResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1"] = "1"
    version: str
    formats: int
    assets: int
    effects: int
    projects: list[ProjectSummaryRecord]


class ClipRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    index: int
    name: str
    clip_type: str
    offset: str
    offset_raw: str
    start: str
    duration_seconds: float
    duration_raw: str
    role: str
    ref: str
    connected_clip_count: int
    marker_count: int


class ClipListResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1"] = "1"
    clips: list[ClipRecord]


class MarkerRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    clip: str
    marker_type: str
    value: str
    note: str
    start_timecode: str
    start_raw: str


class KeywordRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    clip: str
    value: str
    start_raw: str
    duration_raw: str


class MarkerListResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1"] = "1"
    markers: list[MarkerRecord]
    keywords: list[KeywordRecord]


class PacingHistogram(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    under_one_second: int
    one_to_three_seconds: int
    three_to_five_seconds: int
    five_to_ten_seconds: int
    ten_to_thirty_seconds: int
    thirty_seconds_or_more: int


class PacingRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    average_shot_length: float
    median_shot_length: float
    std_deviation: float
    shortest_shot: float
    longest_shot: float
    pacing_curve: list[float]
    histogram: PacingHistogram


class PacingAnalysisResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1"] = "1"
    analyses: list[PacingRecord]


class GapRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    offset_seconds: float
    offset_timecode: str
    duration_seconds: float
    before_clip: str
    after_clip: str


class GapDetectionResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1"] = "1"
    count: int
    gaps: list[GapRecord]


class FlashFrameRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    clip_name: str
    offset_seconds: float
    offset_timecode: str
    duration_seconds: float
    frame_count: int


class FlashFrameDetectionResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1"] = "1"
    max_frames: int
    count: int
    items: list[FlashFrameRecord]


class DuplicateOccurrenceRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    offset_seconds: float
    duration_seconds: float


class DuplicateGroupRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    asset_ref: str
    asset_name: str
    occurrences: list[DuplicateOccurrenceRecord]


class DuplicateDetectionResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1"] = "1"
    groups: list[DuplicateGroupRecord]


class ValidationIssueRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    severity: Literal["error", "warning", "info"]
    message: str
    location: str


class FCPXMLValidationResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1"] = "1"
    valid: bool
    issues: list[ValidationIssueRecord]
    error_count: int
    warning_count: int
    info_count: int
    summary: str


class EffectParameterRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    value: str


class AppliedEffectRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    clip: str
    effect_name: str
    effect_ref: str
    enabled: bool
    parameters: list[EffectParameterRecord]


class AvailableEffectRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    name: str
    uid: str


class EffectInventoryResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1"] = "1"
    applied: list[AppliedEffectRecord]
    available: list[AvailableEffectRecord]


class RoleListResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1"] = "1"
    roles: list[str]


class TimelineStatisticsRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    project_name: str
    total_duration_seconds: float
    total_duration_timecode: str
    clip_count: int
    non_gap_clip_count: int
    gap_count: int
    total_gap_duration_seconds: float
    transition_count: int
    marker_count: int
    keyword_count: int
    connected_clip_count: int
    roles_used: list[str]
    fps: float
    resolution: str
    average_clip_duration_seconds: float
    shortest_clip_seconds: float
    longest_clip_seconds: float


class TimelineStatsResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1"] = "1"
    projects: list[TimelineStatisticsRecord]


class DiffChangeRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    project_a: str
    project_b: str
    change_type: str
    clip_name: str
    details: str


class DiffCountsRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    added: int
    removed: int
    moved: int
    trimmed: int
    unchanged: int


class ProjectDiffRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    project_a: str
    project_b: str
    changes: list[DiffChangeRecord]
    counts: DiffCountsRecord


class FCPXMLDiffResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1"] = "1"
    projects: list[ProjectDiffRecord]
    changes: list[DiffChangeRecord]
    counts: DiffCountsRecord
    summary: str


class QCReportResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1"] = "1"
    validation: FCPXMLValidationResult
    stats: list[TimelineStatisticsRecord]
    gaps: list[GapRecord]
    flash_frames: list[FlashFrameRecord]
    duplicates: list[DuplicateGroupRecord]
    pacing: list[PacingRecord]
    markdown: str


class MediaLinkCheckResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1"] = "1"
    issues: list[ValidationIssueRecord]
    missing_count: int


class FrameRateFormatRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    format_id: str
    name: str
    frame_duration: str
    fps: float


class FrameRateMismatchRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    format_id: str
    expected_fps: float
    actual_fps: float


class FrameRateCheckResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1"] = "1"
    formats: list[FrameRateFormatRecord]
    mismatches: list[FrameRateMismatchRecord]


class AudioLevelObservationRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    clip_name: str
    kind: Literal["missing_audio", "high_volume", "low_volume"]
    message: str
    role: str
    amount_db: float | None = None


class AudioLevelCheckResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1"] = "1"
    verified: Literal[False] = False
    observations: list[AudioLevelObservationRecord]
    limitations: list[str]


class SafeZoneObservationRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    clip_name: str
    kind: Literal["position", "scale"]
    message: str
    position_x: float | None = None
    position_y: float | None = None
    scale: float | None = None


class SafeZoneCheckResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1"] = "1"
    verified: Literal[False] = False
    observations: list[SafeZoneObservationRecord]
    limitations: list[str]


class DurationCheckResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1"] = "1"
    project_name: str
    target_seconds: float
    actual_seconds: float
    delta_seconds: float
    tolerance_seconds: float
    within_tolerance: bool


class MotionTemplateRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    category: str
    name: str
    path: str


class MotionTemplateListResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1"] = "1"
    templates: list[MotionTemplateRecord]


class ShareDestinationListResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1"] = "1"
    destinations: list[str]


class InstalledEffectListResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1"] = "1"
    effects: list[str]
    transitions: list[str]
    titles: list[str]


class TemplateListResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1"] = "1"
    templates: list[str]
    directory: str


__all__ = [
    "AppliedEffectRecord",
    "AudioLevelCheckResult",
    "AudioLevelObservationRecord",
    "AvailableEffectRecord",
    "ClipListResult",
    "ClipRecord",
    "DiffChangeRecord",
    "DiffCountsRecord",
    "DuplicateDetectionResult",
    "DuplicateGroupRecord",
    "DuplicateOccurrenceRecord",
    "DurationCheckResult",
    "EffectInventoryResult",
    "EffectParameterRecord",
    "FCPXMLDiffResult",
    "FCPXMLSummaryResult",
    "FCPXMLValidationResult",
    "FlashFrameDetectionResult",
    "FlashFrameRecord",
    "FrameRateCheckResult",
    "FrameRateFormatRecord",
    "FrameRateMismatchRecord",
    "GapDetectionResult",
    "GapRecord",
    "InstalledEffectListResult",
    "KeywordRecord",
    "MarkerListResult",
    "MarkerRecord",
    "MediaLinkCheckResult",
    "MotionTemplateListResult",
    "MotionTemplateRecord",
    "PacingAnalysisResult",
    "PacingHistogram",
    "PacingRecord",
    "ProjectDiffRecord",
    "ProjectSummaryRecord",
    "QCReportResult",
    "RoleListResult",
    "SafeZoneCheckResult",
    "SafeZoneObservationRecord",
    "ShareDestinationListResult",
    "TemplateListResult",
    "TimelineStatisticsRecord",
    "TimelineStatsResult",
    "ValidationIssueRecord",
]
