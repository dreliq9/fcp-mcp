"""FCP-MCP Server — Final Cut Pro MCP Server.

The most capable FCP MCP server: FCPXML engine + live FCP control + media analysis.
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import xml.etree.ElementTree as ET
from collections.abc import Callable, Collection
from dataclasses import asdict
from itertools import pairwise
from math import isfinite
from pathlib import Path
from typing import Any
from urllib.parse import unquote as url_unquote

from .automation import osascript as automation
from .config import RuntimeConfig
from .contracts import DoctorReport, ErrorCode, FCPMCPError
from .diagnostics import collect_doctor
from .fcpxml.analysis import (
    analyze_pacing,
    analyze_timeline_stats,
    detect_duplicates,
    detect_flash_frames,
    detect_gaps,
)
from .fcpxml.diff import diff_files
from .fcpxml.generator import FCPXMLGenerator
from .fcpxml.models import FCPXMLDocument
from .fcpxml.parser import FCPXMLParser
from .fcpxml.puppet import (
    Keyframe,
    PartAnimation,
    PuppetSceneBuilder,
    preset_bounce,
    preset_idle,
    preset_talk,
    preset_walk,
    preset_wave,
    rig_from_json,
    standard_humanoid_rig,
)
from .fcpxml.time_utils import RationalTime
from .fcpxml.transaction import (
    FCPXMLTransactionReceipt,
    commit_fcpxml,
    commit_fcpxml_bytes,
)
from .fcpxml.validator import FCPXMLValidator
from .fcpxml.writer import FCPXMLModifier
from .mcp_boundary import FCPFastMCP, build_mcp_server  # noqa: F401
from .profiles import Profile, ToolClass
from .registry import PromptRegistry, ToolRegistry
from .result_models.common import ArtifactReference, ToolOutcome
from .result_models.fcpxml import (
    AppliedEffectRecord,
    AudioLevelCheckResult,
    AudioLevelObservationRecord,
    CleanupMutationResult,
    ClipBatchMutationResult,
    ClipFieldMutationRecord,
    ClipListResult,
    ClipMutationRecord,
    ClipMutationResult,
    ClipRecord,
    DiffChangeRecord,
    DiffCountsRecord,
    DuplicateDetectionResult,
    DurationCheckResult,
    EDLImportResult,
    EffectInventoryResult,
    EffectParameterRecord,
    ExportResult,
    FCPXMLDiffResult,
    FCPXMLGenerationResult,
    FCPXMLMutationResult,
    FCPXMLSummaryResult,
    FCPXMLValidationResult,
    FlashFrameDetectionResult,
    FrameRateCheckResult,
    FrameRateFormatRecord,
    FrameRateMismatchRecord,
    GapDetectionResult,
    InstalledEffectListResult,
    KeywordMutationRecord,
    KeywordMutationResult,
    KeywordRecord,
    MarkerBatchMutationResult,
    MarkerListResult,
    MarkerMutationRecord,
    MarkerMutationResult,
    MarkerRecord,
    MediaLinkCheckResult,
    MotionTemplateListResult,
    MotionTemplateRecord,
    PacingAnalysisResult,
    PacingHistogram,
    PacingRecord,
    ProjectDiffRecord,
    QCReportResult,
    RoleBatchMutationResult,
    RoleListResult,
    RoleMutationRecord,
    RoleMutationResult,
    RoleRuleRecord,
    SafeZoneCheckResult,
    SafeZoneObservationRecord,
    ShareDestinationListResult,
    SubtitleImportResult,
    TemplateListResult,
    TemplateSaveResult,
    TimelineElementMutationRecord,
    TimelineElementMutationResult,
    TimelineStatsResult,
    TransactionReceiptResult,
    TransitionBatchMutationResult,
    TransitionMutationRecord,
    TransitionMutationResult,
    UnsupportedToolResult,
)
from .result_models.media import (
    AudioStreamRecord,
    BeatCadenceRecord,
    BeatDetectionResult,
    BeatRecord,
    LoudnessResult,
    MediaInfoResult,
    ParserWarningRecord,
    SceneChangeRecord,
    SceneDetectionResult,
    SilenceDetectionResult,
    SilenceRangeRecord,
    StreamListResult,
    StreamRecord,
    SubtitleStreamRecord,
    UnsupportedStreamRecord,
    VideoStreamRecord,
)
from .result_models.puppet import (
    PuppetPresetListResult,
    PuppetPresetParameterRecord,
    PuppetPresetRecord,
)
from .security.paths import PathPolicy
from .tool_metadata import (
    DIAGNOSTIC,
    LIVE_READ,
    LIVE_WRITE,
    OFFLINE_READ,
    OFFLINE_WRITE,
    STATEFUL_WRITE,
)
from .utils.atomic_write import AtomicWriteReceipt, atomic_replace_bytes

logger = logging.getLogger(__name__)

CONFIG = RuntimeConfig.from_env()
PATHS = PathPolicy(CONFIG)
TOOLS = ToolRegistry()
PROMPTS = PromptRegistry()

_parser = FCPXMLParser()
_validator = FCPXMLValidator()


def catalog_expectations(
    profile: Profile,
) -> tuple[frozenset[str], frozenset[str]]:
    visible_tools = TOOLS.for_profile(profile)
    visible_tool_names = frozenset(
        definition.name for definition in visible_tools
    )
    visible_prompt_names = frozenset(
        definition.name
        for definition in PROMPTS.for_tools(visible_tool_names)
    )
    return visible_tool_names, visible_prompt_names


def _resolve_input(
    path: str,
    *,
    suffixes: Collection[str] = (),
    kind: str = "file",
) -> Path:
    return PATHS.resolve_input(path, suffixes=suffixes, kind=kind)


def _resolve_output(
    output_path: str,
    *,
    input_path: Path | None = None,
    default_name: str | None = None,
    suffixes: Collection[str] = (),
) -> Path:
    return PATHS.resolve_output(
        output_path or None,
        input_path=input_path,
        default_name=default_name,
        suffixes=suffixes,
    )


def _save_modifier(modifier: FCPXMLModifier, output_path: str = "") -> Path:
    destination = _resolve_output(
        output_path,
        input_path=modifier.path,
        suffixes={".fcpxml"},
    )
    return modifier.save(destination, event_format=CONFIG.log_format)


def _save_modifier_receipt(
    modifier: FCPXMLModifier,
    output_path: str = "",
    *,
    validate_candidate: Callable[[Path], None] | None = None,
) -> FCPXMLTransactionReceipt:
    destination = _resolve_output(
        output_path,
        input_path=modifier.path,
        suffixes={".fcpxml"},
    )
    if validate_candidate is None:
        return modifier.save_with_receipt(
            destination,
            event_format=CONFIG.log_format,
        )
    ET.indent(modifier.root, space="    ")
    xml_text = ET.tostring(
        modifier.root,
        encoding="unicode",
        xml_declaration=True,
    )
    return commit_fcpxml(
        source=modifier.path,
        destination=destination,
        xml_text=xml_text,
        validate_candidate=validate_candidate,
        event_format=CONFIG.log_format,
        operation="modifier_save",
    )


def _receipt_result(
    receipt: FCPXMLTransactionReceipt,
) -> TransactionReceiptResult:
    return TransactionReceiptResult(
        transaction_id=receipt.transaction_id,
        source=str(receipt.source) if receipt.source is not None else None,
        destination=str(receipt.destination),
        backup_path=(
            str(receipt.backup_path)
            if receipt.backup_path is not None
            else None
        ),
        input_sha256=receipt.input_sha256,
        prior_sha256=receipt.prior_sha256,
        output_sha256=receipt.output_sha256,
        validation_warnings=list(receipt.validation_warnings),
        elapsed_ms=receipt.elapsed_ms,
        disposition=receipt.disposition,
    )


def _atomic_receipt_result(
    receipt: AtomicWriteReceipt,
    *,
    source: ArtifactReference,
) -> TransactionReceiptResult:
    return TransactionReceiptResult(
        transaction_id=receipt.transaction_id,
        source=source.path,
        destination=str(receipt.destination),
        backup_path=(
            str(receipt.backup_path)
            if receipt.backup_path is not None
            else None
        ),
        input_sha256=source.sha256,
        prior_sha256=receipt.prior_sha256,
        output_sha256=receipt.output_sha256,
        validation_warnings=[],
        elapsed_ms=receipt.elapsed_ms,
        disposition="committed",
    )


def _raise_validation_failure(message: str) -> None:
    raise FCPMCPError(ErrorCode.VALIDATION_FAILED, message)


def _validate_resolve_export(path: Path) -> None:
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as error:
        raise FCPMCPError(
            ErrorCode.VALIDATION_FAILED,
            "Resolve export is not valid XML",
        ) from error
    if root.tag != "fcpxml" or root.get("version") != "1.9":
        _raise_validation_failure(
            "Resolve export must be FCPXML version 1.9"
        )


def _validate_fcp7_export(path: Path) -> None:
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as error:
        raise FCPMCPError(
            ErrorCode.VALIDATION_FAILED,
            "FCP7 export is not valid XML",
        ) from error
    if root.tag != "xmeml" or root.get("version") != "5":
        _raise_validation_failure(
            "FCP7 export must be XMEML version 5"
        )


def _validate_edl_export(
    path: Path,
    *,
    expected_edit_count: int,
) -> None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError as error:
        raise FCPMCPError(
            ErrorCode.VALIDATION_FAILED,
            "EDL export is not valid UTF-8",
        ) from error
    if (
        len(lines) < 2
        or not lines[0].startswith("TITLE: ")
        or not lines[1].startswith("FCM: ")
    ):
        _raise_validation_failure(
            "EDL export is missing its required header"
        )
    edit_count = sum(
        len(line) >= 3 and line[:3].isdigit()
        for line in lines
    )
    if edit_count != expected_edit_count:
        _raise_validation_failure(
            "EDL export edit count is inconsistent"
        )


def _artifact_reference(
    receipt: FCPXMLTransactionReceipt,
) -> ArtifactReference:
    return ArtifactReference(
        path=str(receipt.destination),
        media_type="application/vnd.apple.fcpxml+xml",
        sha256=receipt.output_sha256,
        size_bytes=receipt.destination.stat().st_size,
    )


def _artifact_reference_for_path(
    path: Path,
    *,
    media_type: str = "application/vnd.apple.fcpxml+xml",
) -> ArtifactReference:
    resolved = path.resolve()
    digest = hashlib.sha256()
    with resolved.open("rb") as artifact:
        for chunk in iter(lambda: artifact.read(1024 * 1024), b""):
            digest.update(chunk)
    return ArtifactReference(
        path=str(resolved),
        media_type=media_type,
        sha256=digest.hexdigest(),
        size_bytes=resolved.stat().st_size,
    )


def _committed_root(
    receipt: FCPXMLTransactionReceipt,
) -> ET.Element:
    return ET.parse(receipt.destination).getroot()


def _committed_named_element(
    root: ET.Element,
    *,
    name: str,
    tags: Collection[str],
) -> ET.Element:
    element = next(
        (
            candidate
            for candidate in root.iter()
            if candidate.tag in tags and candidate.get("name") == name
        ),
        None,
    )
    if element is None:
        raise RuntimeError(
            f"Committed FCPXML is missing expected entity '{name}'"
        )
    return element


def _clip_mutation_record(element: ET.Element) -> ClipMutationRecord:
    return ClipMutationRecord(
        name=element.get("name", ""),
        element_type=element.tag,
        offset=element.get("offset", ""),
        start=element.get("start", ""),
        duration=element.get("duration", ""),
        role=element.get("role", ""),
        ref=element.get("ref", ""),
    )


def _spine_clip_names(root: ET.Element) -> list[str]:
    spine = root.find(".//spine")
    if spine is None:
        raise RuntimeError("Committed FCPXML is missing its timeline spine")
    return [
        child.get("name", "")
        for child in spine
        if child.tag != "transition"
    ]


def _save_generator(
    generator: FCPXMLGenerator,
    output_path: str,
    *,
    default_name: str,
) -> Path:
    destination = _resolve_output(
        output_path,
        default_name=default_name,
        suffixes={".fcpxml"},
    )
    return generator.save(destination, event_format=CONFIG.log_format)


def _save_generator_receipt(
    generator: FCPXMLGenerator,
    output_path: str,
    *,
    default_name: str,
    validate_candidate: Callable[[Path], None] | None = None,
) -> FCPXMLTransactionReceipt:
    destination = _resolve_output(
        output_path,
        default_name=default_name,
        suffixes={".fcpxml"},
    )
    return commit_fcpxml(
        source=None,
        destination=destination,
        xml_text=f"{generator.to_string()}\n",
        validate_candidate=validate_candidate,
        event_format=CONFIG.log_format,
        operation="generator_save",
    )


def _generation_evidence(path: Path) -> tuple[str, int, float]:
    root = ET.parse(path).getroot()
    project = next(root.iter("project"), None)
    spine = root.find(".//spine")
    if project is None or spine is None:
        _raise_validation_failure(
            "Generated FCPXML is missing project or timeline evidence"
        )
    selected = [child for child in spine if child.tag != "transition"]
    duration_seconds = sum(
        RationalTime.from_fcpxml(
            child.get("duration", "0s")
        ).to_seconds()
        for child in selected
    )
    return project.get("name", ""), len(selected), duration_seconds


def _save_generation_result(
    generator: FCPXMLGenerator,
    output_path: str,
    *,
    default_name: str,
) -> tuple[FCPXMLTransactionReceipt, FCPXMLGenerationResult]:
    evidence = ("", 0, 0.0)

    def validate_generation(candidate: Path) -> None:
        nonlocal evidence
        evidence = _generation_evidence(candidate)

    receipt = _save_generator_receipt(
        generator,
        output_path,
        default_name=default_name,
        validate_candidate=validate_generation,
    )
    project, selected_clip_count, target_duration_seconds = evidence
    return receipt, FCPXMLGenerationResult(
        project=project,
        selected_clip_count=selected_clip_count,
        target_duration_seconds=target_duration_seconds,
        destination=_artifact_reference(receipt),
        receipt=_receipt_result(receipt),
    )


def _load_json(raw: str, label: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as error:
        raise FCPMCPError(
            ErrorCode.INVALID_ARGUMENTS,
            f"{label} must be valid JSON: {error.msg}",
        ) from error


def _load_json_list(raw: str, label: str) -> list[Any]:
    value = _load_json(raw, label)
    if not isinstance(value, list):
        raise FCPMCPError(
            ErrorCode.INVALID_ARGUMENTS,
            f"{label} must be a JSON array",
        )
    return value


def _load_json_object(raw: str, label: str) -> dict[str, Any]:
    value = _load_json(raw, label)
    if not isinstance(value, dict):
        raise FCPMCPError(
            ErrorCode.INVALID_ARGUMENTS,
            f"{label} must be a JSON object",
        )
    return value


def _parse_time(raw: str, label: str) -> RationalTime:
    try:
        return RationalTime.from_fcpxml(raw)
    except (ValueError, ZeroDivisionError) as error:
        raise FCPMCPError(
            ErrorCode.INVALID_ARGUMENTS,
            f"{label} must be a valid FCPXML time: {raw}",
        ) from error


def _resolve_clip_sources(
    clips: list[Any],
) -> list[dict[str, Any]]:
    resolved_clips = []
    for index, clip in enumerate(clips):
        if (
            not isinstance(clip, dict)
            or not isinstance(clip.get("src"), str)
            or not isinstance(clip.get("duration"), str)
        ):
            raise FCPMCPError(
                ErrorCode.INVALID_ARGUMENTS,
                (
                    f"clips_json[{index}] must be an object with string "
                    "src and duration fields"
                ),
            )
        resolved = dict(clip)
        resolved["src"] = str(_resolve_input(str(clip["src"])))
        _parse_time(resolved["duration"], "clip duration")
        if "start" in resolved:
            if not isinstance(resolved["start"], str):
                raise FCPMCPError(
                    ErrorCode.INVALID_ARGUMENTS,
                    f"clips_json[{index}].start must be a string",
                )
            _parse_time(str(resolved["start"]), "clip start")
        resolved_clips.append(resolved)
    return resolved_clips


def _resolve_rig_images(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict) or not isinstance(data.get("name"), str):
        raise FCPMCPError(
            ErrorCode.INVALID_ARGUMENTS,
            "Each rig must be an object with a string name",
        )
    resolved = dict(data)
    resolved_parts = []
    parts = data.get("parts", [])
    if not isinstance(parts, list):
        raise FCPMCPError(
            ErrorCode.INVALID_ARGUMENTS,
            "Rig parts must be a JSON array",
        )
    if not parts:
        raise FCPMCPError(
            ErrorCode.INVALID_ARGUMENTS,
            "Each rig must contain at least one part",
        )
    for index, part in enumerate(parts):
        if (
            not isinstance(part, dict)
            or not isinstance(part.get("name"), str)
            or not isinstance(part.get("image"), str)
        ):
            raise FCPMCPError(
                ErrorCode.INVALID_ARGUMENTS,
                f"parts[{index}] must contain string name and image fields",
            )
        resolved_part = dict(part)
        resolved_part["image"] = str(
            _resolve_input(
                str(part["image"]),
                suffixes={".png", ".jpg", ".jpeg", ".tif", ".tiff"},
            )
        )
        resolved_parts.append(resolved_part)
    resolved["parts"] = resolved_parts
    return resolved


def _load_rigs(raw: str, label: str = "rigs_json") -> list[dict[str, Any]]:
    value = _load_json(raw, label)
    if isinstance(value, dict):
        values = [value]
    elif isinstance(value, list):
        values = value
    else:
        raise FCPMCPError(
            ErrorCode.INVALID_ARGUMENTS,
            f"{label} must be a JSON object or array",
        )
    if not values:
        raise FCPMCPError(
            ErrorCode.INVALID_ARGUMENTS,
            f"{label} must contain at least one rig",
        )
    return [_resolve_rig_images(data) for data in values]


def _parse_doc(path: str) -> FCPXMLDocument:
    """Parse an FCPXML file and return the document."""
    return _parser.parse(_resolve_input(path, suffixes={".fcpxml"}))


def _serializable(obj: Any) -> Any:
    """Make dataclass/enum objects JSON-serializable."""
    if hasattr(obj, "__dataclass_fields__"):
        d = {}
        for k, v in asdict(obj).items():
            if k.startswith("_"):
                continue
            d[k] = v
        return d
    return obj


# ============================================================================
# Category 0: Runtime diagnostics (1 tool)
# ============================================================================

@TOOLS.tool(tool_class=ToolClass.INSPECT, safety_hints=DIAGNOSTIC)
async def fcp_doctor() -> DoctorReport:
    """Report structured runtime readiness without prompting or mutating user data."""

    async def catalog() -> tuple[tuple[str, ...], tuple[str, ...]]:
        tools = await mcp.list_tools()
        prompts = await mcp.list_prompts()
        return (
            tuple(tool.name for tool in tools),
            tuple(prompt.name for prompt in prompts),
        )

    visible_tool_names, visible_prompt_names = catalog_expectations(
        CONFIG.profile
    )
    return await collect_doctor(
        CONFIG,
        catalog_provider=catalog,
        expected_tool_names=visible_tool_names,
        expected_prompt_names=visible_prompt_names,
    )


# ============================================================================
# Category 2: FCPXML Analysis (12 tools)
# ============================================================================

@TOOLS.tool(
    tool_class=ToolClass.INSPECT,
    safety_hints=OFFLINE_READ,
    result_model=FCPXMLSummaryResult,
)
def fcpxml_parse(path: str) -> ToolOutcome[FCPXMLSummaryResult]:
    """Parse an FCPXML file and return a structure summary.

    Args:
        path: Path to .fcpxml file (absolute or relative to FCP_PROJECTS_DIR)
    """
    doc = _parse_doc(path)
    projects = doc.all_projects
    summary = {
        "version": doc.version,
        "formats": len(doc.formats),
        "assets": len(doc.assets),
        "effects": len(doc.effects),
        "projects": [
            {
                "name": p.name,
                "has_sequence": p.sequence is not None,
                "clip_count": p.sequence.spine.clip_count if p.sequence and p.sequence.spine else 0,
                "duration": p.sequence.duration.to_fcpxml() if p.sequence else "0s",
            }
            for p in projects
        ],
    }
    return ToolOutcome(
        text=json.dumps(summary, indent=2),
        structured=FCPXMLSummaryResult.model_validate(summary),
    )


@TOOLS.tool(
    tool_class=ToolClass.INSPECT,
    safety_hints=OFFLINE_READ,
    result_model=ClipListResult,
)
def fcpxml_list_clips(
    path: str,
    project_name: str = "",
) -> ToolOutcome[ClipListResult]:
    """List all clips in the timeline with timecodes, durations, and roles.

    Args:
        path: Path to .fcpxml file
        project_name: Optional project name filter (uses first project if empty)
    """
    doc = _parse_doc(path)
    fmt = next(iter(doc.formats.values())) if doc.formats else None
    fps = fmt.fps if fmt else 29.97

    clips_data = []
    clip_records = []
    for project in doc.all_projects:
        if project_name and project.name != project_name:
            continue
        if not project.sequence or not project.sequence.spine:
            continue
        for i, clip in enumerate(project.sequence.spine.clips):
            clips_data.append(
                {
                    "index": i,
                    "name": clip.name,
                    "type": clip.clip_type.value,
                    "offset": clip.offset.to_timecode(fps),
                    "offset_raw": clip.offset.to_fcpxml(),
                    "start": clip.start.to_fcpxml(),
                    "duration": f"{clip.duration.to_seconds():.3f}s",
                    "duration_raw": clip.duration.to_fcpxml(),
                    "role": clip.role,
                    "ref": clip.ref,
                    "connected_clips": len(clip.connected_clips),
                    "markers": len(clip.markers),
                }
            )
            clip_records.append(
                ClipRecord(
                    index=i,
                    name=clip.name,
                    clip_type=clip.clip_type.value,
                    offset=clip.offset.to_timecode(fps),
                    offset_raw=clip.offset.to_fcpxml(),
                    start=clip.start.to_fcpxml(),
                    duration_seconds=clip.duration.to_seconds(),
                    duration_raw=clip.duration.to_fcpxml(),
                    role=clip.role,
                    ref=clip.ref,
                    connected_clip_count=len(clip.connected_clips),
                    marker_count=len(clip.markers),
                )
            )
    return ToolOutcome(
        text=json.dumps(clips_data, indent=2),
        structured=ClipListResult(clips=clip_records),
    )


@TOOLS.tool(
    tool_class=ToolClass.INSPECT,
    safety_hints=OFFLINE_READ,
    result_model=MarkerListResult,
)
def fcpxml_list_markers(path: str) -> ToolOutcome[MarkerListResult]:
    """List all markers, chapter markers, and keywords across all clips.

    Args:
        path: Path to .fcpxml file
    """
    doc = _parse_doc(path)
    fmt = next(iter(doc.formats.values())) if doc.formats else None
    fps = fmt.fps if fmt else 29.97

    markers = []
    marker_records = []
    keyword_records = []
    for project in doc.all_projects:
        if not project.sequence or not project.sequence.spine:
            continue
        for clip in project.sequence.spine.clips:
            for m in clip.markers:
                markers.append(
                    {
                        "clip": clip.name,
                        "type": m.marker_type.value,
                        "value": m.value,
                        "note": m.note,
                        "start": m.start.to_timecode(fps),
                        "start_raw": m.start.to_fcpxml(),
                    }
                )
                marker_records.append(
                    MarkerRecord(
                        clip=clip.name,
                        marker_type=m.marker_type.value,
                        value=m.value,
                        note=m.note,
                        start_timecode=m.start.to_timecode(fps),
                        start_raw=m.start.to_fcpxml(),
                    )
                )
            for kw in clip.keywords:
                markers.append(
                    {
                        "clip": clip.name,
                        "type": "keyword",
                        "value": kw.value,
                        "start": kw.start.to_fcpxml(),
                        "duration": kw.duration.to_fcpxml(),
                    }
                )
                keyword_records.append(
                    KeywordRecord(
                        clip=clip.name,
                        value=kw.value,
                        start_raw=kw.start.to_fcpxml(),
                        duration_raw=kw.duration.to_fcpxml(),
                    )
                )
    return ToolOutcome(
        text=json.dumps(markers, indent=2),
        structured=MarkerListResult(
            markers=marker_records,
            keywords=keyword_records,
        ),
    )


@TOOLS.tool(
    tool_class=ToolClass.INSPECT,
    safety_hints=OFFLINE_READ,
    result_model=PacingAnalysisResult,
)
def fcpxml_analyze_pacing(path: str) -> ToolOutcome[PacingAnalysisResult]:
    """Analyze shot pacing — average/median shot length, distribution histogram.

    Args:
        path: Path to .fcpxml file
    """
    doc = _parse_doc(path)
    results = analyze_pacing(doc)
    legacy_payload = [_serializable(result) for result in results]
    analyses = [
        PacingRecord(
            average_shot_length=result.average_shot_length,
            median_shot_length=result.median_shot_length,
            std_deviation=result.std_deviation,
            shortest_shot=result.shortest_shot,
            longest_shot=result.longest_shot,
            pacing_curve=result.pacing_curve,
            histogram=PacingHistogram(
                under_one_second=result.histogram.get("< 1s", 0),
                one_to_three_seconds=result.histogram.get("1-3s", 0),
                three_to_five_seconds=result.histogram.get("3-5s", 0),
                five_to_ten_seconds=result.histogram.get("5-10s", 0),
                ten_to_thirty_seconds=result.histogram.get("10-30s", 0),
                thirty_seconds_or_more=result.histogram.get("30s+", 0),
            ),
        )
        for result in results
    ]
    return ToolOutcome(
        text=json.dumps(legacy_payload, indent=2),
        structured=PacingAnalysisResult(analyses=analyses),
    )


@TOOLS.tool(
    tool_class=ToolClass.INSPECT,
    safety_hints=OFFLINE_READ,
    result_model=GapDetectionResult,
)
def fcpxml_detect_gaps(path: str) -> ToolOutcome[GapDetectionResult]:
    """Find all gaps in the timeline.

    Args:
        path: Path to .fcpxml file
    """
    doc = _parse_doc(path)
    gaps = detect_gaps(doc)
    legacy_payload = [_serializable(gap) for gap in gaps]
    return ToolOutcome(
        text=json.dumps(legacy_payload, indent=2),
        structured=GapDetectionResult(
            count=len(legacy_payload),
            gaps=legacy_payload,
        ),
    )


@TOOLS.tool(
    tool_class=ToolClass.INSPECT,
    safety_hints=OFFLINE_READ,
    result_model=FlashFrameDetectionResult,
)
def fcpxml_detect_flash_frames(
    path: str,
    max_frames: int = 2,
) -> ToolOutcome[FlashFrameDetectionResult]:
    """Find clips shorter than max_frames (potential flash frames / accidental edits).

    Args:
        path: Path to .fcpxml file
        max_frames: Maximum frame count to flag (default 2)
    """
    doc = _parse_doc(path)
    flashes = detect_flash_frames(doc, max_frames=max_frames)
    legacy_payload = [_serializable(flash) for flash in flashes]
    return ToolOutcome(
        text=json.dumps(legacy_payload, indent=2),
        structured=FlashFrameDetectionResult(
            max_frames=max_frames,
            count=len(legacy_payload),
            items=legacy_payload,
        ),
    )


@TOOLS.tool(
    tool_class=ToolClass.INSPECT,
    safety_hints=OFFLINE_READ,
    result_model=DuplicateDetectionResult,
)
def fcpxml_detect_duplicates(
    path: str,
) -> ToolOutcome[DuplicateDetectionResult]:
    """Find clips that use the same source media.

    Args:
        path: Path to .fcpxml file
    """
    doc = _parse_doc(path)
    dupes = detect_duplicates(doc)
    legacy_payload = [_serializable(dupe) for dupe in dupes]
    return ToolOutcome(
        text=json.dumps(legacy_payload, indent=2),
        structured=DuplicateDetectionResult(groups=legacy_payload),
    )


@TOOLS.tool(
    tool_class=ToolClass.INSPECT,
    safety_hints=OFFLINE_READ,
    result_model=FCPXMLValidationResult,
)
def fcpxml_validate(path: str) -> ToolOutcome[FCPXMLValidationResult]:
    """Validate FCPXML structure and report errors/warnings.

    Args:
        path: Path to .fcpxml file
    """
    result = _validator.validate_file(
        _resolve_input(path, suffixes={".fcpxml"})
    )
    summary = result.summary()
    return ToolOutcome(
        text=summary,
        structured=FCPXMLValidationResult(
            valid=result.valid,
            issues=[_serializable(issue) for issue in result.issues],
            error_count=len(result.errors),
            warning_count=len(result.warnings),
            info_count=(
                len(result.issues) - len(result.errors) - len(result.warnings)
            ),
            summary=summary,
        ),
    )


@TOOLS.tool(
    tool_class=ToolClass.INSPECT,
    safety_hints=OFFLINE_READ,
    result_model=EffectInventoryResult,
)
def fcpxml_list_effects(path: str) -> ToolOutcome[EffectInventoryResult]:
    """List all effects and transitions applied to clips.

    Args:
        path: Path to .fcpxml file
    """
    doc = _parse_doc(path)
    effects = []
    applied_records = []
    for project in doc.all_projects:
        if not project.sequence or not project.sequence.spine:
            continue
        for clip in project.sequence.spine.clips:
            for effect in clip.effects:
                effects.append(
                    {
                        "clip": clip.name,
                        "effect_name": effect.name,
                        "effect_ref": effect.ref,
                        "enabled": effect.enabled,
                        "parameters": effect.parameters,
                    }
                )
                applied_records.append(
                    AppliedEffectRecord(
                        clip=clip.name,
                        effect_name=effect.name,
                        effect_ref=effect.ref,
                        enabled=effect.enabled,
                        parameters=[
                            EffectParameterRecord(name=name, value=value)
                            for name, value in effect.parameters.items()
                        ],
                    )
                )
    # Also list effect resources
    resources = [{"id": k, "name": v.name, "uid": v.uid} for k, v in doc.effects.items()]
    legacy_payload = {"applied": effects, "available": resources}
    return ToolOutcome(
        text=json.dumps(legacy_payload, indent=2),
        structured=EffectInventoryResult(
            applied=applied_records,
            available=resources,
        ),
    )


@TOOLS.tool(
    tool_class=ToolClass.INSPECT,
    safety_hints=OFFLINE_READ,
    result_model=RoleListResult,
)
def fcpxml_list_roles(path: str) -> ToolOutcome[RoleListResult]:
    """List all roles and subroles used in the timeline.

    Args:
        path: Path to .fcpxml file
    """
    doc = _parse_doc(path)
    roles = set()
    for clip in doc.all_clips:
        if clip.role:
            roles.add(clip.role)
        for cc in clip.connected_clips:
            if cc.role:
                roles.add(cc.role)
    legacy_payload = {"roles": sorted(roles)}
    return ToolOutcome(
        text=json.dumps(legacy_payload, indent=2),
        structured=RoleListResult.model_validate(legacy_payload),
    )


@TOOLS.tool(
    tool_class=ToolClass.INSPECT,
    safety_hints=OFFLINE_READ,
    result_model=TimelineStatsResult,
)
def fcpxml_timeline_stats(path: str) -> ToolOutcome[TimelineStatsResult]:
    """Get comprehensive timeline statistics — duration, clip count, resolution, pacing, etc.

    Args:
        path: Path to .fcpxml file
    """
    doc = _parse_doc(path)
    stats = analyze_timeline_stats(doc)
    legacy_payload = [_serializable(project_stats) for project_stats in stats]
    return ToolOutcome(
        text=json.dumps(legacy_payload, indent=2),
        structured=TimelineStatsResult(projects=legacy_payload),
    )


@TOOLS.tool(
    tool_class=ToolClass.INSPECT,
    safety_hints=OFFLINE_READ,
    result_model=FCPXMLDiffResult,
)
def fcpxml_diff(
    path_a: str,
    path_b: str,
) -> ToolOutcome[FCPXMLDiffResult]:
    """Compare two FCPXML files and show differences.

    Args:
        path_a: Path to first .fcpxml file
        path_b: Path to second .fcpxml file
    """
    results = diff_files(
        _resolve_input(path_a, suffixes={".fcpxml"}),
        _resolve_input(path_b, suffixes={".fcpxml"}),
    )
    summary = "\n\n".join(result.summary() for result in results)
    project_records = []
    all_changes = []
    for result in results:
        changes = [
            DiffChangeRecord(
                project_a=result.project_a,
                project_b=result.project_b,
                change_type=change.change_type,
                clip_name=change.clip_name,
                details=change.details,
            )
            for change in result.changes
        ]
        counts = DiffCountsRecord(
            added=result.clips_added,
            removed=result.clips_removed,
            moved=result.clips_moved,
            trimmed=result.clips_trimmed,
            unchanged=result.clips_unchanged,
        )
        project_records.append(
            ProjectDiffRecord(
                project_a=result.project_a,
                project_b=result.project_b,
                changes=changes,
                counts=counts,
            )
        )
        all_changes.extend(changes)
    aggregate_counts = DiffCountsRecord(
        added=sum(result.clips_added for result in results),
        removed=sum(result.clips_removed for result in results),
        moved=sum(result.clips_moved for result in results),
        trimmed=sum(result.clips_trimmed for result in results),
        unchanged=sum(result.clips_unchanged for result in results),
    )
    return ToolOutcome(
        text=summary,
        structured=FCPXMLDiffResult(
            projects=project_records,
            changes=all_changes,
            counts=aggregate_counts,
            summary=summary,
        ),
    )


# ============================================================================
# Category 3: FCPXML Editing (12 tools)
# ============================================================================

@TOOLS.tool(
    tool_class=ToolClass.OFFLINE_WRITE,
    safety_hints=OFFLINE_WRITE,
    result_model=MarkerMutationResult,
)
def fcpxml_add_marker(
    path: str,
    clip_name: str,
    start: str,
    value: str,
    note: str = "",
    marker_type: str = "standard",
    output_path: str = "",
) -> ToolOutcome[MarkerMutationResult]:
    """Add a marker to a clip.

    Args:
        path: Path to .fcpxml file
        clip_name: Name of the clip to add the marker to
        start: Marker position in FCPXML time (e.g., "60060/30000s")
        value: Marker title/value
        note: Optional marker note
        marker_type: "standard" or "chapter"
        output_path: Output file path (default: adds _modified suffix)
    """
    _parse_time(start, "start")
    if marker_type not in {"standard", "chapter"}:
        raise FCPMCPError(
            ErrorCode.INVALID_ARGUMENTS,
            "marker_type must be standard or chapter",
        )
    mod = FCPXMLModifier(_resolve_input(path, suffixes={".fcpxml"}))
    if not mod.add_marker(clip_name, start, value, note, marker_type):
        raise FCPMCPError(
            ErrorCode.TARGET_NOT_FOUND,
            f"Clip '{clip_name}' not found",
        )
    receipt = _save_modifier_receipt(mod, output_path)
    root = _committed_root(receipt)
    clip = _committed_named_element(
        root,
        name=clip_name,
        tags={"asset-clip", "clip", "title", "audio", "video"},
    )
    tag = "chapter-marker" if marker_type == "chapter" else "marker"
    marker = next(
        (
            candidate
            for candidate in clip.findall(tag)
            if candidate.get("start") == start
            and candidate.get("value") == value
            and candidate.get("note", "") == note
        ),
        None,
    )
    if marker is None:
        raise RuntimeError("Committed FCPXML is missing the added marker")
    text = f"Marker added. Saved to: {receipt.destination}"
    return ToolOutcome(
        text=text,
        structured=MarkerMutationResult(
            destination=_artifact_reference(receipt),
            receipt=_receipt_result(receipt),
            marker=MarkerMutationRecord(
                clip_name=clip_name,
                marker_type=marker_type,
                start=marker.get("start", ""),
                duration=marker.get("duration", ""),
                value=marker.get("value", ""),
                note=marker.get("note", ""),
            ),
        ),
    )


@TOOLS.tool(
    tool_class=ToolClass.OFFLINE_WRITE,
    safety_hints=OFFLINE_WRITE,
    result_model=MarkerBatchMutationResult,
)
def fcpxml_batch_add_markers(
    path: str,
    markers_json: str,
    output_path: str = "",
) -> ToolOutcome[MarkerBatchMutationResult]:
    """Add multiple markers at once.

    Args:
        path: Path to .fcpxml file
        markers_json: JSON array of markers, each: {"clip_name": str, "start": str, "value": str, "note"?: str, "type"?: str}
        output_path: Output file path (default: adds _modified suffix)
    """
    markers = _load_json_list(markers_json, "markers_json")
    if not markers:
        raise FCPMCPError(
            ErrorCode.INVALID_ARGUMENTS,
            "markers_json must contain at least one marker",
        )
    for index, marker in enumerate(markers):
        if (
            not isinstance(marker, dict)
            or not isinstance(marker.get("clip_name"), str)
            or not isinstance(marker.get("start"), str)
            or not isinstance(marker.get("value"), str)
        ):
            raise FCPMCPError(
                ErrorCode.INVALID_ARGUMENTS,
                (
                    f"markers_json[{index}] must contain string clip_name, "
                    "start, and value fields"
                ),
            )
        if marker.get("type", "standard") not in {"standard", "chapter"}:
            raise FCPMCPError(
                ErrorCode.INVALID_ARGUMENTS,
                f"markers_json[{index}].type must be standard or chapter",
            )
        _parse_time(marker["start"], f"markers_json[{index}].start")
    mod = FCPXMLModifier(_resolve_input(path, suffixes={".fcpxml"}))
    count = mod.batch_add_markers(markers)
    if count != len(markers):
        raise FCPMCPError(
            ErrorCode.TARGET_NOT_FOUND,
            f"{len(markers) - count} marker target(s) were not found",
        )
    receipt = _save_modifier_receipt(mod, output_path)
    root = _committed_root(receipt)
    marker_records = []
    for requested in markers:
        requested_type = requested.get("type", "standard")
        clip = _committed_named_element(
            root,
            name=requested["clip_name"],
            tags={"asset-clip", "clip", "title", "audio", "video"},
        )
        tag = (
            "chapter-marker"
            if requested_type == "chapter"
            else "marker"
        )
        committed = next(
            (
                candidate
                for candidate in clip.findall(tag)
                if candidate.get("start") == requested["start"]
                and candidate.get("value") == requested["value"]
                and candidate.get("note", "") == requested.get("note", "")
            ),
            None,
        )
        if committed is None:
            raise RuntimeError(
                "Committed FCPXML is missing a batch-added marker"
            )
        marker_records.append(
            MarkerMutationRecord(
                clip_name=requested["clip_name"],
                marker_type=requested_type,
                start=committed.get("start", ""),
                duration=committed.get("duration", ""),
                value=committed.get("value", ""),
                note=committed.get("note", ""),
            )
        )
    text = (
        f"{count}/{len(markers)} markers added. "
        f"Saved to: {receipt.destination}"
    )
    return ToolOutcome(
        text=text,
        structured=MarkerBatchMutationResult(
            destination=_artifact_reference(receipt),
            receipt=_receipt_result(receipt),
            requested_count=len(markers),
            changed_count=count,
            markers=marker_records,
        ),
    )


@TOOLS.tool(
    tool_class=ToolClass.OFFLINE_WRITE,
    safety_hints=OFFLINE_WRITE,
    result_model=KeywordMutationResult,
)
def fcpxml_add_keyword(
    path: str, clip_name: str, value: str, start: str = "0s",
    duration: str = "", output_path: str = "",
) -> ToolOutcome[KeywordMutationResult]:
    """Add a keyword to a clip.

    Args:
        path: Path to .fcpxml file
        clip_name: Name of the clip
        value: Keyword text
        start: Start time in FCPXML format
        duration: Duration of keyword range (optional)
        output_path: Output file path
    """
    _parse_time(start, "start")
    if duration:
        _parse_time(duration, "duration")
    mod = FCPXMLModifier(_resolve_input(path, suffixes={".fcpxml"}))
    if not mod.add_keyword(clip_name, value, start, duration or None):
        raise FCPMCPError(
            ErrorCode.TARGET_NOT_FOUND,
            f"Clip '{clip_name}' not found",
        )
    receipt = _save_modifier_receipt(mod, output_path)
    root = _committed_root(receipt)
    clip = _committed_named_element(
        root,
        name=clip_name,
        tags={"asset-clip", "clip", "title", "audio", "video"},
    )
    keyword = next(
        (
            candidate
            for candidate in clip.findall("keyword")
            if candidate.get("start") == start
            and candidate.get("value") == value
            and candidate.get("duration") == (duration or None)
        ),
        None,
    )
    if keyword is None:
        raise RuntimeError("Committed FCPXML is missing the added keyword")
    text = f"Keyword '{value}' added. Saved to: {receipt.destination}"
    return ToolOutcome(
        text=text,
        structured=KeywordMutationResult(
            destination=_artifact_reference(receipt),
            receipt=_receipt_result(receipt),
            keyword=KeywordMutationRecord(
                clip_name=clip_name,
                value=keyword.get("value", ""),
                start=keyword.get("start", ""),
                duration=keyword.get("duration"),
            ),
        ),
    )


@TOOLS.tool(
    tool_class=ToolClass.OFFLINE_WRITE,
    safety_hints=OFFLINE_WRITE,
    result_model=ClipMutationResult,
)
def fcpxml_trim_clip(
    path: str, clip_name: str,
    new_start: str = "", new_duration: str = "",
    output_path: str = "",
) -> ToolOutcome[ClipMutationResult]:
    """Trim a clip's source in/out points.

    Args:
        path: Path to .fcpxml file
        clip_name: Name of the clip to trim
        new_start: New source start time (optional)
        new_duration: New duration (optional)
        output_path: Output file path
    """
    if new_start:
        _parse_time(new_start, "new_start")
    if new_duration:
        _parse_time(new_duration, "new_duration")
    mod = FCPXMLModifier(_resolve_input(path, suffixes={".fcpxml"}))
    original = mod._find_clip_by_name(clip_name)
    before_start = original.get("start", "") if original is not None else ""
    before_duration = (
        original.get("duration", "") if original is not None else ""
    )
    if not mod.trim_clip(clip_name, new_start or None, new_duration or None):
        raise FCPMCPError(
            ErrorCode.TARGET_NOT_FOUND,
            f"Clip '{clip_name}' not found",
        )
    receipt = _save_modifier_receipt(mod, output_path)
    root = _committed_root(receipt)
    committed = _committed_named_element(
        root,
        name=clip_name,
        tags={"asset-clip", "clip", "title", "audio", "video"},
    )
    changes = []
    after_start = committed.get("start", "")
    after_duration = committed.get("duration", "")
    if before_start != after_start:
        changes.append(
            ClipFieldMutationRecord(
                field="start",
                before=before_start,
                after=after_start,
            )
        )
    if before_duration != after_duration:
        changes.append(
            ClipFieldMutationRecord(
                field="duration",
                before=before_duration,
                after=after_duration,
            )
        )
    text = f"Clip trimmed. Saved to: {receipt.destination}"
    return ToolOutcome(
        text=text,
        structured=ClipMutationResult(
            destination=_artifact_reference(receipt),
            receipt=_receipt_result(receipt),
            operation="trim",
            clip=_clip_mutation_record(committed),
            created_clip=None,
            changed_fields=changes,
            speed_factor=None,
        ),
    )


@TOOLS.tool(
    tool_class=ToolClass.OFFLINE_WRITE,
    safety_hints=OFFLINE_WRITE,
    result_model=ClipMutationResult,
)
def fcpxml_split_clip(
    path: str,
    clip_name: str,
    split_at: str,
    output_path: str = "",
) -> ToolOutcome[ClipMutationResult]:
    """Split a clip at a given offset within the clip.

    Args:
        path: Path to .fcpxml file
        clip_name: Name of the clip to split
        split_at: Offset within the clip to split at (FCPXML time)
        output_path: Output file path
    """
    _parse_time(split_at, "split_at")
    mod = FCPXMLModifier(_resolve_input(path, suffixes={".fcpxml"}))
    original = mod._find_clip_by_name(clip_name)
    if original is None:
        raise FCPMCPError(
            ErrorCode.TARGET_NOT_FOUND,
            f"Clip '{clip_name}' not found",
        )
    before_duration = original.get("duration", "")
    if not mod.split_clip(clip_name, split_at):
        raise FCPMCPError(
            ErrorCode.INVALID_ARGUMENTS,
            f"split_at must fall strictly inside clip '{clip_name}'",
        )
    receipt = _save_modifier_receipt(mod, output_path)
    root = _committed_root(receipt)
    committed_original = _committed_named_element(
        root,
        name=clip_name,
        tags={"asset-clip", "clip", "title", "audio", "video"},
    )
    committed_created = _committed_named_element(
        root,
        name=f"{clip_name}_split",
        tags={"asset-clip", "clip", "title", "audio", "video"},
    )
    after_duration = committed_original.get("duration", "")
    text = f"Clip split. Saved to: {receipt.destination}"
    return ToolOutcome(
        text=text,
        structured=ClipMutationResult(
            destination=_artifact_reference(receipt),
            receipt=_receipt_result(receipt),
            operation="split",
            clip=_clip_mutation_record(committed_original),
            created_clip=_clip_mutation_record(committed_created),
            changed_fields=[
                ClipFieldMutationRecord(
                    field="duration",
                    before=before_duration,
                    after=after_duration,
                )
            ],
            speed_factor=None,
        ),
    )


@TOOLS.tool(
    tool_class=ToolClass.OFFLINE_WRITE,
    safety_hints=OFFLINE_WRITE,
    result_model=ClipBatchMutationResult,
)
def fcpxml_delete_clips(
    path: str,
    clip_names_json: str,
    output_path: str = "",
) -> ToolOutcome[ClipBatchMutationResult]:
    """Remove clips from the timeline by name.

    Args:
        path: Path to .fcpxml file
        clip_names_json: JSON array of clip names to delete
        output_path: Output file path
    """
    names = _load_json_list(clip_names_json, "clip_names_json")
    if not names or not all(isinstance(name, str) for name in names):
        raise FCPMCPError(
            ErrorCode.INVALID_ARGUMENTS,
            "clip_names_json must be a nonempty array of strings",
        )
    mod = FCPXMLModifier(_resolve_input(path, suffixes={".fcpxml"}))
    count = mod.delete_clips(names)
    if count != len(names):
        raise FCPMCPError(
            ErrorCode.TARGET_NOT_FOUND,
            f"{len(names) - count} clip target(s) were not found",
        )
    receipt = _save_modifier_receipt(mod, output_path)
    root = _committed_root(receipt)
    remaining = _spine_clip_names(root)
    if any(name in remaining for name in names):
        raise RuntimeError(
            "Committed FCPXML still contains a deleted clip target"
        )
    text = (
        f"{count}/{len(names)} clips deleted. "
        f"Saved to: {receipt.destination}"
    )
    return ToolOutcome(
        text=text,
        structured=ClipBatchMutationResult(
            destination=_artifact_reference(receipt),
            receipt=_receipt_result(receipt),
            operation="delete",
            requested_count=len(names),
            changed_count=count,
            requested_names=names,
            deleted_names=names,
            requested_order=[],
            resulting_order=remaining,
        ),
    )


@TOOLS.tool(
    tool_class=ToolClass.OFFLINE_WRITE,
    safety_hints=OFFLINE_WRITE,
    result_model=ClipBatchMutationResult,
)
def fcpxml_reorder_clips(
    path: str,
    clip_names_json: str,
    output_path: str = "",
) -> ToolOutcome[ClipBatchMutationResult]:
    """Reorder clips in the primary spine to match the given name order.

    Args:
        path: Path to .fcpxml file
        clip_names_json: JSON array of clip names in desired order
        output_path: Output file path
    """
    names = _load_json_list(clip_names_json, "clip_names_json")
    if not names or not all(isinstance(name, str) for name in names):
        raise FCPMCPError(
            ErrorCode.INVALID_ARGUMENTS,
            "clip_names_json must be a nonempty array of strings",
        )
    mod = FCPXMLModifier(_resolve_input(path, suffixes={".fcpxml"}))
    missing = [
        name
        for name in names
        if mod._find_clip_by_name(name) is None
    ]
    if missing:
        raise FCPMCPError(
            ErrorCode.TARGET_NOT_FOUND,
            f"Clip target(s) not found: {', '.join(missing)}",
        )
    if not mod.reorder_clips(names):
        raise FCPMCPError(
            ErrorCode.TARGET_NOT_FOUND,
            "Timeline spine not found",
        )
    receipt = _save_modifier_receipt(mod, output_path)
    root = _committed_root(receipt)
    resulting_order = _spine_clip_names(root)
    if resulting_order[:len(names)] != names:
        raise RuntimeError(
            "Committed FCPXML does not contain the requested clip order"
        )
    text = f"Clips reordered. Saved to: {receipt.destination}"
    return ToolOutcome(
        text=text,
        structured=ClipBatchMutationResult(
            destination=_artifact_reference(receipt),
            receipt=_receipt_result(receipt),
            operation="reorder",
            requested_count=len(names),
            changed_count=len(names),
            requested_names=names,
            deleted_names=[],
            requested_order=names,
            resulting_order=resulting_order,
        ),
    )


@TOOLS.tool(
    tool_class=ToolClass.OFFLINE_WRITE,
    safety_hints=OFFLINE_WRITE,
    result_model=TransitionMutationResult,
)
def fcpxml_add_transition(
    path: str, after_clip_name: str,
    duration: str = "30030/30000s", name: str = "Cross Dissolve",
    output_path: str = "",
) -> ToolOutcome[TransitionMutationResult]:
    """Insert a transition after a clip.

    Args:
        path: Path to .fcpxml file
        after_clip_name: Name of the clip to add transition after
        duration: Transition duration (default: 1 second at 29.97)
        name: Transition name
        output_path: Output file path
    """
    _parse_time(duration, "duration")
    mod = FCPXMLModifier(_resolve_input(path, suffixes={".fcpxml"}))
    if not mod.add_transition(after_clip_name, duration, name):
        raise FCPMCPError(
            ErrorCode.TARGET_NOT_FOUND,
            f"Clip '{after_clip_name}' not found",
        )
    receipt = _save_modifier_receipt(mod, output_path)
    root = _committed_root(receipt)
    after_clip = _committed_named_element(
        root,
        name=after_clip_name,
        tags={"asset-clip", "clip", "title", "audio", "video"},
    )
    parent = next(
        (
            candidate
            for candidate in root.iter()
            if after_clip in list(candidate)
        ),
        None,
    )
    transition = None
    if parent is not None:
        children = list(parent)
        index = children.index(after_clip)
        if index + 1 < len(children):
            candidate = children[index + 1]
            if (
                candidate.tag == "transition"
                and candidate.get("name") == name
                and candidate.get("duration") == duration
            ):
                transition = candidate
    if transition is None:
        raise RuntimeError(
            "Committed FCPXML is missing the added transition"
        )
    text = f"Transition added. Saved to: {receipt.destination}"
    return ToolOutcome(
        text=text,
        structured=TransitionMutationResult(
            destination=_artifact_reference(receipt),
            receipt=_receipt_result(receipt),
            transition=TransitionMutationRecord(
                after_clip_name=after_clip_name,
                name=transition.get("name", ""),
                duration=transition.get("duration", ""),
                offset=transition.get("offset", ""),
                ref=transition.get("ref", ""),
            ),
        ),
    )


@TOOLS.tool(
    tool_class=ToolClass.OFFLINE_WRITE,
    safety_hints=OFFLINE_WRITE,
    result_model=ClipMutationResult,
)
def fcpxml_change_speed(
    path: str, clip_name: str, speed_factor: float,
    output_path: str = "",
) -> ToolOutcome[ClipMutationResult]:
    """Change clip playback speed.

    Args:
        path: Path to .fcpxml file
        clip_name: Name of the clip
        speed_factor: Speed multiplier (2.0 = 2x fast, 0.5 = half speed)
        output_path: Output file path
    """
    if speed_factor <= 0:
        raise FCPMCPError(
            ErrorCode.INVALID_ARGUMENTS,
            "speed_factor must be greater than zero",
        )
    mod = FCPXMLModifier(_resolve_input(path, suffixes={".fcpxml"}))
    original = mod._find_clip_by_name(clip_name)
    before_duration = (
        original.get("duration", "") if original is not None else ""
    )
    if not mod.change_speed(clip_name, speed_factor):
        raise FCPMCPError(
            ErrorCode.TARGET_NOT_FOUND,
            f"Clip '{clip_name}' not found",
        )
    receipt = _save_modifier_receipt(mod, output_path)
    root = _committed_root(receipt)
    committed = _committed_named_element(
        root,
        name=clip_name,
        tags={"asset-clip", "clip", "title", "audio", "video"},
    )
    original_duration = RationalTime.from_fcpxml(
        before_duration or "0s"
    )
    expected_duration = RationalTime(
        round(original_duration.numerator / speed_factor),
        original_duration.denominator,
    ).to_fcpxml()
    expected_time_points = [
        {
            "time": "0s",
            "value": "0s",
            "interp": "smooth2",
        },
        {
            "time": expected_duration,
            "value": original_duration.to_fcpxml(),
            "interp": "smooth2",
        },
    ]
    time_points = committed.findall("./timeMap/timept")
    if (
        committed.get("duration") != expected_duration
        or [point.attrib for point in time_points] != expected_time_points
    ):
        raise RuntimeError(
            "Committed FCPXML speed time map does not encode "
            "the requested factor"
        )
    after_duration = committed.get("duration", "")
    changes = []
    if before_duration != after_duration:
        changes.append(
            ClipFieldMutationRecord(
                field="duration",
                before=before_duration,
                after=after_duration,
            )
        )
    text = (
        f"Speed changed to {speed_factor}x. "
        f"Saved to: {receipt.destination}"
    )
    return ToolOutcome(
        text=text,
        structured=ClipMutationResult(
            destination=_artifact_reference(receipt),
            receipt=_receipt_result(receipt),
            operation="change_speed",
            clip=_clip_mutation_record(committed),
            created_clip=None,
            changed_fields=changes,
            speed_factor=speed_factor,
        ),
    )


@TOOLS.tool(
    tool_class=ToolClass.OFFLINE_WRITE,
    safety_hints=OFFLINE_WRITE,
    result_model=RoleMutationResult,
)
def fcpxml_assign_role(
    path: str, clip_name: str, role: str, output_path: str = "",
) -> ToolOutcome[RoleMutationResult]:
    """Set the role on a clip (e.g., "Dialogue", "Video", "Music", "Effects").

    Args:
        path: Path to .fcpxml file
        clip_name: Name of the clip
        role: Role name
        output_path: Output file path
    """
    mod = FCPXMLModifier(_resolve_input(path, suffixes={".fcpxml"}))
    if not mod.assign_role(clip_name, role):
        raise FCPMCPError(
            ErrorCode.TARGET_NOT_FOUND,
            f"Clip '{clip_name}' not found",
        )
    receipt = _save_modifier_receipt(mod, output_path)
    root = _committed_root(receipt)
    committed = _committed_named_element(
        root,
        name=clip_name,
        tags={"asset-clip", "clip", "title", "audio", "video"},
    )
    if committed.get("role") != role:
        raise RuntimeError(
            "Committed FCPXML is missing the assigned role"
        )
    text = f"Role '{role}' assigned. Saved to: {receipt.destination}"
    return ToolOutcome(
        text=text,
        structured=RoleMutationResult(
            destination=_artifact_reference(receipt),
            receipt=_receipt_result(receipt),
            assignment=RoleMutationRecord(
                clip_name=clip_name,
                role=committed.get("role", ""),
            ),
        ),
    )


@TOOLS.tool(
    tool_class=ToolClass.OFFLINE_WRITE,
    safety_hints=OFFLINE_WRITE,
    result_model=TimelineElementMutationResult,
)
def fcpxml_add_title(
    path: str,
    text: str,
    duration: str = "150150/30000s",
    position: str = "end",
    output_path: str = "",
) -> ToolOutcome[TimelineElementMutationResult]:
    """Add a title clip to the timeline.

    Args:
        path: Path to .fcpxml file
        text: Title text
        duration: Title duration in FCPXML time
        position: "start", "end", or clip name to insert after
        output_path: Output file path
    """
    _parse_time(duration, "duration")
    # For titles we need to generate a title element referencing Basic Title
    mod = FCPXMLModifier(_resolve_input(path, suffixes={".fcpxml"}))

    # Find or create a title effect resource
    title_ref = ""
    title_resources = mod.root.find("resources")
    if title_resources is not None:
        for el in title_resources:
            if el.tag == "effect" and "Title" in el.get("name", ""):
                title_ref = el.get("id", "")
                break

    if not title_ref:
        # Add a Basic Title effect resource
        resources = mod.root.find("resources")
        if resources is None:
            raise FCPMCPError(
                ErrorCode.TARGET_NOT_FOUND,
                "FCPXML resources element not found",
            )
        effect = ET.SubElement(resources, "effect")
        title_ref = "r_title"
        effect.set("id", title_ref)
        effect.set("name", "Basic Title")
        effect.set("uid", ".../Titles.localized/Build In:Out.localized/Basic Title.localized/Basic Title.moti")

    spine = mod.root.find(".//spine")
    if spine is None:
        raise FCPMCPError(
            ErrorCode.TARGET_NOT_FOUND,
            "Timeline spine not found",
        )

    title_el = ET.Element("title")
    title_el.set("ref", title_ref)
    title_el.set("name", text)
    title_el.set("duration", duration)
    title_el.set("role", "Titles")

    # Add text parameter
    param = ET.SubElement(title_el, "param")
    param.set("name", "Text")
    param.set("key", "Text")
    param.set("value", text)

    if position == "start":
        spine.insert(0, title_el)
    elif position == "end":
        spine.append(title_el)
    else:
        clip_el = mod._find_clip_by_name(position)
        if clip_el is None:
            raise FCPMCPError(
                ErrorCode.TARGET_NOT_FOUND,
                f"Clip '{position}' not found",
            )
        parent = mod._find_parent(clip_el)
        if parent is None:
            raise FCPMCPError(
                ErrorCode.TARGET_NOT_FOUND,
                f"Parent for clip '{position}' not found",
            )
        children = list(parent)
        idx = children.index(clip_el)
        parent.insert(idx + 1, title_el)

    # Recalculate offsets
    mod._recalculate_offsets(spine)

    receipt = _save_modifier_receipt(mod, output_path)
    root = _committed_root(receipt)
    committed_spine = root.find(".//spine")
    if committed_spine is None:
        raise RuntimeError("Committed FCPXML is missing its timeline spine")
    committed_title = next(
        (
            candidate
            for candidate in committed_spine.findall("title")
            if candidate.get("name") == text
            and candidate.get("duration") == duration
            and candidate.get("ref") == title_ref
            and candidate.find("param") is not None
            and candidate.find("param").get("value") == text
        ),
        None,
    )
    if committed_title is None:
        raise RuntimeError("Committed FCPXML is missing the added title")
    children = list(committed_spine)
    title_index = children.index(committed_title)
    if position == "start":
        positioned = title_index == 0
    elif position == "end":
        positioned = title_index == len(children) - 1
    else:
        positioned = (
            title_index > 0
            and children[title_index - 1].get("name") == position
        )
    if not positioned:
        raise RuntimeError(
            "Committed FCPXML title is not in the requested position"
        )
    text_result = f"Title '{text}' added. Saved to: {receipt.destination}"
    return ToolOutcome(
        text=text_result,
        structured=TimelineElementMutationResult(
            destination=_artifact_reference(receipt),
            receipt=_receipt_result(receipt),
            element=TimelineElementMutationRecord(
                kind="title",
                name=committed_title.get("name", ""),
                ref=committed_title.get("ref", ""),
                position=position,
                source=None,
                lane=int(committed_title.get("lane", "0")),
                offset=committed_title.get("offset", ""),
                start=committed_title.get("start"),
                duration=committed_title.get("duration", ""),
                role=committed_title.get("role", ""),
            ),
        ),
    )


@TOOLS.tool(
    tool_class=ToolClass.OFFLINE_WRITE,
    safety_hints=OFFLINE_WRITE,
    result_model=TimelineElementMutationResult,
)
def fcpxml_add_audio(
    path: str,
    audio_src: str,
    name: str = "",
    duration: str = "",
    position: str = "end",
    output_path: str = "",
) -> ToolOutcome[TimelineElementMutationResult]:
    """Add an audio clip to the timeline.

    Args:
        path: Path to .fcpxml file
        audio_src: Path to audio file
        name: Clip name (defaults to filename)
        duration: Duration in FCPXML time (defaults to asset duration)
        position: "start", "end", or clip name to insert after
        output_path: Output file path
    """
    mod = FCPXMLModifier(_resolve_input(path, suffixes={".fcpxml"}))
    audio_path = _resolve_input(audio_src)
    if duration:
        _parse_time(duration, "duration")

    if not name:
        name = audio_path.stem

    # Add asset resource
    resources = mod.root.find("resources")
    if resources is None:
        raise FCPMCPError(
            ErrorCode.TARGET_NOT_FOUND,
            "FCPXML resources element not found",
        )
    asset_id = f"r_audio_{name.replace(' ', '_')}"
    asset = ET.SubElement(resources, "asset")
    asset.set("id", asset_id)
    asset.set("name", name)
    asset.set("src", f"file://{audio_path}")
    asset.set("hasVideo", "0")
    asset.set("hasAudio", "1")
    if duration:
        asset.set("duration", duration)

    spine = mod.root.find(".//spine")
    if spine is None:
        raise FCPMCPError(
            ErrorCode.TARGET_NOT_FOUND,
            "Timeline spine not found",
        )

    clip = ET.Element("asset-clip")
    clip.set("ref", asset_id)
    clip.set("name", name)
    clip.set("start", "0s")
    clip.set("duration", duration or "0s")
    clip.set("role", "Music")

    if position == "start":
        spine.insert(0, clip)
    elif position == "end":
        spine.append(clip)
    else:
        target = mod._find_clip_by_name(position)
        if target is None:
            raise FCPMCPError(
                ErrorCode.TARGET_NOT_FOUND,
                f"Clip '{position}' not found",
            )
        parent = mod._find_parent(target)
        if parent is None:
            raise FCPMCPError(
                ErrorCode.TARGET_NOT_FOUND,
                f"Parent for clip '{position}' not found",
            )
        parent.insert(list(parent).index(target) + 1, clip)

    mod._recalculate_offsets(spine)
    receipt = _save_modifier_receipt(mod, output_path)
    root = _committed_root(receipt)
    committed_spine = root.find(".//spine")
    if committed_spine is None:
        raise RuntimeError("Committed FCPXML is missing its timeline spine")
    committed_clip = next(
        (
            candidate
            for candidate in committed_spine.findall("asset-clip")
            if candidate.get("name") == name
            and candidate.get("ref") == asset_id
            and candidate.get("duration") == (duration or "0s")
        ),
        None,
    )
    committed_asset = next(
        (
            candidate
            for candidate in root.iter("asset")
            if candidate.get("id") == asset_id
            and candidate.get("name") == name
            and candidate.get("src") == f"file://{audio_path}"
        ),
        None,
    )
    if committed_clip is None or committed_asset is None:
        raise RuntimeError("Committed FCPXML is missing the added audio")
    children = list(committed_spine)
    clip_index = children.index(committed_clip)
    if position == "start":
        positioned = clip_index == 0
    elif position == "end":
        positioned = clip_index == len(children) - 1
    else:
        positioned = (
            clip_index > 0
            and children[clip_index - 1].get("name") == position
        )
    if not positioned:
        raise RuntimeError(
            "Committed FCPXML audio is not in the requested position"
        )
    text_result = f"Audio '{name}' added. Saved to: {receipt.destination}"
    return ToolOutcome(
        text=text_result,
        structured=TimelineElementMutationResult(
            destination=_artifact_reference(receipt),
            receipt=_receipt_result(receipt),
            element=TimelineElementMutationRecord(
                kind="audio",
                name=committed_clip.get("name", ""),
                ref=committed_clip.get("ref", ""),
                position=position,
                source=str(audio_path),
                lane=int(committed_clip.get("lane", "0")),
                offset=committed_clip.get("offset", ""),
                start=committed_clip.get("start"),
                duration=committed_clip.get("duration", ""),
                role=committed_clip.get("role", ""),
            ),
        ),
    )


# ============================================================================
# Category 4: FCPXML Generation (8 tools)
# ============================================================================

@TOOLS.tool(
    tool_class=ToolClass.OFFLINE_WRITE,
    safety_hints=OFFLINE_WRITE,
    result_model=FCPXMLGenerationResult,
)
def fcpxml_create_project(
    name: str = "Untitled Project",
    format_name: str = "FFVideoFormat1080p2997",
    width: int = 1920,
    height: int = 1080,
    frame_duration: str = "1001/30000s",
    event_name: str = "Default Event",
    output_path: str = "",
) -> ToolOutcome[FCPXMLGenerationResult]:
    """Create a new empty FCPXML project file.

    Args:
        name: Project name
        format_name: Video format (e.g., FFVideoFormat1080p2997, FFVideoFormat4Kp24)
        width: Frame width
        height: Frame height
        frame_duration: Frame duration in FCPXML time
        event_name: Event name
        output_path: Where to save (default: ~/Movies/<name>.fcpxml)
    """
    _parse_time(frame_duration, "frame_duration")
    gen = FCPXMLGenerator()
    fmt_ref = gen.add_format(name=format_name, width=width, height=height,
                              frame_duration=frame_duration)
    gen.create_project(name=name, format_ref=fmt_ref, event_name=event_name)

    receipt, structured = _save_generation_result(
        gen,
        output_path,
        default_name=f"{name}.fcpxml",
    )
    return ToolOutcome(
        text=f"Project created: {receipt.destination}",
        structured=structured,
    )


@TOOLS.tool(
    tool_class=ToolClass.OFFLINE_WRITE,
    safety_hints=OFFLINE_WRITE,
    result_model=FCPXMLGenerationResult,
)
def fcpxml_create_timeline(
    clips_json: str,
    project_name: str = "Generated Timeline",
    format_name: str = "FFVideoFormat1080p2997",
    event_name: str = "Generated",
    output_path: str = "",
) -> ToolOutcome[FCPXMLGenerationResult]:
    """Build a timeline from a list of clip definitions.

    Args:
        clips_json: JSON array of clips, each: {"src": "/path/to/file.mov", "name"?: str, "duration": "FCPXML_time", "start"?: str, "role"?: str}
        project_name: Project name
        format_name: Video format name
        event_name: Event name
        output_path: Where to save
    """
    clips = _resolve_clip_sources(
        _load_json_list(clips_json, "clips_json")
    )
    gen = FCPXMLGenerator()
    gen.build_timeline_from_clips(clips, project_name=project_name,
                                   format_name=format_name, event_name=event_name)
    receipt, structured = _save_generation_result(
        gen,
        output_path,
        default_name=f"{project_name}.fcpxml",
    )
    return ToolOutcome(
        text=(
            f"Timeline created with {structured.selected_clip_count} clips: "
            f"{receipt.destination}"
        ),
        structured=structured,
    )


@TOOLS.tool(
    tool_class=ToolClass.OFFLINE_WRITE,
    safety_hints=OFFLINE_WRITE,
    result_model=FCPXMLGenerationResult,
)
def fcpxml_auto_rough_cut(
    clips_json: str,
    target_duration: str = "",
    max_clip_duration: str = "150150/30000s",
    transition_duration: str = "",
    project_name: str = "Rough Cut",
    output_path: str = "",
) -> ToolOutcome[FCPXMLGenerationResult]:
    """Auto-assemble clips into a rough cut timeline.

    Args:
        clips_json: JSON array of clips: [{"src": str, "name"?: str, "duration": str}, ...]
        target_duration: Target total duration (optional — uses all clips if empty)
        max_clip_duration: Maximum clip duration (trims longer clips)
        transition_duration: If set, adds transitions between clips
        project_name: Project name
        output_path: Where to save
    """
    clips = _resolve_clip_sources(
        _load_json_list(clips_json, "clips_json")
    )
    gen = FCPXMLGenerator()
    fmt_ref = gen.add_format()

    max_dur = (
        _parse_time(max_clip_duration, "max_clip_duration")
        if max_clip_duration
        else None
    )
    target = (
        _parse_time(target_duration, "target_duration")
        if target_duration
        else None
    )

    trans_ref = ""
    if transition_duration:
        trans_ref = gen.add_effect("Cross Dissolve")

    # Register assets
    asset_refs = {}
    for clip_def in clips:
        src = clip_def["src"]
        if src not in asset_refs:
            asset_refs[src] = gen.add_asset(src=src, name=clip_def.get("name"),
                                              duration=clip_def.get("duration", "0s"),
                                              format_ref=fmt_ref)

    spine = gen.create_project(name=project_name, format_ref=fmt_ref,
                                event_name="Rough Cut")

    running_total = RationalTime.zero()
    for clip_def in clips:
        clip_dur = _parse_time(
            str(clip_def["duration"]),
            "clip duration",
        )
        if max_dur and clip_dur > max_dur:
            clip_dur = max_dur

        if target and running_total + clip_dur > target:
            remaining = target - running_total
            if remaining.is_zero or remaining.numerator < 0:
                break
            clip_dur = remaining

        gen.add_clip_to_spine(spine, asset_refs[clip_def["src"]],
                               name=clip_def.get("name", ""),
                               duration=clip_dur.to_fcpxml(),
                               role=clip_def.get("role", ""))

        if transition_duration and running_total.numerator > 0:
            gen.add_transition(spine, duration=transition_duration,
                                effect_ref=trans_ref)

        running_total = running_total + clip_dur

    receipt, structured = _save_generation_result(
        gen,
        output_path,
        default_name=f"{project_name}.fcpxml",
    )
    return ToolOutcome(
        text=(
            "Rough cut created "
            f"({structured.target_duration_seconds:.1f}s): "
            f"{receipt.destination}"
        ),
        structured=structured,
    )


@TOOLS.tool(
    tool_class=ToolClass.OFFLINE_WRITE,
    safety_hints=OFFLINE_WRITE,
    result_model=FCPXMLGenerationResult,
)
def fcpxml_generate_montage(
    clips_json: str,
    clip_duration: str = "90090/30000s",
    transition_duration: str = "30030/30000s",
    project_name: str = "Montage",
    output_path: str = "",
) -> ToolOutcome[FCPXMLGenerationResult]:
    """Generate a montage/highlight reel with uniform clip durations and transitions.

    Args:
        clips_json: JSON array of clips: [{"src": str, "name"?: str, "duration": str}, ...]
        clip_duration: Duration for each clip in the montage
        transition_duration: Transition duration between clips
        project_name: Project name
        output_path: Where to save
    """
    clips = _resolve_clip_sources(
        _load_json_list(clips_json, "clips_json")
    )
    _parse_time(clip_duration, "clip_duration")
    _parse_time(transition_duration, "transition_duration")
    gen = FCPXMLGenerator()
    fmt_ref = gen.add_format()
    trans_ref = gen.add_effect("Cross Dissolve")

    asset_refs = {}
    for clip_def in clips:
        src = clip_def["src"]
        if src not in asset_refs:
            asset_refs[src] = gen.add_asset(src=src, name=clip_def.get("name"),
                                              duration=clip_def.get("duration", "0s"),
                                              format_ref=fmt_ref)

    spine = gen.create_project(name=project_name, format_ref=fmt_ref,
                                event_name="Montage")

    for i, clip_def in enumerate(clips):
        gen.add_clip_to_spine(spine, asset_refs[clip_def["src"]],
                               name=clip_def.get("name", f"Shot {i+1}"),
                               duration=clip_duration)
        if i < len(clips) - 1:
            gen.add_transition(spine, duration=transition_duration,
                                effect_ref=trans_ref)

    receipt, structured = _save_generation_result(
        gen,
        output_path,
        default_name=f"{project_name}.fcpxml",
    )
    return ToolOutcome(
        text=(
            f"Montage created ({structured.selected_clip_count} shots): "
            f"{receipt.destination}"
        ),
        structured=structured,
    )


@TOOLS.tool(
    tool_class=ToolClass.OFFLINE_WRITE,
    safety_hints=OFFLINE_WRITE,
    result_model=SubtitleImportResult,
)
def fcpxml_import_srt(
    path: str,
    srt_path: str,
    output_path: str = "",
) -> ToolOutcome[SubtitleImportResult]:
    """Convert SRT subtitles to title clips and add to timeline.

    Args:
        path: Path to .fcpxml file to add subtitles to
        srt_path: Path to .srt subtitle file
        output_path: Output file path
    """
    import re

    srt_file = _resolve_input(srt_path, suffixes={".srt"})
    content = srt_file.read_text(encoding="utf-8")

    # Parse SRT
    pattern = r"(\d+)\n(\d{2}):(\d{2}):(\d{2}),(\d{3}) --> (\d{2}):(\d{2}):(\d{2}),(\d{3})\n((?:.*(?:\n|$))*?)(?:\n|$)"
    subtitles = []
    for match in re.finditer(pattern, content):
        start_h, start_m, start_s, start_ms = int(match.group(2)), int(match.group(3)), int(match.group(4)), int(match.group(5))
        end_h, end_m, end_s, end_ms = int(match.group(6)), int(match.group(7)), int(match.group(8)), int(match.group(9))

        start_total = start_h * 3600 + start_m * 60 + start_s + start_ms / 1000
        end_total = end_h * 3600 + end_m * 60 + end_s + end_ms / 1000
        text = match.group(10).strip()

        subtitles.append({
            "start": RationalTime.from_seconds(start_total, 30000),
            "duration": RationalTime.from_seconds(end_total - start_total, 30000),
            "text": text,
        })

    if not subtitles:
        raise FCPMCPError(
            ErrorCode.INVALID_ARGUMENTS,
            "SRT file contains no parseable subtitles",
        )

    mod = FCPXMLModifier(_resolve_input(path, suffixes={".fcpxml"}))
    import xml.etree.ElementTree as ET
    prior_subtitle_count = len(
        mod.root.findall(".//title[@role='Titles.Subtitle']")
    )

    # Find or create title effect
    title_ref = ""
    resources = mod.root.find("resources")
    if resources is not None:
        for el in resources:
            if el.tag == "effect" and "Title" in el.get("name", ""):
                title_ref = el.get("id", "")
                break
    if not title_ref:
        resources = mod.root.find("resources")
        effect = ET.SubElement(resources, "effect")
        title_ref = "r_subtitle"
        effect.set("id", title_ref)
        effect.set("name", "Basic Title")

    # Add subtitle clips as connected clips to the first spine clip
    spine = mod.root.find(".//spine")
    if spine is None:
        raise FCPMCPError(
            ErrorCode.TARGET_NOT_FOUND,
            "Timeline spine not found",
        )

    first_clip = None
    for child in spine:
        if child.tag not in ("transition",):
            first_clip = child
            break

    if first_clip is None:
        raise FCPMCPError(
            ErrorCode.TARGET_NOT_FOUND,
            "Timeline contains no clip to anchor subtitles",
        )

    for sub in subtitles:
        title_el = ET.SubElement(first_clip, "title")
        title_el.set("ref", title_ref)
        title_el.set("name", sub["text"][:30])
        title_el.set("offset", sub["start"].to_fcpxml())
        title_el.set("duration", sub["duration"].to_fcpxml())
        title_el.set("lane", "1")
        title_el.set("role", "Titles.Subtitle")

        param = ET.SubElement(title_el, "param")
        param.set("name", "Text")
        param.set("key", "Text")
        param.set("value", sub["text"])

    cue_count = len(subtitles)

    def validate_subtitle_delta(candidate: Path) -> None:
        candidate_count = len(
            ET.parse(candidate).getroot().findall(
                ".//title[@role='Titles.Subtitle']"
            )
        )
        if candidate_count - prior_subtitle_count != cue_count:
            _raise_validation_failure(
                "Subtitle import count does not match its candidate output"
            )

    receipt = _save_modifier_receipt(
        mod,
        output_path,
        validate_candidate=validate_subtitle_delta,
    )
    return ToolOutcome(
        text=(
            f"{cue_count} subtitles added. Saved to: "
            f"{receipt.destination}"
        ),
        structured=SubtitleImportResult(
            cue_count=cue_count,
            destination=_artifact_reference(receipt),
            receipt=_receipt_result(receipt),
        ),
    )


@TOOLS.tool(
    tool_class=ToolClass.OFFLINE_WRITE,
    safety_hints=OFFLINE_WRITE,
    result_model=EDLImportResult,
)
def fcpxml_import_edl(
    edl_path: str,
    media_dir: str = "",
    project_name: str = "EDL Import",
    output_path: str = "",
) -> ToolOutcome[EDLImportResult]:
    """Convert an EDL (Edit Decision List) to FCPXML.

    Args:
        edl_path: Path to .edl file
        media_dir: Directory containing media files (for resolving reel names)
        project_name: Project name
        output_path: Where to save
    """
    edl_file = _resolve_input(edl_path, suffixes={".edl"})
    media_path = (
        _resolve_input(media_dir, kind="dir")
        if media_dir
        else None
    )
    lines = edl_file.read_text().splitlines()
    gen = FCPXMLGenerator()
    fmt_ref = gen.add_format()
    spine = gen.create_project(name=project_name, format_ref=fmt_ref, event_name="EDL Import")

    clip_count = 0
    for line in lines:
        parts = line.split()
        if len(parts) >= 7 and parts[0].isdigit():
            reel = parts[1]
            # Try to find media file
            src = ""
            if media_path is not None:
                for ext in (".mov", ".mp4", ".mxf", ".avi"):
                    candidate = media_path / f"{reel}{ext}"
                    if candidate.exists():
                        src = str(candidate)
                        break
            if not src:
                src = f"/placeholder/{reel}.mov"

            # Parse timecodes (simplified — EDL TC format)
            src_in = parts[4] if len(parts) > 4 else "00:00:00:00"
            src_out = parts[5] if len(parts) > 5 else "00:00:01:00"

            # Approximate duration from timecodes
            duration = "90090/30000s"  # default 3 seconds

            asset_ref = gen.add_asset(src=src, name=reel, duration=duration, format_ref=fmt_ref)
            gen.add_clip_to_spine(spine, asset_ref, name=f"{reel}_edit{parts[0]}",
                                   duration=duration)
            clip_count += 1

    if clip_count == 0:
        raise FCPMCPError(
            ErrorCode.INVALID_ARGUMENTS,
            "EDL contains no parseable edit events",
        )
    event_count = 0

    def validate_edl_import(candidate: Path) -> None:
        nonlocal event_count
        event_count = len(
            ET.parse(candidate).getroot().findall(
                ".//spine/asset-clip"
            )
        )
        if event_count != clip_count:
            _raise_validation_failure(
                "EDL import event count does not match its candidate output"
            )

    receipt = _save_generator_receipt(
        gen,
        output_path,
        default_name=f"{project_name}.fcpxml",
        validate_candidate=validate_edl_import,
    )
    return ToolOutcome(
        text=f"EDL imported ({event_count} clips): {receipt.destination}",
        structured=EDLImportResult(
            event_count=event_count,
            destination=_artifact_reference(receipt),
            receipt=_receipt_result(receipt),
        ),
    )


@TOOLS.tool(
    tool_class=ToolClass.OFFLINE_WRITE,
    safety_hints=OFFLINE_WRITE,
    result_model=FCPXMLMutationResult,
)
def fcpxml_reformat(
    path: str,
    target_width: int = 1080,
    target_height: int = 1920,
    target_format_name: str = "FFVideoFormat1080x1920p2997",
    output_path: str = "",
) -> ToolOutcome[FCPXMLMutationResult]:
    """Reformat a timeline for a different aspect ratio (e.g., 16:9 → 9:16 for vertical).

    Args:
        path: Path to .fcpxml file
        target_width: Target width
        target_height: Target height
        target_format_name: Target format name
        output_path: Output file path
    """

    mod = FCPXMLModifier(_resolve_input(path, suffixes={".fcpxml"}))

    source_version = mod.root.get("version", "")

    # Update format
    format_element = next(mod.root.iter("format"), None)
    if format_element is None:
        raise FCPMCPError(
            ErrorCode.TARGET_NOT_FOUND,
            "FCPXML format resource not found",
        )
    for fmt_el in (format_element,):
        fmt_el.set("width", str(target_width))
        fmt_el.set("height", str(target_height))
        fmt_el.set("name", target_format_name)
        break

    target_version = ""
    committed_width = 0
    committed_height = 0
    committed_name = ""

    def validate_reformat(candidate: Path) -> None:
        nonlocal target_version
        nonlocal committed_width
        nonlocal committed_height
        nonlocal committed_name
        root = ET.parse(candidate).getroot()
        candidate_format = next(root.iter("format"), None)
        if candidate_format is None:
            _raise_validation_failure(
                "Reformat candidate is missing its format resource"
            )
        try:
            candidate_width = int(candidate_format.get("width", "0"))
            candidate_height = int(candidate_format.get("height", "0"))
        except ValueError as error:
            raise FCPMCPError(
                ErrorCode.VALIDATION_FAILED,
                "Reformat candidate dimensions are not integers",
            ) from error
        candidate_name = candidate_format.get("name", "")
        if (
            candidate_width != target_width
            or candidate_height != target_height
            or candidate_name != target_format_name
        ):
            _raise_validation_failure(
                "Reformat candidate does not match the requested format"
            )
        target_version = root.get("version", "")
        committed_width = candidate_width
        committed_height = candidate_height
        committed_name = candidate_name

    receipt = _save_modifier_receipt(
        mod,
        output_path,
        validate_candidate=validate_reformat,
    )
    return ToolOutcome(
        text=(
            f"Reformatted to {committed_width}x{committed_height}. "
            f"Saved to: {receipt.destination}"
        ),
        structured=FCPXMLMutationResult(
            source_version=source_version,
            target_version=target_version,
            target_width=committed_width,
            target_height=committed_height,
            target_format_name=committed_name,
            destination=_artifact_reference(receipt),
            receipt=_receipt_result(receipt),
        ),
    )


# ============================================================================
# Category 8: Batch Operations (6 tools)
# ============================================================================

@TOOLS.tool(
    tool_class=ToolClass.OFFLINE_WRITE,
    safety_hints=OFFLINE_WRITE,
    result_model=CleanupMutationResult,
)
def fcpxml_fix_flash_frames(
    path: str,
    min_frames: int = 3,
    frame_duration: str = "1001/30000s",
    output_path: str = "",
) -> ToolOutcome[CleanupMutationResult]:
    """Auto-fix flash frames by extending very short clips to minimum duration.

    Args:
        path: Path to .fcpxml file
        min_frames: Minimum frame count (clips shorter than this get extended)
        frame_duration: Frame duration for calculating frame count
        output_path: Output file path
    """
    _parse_time(frame_duration, "frame_duration")
    mod = FCPXMLModifier(_resolve_input(path, suffixes={".fcpxml"}))
    count = mod.fix_flash_frames(min_frames, frame_duration)
    minimum = RationalTime.from_fcpxml(frame_duration) * min_frames

    def validate_flash_fix(candidate: Path) -> None:
        root = ET.parse(candidate).getroot()
        remaining = [
            element
            for spine in root.iter("spine")
            for element in spine
            if element.tag not in {"gap", "transition"}
            and not RationalTime.from_fcpxml(
                element.get("duration", "0s")
            ).is_zero
            and RationalTime.from_fcpxml(
                element.get("duration", "0s")
            ) < minimum
        ]
        if remaining:
            _raise_validation_failure(
                "Flash-frame candidate still contains short clips"
            )

    receipt = _save_modifier_receipt(
        mod,
        output_path,
        validate_candidate=validate_flash_fix,
    )
    return ToolOutcome(
        text=(
            f"{count} flash frames fixed. Saved to: "
            f"{receipt.destination}"
        ),
        structured=CleanupMutationResult(
            action="fix_flash_frames",
            changed_count=count,
            destination=_artifact_reference(receipt),
            receipt=_receipt_result(receipt),
        ),
    )


@TOOLS.tool(
    tool_class=ToolClass.OFFLINE_WRITE,
    safety_hints=OFFLINE_WRITE,
    result_model=CleanupMutationResult,
)
def fcpxml_fill_gaps(
    path: str,
    fill_asset_ref: str,
    fill_name: str = "Fill",
    output_path: str = "",
) -> ToolOutcome[CleanupMutationResult]:
    """Replace all gaps in the timeline with clips from a specified asset.

    Args:
        path: Path to .fcpxml file
        fill_asset_ref: Asset resource ID to use as fill (e.g., "r3")
        fill_name: Name for the fill clips
        output_path: Output file path
    """
    mod = FCPXMLModifier(_resolve_input(path, suffixes={".fcpxml"}))
    if not any(
        element.get("id") == fill_asset_ref
        for element in mod.root.findall("./resources/asset")
    ):
        raise FCPMCPError(
            ErrorCode.TARGET_NOT_FOUND,
            f"Asset resource '{fill_asset_ref}' not found",
        )
    prior_gap_count = sum(
        len(spine.findall("gap"))
        for spine in mod.root.iter("spine")
    )
    count = mod.fill_gaps(fill_asset_ref, fill_name)

    def validate_gap_fill(candidate: Path) -> None:
        root = ET.parse(candidate).getroot()
        candidate_gap_count = sum(
            len(spine.findall("gap"))
            for spine in root.iter("spine")
        )
        if prior_gap_count - candidate_gap_count != count:
            _raise_validation_failure(
                "Gap-fill count does not match its candidate output"
            )

    receipt = _save_modifier_receipt(
        mod,
        output_path,
        validate_candidate=validate_gap_fill,
    )
    return ToolOutcome(
        text=f"{count} gaps filled. Saved to: {receipt.destination}",
        structured=CleanupMutationResult(
            action="fill_gaps",
            changed_count=count,
            destination=_artifact_reference(receipt),
            receipt=_receipt_result(receipt),
        ),
    )


@TOOLS.tool(
    tool_class=ToolClass.OFFLINE_WRITE,
    safety_hints=OFFLINE_WRITE,
    result_model=CleanupMutationResult,
)
def fcpxml_remove_silence(
    path: str,
    silence_threshold_seconds: float = 2.0,
    output_path: str = "",
) -> ToolOutcome[CleanupMutationResult]:
    """Remove gaps longer than the threshold from the timeline.

    Args:
        path: Path to .fcpxml file
        silence_threshold_seconds: Minimum gap duration to remove (seconds)
        output_path: Output file path
    """
    if silence_threshold_seconds < 0:
        raise FCPMCPError(
            ErrorCode.INVALID_ARGUMENTS,
            "silence_threshold_seconds must be nonnegative",
        )
    mod = FCPXMLModifier(_resolve_input(path, suffixes={".fcpxml"}))
    prior_gap_count = sum(
        len(spine.findall("gap"))
        for spine in mod.root.iter("spine")
    )
    count = 0
    for spine_el in mod.root.iter("spine"):
        for gap_el in list(spine_el.findall("gap")):
            dur = RationalTime.from_fcpxml(gap_el.get("duration", "0s"))
            if dur.to_seconds() >= silence_threshold_seconds:
                spine_el.remove(gap_el)
                count += 1
        if count > 0:
            mod._recalculate_offsets(spine_el)

    def validate_silence_removal(candidate: Path) -> None:
        root = ET.parse(candidate).getroot()
        candidate_gap_count = sum(
            len(spine.findall("gap"))
            for spine in root.iter("spine")
        )
        if prior_gap_count - candidate_gap_count != count:
            _raise_validation_failure(
                "Silence-removal count does not match its candidate output"
            )

    receipt = _save_modifier_receipt(
        mod,
        output_path,
        validate_candidate=validate_silence_removal,
    )
    return ToolOutcome(
        text=f"{count} gaps removed. Saved to: {receipt.destination}",
        structured=CleanupMutationResult(
            action="remove_silence",
            changed_count=count,
            destination=_artifact_reference(receipt),
            receipt=_receipt_result(receipt),
        ),
    )


@TOOLS.tool(
    tool_class=ToolClass.OFFLINE_WRITE,
    safety_hints=OFFLINE_WRITE,
    result_model=ClipBatchMutationResult,
)
def fcpxml_batch_rename_clips(
    path: str, pattern: str, replacement: str, output_path: str = "",
) -> ToolOutcome[ClipBatchMutationResult]:
    """Rename clips matching a pattern (substring replacement).

    Args:
        path: Path to .fcpxml file
        pattern: Text to find in clip names
        replacement: Text to replace with
        output_path: Output file path
    """
    if not pattern:
        raise FCPMCPError(
            ErrorCode.INVALID_ARGUMENTS,
            "pattern must not be empty",
        )
    mod = FCPXMLModifier(_resolve_input(path, suffixes={".fcpxml"}))
    prior_names = [
        element.get("name")
        for element in mod.root.iter()
    ]
    count = mod.batch_rename_clips(pattern, replacement)
    if count == 0:
        raise FCPMCPError(
            ErrorCode.TARGET_NOT_FOUND,
            f"No clip name contains pattern '{pattern}'",
        )
    expected_names = [
        element.get("name")
        for element in mod.root.iter()
    ]

    def validate_batch_rename(candidate: Path) -> None:
        candidate_names = [
            element.get("name")
            for element in ET.parse(candidate).getroot().iter()
        ]
        if candidate_names != expected_names:
            _raise_validation_failure(
                "Rename candidate does not match the requested names"
            )
        verified_count = sum(
            before != after
            for before, after in zip(
                prior_names,
                candidate_names,
                strict=True,
            )
        )
        if verified_count != count:
            _raise_validation_failure(
                "Rename count does not match its candidate output"
            )

    receipt = _save_modifier_receipt(
        mod,
        output_path,
        validate_candidate=validate_batch_rename,
    )
    return ToolOutcome(
        text=f"{count} clips renamed. Saved to: {receipt.destination}",
        structured=ClipBatchMutationResult(
            destination=_artifact_reference(receipt),
            receipt=_receipt_result(receipt),
            operation="rename",
            changed_count=count,
            pattern=pattern,
            replacement=replacement,
        ),
    )


@TOOLS.tool(
    tool_class=ToolClass.OFFLINE_WRITE,
    safety_hints=OFFLINE_WRITE,
    result_model=RoleBatchMutationResult,
)
def fcpxml_batch_assign_roles(
    path: str, rules_json: str, output_path: str = "",
) -> ToolOutcome[RoleBatchMutationResult]:
    """Assign roles to clips based on name matching rules.

    Args:
        path: Path to .fcpxml file
        rules_json: JSON array of rules: [{"match": "interview", "role": "Dialogue"}, ...]
        output_path: Output file path
    """
    rules = _load_json_list(rules_json, "rules_json")
    if not rules or not all(
        isinstance(rule, dict)
        and isinstance(rule.get("match"), str)
        and isinstance(rule.get("role"), str)
        for rule in rules
    ):
        raise FCPMCPError(
            ErrorCode.INVALID_ARGUMENTS,
            "rules_json entries must contain string match and role fields",
        )
    mod = FCPXMLModifier(_resolve_input(path, suffixes={".fcpxml"}))
    count = mod.batch_assign_roles(rules)
    if count == 0:
        raise FCPMCPError(
            ErrorCode.TARGET_NOT_FOUND,
            "No clips matched the supplied role rules",
        )
    role_tags = {
        "asset-clip",
        "clip",
        "title",
        "audio",
        "video",
    }
    expected_roles = [
        (element.tag, element.get("name"), element.get("role"))
        for element in mod.root.iter()
        if element.tag in role_tags
    ]

    def validate_role_assignment(candidate: Path) -> None:
        candidate_elements = [
            element
            for element in ET.parse(candidate).getroot().iter()
            if element.tag in role_tags
        ]
        candidate_roles = [
            (element.tag, element.get("name"), element.get("role"))
            for element in candidate_elements
        ]
        if candidate_roles != expected_roles:
            _raise_validation_failure(
                "Role-assignment candidate does not match requested roles"
            )
        candidate_matches = 0
        for element in candidate_elements:
            if element.tag not in {
            "asset-clip",
            "clip",
            "title",
            "audio",
            "video",
            }:
                continue
            element_name = element.get("name", "")
            matching_rule = next(
                (
                    rule
                    for rule in rules
                    if rule["match"].lower() in element_name.lower()
                ),
                None,
            )
            if matching_rule is not None:
                if element.get("role") != matching_rule["role"]:
                    _raise_validation_failure(
                        "Role-assignment candidate contains a wrong role"
                    )
                candidate_matches += 1
        if candidate_matches != count:
            _raise_validation_failure(
                "Role-assignment count does not match candidate output"
            )

    receipt = _save_modifier_receipt(
        mod,
        output_path,
        validate_candidate=validate_role_assignment,
    )
    return ToolOutcome(
        text=f"{count} roles assigned. Saved to: {receipt.destination}",
        structured=RoleBatchMutationResult(
            rules=[
                RoleRuleRecord(
                    match=rule["match"],
                    role=rule["role"],
                )
                for rule in rules
            ],
            changed_count=count,
            destination=_artifact_reference(receipt),
            receipt=_receipt_result(receipt),
        ),
    )


@TOOLS.tool(
    tool_class=ToolClass.OFFLINE_WRITE,
    safety_hints=OFFLINE_WRITE,
    result_model=TransitionBatchMutationResult,
)
def fcpxml_batch_apply_transition(
    path: str,
    duration: str = "30030/30000s",
    name: str = "Cross Dissolve",
    output_path: str = "",
) -> ToolOutcome[TransitionBatchMutationResult]:
    """Add transitions between all adjacent clips in the timeline.

    Args:
        path: Path to .fcpxml file
        duration: Transition duration
        name: Transition name
        output_path: Output file path
    """
    _parse_time(duration, "duration")
    mod = FCPXMLModifier(_resolve_input(path, suffixes={".fcpxml"}))
    prior_matching = sum(
        element.get("name") == name
        and element.get("duration") == duration
        for element in mod.root.iter("transition")
    )
    count = mod.batch_apply_transition(duration, name)
    if count == 0:
        raise FCPMCPError(
            ErrorCode.TARGET_NOT_FOUND,
            "No adjacent clips were available for transitions",
        )
    expected_transitions = [
        (
            element.get("name"),
            element.get("duration"),
            element.get("offset"),
            element.get("ref"),
        )
        for element in mod.root.iter("transition")
    ]

    def validate_transition_batch(candidate: Path) -> None:
        root = ET.parse(candidate).getroot()
        candidate_transitions = [
            (
                element.get("name"),
                element.get("duration"),
                element.get("offset"),
                element.get("ref"),
            )
            for element in root.iter("transition")
        ]
        if candidate_transitions != expected_transitions:
            _raise_validation_failure(
                "Transition candidate does not match requested transitions"
            )
        candidate_matching = sum(
            element.get("name") == name
            and element.get("duration") == duration
            for element in root.iter("transition")
        )
        if candidate_matching - prior_matching != count:
            _raise_validation_failure(
                "Transition count does not match its candidate output"
            )

    receipt = _save_modifier_receipt(
        mod,
        output_path,
        validate_candidate=validate_transition_batch,
    )
    return ToolOutcome(
        text=(
            f"{count} transitions added. Saved to: "
            f"{receipt.destination}"
        ),
        structured=TransitionBatchMutationResult(
            name=name,
            duration=duration,
            changed_count=count,
            destination=_artifact_reference(receipt),
            receipt=_receipt_result(receipt),
        ),
    )


# ============================================================================
# Category 9: QC & Validation (6 tools)
# ============================================================================

@TOOLS.tool(
    tool_class=ToolClass.INSPECT,
    safety_hints=OFFLINE_READ,
    result_model=QCReportResult,
)
def fcpxml_qc_report(path: str) -> ToolOutcome[QCReportResult]:
    """Generate a comprehensive quality check report for the timeline.

    Checks: validation, gaps, flash frames, duplicates, pacing.

    Args:
        path: Path to .fcpxml file
    """
    doc = _parse_doc(path)

    lines = ["# QC Report\n"]

    # Validation
    validation = _validator.validate_document(doc)
    lines.append("## Validation")
    lines.append(validation.summary())
    lines.append("")

    # Stats
    stats_list = analyze_timeline_stats(doc)
    for stats in stats_list:
        lines.append(f"## Stats: {stats.project_name}")
        lines.append(f"Duration: {stats.total_duration_timecode} ({stats.total_duration_seconds:.1f}s)")
        lines.append(f"Clips: {stats.non_gap_clip_count} (+{stats.gap_count} gaps)")
        lines.append(f"Resolution: {stats.resolution} @ {stats.fps:.2f}fps")
        lines.append(f"Avg clip: {stats.average_clip_duration_seconds:.2f}s")
        lines.append(f"Markers: {stats.marker_count}, Keywords: {stats.keyword_count}")
        lines.append(f"Roles: {', '.join(stats.roles_used)}")
        lines.append("")

    # Gaps
    gaps = detect_gaps(doc)
    if gaps:
        lines.append(f"## Gaps ({len(gaps)})")
        for g in gaps:
            lines.append(f"- {g.offset_timecode}: {g.duration_seconds:.3f}s (between '{g.before_clip}' and '{g.after_clip}')")
        lines.append("")

    # Flash frames
    flashes = detect_flash_frames(doc)
    if flashes:
        lines.append(f"## Flash Frames ({len(flashes)})")
        for f in flashes:
            lines.append(f"- '{f.clip_name}' at {f.offset_timecode}: {f.frame_count} frames ({f.duration_seconds:.3f}s)")
        lines.append("")

    # Duplicates
    dupes = detect_duplicates(doc)
    if dupes:
        lines.append(f"## Duplicate Sources ({len(dupes)})")
        for d in dupes:
            lines.append(f"- '{d.asset_name}' used {len(d.occurrences)}x")
        lines.append("")

    # Pacing
    pacing_list = analyze_pacing(doc)
    for pacing in pacing_list:
        lines.append("## Pacing")
        lines.append(f"Avg shot: {pacing.average_shot_length:.2f}s, Median: {pacing.median_shot_length:.2f}s")
        lines.append(f"Shortest: {pacing.shortest_shot:.3f}s, Longest: {pacing.longest_shot:.2f}s")
        lines.append(f"Distribution: {json.dumps(pacing.histogram)}")

    markdown = "\n".join(lines)
    validation_record = FCPXMLValidationResult(
        valid=validation.valid,
        issues=[_serializable(issue) for issue in validation.issues],
        error_count=len(validation.errors),
        warning_count=len(validation.warnings),
        info_count=(
            len(validation.issues)
            - len(validation.errors)
            - len(validation.warnings)
        ),
        summary=validation.summary(),
    )
    pacing_records = [
        PacingRecord(
            average_shot_length=pacing.average_shot_length,
            median_shot_length=pacing.median_shot_length,
            std_deviation=pacing.std_deviation,
            shortest_shot=pacing.shortest_shot,
            longest_shot=pacing.longest_shot,
            pacing_curve=pacing.pacing_curve,
            histogram=PacingHistogram(
                under_one_second=pacing.histogram.get("< 1s", 0),
                one_to_three_seconds=pacing.histogram.get("1-3s", 0),
                three_to_five_seconds=pacing.histogram.get("3-5s", 0),
                five_to_ten_seconds=pacing.histogram.get("5-10s", 0),
                ten_to_thirty_seconds=pacing.histogram.get("10-30s", 0),
                thirty_seconds_or_more=pacing.histogram.get("30s+", 0),
            ),
        )
        for pacing in pacing_list
    ]
    return ToolOutcome(
        text=markdown,
        structured=QCReportResult(
            validation=validation_record,
            stats=[_serializable(stats) for stats in stats_list],
            gaps=[_serializable(gap) for gap in gaps],
            flash_frames=[_serializable(flash) for flash in flashes],
            duplicates=[_serializable(dupe) for dupe in dupes],
            pacing=pacing_records,
            markdown=markdown,
        ),
    )


@TOOLS.tool(
    tool_class=ToolClass.INSPECT,
    safety_hints=OFFLINE_READ,
    result_model=MediaLinkCheckResult,
)
def fcpxml_check_media_links(path: str) -> ToolOutcome[MediaLinkCheckResult]:
    """Verify all referenced media files exist on disk.

    Args:
        path: Path to .fcpxml file
    """
    doc = _parse_doc(path)
    for asset in doc.assets.values():
        if asset.src.startswith("file://"):
            PATHS.resolve_reference(url_unquote(asset.src[7:]))
    result = _validator.check_media_links(doc)
    text = "All media files found." if not result.issues else result.summary()
    return ToolOutcome(
        text=text,
        structured=MediaLinkCheckResult(
            issues=[_serializable(issue) for issue in result.issues],
            missing_count=sum(
                issue.message.startswith("Media file not found:")
                for issue in result.issues
            ),
        ),
    )


@TOOLS.tool(
    tool_class=ToolClass.INSPECT,
    safety_hints=OFFLINE_READ,
    result_model=FrameRateCheckResult,
)
def fcpxml_check_frame_rates(path: str) -> ToolOutcome[FrameRateCheckResult]:
    """Detect mixed frame rate issues in the timeline.

    Args:
        path: Path to .fcpxml file
    """
    doc = _parse_doc(path)
    frame_rates = set()
    formats = []
    for format_id, fmt in doc.formats.items():
        formats.append(
            FrameRateFormatRecord(
                format_id=format_id,
                name=fmt.name,
                frame_duration=fmt.frame_duration.to_fcpxml(),
                fps=fmt.fps,
            )
        )
        if fmt.frame_duration.numerator > 0:
            frame_rates.add(fmt.fps)

    if len(frame_rates) <= 1:
        text = (
            "Consistent frame rate: "
            f"{frame_rates.pop() if frame_rates else 'unknown'}fps"
        )
        mismatches = []
    else:
        text = (
            "MIXED FRAME RATES DETECTED: "
            + ", ".join(f"{rate:.2f}fps" for rate in sorted(frame_rates))
        )
        referenced_fps = next(
            (
                fmt.fps
                for project in doc.all_projects
                if project.sequence
                and (
                    fmt := doc.formats.get(project.sequence.format_ref)
                ) is not None
                and fmt.frame_duration.numerator > 0
            ),
            None,
        )
        expected_fps = (
            referenced_fps
            if referenced_fps is not None
            else next(
                item.fps for item in formats if item.fps in frame_rates
            )
        )
        mismatches = [
            FrameRateMismatchRecord(
                format_id=item.format_id,
                expected_fps=expected_fps,
                actual_fps=item.fps,
            )
            for item in formats
            if item.fps != expected_fps
        ]

    return ToolOutcome(
        text=text,
        structured=FrameRateCheckResult(
            formats=formats,
            mismatches=mismatches,
        ),
    )


@TOOLS.tool(
    tool_class=ToolClass.INSPECT,
    safety_hints=OFFLINE_READ,
    result_model=AudioLevelCheckResult,
)
def fcpxml_check_audio_levels(path: str) -> ToolOutcome[AudioLevelCheckResult]:
    """Flag clips with potential audio issues (volume adjustments, missing audio).

    Args:
        path: Path to .fcpxml file
    """
    doc = _parse_doc(path)
    issues = []
    observations = []

    for clip in doc.all_clips:
        if clip.is_gap:
            continue

        # Check if asset has audio
        if clip.ref and clip.ref in doc.assets:
            asset = doc.assets[clip.ref]
            if not asset.has_audio and clip.role in ("Dialogue", "Music", ""):
                message = (
                    f"'{clip.name}': no audio in source "
                    f"(role: {clip.role or 'none'})"
                )
                issues.append(message)
                observations.append(
                    AudioLevelObservationRecord(
                        clip_name=clip.name,
                        kind="missing_audio",
                        message=message,
                        role=clip.role,
                    )
                )

        # Check volume adjustments
        if clip.volume:
            amt = clip.volume.amount
            if "dB" in amt:
                try:
                    db_val = float(amt.replace("dB", ""))
                    if db_val > 6:
                        message = (
                            f"'{clip.name}': very high volume (+{db_val}dB)"
                        )
                        issues.append(message)
                        observations.append(
                            AudioLevelObservationRecord(
                                clip_name=clip.name,
                                kind="high_volume",
                                message=message,
                                role=clip.role,
                                amount_db=db_val,
                            )
                        )
                    elif db_val < -20:
                        message = (
                            f"'{clip.name}': very low volume ({db_val}dB)"
                        )
                        issues.append(message)
                        observations.append(
                            AudioLevelObservationRecord(
                                clip_name=clip.name,
                                kind="low_volume",
                                message=message,
                                role=clip.role,
                                amount_db=db_val,
                            )
                        )
                except ValueError:
                    pass

    text = (
        "No audio issues detected."
        if not issues
        else "Audio issues:\n" + "\n".join(f"- {issue}" for issue in issues)
    )
    return ToolOutcome(
        text=text,
        structured=AudioLevelCheckResult(
            observations=observations,
            limitations=[
                (
                    "Only FCPXML source and volume metadata were inspected; "
                    "no media audio signal or loudness analysis was run."
                )
            ],
        ),
    )


@TOOLS.tool(
    tool_class=ToolClass.INSPECT,
    safety_hints=OFFLINE_READ,
    result_model=SafeZoneCheckResult,
)
def fcpxml_check_safe_zones(path: str) -> ToolOutcome[SafeZoneCheckResult]:
    """Check for clips with transforms that might push content outside safe zones.

    Args:
        path: Path to .fcpxml file
    """
    doc = _parse_doc(path)
    issues = []
    observations = []

    for clip in doc.all_clips:
        if clip.transform:
            t = clip.transform
            if abs(t.position_x) > 800 or abs(t.position_y) > 450:
                message = (
                    f"'{clip.name}': position ({t.position_x}, {t.position_y}) "
                    "may be outside safe zone"
                )
                issues.append(message)
                observations.append(
                    SafeZoneObservationRecord(
                        clip_name=clip.name,
                        kind="position",
                        message=message,
                        position_x=t.position_x,
                        position_y=t.position_y,
                    )
                )
            if t.scale > 2.0 or t.scale < 0.3:
                message = (
                    f"'{clip.name}': scale {t.scale}x may cause quality issues"
                )
                issues.append(message)
                observations.append(
                    SafeZoneObservationRecord(
                        clip_name=clip.name,
                        kind="scale",
                        message=message,
                        scale=t.scale,
                    )
                )

    text = (
        "All clips within safe zones."
        if not issues
        else "Safe zone concerns:\n"
        + "\n".join(f"- {issue}" for issue in issues)
    )
    return ToolOutcome(
        text=text,
        structured=SafeZoneCheckResult(
            observations=observations,
            limitations=[
                (
                    "Only FCPXML transform metadata was inspected; no rendered "
                    "frames or media pixels were analyzed."
                )
            ],
        ),
    )


@TOOLS.tool(
    tool_class=ToolClass.INSPECT,
    safety_hints=OFFLINE_READ,
    result_model=DurationCheckResult,
)
def fcpxml_check_duration(
    path: str,
    target_seconds: float,
) -> ToolOutcome[DurationCheckResult]:
    """Verify the timeline fits a target duration.

    Args:
        path: Path to .fcpxml file
        target_seconds: Target duration in seconds
    """
    if target_seconds < 0:
        raise FCPMCPError(
            ErrorCode.INVALID_ARGUMENTS,
            "target_seconds must be nonnegative",
        )
    doc = _parse_doc(path)
    for project in doc.all_projects:
        if not project.sequence:
            continue
        actual = project.sequence.duration.to_seconds()
        if actual == 0 and project.sequence.spine:
            actual = project.sequence.spine.duration.to_seconds()

        diff = actual - target_seconds
        status = "ON TARGET" if abs(diff) < 1 else ("OVER" if diff > 0 else "UNDER")
        text = (
            f"Project '{project.name}': {actual:.1f}s / "
            f"{target_seconds:.1f}s target "
            f"({status}, diff: {diff:+.1f}s)"
        )
        return ToolOutcome(
            text=text,
            structured=DurationCheckResult(
                project_name=project.name,
                target_seconds=target_seconds,
                actual_seconds=actual,
                delta_seconds=diff,
                tolerance_seconds=1.0,
                within_tolerance=abs(diff) < 1,
            ),
        )

    raise FCPMCPError(
        ErrorCode.TARGET_NOT_FOUND,
        "No project with a sequence was found",
    )


# ============================================================================
# Category 10: Templates & Presets (6 tools)
# ============================================================================

@TOOLS.tool(
    tool_class=ToolClass.INSPECT,
    safety_hints=OFFLINE_READ,
    result_model=MotionTemplateListResult,
)
def fcp_list_motion_templates() -> ToolOutcome[MotionTemplateListResult]:
    """List installed Motion templates (titles, transitions, generators, effects)."""
    from .utils.paths import motion_templates_dir

    templates_dir = motion_templates_dir()
    if not templates_dir.exists():
        raise FCPMCPError(
            ErrorCode.TARGET_NOT_FOUND,
            f"Motion Templates directory not found: {templates_dir}",
        )

    categories = {}
    template_records = []
    for category_dir in templates_dir.iterdir():
        if not category_dir.is_dir():
            continue
        cat_name = category_dir.stem.replace(".localized", "")
        templates = []
        for template_dir in category_dir.rglob("*.motn"):
            templates.append(template_dir.stem)
            template_records.append(
                MotionTemplateRecord(
                    category=cat_name,
                    name=template_dir.stem,
                    path=str(template_dir),
                )
            )
        if templates:
            categories[cat_name] = templates

    return ToolOutcome(
        text=json.dumps(categories, indent=2),
        structured=MotionTemplateListResult(templates=template_records),
    )


@TOOLS.tool(
    tool_class=ToolClass.INSPECT,
    safety_hints=OFFLINE_READ,
    result_model=ShareDestinationListResult,
)
def fcp_list_share_destinations() -> ToolOutcome[ShareDestinationListResult]:
    """List configured FCP share destinations."""
    from .utils.paths import fcp_destinations_dir

    dest_dir = fcp_destinations_dir()
    if not dest_dir.exists():
        return ToolOutcome(
            text=json.dumps({"destinations": []}, indent=2),
            structured=ShareDestinationListResult(destinations=[]),
        )

    destinations = []
    for f in dest_dir.glob("*.fcpdestination"):
        destinations.append(f.stem)

    return ToolOutcome(
        text=json.dumps({"destinations": destinations}, indent=2),
        structured=ShareDestinationListResult(destinations=destinations),
    )


@TOOLS.tool(
    tool_class=ToolClass.INSPECT,
    safety_hints=OFFLINE_READ,
    result_model=InstalledEffectListResult,
)
def fcp_discover_effects() -> ToolOutcome[InstalledEffectListResult]:
    """List available FCP effects and transitions by scanning known locations."""
    # Check Motion templates for effects
    from .utils.paths import motion_templates_dir

    templates_dir = motion_templates_dir()
    result = {"built_in_transitions": [], "built_in_titles": [], "custom_effects": []}

    # Built-in transitions are well-known
    result["built_in_transitions"] = [
        "Cross Dissolve", "Fade to Color", "Fade to Black",
        "Wipe", "Band Wipe", "Center Wipe", "Checker Wipe", "Clock Wipe",
        "Edge Wipe", "Gradient Wipe", "Inset Wipe", "Jaws Wipe",
        "Barn Door", "Cube", "Doorway", "Mosaic", "Page Curl", "Puzzle", "Ripple",
        "Spin", "Swap", "Swing", "Zoom & Pan",
    ]

    result["built_in_titles"] = [
        "Basic Title", "Basic Lower Third", "Custom Lower Third",
        "Bumper/Opener", "Centered Title", "Credits", "Focus",
        "Gradient", "Line Title", "Scrolling Credits",
    ]

    if templates_dir.exists():
        for effect_dir in (templates_dir / "Effects.localized").rglob("*.moef") if (templates_dir / "Effects.localized").exists() else []:
            result["custom_effects"].append(effect_dir.stem)

    return ToolOutcome(
        text=json.dumps(result, indent=2),
        structured=InstalledEffectListResult(
            effects=result["custom_effects"],
            transitions=result["built_in_transitions"],
            titles=result["built_in_titles"],
        ),
    )


@TOOLS.tool(
    tool_class=ToolClass.INSPECT,
    safety_hints=OFFLINE_READ,
    result_model=TemplateListResult,
)
def fcpxml_list_templates(
    templates_dir: str = "",
) -> ToolOutcome[TemplateListResult]:
    """List available FCPXML template files.

    Args:
        templates_dir: Directory to search (default: FCP_PROJECTS_DIR)
    """
    search_dir = (
        _resolve_input(templates_dir, kind="dir")
        if templates_dir
        else CONFIG.output_dir
    )
    templates = []
    for f in search_dir.rglob("*.fcpxml"):
        if "template" in f.stem.lower() or "preset" in f.stem.lower():
            templates.append(str(f))
    return ToolOutcome(
        text=json.dumps({"templates": templates}, indent=2),
        structured=TemplateListResult(
            templates=templates,
            directory=str(search_dir),
        ),
    )


@TOOLS.tool(
    tool_class=ToolClass.OFFLINE_WRITE,
    safety_hints=OFFLINE_WRITE,
    result_model=UnsupportedToolResult,
)
def fcpxml_apply_template(
    template_path: str,
    clips_json: str,
    project_name: str = "From Template",
    output_path: str = "",
) -> ToolOutcome[UnsupportedToolResult]:
    """Apply an FCPXML template to a set of clips.

    Clip substitution is intentionally unavailable until a stable schema exists.

    Args:
        template_path: Path to template .fcpxml file
        clips_json: JSON array of clips to insert
        project_name: New project name
        output_path: Where to save
    """
    raise FCPMCPError(
        ErrorCode.UNSUPPORTED_CONTRACT,
        (
            "Template clip replacement has no stable clip substitution "
            "schema in v0.2.1"
        ),
    )


@TOOLS.tool(
    tool_class=ToolClass.OFFLINE_WRITE,
    safety_hints=OFFLINE_WRITE,
    result_model=TemplateSaveResult,
)
def fcpxml_save_template(
    path: str,
    template_name: str,
    output_dir: str = "",
) -> ToolOutcome[TemplateSaveResult]:
    """Save the current FCPXML structure as a reusable template.

    Args:
        path: Path to .fcpxml file to use as template
        template_name: Name for the template
        output_dir: Directory to save template (default: FCP_PROJECTS_DIR)
    """
    src = _resolve_input(path, suffixes={".fcpxml"})
    filename = f"template_{template_name}.fcpxml"
    requested = str(Path(output_dir) / filename) if output_dir else filename
    dest = _resolve_output(
        requested,
        input_path=src,
        suffixes={".fcpxml"},
    )
    source_reference = _artifact_reference_for_path(src)

    def validate_template_copy(candidate: Path) -> None:
        candidate_reference = _artifact_reference_for_path(candidate)
        if candidate_reference.sha256 != source_reference.sha256:
            _raise_validation_failure(
                "Template candidate does not match its source bytes"
            )

    receipt = commit_fcpxml_bytes(
        source=src,
        destination=dest,
        xml_bytes=src.read_bytes(),
        validate_candidate=validate_template_copy,
        event_format=CONFIG.log_format,
        operation="save_template",
    )
    template_reference = _artifact_reference(receipt)
    return ToolOutcome(
        text=f"Template saved: {receipt.destination}",
        structured=TemplateSaveResult(
            template=template_reference,
            source_path=source_reference.path,
            source_sha256=source_reference.sha256,
            backup_path=(
                str(receipt.backup_path.resolve())
                if receipt.backup_path is not None
                else None
            ),
            receipt=_receipt_result(receipt),
        ),
    )


# ============================================================================
# Category 1: Library Inspection — live FCP (6 tools)
# ============================================================================

@TOOLS.tool(tool_class=ToolClass.LIVE_READ, safety_hints=LIVE_READ)
def fcp_is_running() -> str:
    """Check if Final Cut Pro is currently running."""
    try:
        result = subprocess.run(
            ["osascript", "-e", 'tell application "System Events" to (name of processes) contains "Final Cut Pro"'],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except FileNotFoundError as error:
        raise FCPMCPError(
            ErrorCode.DEPENDENCY_MISSING,
            "osascript executable was not found",
        ) from error
    except PermissionError as error:
        raise FCPMCPError(
            ErrorCode.PERMISSION_DENIED,
            f"Could not execute osascript: {error}",
        ) from error
    except subprocess.TimeoutExpired as error:
        raise FCPMCPError(
            ErrorCode.COMMAND_FAILED,
            "Final Cut Pro process probe timed out",
        ) from error
    except OSError as error:
        raise FCPMCPError(
            ErrorCode.COMMAND_FAILED,
            f"Final Cut Pro process probe failed: {error}",
        ) from error

    if result.returncode:
        detail = (result.stderr or result.stdout or "no command output").strip()
        raise FCPMCPError(
            ErrorCode.COMMAND_FAILED,
            f"Final Cut Pro process probe exited with status {result.returncode}: {detail}",
        )
    state = result.stdout.strip().lower()
    if state not in {"true", "false"}:
        raise FCPMCPError(
            ErrorCode.COMMAND_FAILED,
            "Final Cut Pro process probe returned an unexpected response",
        )
    return json.dumps({"running": state == "true"})


@TOOLS.tool(tool_class=ToolClass.LIVE_READ, safety_hints=LIVE_READ)
def fcp_get_libraries() -> str:
    """Get all open libraries in Final Cut Pro (requires FCP to be running)."""
    return automation.run_osascript(
        automation.FCP_LIBRARIES,
        config=CONFIG,
    )


@TOOLS.tool(tool_class=ToolClass.LIVE_READ, safety_hints=LIVE_READ)
def fcp_get_events(library_name: str = "") -> str:
    """Get events in a library (or all libraries if name not specified).

    Args:
        library_name: Library name to filter (optional)
    """
    return automation.run_osascript(
        automation.FCP_EVENTS,
        [library_name],
        config=CONFIG,
    )


@TOOLS.tool(tool_class=ToolClass.LIVE_READ, safety_hints=LIVE_READ)
def fcp_get_projects(event_name: str = "") -> str:
    """Get projects in an event (or all events if name not specified).

    Args:
        event_name: Event name to filter (optional)
    """
    return automation.run_osascript(
        automation.FCP_PROJECTS,
        [event_name],
        config=CONFIG,
    )


@TOOLS.tool(tool_class=ToolClass.LIVE_READ, safety_hints=LIVE_READ)
def fcp_get_timeline_info() -> str:
    """Get info about the current/first timeline in FCP."""
    result = automation.run_osascript(
        automation.FCP_TIMELINE_INFO,
        config=CONFIG,
    )
    try:
        payload = json.loads(result)
    except json.JSONDecodeError:
        return result
    if isinstance(payload, dict) and payload.get("error"):
        raise FCPMCPError(
            ErrorCode.TARGET_NOT_FOUND,
            f"Timeline unavailable: {payload['error']}",
        )
    return result


@TOOLS.tool(tool_class=ToolClass.LIVE_READ, safety_hints=LIVE_READ)
def fcp_get_app_state() -> str:
    """Get FCP application state — version, frontmost status."""
    return automation.run_osascript(
        automation.FCP_APP_STATE,
        config=CONFIG,
    )


# ============================================================================
# Category 6: FCP Live Control (10 tools)
# ============================================================================

@TOOLS.tool(tool_class=ToolClass.LIVE_WRITE, safety_hints=LIVE_WRITE)
def fcp_open_library(library_path: str) -> str:
    """Open a FCP library file.

    Args:
        library_path: Path to .fcpbundle file
    """
    automation.require_live_control(CONFIG)
    path = _resolve_input(
        library_path,
        kind="dir",
        suffixes={".fcpbundle"},
    )
    try:
        subprocess.run(["open", str(path)], check=True, timeout=10)
        return f"Opening library: {path}"
    except subprocess.TimeoutExpired as error:
        raise FCPMCPError(
            ErrorCode.COMMAND_FAILED,
            "Timed out while opening the FCP library",
        ) from error
    except (subprocess.CalledProcessError, OSError) as error:
        raise FCPMCPError(
            ErrorCode.COMMAND_FAILED,
            f"Could not open the FCP library: {error}",
        ) from error


@TOOLS.tool(tool_class=ToolClass.LIVE_WRITE, safety_hints=LIVE_WRITE)
def fcp_import_xml(fcpxml_path: str) -> str:
    """Import an FCPXML file into Final Cut Pro.

    Args:
        fcpxml_path: Path to .fcpxml file
    """
    automation.require_live_control(CONFIG)
    path = _resolve_input(fcpxml_path, suffixes={".fcpxml"})
    try:
        subprocess.run(["open", "-a", "Final Cut Pro", str(path)], check=True, timeout=10)
        return f"Importing FCPXML: {path}"
    except subprocess.TimeoutExpired as error:
        raise FCPMCPError(
            ErrorCode.COMMAND_FAILED,
            "Timed out while importing FCPXML",
        ) from error
    except (subprocess.CalledProcessError, OSError) as error:
        raise FCPMCPError(
            ErrorCode.COMMAND_FAILED,
            f"Could not import FCPXML: {error}",
        ) from error


@TOOLS.tool(tool_class=ToolClass.LIVE_WRITE, safety_hints=LIVE_WRITE)
def fcp_export_xml() -> str:
    """Trigger XML export in FCP via menu automation (requires Accessibility permissions)."""
    return automation.run_osascript(
        automation.FCP_EXPORT_XML,
        timeout=30,
        config=CONFIG,
    )


@TOOLS.tool(tool_class=ToolClass.LIVE_WRITE, safety_hints=LIVE_WRITE)
def fcp_playback(action: str = "toggle") -> str:
    """Control FCP playback.

    Args:
        action: "play", "pause", "stop", or "toggle" (space bar)
    """
    key_map = {
        "toggle": "space",
        "play": "l",
        "stop": "k",
        "pause": "k",
    }
    if action not in key_map:
        raise FCPMCPError(
            ErrorCode.INVALID_ARGUMENTS,
            f"Unsupported playback action: {action}",
        )
    return automation.run_osascript(
        automation.FCP_PLAYBACK,
        [action, key_map[action]],
        timeout=30,
        config=CONFIG,
    )


@TOOLS.tool(tool_class=ToolClass.LIVE_WRITE, safety_hints=LIVE_WRITE)
def fcp_navigate(timecode: str = "") -> str:
    """Navigate to a specific timecode in FCP.

    Args:
        timecode: Timecode to navigate to (e.g., "00:01:30:00"). Opens timecode entry if provided.
    """
    if not timecode:
        raise FCPMCPError(ErrorCode.INVALID_ARGUMENTS, "No timecode provided")

    # Control+P opens the timecode entry field in FCP
    clean_tc = timecode.replace(":", "").replace(";", "")
    return automation.run_osascript(
        automation.FCP_NAVIGATE,
        [timecode, clean_tc],
        timeout=30,
        config=CONFIG,
    )


@TOOLS.tool(tool_class=ToolClass.LIVE_WRITE, safety_hints=LIVE_WRITE)
def fcp_select_tool(tool: str = "select") -> str:
    """Switch FCP editing tool.

    Args:
        tool: "select" (A), "trim" (T), "position" (P), "range" (R), "blade" (B), "zoom" (Z), "hand" (H)
    """
    tool_keys = {
        "select": "a", "trim": "t", "position": "p",
        "range": "r", "blade": "b", "zoom": "z", "hand": "h",
    }
    if tool not in tool_keys:
        raise FCPMCPError(
            ErrorCode.INVALID_ARGUMENTS,
            f"Unsupported FCP tool: {tool}",
        )
    return automation.run_osascript(
        automation.FCP_SELECT_TOOL,
        [tool, tool_keys[tool]],
        timeout=30,
        config=CONFIG,
    )


@TOOLS.tool(tool_class=ToolClass.LIVE_WRITE, safety_hints=LIVE_WRITE)
def fcp_undo() -> str:
    """Undo the last action in FCP."""
    return automation.run_osascript(
        automation.FCP_UNDO,
        timeout=30,
        config=CONFIG,
    )


@TOOLS.tool(tool_class=ToolClass.LIVE_WRITE, safety_hints=LIVE_WRITE)
def fcp_redo() -> str:
    """Redo the last undone action in FCP."""
    return automation.run_osascript(
        automation.FCP_REDO,
        timeout=30,
        config=CONFIG,
    )


@TOOLS.tool(tool_class=ToolClass.LIVE_WRITE, safety_hints=LIVE_WRITE)
def fcp_menu_command(menu_path: str) -> str:
    """Execute any FCP menu command by path.

    Args:
        menu_path: Menu path like "File > Export XML..." or "Edit > Select All"
    """
    parts = [p.strip() for p in menu_path.split(">")]
    if len(parts) not in {2, 3} or any(not part for part in parts):
        raise FCPMCPError(
            ErrorCode.INVALID_ARGUMENTS,
            "Menu path must contain two or three nonempty components",
        )
    validated_json = json.dumps(parts, separators=(",", ":"))
    return automation.run_osascript(
        automation.FCP_MENU_COMMAND,
        [validated_json, menu_path, *parts],
        timeout=30,
        config=CONFIG,
    )


@TOOLS.tool(tool_class=ToolClass.LIVE_WRITE, safety_hints=LIVE_WRITE)
def fcp_keyboard_shortcut(keys: str) -> str:
    """Send a keyboard shortcut to FCP.

    Args:
        keys: Shortcut description like "cmd+c", "cmd+shift+e", "option+w"
    """
    key, modifiers = automation.parse_shortcut(keys)
    return automation.run_osascript(
        automation.FCP_KEYBOARD_SHORTCUT,
        [keys, key, *modifiers],
        timeout=30,
        config=CONFIG,
    )


# ============================================================================
# Category 7: Export & Encoding (6 tools)
# ============================================================================

@TOOLS.tool(tool_class=ToolClass.LIVE_WRITE, safety_hints=LIVE_WRITE)
def fcp_share(destination: str = "") -> str:
    """Trigger a share/export from FCP.

    Args:
        destination: Share destination name (opens default if empty)
    """
    return automation.run_osascript(
        automation.FCP_SHARE,
        [destination],
        timeout=30,
        config=CONFIG,
    )


@TOOLS.tool(tool_class=ToolClass.LIVE_WRITE, safety_hints=LIVE_WRITE)
def compressor_encode(
    input_path: str,
    setting_path: str = "",
    output_dir: str = "",
    batch_name: str = "MCP Encode",
) -> str:
    """Encode a file using Compressor CLI.

    Args:
        input_path: Path to input media file
        setting_path: Path to Compressor preset (.cmprstng)
        output_dir: Output directory
        batch_name: Batch name for Compressor
    """
    from .media.ffprobe import _run_checked
    from .utils.paths import compressor_binary

    automation.require_live_control(CONFIG)
    comp = compressor_binary()
    if not comp.exists():
        raise FCPMCPError(
            ErrorCode.DEPENDENCY_MISSING,
            f"Compressor not found at {comp}",
        )

    source = _resolve_input(input_path)
    cmd = [str(comp), "-batchName", batch_name, "-jobpath", str(source)]
    if setting_path:
        setting = _resolve_input(setting_path, suffixes={".cmprstng"})
        cmd.extend(["-settingpath", str(setting)])
    if output_dir:
        destination = _resolve_output(
            output_dir,
            input_path=source,
        )
        if not destination.is_dir():
            raise FCPMCPError(
                ErrorCode.INVALID_PATH,
                f"Compressor output directory not found: {destination}",
            )
        cmd.extend(["-locationpath", str(destination)])

    result = _run_checked(cmd, timeout=30)
    return f"Compressor encode started: {result.stdout or result.stderr}"


@TOOLS.tool(tool_class=ToolClass.LIVE_READ, safety_hints=LIVE_READ)
def compressor_list_settings() -> str:
    """List available Compressor encoding presets."""
    from .media.ffprobe import _run_checked
    from .utils.paths import compressor_binary, compressor_settings_dir

    # Built-in settings from Compressor
    settings_dir = compressor_settings_dir()
    custom = []
    if settings_dir.exists():
        for f in settings_dir.rglob("*.cmprstng"):
            custom.append(str(f))

    # Also try listing via Compressor CLI
    comp = compressor_binary()
    built_in = []
    if comp.exists():
        result = _run_checked([str(comp), "-info"], timeout=10)
        built_in = [
            line.strip()
            for line in result.stdout.splitlines()
            if line.strip()
        ]

    return json.dumps({"custom_presets": custom, "cli_info": built_in}, indent=2)


@TOOLS.tool(
    tool_class=ToolClass.OFFLINE_WRITE,
    safety_hints=OFFLINE_WRITE,
    result_model=ExportResult,
)
def fcpxml_export_resolve(
    path: str,
    output_path: str = "",
) -> ToolOutcome[ExportResult]:
    """Convert FCPXML to DaVinci Resolve-compatible format (FCPXML v1.9).

    Args:
        path: Path to .fcpxml file
        output_path: Output file path
    """

    source = _resolve_input(path, suffixes={".fcpxml"})
    source_reference = _artifact_reference_for_path(source)
    mod = FCPXMLModifier(source)
    # Downgrade version for Resolve compatibility
    mod.root.set("version", "1.9")

    # Remove FCP-specific attributes that Resolve doesn't understand
    for el in mod.root.iter():
        for attr in list(el.attrib.keys()):
            if attr in ("tcFormat",):
                # Resolve handles this differently but it's fine to keep
                pass

    if not output_path:
        output_path = str(
            mod.path.parent / f"{mod.path.stem}_resolve.fcpxml"
        )
    receipt = _save_modifier_receipt(
        mod,
        output_path,
        validate_candidate=_validate_resolve_export,
    )
    return ToolOutcome(
        text=(
            "Resolve-compatible FCPXML saved: "
            f"{receipt.destination}"
        ),
        structured=ExportResult(
            format="resolve",
            source=source_reference,
            artifact=_artifact_reference(receipt),
            receipt=_receipt_result(receipt),
        ),
    )


@TOOLS.tool(
    tool_class=ToolClass.OFFLINE_WRITE,
    safety_hints=OFFLINE_WRITE,
    result_model=ExportResult,
)
def fcpxml_export_fcp7(
    path: str,
    output_path: str = "",
) -> ToolOutcome[ExportResult]:
    """Convert FCPXML to FCP7 XML format (compatible with Premiere Pro and Avid).

    Args:
        path: Path to .fcpxml file
        output_path: Output file path
    """
    import xml.etree.ElementTree as ET

    source = _resolve_input(path, suffixes={".fcpxml"})
    source_reference = _artifact_reference_for_path(source)
    doc = _parser.parse(source)

    # Build FCP7 XMEML
    xmeml = ET.Element("xmeml")
    xmeml.set("version", "5")

    for project in doc.all_projects:
        proj_el = ET.SubElement(xmeml, "project")
        name_el = ET.SubElement(proj_el, "name")
        name_el.text = project.name

        if not project.sequence or not project.sequence.spine:
            continue

        seq = project.sequence
        seq_el = ET.SubElement(proj_el, "sequence")
        seq_name = ET.SubElement(seq_el, "name")
        seq_name.text = project.name

        # Duration
        dur_el = ET.SubElement(seq_el, "duration")
        dur_el.text = str(round(seq.duration.to_seconds() * 30))  # in frames at 30fps

        # Rate
        rate_el = ET.SubElement(seq_el, "rate")
        tb_el = ET.SubElement(rate_el, "timebase")
        fmt = doc.formats.get(seq.format_ref)
        tb_el.text = str(round(fmt.fps)) if fmt else "30"

        # Media
        media_el = ET.SubElement(seq_el, "media")
        video_el = ET.SubElement(media_el, "video")
        track_el = ET.SubElement(video_el, "track")

        for clip in seq.spine.clips:
            if clip.is_gap:
                continue
            clipitem = ET.SubElement(track_el, "clipitem")
            ci_name = ET.SubElement(clipitem, "name")
            ci_name.text = clip.name

            start_el = ET.SubElement(clipitem, "start")
            start_el.text = str(round(clip.offset.to_seconds() * 30))
            end_el = ET.SubElement(clipitem, "end")
            end_el.text = str(round(clip.end_offset.to_seconds() * 30))

            in_el = ET.SubElement(clipitem, "in")
            in_el.text = str(round(clip.start.to_seconds() * 30))
            out_el = ET.SubElement(clipitem, "out")
            out_el.text = str(round(clip.source_end.to_seconds() * 30))

            # File reference
            if clip.ref and clip.ref in doc.assets:
                asset = doc.assets[clip.ref]
                file_el = ET.SubElement(clipitem, "file")
                file_el.set("id", clip.ref)
                fname = ET.SubElement(file_el, "name")
                fname.text = asset.name
                pathurl = ET.SubElement(file_el, "pathurl")
                pathurl.text = asset.src

    destination = _resolve_output(
        output_path or str(source.parent / f"{source.stem}_fcp7.xml"),
        input_path=source,
        suffixes={".xml"},
    )
    ET.indent(xmeml, space="    ")
    xml_text = ET.tostring(
        xmeml,
        encoding="unicode",
        xml_declaration=True,
    )
    write_receipt = atomic_replace_bytes(
        destination,
        f"{xml_text}\n".encode(),
        validate=_validate_fcp7_export,
        event_format=CONFIG.log_format,
    )
    return ToolOutcome(
        text=f"FCP7 XML saved: {write_receipt.destination}",
        structured=ExportResult(
            format="fcp7",
            source=source_reference,
            artifact=_artifact_reference_for_path(
                write_receipt.destination,
                media_type="application/xml",
            ),
            receipt=_atomic_receipt_result(
                write_receipt,
                source=source_reference,
            ),
        ),
    )


@TOOLS.tool(
    tool_class=ToolClass.OFFLINE_WRITE,
    safety_hints=OFFLINE_WRITE,
    result_model=ExportResult,
)
def fcpxml_export_edl(
    path: str,
    output_path: str = "",
) -> ToolOutcome[ExportResult]:
    """Export timeline as EDL (Edit Decision List).

    Args:
        path: Path to .fcpxml file
        output_path: Output .edl file path
    """
    source = _resolve_input(path, suffixes={".fcpxml"})
    source_reference = _artifact_reference_for_path(source)
    doc = _parser.parse(source)
    fmt = next(iter(doc.formats.values())) if doc.formats else None
    fps = fmt.fps if fmt else 29.97

    lines = ["TITLE: " + (doc.all_projects[0].name if doc.all_projects else "Untitled")]
    lines.append(f"FCM: {'DROP FRAME' if fps in (29.97, 59.94) else 'NON-DROP FRAME'}")
    lines.append("")

    edit_num = 1
    for project in doc.all_projects:
        if not project.sequence or not project.sequence.spine:
            continue
        for clip in project.sequence.spine.non_gap_clips:
            asset = doc.assets.get(clip.ref)
            reel = asset.name[:8] if asset else "AX"

            src_in = clip.start.to_timecode(fps)
            src_out = clip.source_end.to_timecode(fps)
            rec_in = clip.offset.to_timecode(fps)
            rec_out = clip.end_offset.to_timecode(fps)

            lines.append(f"{edit_num:03d}  {reel:8s} V     C        {src_in} {src_out} {rec_in} {rec_out}")

            if clip.name:
                lines.append(f"* FROM CLIP NAME: {clip.name}")

            lines.append("")
            edit_num += 1

    edl_content = "\n".join(lines)

    destination = _resolve_output(
        output_path or str(source.parent / f"{source.stem}.edl"),
        input_path=source,
        suffixes={".edl"},
    )
    write_receipt = atomic_replace_bytes(
        destination,
        edl_content.encode(),
        validate=lambda candidate: _validate_edl_export(
            candidate,
            expected_edit_count=edit_num - 1,
        ),
        event_format=CONFIG.log_format,
    )
    committed_edit_count = edit_num - 1
    return ToolOutcome(
        text=(
            f"EDL exported ({committed_edit_count} edits): "
            f"{write_receipt.destination}"
        ),
        structured=ExportResult(
            format="edl",
            source=source_reference,
            artifact=_artifact_reference_for_path(
                write_receipt.destination,
                media_type="text/x-cmx3600",
            ),
            receipt=_atomic_receipt_result(
                write_receipt,
                source=source_reference,
            ),
        ),
    )


# ============================================================================
# Category 5: Media Analysis — FFmpeg (8 tools)
# ============================================================================


def _seconds_timecode(seconds: float) -> str:
    milliseconds = round(seconds * 1000)
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, milliseconds = divmod(remainder, 1000)
    return (
        f"{hours:02d}:{minutes:02d}:{whole_seconds:02d}."
        f"{milliseconds:03d}"
    )


def _bounded_text(value: str) -> str:
    return value[-2000:]


def _raw_summary(lines: list[str]) -> str | None:
    if not lines:
        return None
    return _bounded_text("\n".join(lines))


def _optional_number(
    value: object,
    *,
    label: str,
    failures: list[str],
    allow_fraction: bool = False,
) -> float | None:
    if value is None or value == "":
        return None
    try:
        if allow_fraction and isinstance(value, str) and "/" in value:
            numerator, denominator = value.split("/", 1)
            number = float(numerator) / float(denominator)
        else:
            number = float(value)
        if not isfinite(number):
            raise ValueError
        return number
    except (TypeError, ValueError, ZeroDivisionError):
        failures.append(f"{label}: {value}")
        return None


def _legacy_number(value: object, normalized: float | None) -> float:
    if normalized is not None:
        return normalized
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _stream_record(
    stream: dict[str, Any],
) -> StreamRecord:
    failures: list[str] = []
    raw_index = stream.get("index")
    if isinstance(raw_index, int) and not isinstance(raw_index, bool):
        index = raw_index
    else:
        index = None
        failures.append(
            "index: <missing>" if raw_index is None else f"index: {raw_index}"
        )
    tags = stream.get("tags")
    tag_values = tags if isinstance(tags, dict) else {}
    common = {
        "index": index,
        "codec": stream.get("codec_name"),
        "codec_long_name": stream.get("codec_long_name"),
        "language": str(tag_values.get("language", "")),
        "duration_seconds": _optional_number(
            stream.get("duration"),
            label="duration",
            failures=failures,
        ),
        "bitrate_kbps": (
            value / 1000
            if (
                value := _optional_number(
                    stream.get("bit_rate"),
                    label="bit_rate",
                    failures=failures,
                )
            )
            is not None
            else None
        ),
    }
    codec_type = stream.get("codec_type")
    if codec_type == "video":
        record: StreamRecord = VideoStreamRecord(
            **common,
            codec_type="video",
            width=stream.get("width"),
            height=stream.get("height"),
            frame_rate=_optional_number(
                stream.get("r_frame_rate"),
                label="r_frame_rate",
                failures=failures,
                allow_fraction=True,
            ),
            pixel_format=stream.get("pix_fmt"),
            raw_summary=_raw_summary(failures),
        )
    elif codec_type == "audio":
        sample_rate = _optional_number(
            stream.get("sample_rate"),
            label="sample_rate",
            failures=failures,
        )
        record = AudioStreamRecord(
            **common,
            codec_type="audio",
            sample_rate_hz=int(sample_rate) if sample_rate is not None else None,
            channels=stream.get("channels"),
            channel_layout=stream.get("channel_layout"),
            raw_summary=_raw_summary(failures),
        )
    elif codec_type == "subtitle":
        record = SubtitleStreamRecord(
            **common,
            codec_type="subtitle",
            title=(
                str(tag_values["title"])
                if tag_values.get("title") is not None
                else None
            ),
            raw_summary=_raw_summary(failures),
        )
    else:
        if codec_type in {"data", "attachment"}:
            normalized_codec_type = codec_type
            reason = "unsupported_codec_type"
        elif codec_type is None:
            normalized_codec_type = "unknown"
            reason = "missing_codec_type"
            failures.append("codec_type: <missing>")
        elif isinstance(codec_type, str):
            normalized_codec_type = "unknown"
            reason = "unrecognized_codec_type"
            failures.append(f"codec_type: {codec_type}")
        else:
            normalized_codec_type = "unknown"
            reason = "malformed_codec_type"
            failures.append(f"codec_type: {codec_type}")
        record = UnsupportedStreamRecord(
            **common,
            codec_type=normalized_codec_type,
            reason=reason,
            raw_summary=_raw_summary(failures),
        )
    return record


@TOOLS.tool(
    tool_class=ToolClass.INSPECT,
    safety_hints=OFFLINE_READ,
    result_model=MediaInfoResult,
)
def media_info(path: str) -> ToolOutcome[MediaInfoResult]:
    """Get detailed media file info (codec, resolution, duration, bitrate, etc.).

    Args:
        path: Path to media file
    """
    from .media.ffprobe import probe_file

    source = _resolve_input(path)
    info = probe_file(str(source))
    # Simplify for readability
    fmt = info.get("format", {})
    streams = info.get("streams", [])
    failures: list[str] = []
    duration = _optional_number(
        fmt.get("duration"),
        label="duration",
        failures=failures,
    )
    size = _optional_number(fmt.get("size"), label="size", failures=failures)
    bitrate = _optional_number(
        fmt.get("bit_rate"),
        label="bit_rate",
        failures=failures,
    )
    legacy_duration = _legacy_number(fmt.get("duration"), duration)
    legacy_size = _legacy_number(fmt.get("size"), size)
    legacy_bitrate = _legacy_number(fmt.get("bit_rate"), bitrate)
    summary = {
        "filename": fmt.get("filename"),
        "duration": f"{legacy_duration:.2f}s",
        "size_mb": f"{legacy_size / 1048576:.1f}",
        "bitrate_kbps": f"{legacy_bitrate / 1000:.0f}",
        "format": fmt.get("format_long_name"),
        "streams": [],
    }
    for stream in streams:
        stream_info = {
            "type": stream.get("codec_type"),
            "codec": stream.get("codec_name"),
        }
        if stream.get("codec_type") == "video":
            stream_info.update({
                "width": stream.get("width"),
                "height": stream.get("height"),
                "fps": stream.get("r_frame_rate"),
                "pix_fmt": stream.get("pix_fmt"),
            })
        elif stream.get("codec_type") == "audio":
            stream_info.update({
                "sample_rate": stream.get("sample_rate"),
                "channels": stream.get("channels"),
                "channel_layout": stream.get("channel_layout"),
            })
        summary["streams"].append(stream_info)
    return ToolOutcome(
        text=json.dumps(summary, indent=2),
        structured=MediaInfoResult(
            filename=fmt.get("filename"),
            format=fmt.get("format_long_name"),
            duration_seconds=duration,
            size_mb=size / 1048576 if size is not None else None,
            bitrate_kbps=bitrate / 1000 if bitrate is not None else None,
            streams=[
                _stream_record(stream)
                for stream in streams
            ],
            raw_summary=_raw_summary(failures),
        ),
    )


@TOOLS.tool(
    tool_class=ToolClass.INSPECT,
    safety_hints=OFFLINE_READ,
    result_model=SilenceDetectionResult,
)
def media_detect_silence(
    path: str,
    noise_threshold: str = "-30dB",
    min_duration: float = 0.5,
) -> ToolOutcome[SilenceDetectionResult]:
    """Detect silent sections in audio/video files.

    Args:
        path: Path to media file
        noise_threshold: Noise floor threshold (e.g., "-30dB", "-40dB")
        min_duration: Minimum silence duration in seconds
    """
    from .media.ffprobe import detect_silence

    source = _resolve_input(path)
    silences = detect_silence(
        str(source),
        noise_threshold,
        min_duration,
    )
    structured = SilenceDetectionResult(
        noise_threshold=noise_threshold,
        minimum_duration_seconds=min_duration,
        ranges=[
            SilenceRangeRecord(
                start_seconds=item["start"],
                end_seconds=item["end"],
                duration_seconds=item["duration"],
            )
            for item in silences
        ],
    )
    if not silences:
        return ToolOutcome(
            text="No silent sections detected.",
            structured=structured,
        )
    return ToolOutcome(
        text=json.dumps(silences, indent=2),
        structured=structured,
    )


@TOOLS.tool(
    tool_class=ToolClass.INSPECT,
    safety_hints=OFFLINE_READ,
    result_model=BeatDetectionResult,
)
def media_detect_beats(path: str) -> ToolOutcome[BeatDetectionResult]:
    """Detect beat positions in audio/music files.

    Returns beat timestamps that can be used for music-synced editing.

    Args:
        path: Path to audio/video file
    """
    from .media.ffprobe import detect_beats

    source = _resolve_input(path)
    beats = detect_beats(str(source))
    intervals = [
        round(current - previous, 3)
        for previous, current in pairwise(beats)
    ]
    average_interval = (
        sum(intervals) / len(intervals) if intervals else None
    )
    return ToolOutcome(
        text=json.dumps(
            {"beat_count": len(beats), "beats": beats},
            indent=2,
        ),
        structured=BeatDetectionResult(
            beat_count=len(beats),
            beats=[
                BeatRecord(
                    seconds=seconds,
                    timecode=_seconds_timecode(seconds),
                )
                for seconds in beats
            ],
            cadence=BeatCadenceRecord(
                intervals_seconds=intervals,
                average_interval_seconds=average_interval,
                estimated_bpm=(
                    60 / average_interval
                    if average_interval is not None and average_interval > 0
                    else None
                ),
            ),
        ),
    )


@TOOLS.tool(
    tool_class=ToolClass.INSPECT,
    safety_hints=OFFLINE_READ,
    result_model=LoudnessResult,
)
def media_loudness(path: str) -> ToolOutcome[LoudnessResult]:
    """Analyze audio loudness (EBU R128 / LUFS).

    Returns integrated loudness, loudness range, and true peak.

    Args:
        path: Path to audio/video file
    """
    from .media.ffprobe import _find_ffmpeg, _run_checked

    source = _resolve_input(path)
    command_result = _run_checked(
        [
            _find_ffmpeg(),
            "-i",
            str(source),
            "-af",
            "ebur128=peak=true",
            "-f",
            "null",
            "-",
        ],
        timeout=120,
    )
    legacy_result: dict[str, float] = {}
    result: dict[str, float] = {}
    warnings: list[ParserWarningRecord] = []
    raw_lines: list[str] = []
    fields = (
        ("I:", "LUFS", "integrated_lufs"),
        ("LRA:", "LU", "loudness_range_lu"),
        ("Peak:", "dBFS", "true_peak_dbfs"),
    )
    for raw_line in (command_result.stderr or "").splitlines():
        line = raw_line.strip()
        for marker, unit, field in fields:
            if marker not in line or unit not in line:
                continue
            raw_value = line.split(marker, 1)[1].split(unit, 1)[0].strip()
            try:
                value = float(raw_value)
            except ValueError:
                warnings.append(
                    ParserWarningRecord(
                        kind="malformed_value",
                        field=field,
                        message=f"{field} was not numeric",
                    )
                )
                raw_lines.append(line)
            else:
                legacy_result[field] = value
                if isfinite(value):
                    result[field] = value
                else:
                    warnings.append(
                        ParserWarningRecord(
                            kind="malformed_value",
                            field=field,
                            message=f"{field} was not finite",
                        )
                    )
                    raw_lines.append(line)
            break
        if "warning:" in line.lower():
            warnings.append(
                ParserWarningRecord(
                    kind="command_warning",
                    field=None,
                    message=_bounded_text(line),
                )
            )
    if not legacy_result:
        raise FCPMCPError(
            ErrorCode.OUTPUT_MISSING,
            "FFmpeg returned no loudness summary; the file may have no audio",
        )
    return ToolOutcome(
        text=json.dumps(legacy_result, indent=2),
        structured=LoudnessResult(
            integrated_lufs=result.get("integrated_lufs"),
            loudness_range_lu=result.get("loudness_range_lu"),
            true_peak_dbfs=result.get("true_peak_dbfs"),
            warnings=warnings,
            raw_summary=_raw_summary(raw_lines),
        ),
    )


@TOOLS.tool(tool_class=ToolClass.OFFLINE_WRITE, safety_hints=OFFLINE_WRITE)
def media_extract_thumbnail(
    path: str,
    time: float = 0.0,
    output_path: str = "",
    width: int = 320,
) -> str:
    """Extract a frame thumbnail from video at a specific time.

    Args:
        path: Path to video file
        time: Time in seconds to extract frame from
        output_path: Where to save thumbnail (default: auto-generated)
        width: Thumbnail width in pixels
    """
    from .media.ffprobe import extract_thumbnail

    source = _resolve_input(path)
    destination = _resolve_output(
        output_path
        or str(source.parent / f"{source.stem}_thumb_{time:.0f}s.jpg"),
        input_path=source,
        suffixes={".jpg", ".jpeg", ".png"},
    )
    out = extract_thumbnail(
        str(source),
        time,
        str(destination),
        width,
    )
    return f"Thumbnail saved: {out}"


@TOOLS.tool(tool_class=ToolClass.OFFLINE_WRITE, safety_hints=OFFLINE_WRITE)
def media_extract_thumbnails(
    path: str,
    interval: float = 5.0,
    output_dir: str = "",
    width: int = 320,
) -> str:
    """Extract thumbnails at regular intervals (contact sheet / storyboard).

    Args:
        path: Path to video file
        interval: Seconds between thumbnails
        output_dir: Directory to save thumbnails
        width: Thumbnail width in pixels
    """
    from .media.ffprobe import extract_thumbnails

    source = _resolve_input(path)
    destination = _resolve_output(
        output_dir or str(source.parent / f"{source.stem}_thumbs"),
        input_path=source,
    )
    thumbs = extract_thumbnails(
        str(source),
        interval,
        str(destination),
        width,
    )
    return json.dumps({"count": len(thumbs), "thumbnails": thumbs}, indent=2)


@TOOLS.tool(
    tool_class=ToolClass.INSPECT,
    safety_hints=OFFLINE_READ,
    result_model=StreamListResult,
)
def media_list_streams(path: str) -> ToolOutcome[StreamListResult]:
    """List all audio, video, and subtitle streams in a media file.

    Args:
        path: Path to media file
    """
    from .media.ffprobe import get_streams

    source = _resolve_input(path)
    streams = get_streams(str(source))
    result = []
    for index, stream in enumerate(streams):
        result.append({
            "index": index,
            "type": stream.get("codec_type"),
            "codec": stream.get("codec_name"),
            "language": stream.get("tags", {}).get("language", ""),
            "duration": stream.get("duration"),
        })
    return ToolOutcome(
        text=json.dumps(result, indent=2),
        structured=StreamListResult(
            streams=[
                _stream_record(stream)
                for stream in streams
            ],
        ),
    )


@TOOLS.tool(
    tool_class=ToolClass.INSPECT,
    safety_hints=OFFLINE_READ,
    result_model=SceneDetectionResult,
)
def media_scene_detect(
    path: str,
    threshold: float = 0.3,
) -> ToolOutcome[SceneDetectionResult]:
    """Detect scene changes in video.

    Useful for automatic clip segmentation.

    Args:
        path: Path to video file
        threshold: Scene change sensitivity (0.0-1.0, lower = more sensitive)
    """
    from .media.ffprobe import _find_ffmpeg, _run_checked

    source = _resolve_input(path)
    command_result = _run_checked(
        [
            _find_ffmpeg(),
            "-i",
            str(source),
            "-vf",
            f"select='gt(scene,{threshold})',showinfo",
            "-f",
            "null",
            "-",
        ],
        timeout=300,
    )
    legacy_scenes: list[dict[str, float]] = []
    scene_changes: list[SceneChangeRecord] = []
    warnings: list[ParserWarningRecord] = []
    raw_lines: list[str] = []
    for raw_line in (command_result.stderr or "").splitlines():
        line = raw_line.strip()
        if "warning:" in line.lower():
            warnings.append(
                ParserWarningRecord(
                    kind="command_warning",
                    field=None,
                    message=_bounded_text(line),
                )
            )
        if "pts_time:" in line:
            raw_time = line.split("pts_time:", 1)[1].split()[0]
            line_failed = False
            try:
                legacy_time_seconds = float(raw_time)
            except ValueError:
                time_seconds = None
                warnings.append(
                    ParserWarningRecord(
                        kind="malformed_value",
                        field="time_seconds",
                        message="scene timestamp was not numeric",
                    )
                )
                line_failed = True
            else:
                legacy_scenes.append({"time": legacy_time_seconds})
                if isfinite(legacy_time_seconds):
                    time_seconds = legacy_time_seconds
                else:
                    time_seconds = None
                    warnings.append(
                        ParserWarningRecord(
                            kind="malformed_value",
                            field="time_seconds",
                            message="scene timestamp was not finite",
                        )
                    )
                    line_failed = True
            score = None
            if "lavfi.scene_score:" in line:
                raw_score = line.split("lavfi.scene_score:", 1)[1].split()[0]
                try:
                    score = float(raw_score)
                    if not isfinite(score):
                        raise ValueError
                except ValueError:
                    score = None
                    warnings.append(
                        ParserWarningRecord(
                            kind="malformed_value",
                            field="score",
                            message="scene score was not numeric",
                        )
                    )
                    line_failed = True
            if line_failed:
                raw_lines.append(line)
            scene_changes.append(
                SceneChangeRecord(
                    time_seconds=time_seconds,
                    timecode=(
                        _seconds_timecode(time_seconds)
                        if time_seconds is not None
                        else None
                    ),
                    score=score,
                )
            )
    return ToolOutcome(
        text=json.dumps(
            {
                "scene_count": len(legacy_scenes),
                "scenes": legacy_scenes,
            },
            indent=2,
        ),
        structured=SceneDetectionResult(
            threshold=threshold,
            scene_count=sum(
                change.time_seconds is not None
                for change in scene_changes
            ),
            scene_changes=scene_changes,
            warnings=warnings,
            raw_summary=_raw_summary(raw_lines),
        ),
    )


@TOOLS.tool(tool_class=ToolClass.OFFLINE_WRITE, safety_hints=OFFLINE_WRITE)
def media_extract_audio(
    path: str,
    output_path: str = "",
    format: str = "wav",
) -> str:
    """Extract audio track from a video file.

    Useful for feeding video audio into transcription, analysis, or
    the sheet-music-maker pipeline (basic-pitch → MIDI → notation).

    Args:
        path: Path to video file
        output_path: Where to save audio (default: same dir, .wav extension)
        format: Audio format: wav, mp3, flac (default: wav)
    """
    from .media.ffprobe import _find_ffmpeg, _run_checked

    source = _resolve_input(path)
    if format not in {"wav", "mp3", "flac"}:
        raise FCPMCPError(
            ErrorCode.INVALID_ARGUMENTS,
            f"Unsupported audio format: {format}",
        )

    output = _resolve_output(
        output_path or str(source.with_suffix(f".{format}")),
        input_path=source,
        suffixes={f".{format}"},
    )

    cmd = [_find_ffmpeg(), "-y", "-i", str(source), "-vn"]
    if format == "wav":
        cmd.extend(["-c:a", "pcm_s16le"])
    elif format == "mp3":
        cmd.extend(["-c:a", "libmp3lame", "-q:a", "2"])
    elif format == "flac":
        cmd.extend(["-c:a", "flac"])
    cmd.append(str(output))

    _run_checked(
        cmd,
        timeout=300,
        expected_outputs=[output],
    )

    size_mb = output.stat().st_size / (1024 * 1024)
    return json.dumps({
        "audio_file": str(output),
        "format": format,
        "size_mb": f"{size_mb:.1f}",
        "source": str(source),
    }, indent=2)


@TOOLS.tool(tool_class=ToolClass.OFFLINE_WRITE, safety_hints=OFFLINE_WRITE)
def media_audio_to_midi(
    path: str,
    output_path: str = "",
) -> str:
    """Transcribe audio to MIDI using basic-pitch (ML audio transcription).

    Converts audio (from video or standalone) into MIDI note data.
    Returns the MIDI file path and a summary of detected notes.

    Requires basic-pitch: pip install basic-pitch

    Args:
        path: Path to audio file (wav, mp3, flac) or video file
        output_path: Where to save MIDI (default: same dir, .mid extension)
    """
    from .media.ffprobe import _find_ffmpeg, _run_checked

    source = _resolve_input(path)
    inference_source = source

    # If video, extract audio first
    video_exts = {'.mp4', '.mov', '.avi', '.mkv', '.webm'}
    if source.suffix.lower() in video_exts:
        wav_path = _resolve_output(
            str(source.with_suffix(".wav")),
            input_path=source,
            suffixes={".wav"},
        )
        _run_checked(
            [
                _find_ffmpeg(),
                "-y",
                "-i",
                str(source),
                "-vn",
                "-c:a",
                "pcm_s16le",
                str(wav_path),
            ],
            timeout=300,
            expected_outputs=[wav_path],
        )
        inference_source = wav_path

    try:
        from basic_pitch import ICASSP_2022_MODEL_PATH
        from basic_pitch.inference import predict

        _model_output, midi_data, note_events = predict(
            str(inference_source), model_or_model_path=ICASSP_2022_MODEL_PATH,
        )

        output = _resolve_output(
            output_path or str(inference_source.with_suffix(".mid")),
            input_path=source,
            suffixes={".mid", ".midi"},
        )

        midi_data.write(str(output))
        if not output.is_file() or output.stat().st_size == 0:
            raise FCPMCPError(
                ErrorCode.OUTPUT_MISSING,
                f"basic-pitch did not create a nonempty MIDI file: {output}",
            )

        # Summarize what was detected
        note_count = len(note_events)
        if note_events:
            pitches = [n[2] for n in note_events]
            min_pitch, max_pitch = min(pitches), max(pitches)
            total_dur = max(n[1] for n in note_events) - min(n[0] for n in note_events)
        else:
            min_pitch = max_pitch = 0
            total_dur = 0

        return json.dumps({
            "midi_file": str(output),
            "notes_detected": note_count,
            "pitch_range": f"MIDI {min_pitch}-{max_pitch}",
            "duration_seconds": f"{total_dur:.1f}",
            "source": str(inference_source),
        }, indent=2)

    except ImportError as error:
        raise FCPMCPError(
            ErrorCode.DEPENDENCY_MISSING,
            "basic-pitch not installed. Run: pip install basic-pitch",
        ) from error
    except FCPMCPError:
        raise
    except Exception as error:
        logger.exception("Audio transcription failed")
        raise FCPMCPError(
            ErrorCode.COMMAND_FAILED,
            "Audio transcription failed",
        ) from error


# ============================================================================
# Category 11: Puppet Animation (7 tools)
# ============================================================================

@TOOLS.tool(tool_class=ToolClass.STATEFUL_WRITE, safety_hints=STATEFUL_WRITE)
def puppet_create_rig(
    rig_json: str,
) -> str:
    """Define a puppet character rig from body parts.

    Each part is a separate image (PNG) that gets positioned and layered
    to form a character. Parts can be animated independently.

    Args:
        rig_json: JSON object defining the character:
            {
                "name": "my_character",
                "position": [0, 0],
                "parts": [
                    {
                        "name": "head",
                        "image": "/absolute/path/to/head.png",
                        "position": [0, 200],
                        "scale": 1.0,
                        "rotation": 0,
                        "anchor": [0, -50],
                        "z_order": 5
                    },
                    ...
                ]
            }
            - position: [x, y] offset from character center (y-positive = up)
            - anchor: pivot point for rotation
            - z_order: higher = in front

    Returns:
        JSON summary of the rig (use this to verify before building a scene).
    """
    data = _resolve_rig_images(_load_json_object(rig_json, "rig_json"))
    rig = rig_from_json(data)
    return json.dumps({
        "name": rig.name,
        "position": list(rig.position),
        "parts": [
            {
                "name": p.name,
                "image": p.image_path,
                "position": list(p.position),
                "scale": p.scale,
                "rotation": p.rotation,
                "anchor": list(p.anchor),
                "z_order": p.z_order,
            }
            for p in rig.parts
        ],
        "status": "rig_valid",
    }, indent=2)


@TOOLS.tool(tool_class=ToolClass.STATEFUL_WRITE, safety_hints=STATEFUL_WRITE)
def puppet_create_humanoid_rig(
    name: str,
    image_dir: str,
    position_x: float = 0.0,
    position_y: float = 0.0,
    scale: float = 1.0,
) -> str:
    """Create a standard 6-part humanoid rig from a folder of images.

    Expects PNG files named: head.png, body.png, left_arm.png, right_arm.png,
    left_leg.png, right_leg.png in the image directory.

    Parts are auto-positioned for a standard humanoid layout on a 1080p frame.

    Args:
        name: Character name
        image_dir: Absolute path to folder containing part images
        position_x: X position on screen (0 = center)
        position_y: Y position on screen (0 = center)
        scale: Overall scale multiplier
    """
    resolved_image_dir = _resolve_input(image_dir, kind="dir")
    rig = standard_humanoid_rig(
        name,
        str(resolved_image_dir),
        position=(position_x, position_y),
        scale=scale,
    )
    if not rig.parts:
        raise FCPMCPError(
            ErrorCode.TARGET_NOT_FOUND,
            (
                f"No humanoid part images found in {resolved_image_dir}; "
                "expected head.png, body.png, left_arm.png, right_arm.png, "
                "left_leg.png, or right_leg.png"
            ),
        )
    found = [p.name for p in rig.parts]
    missing = [n for n in ["head", "body", "left_arm", "right_arm", "left_leg", "right_leg"] if n not in found]
    result = {
        "name": rig.name,
        "position": list(rig.position),
        "parts_found": found,
        "parts_missing": missing,
        "status": "rig_valid",
    }
    return json.dumps(result, indent=2)


@TOOLS.tool(tool_class=ToolClass.STATEFUL_WRITE, safety_hints=STATEFUL_WRITE)
def puppet_build_scene(
    rigs_json: str,
    duration: str = "300300/30000s",
    project_name: str = "Puppet Animation",
    output_path: str = "",
) -> str:
    """Build an FCPXML timeline from one or more puppet rigs.

    Each rig's parts become layered connected clips with transforms applied.
    Import the resulting .fcpxml into FCP to see the assembled characters.

    Args:
        rigs_json: JSON array of rig definitions (same format as puppet_create_rig).
            Each rig: {"name": str, "position": [x, y], "parts": [...]}
        duration: Scene duration in FCPXML time (default 10 seconds at 29.97fps)
        project_name: Project name
        output_path: Where to save (default: ~/Movies/<project_name>.fcpxml)
    """
    _parse_time(duration, "duration")
    rigs_data = _load_rigs(rigs_json)

    builder = PuppetSceneBuilder(duration=duration)

    for rd in rigs_data:
        rig = rig_from_json(rd)
        builder.add_rig(rig)

    gen = builder.build(project_name=project_name)

    out = _save_generator(
        gen,
        output_path,
        default_name=f"{project_name}.fcpxml",
    )

    total_parts = sum(len(rig_from_json(rd).parts) for rd in rigs_data)
    return json.dumps({
        "file": str(out),
        "rigs": len(rigs_data),
        "total_parts": total_parts,
        "duration": duration,
        "status": "scene_built",
    }, indent=2)


@TOOLS.tool(tool_class=ToolClass.STATEFUL_WRITE, safety_hints=STATEFUL_WRITE)
def puppet_animate(
    rigs_json: str,
    animations_json: str,
    duration: str = "300300/30000s",
    project_name: str = "Puppet Animation",
    output_path: str = "",
) -> str:
    """Build a puppet scene with custom keyframe animations.

    Args:
        rigs_json: JSON array of rig definitions
        animations_json: JSON array of animations:
            [
                {
                    "part": "head",
                    "property": "position",
                    "keyframes": [
                        {"time": "0s", "value": [0, 200], "interp": "smooth2"},
                        {"time": "150150/30000s", "value": [20, 210], "interp": "smooth2"},
                        {"time": "300300/30000s", "value": [0, 200], "interp": "smooth2"}
                    ]
                },
                {
                    "part": "left_arm",
                    "property": "rotation",
                    "keyframes": [
                        {"time": "0s", "value": 0},
                        {"time": "150150/30000s", "value": 45},
                        {"time": "300300/30000s", "value": 0}
                    ]
                }
            ]
            property: "position" (value=[x,y]), "rotation" (value=degrees), "scale" (value=[sx,sy])
            interp: "smooth2" (default, ease), "linear", "hold"
        duration: Scene duration
        project_name: Project name
        output_path: Where to save
    """
    _parse_time(duration, "duration")
    rigs_data = _load_rigs(rigs_json)
    anims_data = _load_json_list(animations_json, "animations_json")
    if not anims_data:
        raise FCPMCPError(
            ErrorCode.INVALID_ARGUMENTS,
            "animations_json must contain at least one animation",
        )

    builder = PuppetSceneBuilder(duration=duration)

    for rd in rigs_data:
        rig = rig_from_json(rd)
        builder.add_rig(rig)
    part_names = {
        part["name"]
        for rig_data in rigs_data
        for part in rig_data["parts"]
    }

    # Parse animations
    for index, ad in enumerate(anims_data):
        if (
            not isinstance(ad, dict)
            or not isinstance(ad.get("part"), str)
            or ad.get("property") not in {"position", "rotation", "scale"}
            or not isinstance(ad.get("keyframes"), list)
            or not ad["keyframes"]
        ):
            raise FCPMCPError(
                ErrorCode.INVALID_ARGUMENTS,
                (
                    f"animations_json[{index}] must contain part, a supported "
                    "property, and a keyframes array"
                ),
            )
        if ad["part"] not in part_names:
            raise FCPMCPError(
                ErrorCode.TARGET_NOT_FOUND,
                f"Animation part '{ad['part']}' not found in any rig",
            )
        keyframes = []
        for keyframe_index, kf in enumerate(ad["keyframes"]):
            if (
                not isinstance(kf, dict)
                or "time" not in kf
                or "value" not in kf
            ):
                raise FCPMCPError(
                    ErrorCode.INVALID_ARGUMENTS,
                    (
                        f"animations_json[{index}].keyframes"
                        f"[{keyframe_index}] must contain time and value"
                    ),
                )
            _parse_time(
                str(kf["time"]),
                (
                    f"animations_json[{index}].keyframes"
                    f"[{keyframe_index}].time"
                ),
            )
            val = kf["value"]
            if ad["property"] == "rotation":
                if not isinstance(val, (int, float)) or isinstance(val, bool):
                    raise FCPMCPError(
                        ErrorCode.INVALID_ARGUMENTS,
                        (
                            f"animations_json[{index}].keyframes"
                            f"[{keyframe_index}].value must be numeric"
                        ),
                    )
            elif not (
                isinstance(val, list)
                and len(val) == 2
                and all(
                    isinstance(item, (int, float))
                    and not isinstance(item, bool)
                    for item in val
                )
            ):
                raise FCPMCPError(
                    ErrorCode.INVALID_ARGUMENTS,
                    (
                        f"animations_json[{index}].keyframes"
                        f"[{keyframe_index}].value must be a two-number array"
                    ),
                )
            if kf.get("interp", "smooth2") not in {"smooth2", "linear", "hold"}:
                raise FCPMCPError(
                    ErrorCode.INVALID_ARGUMENTS,
                    (
                        f"animations_json[{index}].keyframes"
                        f"[{keyframe_index}].interp is unsupported"
                    ),
                )
            if isinstance(val, list):
                val = tuple(val)
            keyframes.append(Keyframe(
                time=kf["time"],
                value=val,
                interp=kf.get("interp", "smooth2"),
            ))
        builder.add_animation(PartAnimation(
            part_name=ad["part"],
            property_name=ad["property"],
            keyframes=keyframes,
        ))

    gen = builder.build(project_name=project_name)

    out = _save_generator(
        gen,
        output_path,
        default_name=f"{project_name}.fcpxml",
    )

    return json.dumps({
        "file": str(out),
        "rigs": len(rigs_data),
        "animations": len(anims_data),
        "duration": duration,
        "status": "animated_scene_built",
    }, indent=2)


@TOOLS.tool(tool_class=ToolClass.STATEFUL_WRITE, safety_hints=STATEFUL_WRITE)
def puppet_preset_motion(
    rig_json: str,
    preset: str = "idle",
    duration: str = "300300/30000s",
    project_name: str = "Puppet Animation",
    output_path: str = "",
    cycles: int = 3,
    intensity: float = 1.0,
) -> str:
    """Build a puppet scene with a preset motion applied.

    Available presets:
    - "idle": Subtle breathing/sway (keeps character alive)
    - "bounce": Vertical bouncing
    - "walk": Full walk cycle (arms, legs, body bob)
    - "talk": Mouth movement + head bob
    - "wave": Arm waving (applies to left_arm)

    Args:
        rig_json: JSON rig definition
        preset: Motion preset name
        duration: Scene duration
        project_name: Project name
        output_path: Where to save
        cycles: Number of motion cycles (more = faster movement)
        intensity: Scale factor for motion amplitude (0.5 = subtle, 2.0 = exaggerated)
    """
    _parse_time(duration, "duration")
    if cycles <= 0 or intensity <= 0:
        raise FCPMCPError(
            ErrorCode.INVALID_ARGUMENTS,
            "cycles and intensity must be greater than zero",
        )
    rig_data = _resolve_rig_images(
        _load_json_object(rig_json, "rig_json")
    )
    rig = rig_from_json(rig_data)

    builder = PuppetSceneBuilder(duration=duration)
    builder.add_rig(rig)

    # Generate preset animations
    if preset == "idle":
        anims = preset_idle(rig, duration, breathe_amount=8.0 * intensity, sway_amount=3.0 * intensity)
    elif preset == "bounce":
        # Apply bounce to body or first part
        target = rig.get_part("body") or rig.get_part("torso") or (rig.parts[0] if rig.parts else None)
        anims = [preset_bounce(target, duration, amplitude=20.0 * intensity, cycles=cycles)] if target else []
    elif preset == "walk":
        anims = preset_walk(
            rig, duration,
            stride=100.0 * intensity,
            bob_height=15.0 * intensity,
            arm_swing=30.0 * intensity,
            leg_swing=35.0 * intensity,
            cycles=cycles,
        )
    elif preset == "talk":
        anims = preset_talk(rig, duration, jaw_range=15.0 * intensity, head_bob=5.0 * intensity)
    elif preset == "wave":
        arm = rig.get_part("left_arm") or rig.get_part("right_arm")
        anims = [preset_wave(arm, duration, angle_range=45.0 * intensity, cycles=cycles)] if arm else []
    else:
        raise FCPMCPError(
            ErrorCode.INVALID_ARGUMENTS,
            (
                f"Unknown preset: {preset}. "
                "Available: idle, bounce, walk, talk, wave"
            ),
        )

    if not anims:
        raise FCPMCPError(
            ErrorCode.TARGET_NOT_FOUND,
            f"Preset '{preset}' has no compatible parts in rig '{rig.name}'",
        )
    for anim in anims:
        builder.add_animation(anim)

    gen = builder.build(project_name=project_name)

    out = _save_generator(
        gen,
        output_path,
        default_name=f"{project_name}.fcpxml",
    )

    return json.dumps({
        "file": str(out),
        "preset": preset,
        "animations_applied": len(anims),
        "parts_animated": [a.part_name for a in anims],
        "duration": duration,
        "status": "preset_applied",
    }, indent=2)


@TOOLS.tool(tool_class=ToolClass.STATEFUL_WRITE, safety_hints=STATEFUL_WRITE)
def puppet_multi_scene(
    rigs_json: str,
    scenes_json: str,
    project_name: str = "Puppet Multi-Scene",
    output_path: str = "",
) -> str:
    """Build a multi-scene puppet animation with different presets per scene.

    Generates one FCPXML per scene. Useful for storyboarding a sequence.

    Args:
        rigs_json: JSON array of rig definitions (shared across scenes)
        scenes_json: JSON array of scene definitions:
            [
                {"name": "intro", "duration": "300300/30000s", "preset": "idle"},
                {"name": "greeting", "duration": "150150/30000s", "preset": "wave"},
                {"name": "walking", "duration": "600600/30000s", "preset": "walk", "cycles": 4}
            ]
        project_name: Base project name (scenes get suffixed)
        output_path: Output directory (default: ~/Movies/)
    """
    rigs_data = _load_rigs(rigs_json)
    scenes_data = _load_json_list(scenes_json, "scenes_json")
    if not scenes_data:
        raise FCPMCPError(
            ErrorCode.INVALID_ARGUMENTS,
            "scenes_json must contain at least one scene",
        )
    supported_presets = {"idle", "walk", "talk", "wave", "bounce"}
    for index, scene in enumerate(scenes_data):
        if not isinstance(scene, dict):
            raise FCPMCPError(
                ErrorCode.INVALID_ARGUMENTS,
                f"scenes_json[{index}] must be an object",
            )
        _parse_time(
            str(scene.get("duration", "300300/30000s")),
            f"scenes_json[{index}].duration",
        )
        preset = scene.get("preset", "idle")
        if not isinstance(preset, str) or preset not in supported_presets:
            raise FCPMCPError(
                ErrorCode.INVALID_ARGUMENTS,
                f"Unknown scene preset: {preset}",
            )

    out_dir = _resolve_output(output_path or str(CONFIG.output_dir))
    if not out_dir.is_dir():
        raise FCPMCPError(
            ErrorCode.INVALID_PATH,
            f"Expected an existing output directory: {out_dir}",
        )
    results = []

    for i, scene in enumerate(scenes_data):
        scene_name = scene.get("name", f"scene_{i+1}")
        scene_duration = scene.get("duration", "300300/30000s")
        scene_preset = scene.get("preset", "idle")
        scene_cycles = scene.get("cycles", 3)
        scene_intensity = scene.get("intensity", 1.0)

        builder = PuppetSceneBuilder(duration=scene_duration)

        for rd in rigs_data:
            rig = rig_from_json(rd)
            builder.add_rig(rig)

            # Apply preset
            if scene_preset == "idle":
                anims = preset_idle(rig, scene_duration, breathe_amount=8.0 * scene_intensity)
            elif scene_preset == "walk":
                anims = preset_walk(rig, scene_duration, cycles=scene_cycles)
            elif scene_preset == "talk":
                anims = preset_talk(rig, scene_duration)
            elif scene_preset == "wave":
                arm = rig.get_part("left_arm") or rig.get_part("right_arm")
                anims = [preset_wave(arm, scene_duration, cycles=scene_cycles)] if arm else []
            elif scene_preset == "bounce":
                target = rig.get_part("body") or rig.get_part("torso")
                anims = [preset_bounce(target, scene_duration, cycles=scene_cycles)] if target else []
            else:
                anims = []

            for anim in anims:
                builder.add_animation(anim)

        full_name = f"{project_name}_{scene_name}"
        gen = builder.build(project_name=full_name)
        out = _save_generator(
            gen,
            str(out_dir / f"{full_name}.fcpxml"),
            default_name=f"{full_name}.fcpxml",
        )
        results.append({"scene": scene_name, "file": str(out), "preset": scene_preset})

    return json.dumps({
        "scenes_created": len(results),
        "files": results,
        "status": "multi_scene_built",
    }, indent=2)


@TOOLS.tool(
    tool_class=ToolClass.INSPECT,
    safety_hints=OFFLINE_READ,
    result_model=PuppetPresetListResult,
)
def puppet_list_presets() -> ToolOutcome[PuppetPresetListResult]:
    """List all available puppet animation presets with descriptions.

    Returns details on each preset, what body parts it uses,
    and what parameters it accepts.
    """
    presets = {
        "idle": {
            "description": "Subtle breathing and sway — keeps the character looking alive",
            "parts_used": ["body/torso", "head"],
            "parameters": {"breathe_amount": 8.0, "sway_amount": 3.0},
            "good_for": "Background characters, dialogue scenes, any time a character is standing still",
        },
        "bounce": {
            "description": "Vertical bouncing motion on a single part",
            "parts_used": ["body/torso (or first available part)"],
            "parameters": {"amplitude": 20.0, "cycles": 3},
            "good_for": "Excited characters, reactions, comedic emphasis",
        },
        "walk": {
            "description": "Full walk cycle with arm swing, leg swing, and body bob",
            "parts_used": ["body/torso", "head", "left_arm", "right_arm", "left_leg", "right_leg"],
            "parameters": {"stride": 100.0, "bob_height": 15.0, "arm_swing": 30.0, "leg_swing": 35.0, "cycles": 3},
            "good_for": "Characters walking in place (combine with position keyframes for actual movement)",
        },
        "talk": {
            "description": "Talking animation with mouth movement and subtle head bob",
            "parts_used": ["mouth/jaw", "head"],
            "parameters": {"jaw_range": 15.0, "head_bob": 5.0, "tempo": 4.0},
            "good_for": "Dialogue scenes, narration, any speaking character",
        },
        "wave": {
            "description": "Arm waving rotation",
            "parts_used": ["left_arm (or right_arm)"],
            "parameters": {"angle_range": 45.0, "cycles": 2},
            "good_for": "Greetings, goodbyes, getting attention",
        },
    }
    return ToolOutcome(
        text=json.dumps(presets, indent=2),
        structured=PuppetPresetListResult(
            presets=[
                PuppetPresetRecord(
                    name=name,
                    description=details["description"],
                    parts=details["parts_used"],
                    parameters=[
                        PuppetPresetParameterRecord(
                            name=parameter,
                            default=default,
                        )
                        for parameter, default in details["parameters"].items()
                    ],
                    usage=details["good_for"],
                )
                for name, details in presets.items()
            ],
        ),
    )


# ============================================================================
# MCP Prompts (5) — pre-baked flows that wrap the most common tool sequences
# ============================================================================


def _tool_call_block(name: str, arguments: dict[str, Any]) -> str:
    """Render one machine-valid MCP call for prompt consumers."""
    payload = json.dumps(
        {"name": name, "arguments": arguments},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"```tool-call\n{payload}\n```"


@PROMPTS.prompt(
    name="qc-check",
    description="Run the structural FCPXML QC report and interpret its Markdown results.",
    dependencies={"fcpxml_qc_report"},
)
def prompt_qc_check(path: str) -> str:
    """Run qc report, then explain what needs attention."""
    qc_call = _tool_call_block("fcpxml_qc_report", {"path": path})
    return (
        f"Run the structural QC report on {path!r}:\n\n"
        f"{qc_call}\n\n"
        "The tool returns Markdown covering validation, timeline statistics, gaps, "
        "flash frames, duplicate sources, and pacing. For every reported section, tell me:\n"
        "  1. How many items were flagged.\n"
        "  2. Whether a registered tool can address it, without claiming that the "
        "aggregate report checked media links, frame rates, audio levels, or safe zones.\n"
        "  3. Whether it requires manual intervention in Final Cut Pro.\n\n"
        "End with a short prioritized checklist of the next 3 actions."
    )


@PROMPTS.prompt(
    name="rough-cut",
    description="Assemble a rough cut from a list of clips, optionally target a duration.",
    dependencies={"fcpxml_auto_rough_cut", "fcpxml_qc_report"},
)
def prompt_rough_cut(clips_json: str, target_duration: str = "") -> str:
    """Auto-assemble a rough cut, then QC it."""
    target_clause = (
        f" targeting a total duration of {target_duration}"
        if target_duration
        else " using all clips"
    )
    rough_cut_call = _tool_call_block(
        "fcpxml_auto_rough_cut",
        {
            "clips_json": clips_json,
            "target_duration": target_duration,
        },
    )
    qc_call = _tool_call_block(
        "fcpxml_qc_report",
        {"path": "<path returned by fcpxml_auto_rough_cut>"},
    )
    return (
        f"Assemble a rough cut from the clips in:\n\n{clips_json}\n\n"
        f"Build the timeline{target_clause}:\n\n"
        f"{rough_cut_call}\n\n"
        "Then substitute the returned output path in this call:\n\n"
        f"{qc_call}\n\n"
        "Surface structural validation, flash-frame, gap, duplicate-source, and pacing "
        "results. Return the final output path and a one-line assembly summary. Do not "
        "run gap filling until an existing fill asset resource ID has been selected."
    )


@PROMPTS.prompt(
    name="cleanup",
    description="Extend flash frames, replace gaps with a chosen asset, then re-run QC.",
    dependencies={
        "fcpxml_fix_flash_frames",
        "fcpxml_fill_gaps",
        "fcpxml_qc_report",
    },
)
def prompt_cleanup(
    path: str,
    fill_asset_ref: str,
    output_path: str = "",
) -> str:
    """Fix flash frames, fill gaps with a selected asset, and re-run QC."""
    out_clause = (
        f" Save the cleaned file to {output_path!r}."
        if output_path
        else " Use each tool's returned output path for the next step."
    )
    next_path = output_path or "<path returned by fcpxml_fix_flash_frames>"
    fix_call = _tool_call_block(
        "fcpxml_fix_flash_frames",
        {"path": path, "output_path": output_path},
    )
    fill_call = _tool_call_block(
        "fcpxml_fill_gaps",
        {
            "path": next_path,
            "fill_asset_ref": fill_asset_ref,
            "output_path": output_path,
        },
    )
    qc_call = _tool_call_block(
        "fcpxml_qc_report",
        {"path": next_path},
    )
    return (
        f"Clean up the FCPXML at {path!r}.{out_clause}\n\n"
        "1. Extend clips shorter than the configured flash-frame minimum:\n\n"
        f"{fix_call}\n\n"
        f"2. Replace gap elements with existing asset resource {fill_asset_ref!r}:\n\n"
        f"{fill_call}\n\n"
        "3. Use the actual output path from step 2 in this structural QC call:\n\n"
        f"{qc_call}\n\n"
        "Report before/after flash-frame and gap counts plus remaining QC items. "
        "Do not call these changes non-destructive: they alter clip durations and "
        "replace gaps with the selected media asset."
    )


@PROMPTS.prompt(
    name="youtube-chapters",
    description="Extract FCPXML markers and emit a YouTube chapter timestamp blob.",
    dependencies={"fcpxml_list_markers"},
)
def prompt_youtube_chapters(path: str) -> str:
    """Convert FCPXML markers to a YouTube description chapter blob."""
    marker_call = _tool_call_block(
        "fcpxml_list_markers",
        {"path": path},
    )
    return (
        f"Turn the markers in {path!r} into a YouTube chapter list.\n\n"
        "Get every marker with its start time:\n\n"
        f"{marker_call}\n\n"
        "Then:\n"
        "  2. Sort by start time ascending. Drop markers before 00:00.\n"
        "  3. Format each as `HH:MM:SS Title` (or `MM:SS Title` if the video is under an "
        "hour). The first entry MUST start at `00:00` — if the earliest marker is later, "
        "prepend a `00:00 Intro` line.\n"
        "  4. Return the blob as a single code block ready to paste into a YouTube "
        "description. Below it, note how many markers were used and flag any that looked "
        "like internal notes (e.g., TODO/FIXME) that were skipped."
    )


@PROMPTS.prompt(
    name="beat-sync",
    description="Detect beats and use their median cadence for an approximate rough cut.",
    dependencies={"media_detect_beats", "fcpxml_auto_rough_cut"},
)
def prompt_beat_sync(audio_path: str, clips_json: str, project_name: str = "Beat Sync") -> str:
    """Detect beats and use their cadence to constrain rough-cut clip length."""
    beat_call = _tool_call_block(
        "media_detect_beats",
        {"path": audio_path},
    )
    rough_cut_call = _tool_call_block(
        "fcpxml_auto_rough_cut",
        {
            "clips_json": clips_json,
            "max_clip_duration": "<median inter-beat interval as FCPXML time>",
            "project_name": project_name,
        },
    )
    marker_call = _tool_call_block(
        "fcpxml_batch_add_markers",
        {
            "path": "<path returned by fcpxml_auto_rough_cut>",
            "markers_json": (
                '[{"clip_name":"<resolved clip name>","start":"0s",'
                '"value":"Beat 1"}]'
            ),
        },
    )
    qc_call = _tool_call_block(
        "fcpxml_qc_report",
        {"path": "<path returned by fcpxml_batch_add_markers>"},
    )
    return (
        f"Build a beat-informed cut using audio {audio_path!r} and clips:\n\n"
        f"{clips_json}\n\n"
        "1. Detect beat timestamps:\n\n"
        f"{beat_call}\n\n"
        "2. Compute the median positive interval between beats and serialize it as "
        "FCPXML rational time, then substitute it here:\n\n"
        f"{rough_cut_call}\n\n"
        "3. Resolve real clip names in the generated timeline and build one marker "
        "entry per beat before calling:\n\n"
        f"{marker_call}\n\n"
        "4. Substitute the marker tool's returned path and run:\n\n"
        f"{qc_call}\n\n"
        "Report the final path and beat count. v0.2.1 cannot pass individual cut points "
        "to the rough-cut tool, so describe this as cadence approximation, not exact "
        "beat-synced editing."
    )


# ============================================================================
# Entry point
# ============================================================================

mcp = build_mcp_server(CONFIG, TOOLS, PROMPTS)


def main():
    """Run the MCP server."""
    mcp.run()


if __name__ == "__main__":
    main()
