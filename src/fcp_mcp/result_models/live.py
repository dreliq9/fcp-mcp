"""Truthful typed result contracts for live FCP and Compressor tools."""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

BoundedText = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=2000),
]
WarningText = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=1000),
]
Identifier = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]


class StrictFrozenModel(BaseModel):
    """Shared validation policy for public live-result contracts."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class FCPRunningResult(StrictFrozenModel):
    schema_version: Literal["1"] = "1"
    running: bool
    observation_method: Literal["system_events_process_list"]


class LibraryRecord(StrictFrozenModel):
    kind: Literal["library"] = "library"
    name: str
    id: str
    file: str


class EventRecord(StrictFrozenModel):
    kind: Literal["event"] = "event"
    library: str
    name: str
    id: str


class ProjectRecord(StrictFrozenModel):
    kind: Literal["project"] = "project"
    library: str
    event: str
    name: str
    id: str
    duration: str


CollectionRecord = Annotated[
    LibraryRecord | EventRecord | ProjectRecord,
    Field(discriminator="kind"),
]


class CollectionParentFilter(StrictFrozenModel):
    field: Literal["library_name", "event_name"]
    value: str


class FCPCollectionResult(StrictFrozenModel):
    schema_version: Literal["1"] = "1"
    collection_type: Literal["libraries", "events", "projects"]
    parent_filter: CollectionParentFilter | None
    names: list[str]
    records: list[CollectionRecord]

    @model_validator(mode="after")
    def validate_collection_consistency(self) -> Self:
        expected_kind = {
            "libraries": "library",
            "events": "event",
            "projects": "project",
        }[self.collection_type]
        if any(record.kind != expected_kind for record in self.records):
            raise ValueError("records must match collection_type")
        if self.names != [record.name for record in self.records]:
            raise ValueError("names must preserve the record order")
        if self.collection_type == "libraries":
            if self.parent_filter is not None:
                raise ValueError("library collections do not have a parent filter")
        else:
            expected_filter = (
                "library_name"
                if self.collection_type == "events"
                else "event_name"
            )
            if (
                self.parent_filter is not None
                and self.parent_filter.field != expected_filter
            ):
                raise ValueError("parent_filter does not match collection_type")
        return self


class FCPTimeObservation(StrictFrozenModel):
    value: int | float
    timescale: int | float


class FCPTimelineSelectionRange(StrictFrozenModel):
    start: str
    duration: str


class FCPTimelineInfoResult(StrictFrozenModel):
    schema_version: Literal["1"] = "1"
    library: str
    event: str
    project: str
    duration: FCPTimeObservation | None
    frame_duration: FCPTimeObservation | None
    timecode_format: str | None
    playhead: str | None
    selection_range: FCPTimelineSelectionRange | None
    warnings: Annotated[list[WarningText], Field(max_length=20)]


class FCPAppStateResult(StrictFrozenModel):
    schema_version: Literal["1"] = "1"
    name: str
    version: str
    frontmost: bool
    library_count: int


class CompressorCustomSettingRecord(StrictFrozenModel):
    path: str
    source: Literal["custom_setting_path"] = "custom_setting_path"
    contents_validated: Literal[False] = False


class CompressorCLIInformationRecord(StrictFrozenModel):
    raw_line: str
    source: Literal["compressor_info_stdout"] = "compressor_info_stdout"


class CompressorSettingsResult(StrictFrozenModel):
    schema_version: Literal["1"] = "1"
    custom_settings: list[CompressorCustomSettingRecord]
    cli_information: list[CompressorCLIInformationRecord]
    warnings: Annotated[list[WarningText], Field(max_length=20)]


class VerificationStatus(str, Enum):
    VERIFIED = "verified"
    UNVERIFIED = "unverified"
    FAILED = "failed"


class EmptyActionRequest(StrictFrozenModel):
    pass


class FCPOpenLibraryRequest(StrictFrozenModel):
    library_path: str


class FCPImportXMLRequest(StrictFrozenModel):
    fcpxml_path: str


class FCPPlaybackRequest(StrictFrozenModel):
    playback_action: Literal["play", "pause", "stop", "toggle"]
    key: Literal["space", "l", "k"]

    @model_validator(mode="after")
    def validate_key(self) -> Self:
        expected = {
            "toggle": "space",
            "play": "l",
            "pause": "k",
            "stop": "k",
        }[self.playback_action]
        if self.key != expected:
            raise ValueError("key does not match playback_action")
        return self


class FCPNavigateRequest(StrictFrozenModel):
    timecode: str
    normalized_timecode: str

    @model_validator(mode="after")
    def validate_normalized_timecode(self) -> Self:
        if not self.timecode:
            raise ValueError("timecode must not be empty")
        expected = self.timecode.replace(":", "").replace(";", "")
        if self.normalized_timecode != expected:
            raise ValueError("normalized_timecode does not match timecode")
        return self


class FCPSelectToolRequest(StrictFrozenModel):
    tool: Literal["select", "trim", "position", "range", "blade", "zoom", "hand"]
    key: Literal["a", "t", "p", "r", "b", "z", "h"]

    @model_validator(mode="after")
    def validate_key(self) -> Self:
        expected = {
            "select": "a",
            "trim": "t",
            "position": "p",
            "range": "r",
            "blade": "b",
            "zoom": "z",
            "hand": "h",
        }[self.tool]
        if self.key != expected:
            raise ValueError("key does not match tool")
        return self


class FCPMenuCommandRequest(StrictFrozenModel):
    menu_path: str
    components: Annotated[list[str], Field(min_length=2, max_length=3)]

    @model_validator(mode="after")
    def validate_components(self) -> Self:
        expected = [part.strip() for part in self.menu_path.split(">")]
        if expected != self.components or any(not part for part in self.components):
            raise ValueError("components do not match menu_path")
        return self


class FCPKeyboardShortcutRequest(StrictFrozenModel):
    keys: str
    key: str
    modifiers: list[Literal["command", "shift", "option", "control"]]


class FCPShareRequest(StrictFrozenModel):
    destination: str


class LiveActionResult(StrictFrozenModel):
    schema_version: Literal["1"] = "1"
    action: str
    request: StrictFrozenModel
    raw_response: str
    verification_status: VerificationStatus
    observed_outcome: BoundedText | None
    warnings: Annotated[list[WarningText], Field(max_length=20)]

    @model_validator(mode="after")
    def validate_verification_evidence(self) -> Self:
        if self.verification_status is VerificationStatus.UNVERIFIED:
            if self.observed_outcome is not None:
                raise ValueError("unverified actions cannot claim an observed outcome")
            if not self.warnings:
                raise ValueError("unverified actions require a warning")
        elif self.verification_status is VerificationStatus.VERIFIED:
            if self.observed_outcome is None:
                raise ValueError("verified actions require an observed outcome")
        return self


class FCPOpenLibraryResult(LiveActionResult):
    action: Literal["fcp_open_library"] = "fcp_open_library"
    request: FCPOpenLibraryRequest


class FCPImportXMLResult(LiveActionResult):
    action: Literal["fcp_import_xml"] = "fcp_import_xml"
    request: FCPImportXMLRequest


class FCPExportXMLResult(LiveActionResult):
    action: Literal["fcp_export_xml"] = "fcp_export_xml"
    request: EmptyActionRequest


class FCPPlaybackResult(LiveActionResult):
    action: Literal["fcp_playback"] = "fcp_playback"
    request: FCPPlaybackRequest


class FCPNavigateResult(LiveActionResult):
    action: Literal["fcp_navigate"] = "fcp_navigate"
    request: FCPNavigateRequest


class FCPSelectToolResult(LiveActionResult):
    action: Literal["fcp_select_tool"] = "fcp_select_tool"
    request: FCPSelectToolRequest


class FCPUndoResult(LiveActionResult):
    action: Literal["fcp_undo"] = "fcp_undo"
    request: EmptyActionRequest


class FCPRedoResult(LiveActionResult):
    action: Literal["fcp_redo"] = "fcp_redo"
    request: EmptyActionRequest


class FCPMenuCommandResult(LiveActionResult):
    action: Literal["fcp_menu_command"] = "fcp_menu_command"
    request: FCPMenuCommandRequest


class FCPKeyboardShortcutResult(LiveActionResult):
    action: Literal["fcp_keyboard_shortcut"] = "fcp_keyboard_shortcut"
    request: FCPKeyboardShortcutRequest


class FCPShareResult(LiveActionResult):
    action: Literal["fcp_share"] = "fcp_share"
    request: FCPShareRequest


class CompressorSubmissionRequest(StrictFrozenModel):
    source_path: str
    setting_path: str | None
    output_directory: str | None
    batch_name: str


class CompressorCommandResult(StrictFrozenModel):
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str


class CompressorSubmissionResult(StrictFrozenModel):
    schema_version: Literal["1"] = "1"
    action: Literal["compressor_encode"] = "compressor_encode"
    request: CompressorSubmissionRequest
    command: CompressorCommandResult
    verification_status: Literal[VerificationStatus.UNVERIFIED]
    observed_outcome: None
    job_identifier: Identifier | None
    warnings: Annotated[list[WarningText], Field(min_length=1, max_length=20)]


__all__ = [
    "CompressorCLIInformationRecord",
    "CompressorCommandResult",
    "CompressorCustomSettingRecord",
    "CompressorSettingsResult",
    "CompressorSubmissionRequest",
    "CompressorSubmissionResult",
    "EmptyActionRequest",
    "EventRecord",
    "FCPAppStateResult",
    "FCPCollectionResult",
    "FCPExportXMLResult",
    "FCPImportXMLRequest",
    "FCPImportXMLResult",
    "FCPKeyboardShortcutRequest",
    "FCPKeyboardShortcutResult",
    "FCPMenuCommandRequest",
    "FCPMenuCommandResult",
    "FCPNavigateRequest",
    "FCPNavigateResult",
    "FCPOpenLibraryRequest",
    "FCPOpenLibraryResult",
    "FCPPlaybackRequest",
    "FCPPlaybackResult",
    "FCPRedoResult",
    "FCPRunningResult",
    "FCPSelectToolRequest",
    "FCPSelectToolResult",
    "FCPShareRequest",
    "FCPShareResult",
    "FCPTimeObservation",
    "FCPTimelineInfoResult",
    "FCPUndoResult",
    "LibraryRecord",
    "ProjectRecord",
    "VerificationStatus",
]
