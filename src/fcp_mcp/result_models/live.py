"""Truthful typed result contracts for live FCP and Compressor tools."""

from __future__ import annotations

import re
from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from typing_extensions import Self

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
NonnegativeFiniteNumber = Annotated[
    int | float,
    Field(ge=0, allow_inf_nan=False),
]
PositiveInteger = Annotated[int, Field(gt=0)]

_SHORTCUT_MODIFIER_ALIASES = {
    "cmd": "command",
    "command": "command",
    "shift": "shift",
    "opt": "option",
    "option": "option",
    "alt": "option",
    "ctrl": "control",
    "control": "control",
}
_SHORTCUT_SPECIAL_KEYS = frozenset(
    {"space", "return", "escape", "left", "right", "up", "down"}
)
_COMPRESSOR_JOB_IDENTIFIER = re.compile(
    r"Job ID: ([A-Za-z0-9][A-Za-z0-9._:-]{0,127})(?:\r?\n)?",
)


def parse_compressor_job_identifier(stdout: str) -> str | None:
    """Parse one sanctioned identifier-only Compressor stdout response."""
    match = _COMPRESSOR_JOB_IDENTIFIER.fullmatch(stdout)
    return match.group(1) if match is not None else None


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
            if self.parent_filter is not None:
                if self.collection_type == "events" and any(
                    not isinstance(record, EventRecord)
                    or record.library != self.parent_filter.value
                    for record in self.records
                ):
                    raise ValueError("event records do not match parent_filter")
                if self.collection_type == "projects" and any(
                    not isinstance(record, ProjectRecord)
                    or record.event != self.parent_filter.value
                    for record in self.records
                ):
                    raise ValueError("project records do not match parent_filter")
        return self


class FCPTimeObservation(StrictFrozenModel):
    value: NonnegativeFiniteNumber
    timescale: PositiveInteger


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

    @model_validator(mode="after")
    def validate_frame_duration(self) -> Self:
        if self.frame_duration is not None and self.frame_duration.value <= 0:
            raise ValueError("frame_duration value must be positive")
        return self


class FCPAppStateResult(StrictFrozenModel):
    schema_version: Literal["1"] = "1"
    name: str
    version: str
    frontmost: bool
    library_count: Annotated[int, Field(ge=0)]


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

    @model_validator(mode="after")
    def validate_canonical_shortcut(self) -> Self:
        tokens = [token.strip().lower() for token in self.keys.split("+")]
        if not self.keys.strip() or any(not token for token in tokens):
            raise ValueError("keys must contain one key and optional modifiers")
        expected_modifiers: list[str] = []
        key_tokens: list[str] = []
        for token in tokens:
            modifier = _SHORTCUT_MODIFIER_ALIASES.get(token)
            if modifier is None:
                key_tokens.append(token)
            elif modifier in expected_modifiers:
                raise ValueError("keys contain a duplicate modifier")
            else:
                expected_modifiers.append(modifier)
        if len(key_tokens) != 1:
            raise ValueError("keys must contain exactly one key")
        expected_key = key_tokens[0]
        if not (
            (
                len(expected_key) == 1
                and expected_key.isascii()
                and expected_key.isalnum()
            )
            or expected_key in _SHORTCUT_SPECIAL_KEYS
        ):
            raise ValueError("keys contain an unsupported shortcut key")
        if self.key != expected_key or self.modifiers != expected_modifiers:
            raise ValueError("key and modifiers must be canonical for keys")
        return self


class FCPShareRequest(StrictFrozenModel):
    destination: str


FinalCut12FeatureAction = Literal[
    "set_browser_search",
    "add_adjustment_clip",
    "add_magnetic_mask",
    "open_image_playground",
    "reveal_source_in_browser",
    "open_magnetic_timeline_tutorial",
    "download_demo_project",
    "generate_subtitles",
    "generate_closed_captions",
    "toggle_beat_detection",
    "toggle_beat_grid",
    "detect_edits",
    "add_auto_mask",
    "match_color",
    "toggle_two_up_display",
    "send_frame_to_pixelmator_pro",
    "move_primary_left",
    "move_primary_right",
    "select_connected_clips",
    "duplicate_captions_to_subtitles",
    "select_all_subtitles",
    "open_transcode_media",
    "open_content_library",
]
FinalCutBrowserSearchScope = Literal[
    "all",
    "transcript",
    "visual",
    "all-text",
    "notes",
    "names",
    "markers",
]


