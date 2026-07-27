"""Typed result contracts for FCPXML analysis tools."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .common import ArtifactReference


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


class TransactionReceiptResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    transaction_id: str
    source: str | None
    destination: str
    backup_path: str | None
    input_sha256: str | None
    prior_sha256: str | None
    output_sha256: str
    validation_warnings: list[str]
    elapsed_ms: int
    disposition: Literal["committed"]


class FCPXMLGenerationResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    schema_version: Literal["1"] = "1"
    project: str
    selected_clip_count: int
    target_duration_seconds: float
    destination: ArtifactReference
    receipt: TransactionReceiptResult


class SubtitleImportResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    schema_version: Literal["1"] = "1"
    cue_count: int
    destination: ArtifactReference
    receipt: TransactionReceiptResult


class EDLImportResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    schema_version: Literal["1"] = "1"
    event_count: int
    destination: ArtifactReference
    receipt: TransactionReceiptResult


class FCPXMLMutationResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    schema_version: Literal["1"] = "1"
    source_version: str
    target_version: str
    target_width: int
    target_height: int
    target_format_name: str
    destination: ArtifactReference
    receipt: TransactionReceiptResult


class CleanupMutationResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    schema_version: Literal["1"] = "1"
    action: Literal[
        "fix_flash_frames",
        "fill_gaps",
        "remove_silence",
    ]
    changed_count: int
    destination: ArtifactReference
    receipt: TransactionReceiptResult


class MarkerMutationRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    clip_name: str
    marker_type: Literal["standard", "chapter"]
    start: str
    duration: str
    value: str
    note: str


class MarkerMutationResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1"] = "1"
    destination: ArtifactReference
    receipt: TransactionReceiptResult
    marker: MarkerMutationRecord


class MarkerBatchMutationResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1"] = "1"
    destination: ArtifactReference
    receipt: TransactionReceiptResult
    requested_count: int
    changed_count: int
    markers: list[MarkerMutationRecord]


class KeywordMutationRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    clip_name: str
    value: str
    start: str
    duration: str | None


class KeywordMutationResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1"] = "1"
    destination: ArtifactReference
    receipt: TransactionReceiptResult
    keyword: KeywordMutationRecord


class ClipMutationRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    element_type: str
    offset: str
    start: str
    duration: str
    role: str
    ref: str


class ClipFieldMutationRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    field: Literal["start", "duration", "offset", "role", "time_map"]
    before: str | None
    after: str | None


class ClipMutationResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1"] = "1"
    destination: ArtifactReference
    receipt: TransactionReceiptResult
    operation: Literal["trim", "split", "change_speed"]
    clip: ClipMutationRecord
    created_clip: ClipMutationRecord | None
    changed_fields: list[ClipFieldMutationRecord]
    speed_factor: float | None


class ClipBatchMutationResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    schema_version: Literal["1"] = "1"
    destination: ArtifactReference
    receipt: TransactionReceiptResult
    operation: Literal["delete", "reorder", "rename"]
    requested_count: int | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    changed_count: int
    requested_names: list[str] | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    deleted_names: list[str] | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    requested_order: list[str] | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    resulting_order: list[str] | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    pattern: str | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    replacement: str | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def validate_operation_fields(self) -> Self:
        if self.operation == "rename":
            if self.pattern in {None, ""} or self.replacement is None:
                raise ValueError("rename requires pattern and replacement")
            if any(
                value is not None
                for value in (
                    self.requested_count,
                    self.requested_names,
                    self.deleted_names,
                    self.requested_order,
                    self.resulting_order,
                )
            ):
                raise ValueError(
                    "rename forbids delete and reorder fields"
                )
            return self

        required = (
            self.requested_count,
            self.requested_names,
            self.deleted_names,
            self.requested_order,
            self.resulting_order,
        )
        if any(value is None for value in required):
            raise ValueError(
                f"{self.operation} requires all batch list fields"
            )
        if self.pattern is not None or self.replacement is not None:
            raise ValueError(
                f"{self.operation} forbids rename fields"
            )
        assert self.requested_count is not None
        assert self.requested_names is not None
        assert self.deleted_names is not None
        assert self.requested_order is not None
        assert self.resulting_order is not None
        if self.requested_count != len(self.requested_names):
            raise ValueError(
                "requested_count must match requested_names"
            )
        if self.operation == "delete":
            if self.requested_order:
                raise ValueError("delete requires an empty requested_order")
            if self.deleted_names != self.requested_names:
                raise ValueError(
                    "delete names must match requested names"
                )
            if self.changed_count != len(self.deleted_names):
                raise ValueError(
                    "delete changed_count must match deleted_names"
                )
        else:
            if self.deleted_names:
                raise ValueError("reorder requires empty deleted_names")
            if self.requested_order != self.requested_names:
                raise ValueError(
                    "reorder requested fields must match"
                )
            if self.changed_count != len(self.requested_order):
                raise ValueError(
                    "reorder changed_count must match requested_order"
                )
            if self.resulting_order[: self.requested_count] != (
                self.requested_order
            ):
                raise ValueError(
                    "reorder result must begin with requested_order"
                )
        return self


class RoleRuleRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    match: str
    role: str


class RoleBatchMutationResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    schema_version: Literal["1"] = "1"
    rules: list[RoleRuleRecord]
    changed_count: int
    destination: ArtifactReference
    receipt: TransactionReceiptResult


class TransitionBatchMutationResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    schema_version: Literal["1"] = "1"
    name: str
    duration: str
    changed_count: int
    destination: ArtifactReference
    receipt: TransactionReceiptResult


class TransitionMutationRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    after_clip_name: str
    name: str
    duration: str
    offset: str
    ref: str


class TransitionMutationResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1"] = "1"
    destination: ArtifactReference
    receipt: TransactionReceiptResult
    transition: TransitionMutationRecord


class RoleMutationRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    clip_name: str
    role: str


class RoleMutationResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1"] = "1"
    destination: ArtifactReference
    receipt: TransactionReceiptResult
    assignment: RoleMutationRecord


class TimelineElementMutationRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["title", "audio"]
    name: str
    ref: str
    position: str
    source: str | None
    lane: int
    offset: str
    start: str | None
    duration: str
    role: str


class TimelineElementMutationResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1"] = "1"
    destination: ArtifactReference
    receipt: TransactionReceiptResult
    element: TimelineElementMutationRecord


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


class UnsupportedToolResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    schema_version: Literal["1"] = "1"
    supported: Literal[False] = False
    reason_code: Literal["unsupported_contract"] = "unsupported_contract"
    reason: Literal[
        "Template clip replacement has no stable clip substitution schema "
        "in v0.2.1"
    ] = (
        "Template clip replacement has no stable clip substitution schema "
        "in v0.2.1"
    )


class TemplateSaveResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    schema_version: Literal["1"] = "1"
    template: ArtifactReference
    source_path: str
    source_sha256: str
    backup_path: str | None
    receipt: TransactionReceiptResult


class ExportResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    schema_version: Literal["1"] = "1"
    format: Literal["resolve", "fcp7", "edl"]
    source: ArtifactReference
    artifact: ArtifactReference
    receipt: TransactionReceiptResult


__all__ = [
    "AppliedEffectRecord",
    "AudioLevelCheckResult",
    "AudioLevelObservationRecord",
    "AvailableEffectRecord",
    "CleanupMutationResult",
    "ClipBatchMutationResult",
    "ClipListResult",
    "ClipRecord",
    "DiffChangeRecord",
    "DiffCountsRecord",
    "DuplicateDetectionResult",
    "DuplicateGroupRecord",
    "DuplicateOccurrenceRecord",
    "DurationCheckResult",
    "EDLImportResult",
    "EffectInventoryResult",
    "EffectParameterRecord",
    "ExportResult",
    "FCPXMLDiffResult",
    "FCPXMLGenerationResult",
    "FCPXMLMutationResult",
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
    "RoleBatchMutationResult",
    "RoleListResult",
    "RoleRuleRecord",
    "SafeZoneCheckResult",
    "SafeZoneObservationRecord",
    "ShareDestinationListResult",
    "SubtitleImportResult",
    "TemplateListResult",
    "TemplateSaveResult",
    "TimelineStatisticsRecord",
    "TimelineStatsResult",
    "TransactionReceiptResult",
    "TransitionBatchMutationResult",
    "UnsupportedToolResult",
    "ValidationIssueRecord",
]