class FCPFinalCut12Request(StrictFrozenModel):
    feature_action: FinalCut12FeatureAction
    query: str | None = None
    search_scope: FinalCutBrowserSearchScope | None = None
    transcript_match: Literal["includes", "is_related_to"] | None = None

    @model_validator(mode="after")
    def validate_browser_search(self) -> Self:
        if self.feature_action == "set_browser_search":
            if self.query is None or not self.query.strip():
                raise ValueError("browser search query must not be empty")
            if len(self.query) > 500 or any(ord(char) < 32 for char in self.query):
                raise ValueError("browser search query is not safe to type")
            if self.search_scope is None:
                raise ValueError("browser search scope is required")
            if self.search_scope == "transcript":
                if self.transcript_match is None:
                    raise ValueError("transcript search match mode is required")
            elif self.transcript_match is not None:
                raise ValueError(
                    "transcript match mode is only valid for transcript search"
                )
        elif any(
            value is not None
            for value in (self.query, self.search_scope, self.transcript_match)
        ):
            raise ValueError("search fields are only valid for browser search")
        return self


class LiveActionResult(StrictFrozenModel):
    schema_version: Literal["1"] = "1"
    action: str
    request: StrictFrozenModel
    raw_response: str
    verification_status: Literal[
        VerificationStatus.UNVERIFIED
    ] = VerificationStatus.UNVERIFIED
    observed_outcome: None = None
    warnings: Annotated[list[WarningText], Field(min_length=1, max_length=20)]


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


class FCPFinalCut12Result(LiveActionResult):
    action: Literal["fcp_final_cut_12"] = "fcp_final_cut_12"
    request: FCPFinalCut12Request
    completion: Literal[
        "command_sent",
        "interactive_mode_started",
        "criteria_applied",
    ]
    requires_user_interaction: bool
    next_step: str | None
    observed_query: str | None = None
    criteria_applied: bool = False
    results_observed: Literal[False] = False

    @model_validator(mode="after")
    def validate_feature_evidence(self) -> Self:
        is_search = self.request.feature_action == "set_browser_search"
        if is_search:
            if (
                self.completion != "criteria_applied"
                or self.observed_query != self.request.query
                or not self.criteria_applied
                or self.requires_user_interaction
                or self.next_step is not None
            ):
                raise ValueError("browser search evidence is inconsistent")
        elif self.observed_query is not None or self.criteria_applied:
            raise ValueError("search evidence is only valid for browser search")
        if self.requires_user_interaction and not self.next_step:
            raise ValueError("interactive feature actions require a next step")
        if not self.requires_user_interaction and self.next_step is not None:
            raise ValueError("noninteractive feature actions cannot have a next step")
        return self


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

    @model_validator(mode="after")
    def validate_command_evidence(self) -> Self:
        if self.command.returncode != 0:
            raise ValueError("command returncode must be zero")
        if not self.command.argv:
            raise ValueError("command argv must not be empty")
        executable = self.command.argv[0]
        if not executable:
            raise ValueError("command executable must not be empty")
        expected_argv = [
            executable,
            "-batchName",
            self.request.batch_name,
            "-jobpath",
            self.request.source_path,
        ]
        if self.request.setting_path is not None:
            expected_argv.extend(
                ["-settingpath", self.request.setting_path]
            )
        if self.request.output_directory is not None:
            expected_argv.extend(
                ["-locationpath", self.request.output_directory]
            )
        if self.command.argv != expected_argv:
            raise ValueError("command argv does not match request")
        parsed_identifier = parse_compressor_job_identifier(
            self.command.stdout
        )
        if self.job_identifier != parsed_identifier:
            raise ValueError("job_identifier does not match command stdout")
        return self


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
    "FCPFinalCut12Request",
    "FCPFinalCut12Result",
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
    "FinalCut12FeatureAction",
    "FinalCutBrowserSearchScope",
    "LibraryRecord",
    "ProjectRecord",
    "VerificationStatus",
    "parse_compressor_job_identifier",
]
