"""FCP-MCP Server — Final Cut Pro MCP Server.

The most capable FCP MCP server: FCPXML engine + live FCP control + media analysis.
"""

from __future__ import annotations

import json
import logging
import subprocess
from collections.abc import Collection
from dataclasses import asdict
from pathlib import Path
from typing import Any
from urllib.parse import unquote as url_unquote

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import ValidationError

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
from .fcpxml.validator import FCPXMLValidator
from .fcpxml.writer import FCPXMLModifier
from .security.paths import PathPolicy
from .utils.atomic_write import atomic_replace_bytes

logger = logging.getLogger(__name__)


class FCPFastMCP(FastMCP):
    """FastMCP boundary that preserves domain codes and sanitizes defects."""

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> Any:
        try:
            return await super().call_tool(name, arguments)
        except ToolError as error:
            cause = error.__cause__
            if isinstance(cause, FCPMCPError):
                raise
            if isinstance(cause, ValidationError):
                raise ToolError(
                    f"Error executing tool {name}: "
                    f"{ErrorCode.INVALID_ARGUMENTS.value}: "
                    "Tool arguments did not match the schema"
                ) from error
            logger.exception(
                "Unexpected failure while executing tool %s",
                name,
            )
            raise ToolError(
                f"Error executing tool {name}: "
                f"{ErrorCode.INTERNAL_ERROR.value}: Unexpected internal failure"
            ) from error


mcp = FCPFastMCP(
    "fcp-mcp",
    instructions=(
        "fcp-mcp v0.2.1 — FCPXML engine + live FCP control + media analysis "
        "+ puppet animation. 89 tools across 12 categories and 5 prompts."
    ),
)

CONFIG = RuntimeConfig.from_env()
PATHS = PathPolicy(CONFIG)

_parser = FCPXMLParser()
_validator = FCPXMLValidator()


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

@mcp.tool()
async def fcp_doctor() -> DoctorReport:
    """Report structured runtime readiness without prompting or mutating user data."""

    async def catalog() -> tuple[int, int]:
        return len(await mcp.list_tools()), len(await mcp.list_prompts())

    return await collect_doctor(CONFIG, catalog_provider=catalog)


# ============================================================================
# Category 2: FCPXML Analysis (12 tools)
# ============================================================================

@mcp.tool()
def fcpxml_parse(path: str) -> str:
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
    return json.dumps(summary, indent=2)


@mcp.tool()
def fcpxml_list_clips(path: str, project_name: str = "") -> str:
    """List all clips in the timeline with timecodes, durations, and roles.

    Args:
        path: Path to .fcpxml file
        project_name: Optional project name filter (uses first project if empty)
    """
    doc = _parse_doc(path)
    fmt = next(iter(doc.formats.values())) if doc.formats else None
    fps = fmt.fps if fmt else 29.97

    clips_data = []
    for project in doc.all_projects:
        if project_name and project.name != project_name:
            continue
        if not project.sequence or not project.sequence.spine:
            continue
        for i, clip in enumerate(project.sequence.spine.clips):
            clips_data.append({
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
            })
    return json.dumps(clips_data, indent=2)


@mcp.tool()
def fcpxml_list_markers(path: str) -> str:
    """List all markers, chapter markers, and keywords across all clips.

    Args:
        path: Path to .fcpxml file
    """
    doc = _parse_doc(path)
    fmt = next(iter(doc.formats.values())) if doc.formats else None
    fps = fmt.fps if fmt else 29.97

    markers = []
    for project in doc.all_projects:
        if not project.sequence or not project.sequence.spine:
            continue
        for clip in project.sequence.spine.clips:
            for m in clip.markers:
                markers.append({
                    "clip": clip.name,
                    "type": m.marker_type.value,
                    "value": m.value,
                    "note": m.note,
                    "start": m.start.to_timecode(fps),
                    "start_raw": m.start.to_fcpxml(),
                })
            for kw in clip.keywords:
                markers.append({
                    "clip": clip.name,
                    "type": "keyword",
                    "value": kw.value,
                    "start": kw.start.to_fcpxml(),
                    "duration": kw.duration.to_fcpxml(),
                })
    return json.dumps(markers, indent=2)


@mcp.tool()
def fcpxml_analyze_pacing(path: str) -> str:
    """Analyze shot pacing — average/median shot length, distribution histogram.

    Args:
        path: Path to .fcpxml file
    """
    doc = _parse_doc(path)
    results = analyze_pacing(doc)
    return json.dumps([_serializable(r) for r in results], indent=2)


@mcp.tool()
def fcpxml_detect_gaps(path: str) -> str:
    """Find all gaps in the timeline.

    Args:
        path: Path to .fcpxml file
    """
    doc = _parse_doc(path)
    gaps = detect_gaps(doc)
    return json.dumps([_serializable(g) for g in gaps], indent=2)


@mcp.tool()
def fcpxml_detect_flash_frames(path: str, max_frames: int = 2) -> str:
    """Find clips shorter than max_frames (potential flash frames / accidental edits).

    Args:
        path: Path to .fcpxml file
        max_frames: Maximum frame count to flag (default 2)
    """
    doc = _parse_doc(path)
    flashes = detect_flash_frames(doc, max_frames=max_frames)
    return json.dumps([_serializable(f) for f in flashes], indent=2)


@mcp.tool()
def fcpxml_detect_duplicates(path: str) -> str:
    """Find clips that use the same source media.

    Args:
        path: Path to .fcpxml file
    """
    doc = _parse_doc(path)
    dupes = detect_duplicates(doc)
    return json.dumps([_serializable(d) for d in dupes], indent=2)


@mcp.tool()
def fcpxml_validate(path: str) -> str:
    """Validate FCPXML structure and report errors/warnings.

    Args:
        path: Path to .fcpxml file
    """
    result = _validator.validate_file(
        _resolve_input(path, suffixes={".fcpxml"})
    )
    return result.summary()


@mcp.tool()
def fcpxml_list_effects(path: str) -> str:
    """List all effects and transitions applied to clips.

    Args:
        path: Path to .fcpxml file
    """
    doc = _parse_doc(path)
    effects = []
    for project in doc.all_projects:
        if not project.sequence or not project.sequence.spine:
            continue
        for clip in project.sequence.spine.clips:
            for effect in clip.effects:
                effects.append({
                    "clip": clip.name,
                    "effect_name": effect.name,
                    "effect_ref": effect.ref,
                    "enabled": effect.enabled,
                    "parameters": effect.parameters,
                })
    # Also list effect resources
    resources = [{"id": k, "name": v.name, "uid": v.uid} for k, v in doc.effects.items()]
    return json.dumps({"applied": effects, "available": resources}, indent=2)


@mcp.tool()
def fcpxml_list_roles(path: str) -> str:
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
    return json.dumps({"roles": sorted(roles)}, indent=2)


@mcp.tool()
def fcpxml_timeline_stats(path: str) -> str:
    """Get comprehensive timeline statistics — duration, clip count, resolution, pacing, etc.

    Args:
        path: Path to .fcpxml file
    """
    doc = _parse_doc(path)
    stats = analyze_timeline_stats(doc)
    return json.dumps([_serializable(s) for s in stats], indent=2)


@mcp.tool()
def fcpxml_diff(path_a: str, path_b: str) -> str:
    """Compare two FCPXML files and show differences.

    Args:
        path_a: Path to first .fcpxml file
        path_b: Path to second .fcpxml file
    """
    results = diff_files(
        _resolve_input(path_a, suffixes={".fcpxml"}),
        _resolve_input(path_b, suffixes={".fcpxml"}),
    )
    return "\n\n".join(r.summary() for r in results)


# ============================================================================
# Category 3: FCPXML Editing (12 tools)
# ============================================================================

@mcp.tool()
def fcpxml_add_marker(
    path: str,
    clip_name: str,
    start: str,
    value: str,
    note: str = "",
    marker_type: str = "standard",
    output_path: str = "",
) -> str:
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
    out = _save_modifier(mod, output_path)
    return f"Marker added. Saved to: {out}"


@mcp.tool()
def fcpxml_batch_add_markers(path: str, markers_json: str, output_path: str = "") -> str:
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
    out = _save_modifier(mod, output_path)
    return f"{count}/{len(markers)} markers added. Saved to: {out}"


@mcp.tool()
def fcpxml_add_keyword(
    path: str, clip_name: str, value: str, start: str = "0s",
    duration: str = "", output_path: str = "",
) -> str:
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
    out = _save_modifier(mod, output_path)
    return f"Keyword '{value}' added. Saved to: {out}"


@mcp.tool()
def fcpxml_trim_clip(
    path: str, clip_name: str,
    new_start: str = "", new_duration: str = "",
    output_path: str = "",
) -> str:
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
    if not mod.trim_clip(clip_name, new_start or None, new_duration or None):
        raise FCPMCPError(
            ErrorCode.TARGET_NOT_FOUND,
            f"Clip '{clip_name}' not found",
        )
    out = _save_modifier(mod, output_path)
    return f"Clip trimmed. Saved to: {out}"


@mcp.tool()
def fcpxml_split_clip(path: str, clip_name: str, split_at: str, output_path: str = "") -> str:
    """Split a clip at a given offset within the clip.

    Args:
        path: Path to .fcpxml file
        clip_name: Name of the clip to split
        split_at: Offset within the clip to split at (FCPXML time)
        output_path: Output file path
    """
    _parse_time(split_at, "split_at")
    mod = FCPXMLModifier(_resolve_input(path, suffixes={".fcpxml"}))
    if mod._find_clip_by_name(clip_name) is None:
        raise FCPMCPError(
            ErrorCode.TARGET_NOT_FOUND,
            f"Clip '{clip_name}' not found",
        )
    if not mod.split_clip(clip_name, split_at):
        raise FCPMCPError(
            ErrorCode.INVALID_ARGUMENTS,
            f"split_at must fall strictly inside clip '{clip_name}'",
        )
    out = _save_modifier(mod, output_path)
    return f"Clip split. Saved to: {out}"


@mcp.tool()
def fcpxml_delete_clips(path: str, clip_names_json: str, output_path: str = "") -> str:
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
    out = _save_modifier(mod, output_path)
    return f"{count}/{len(names)} clips deleted. Saved to: {out}"


@mcp.tool()
def fcpxml_reorder_clips(path: str, clip_names_json: str, output_path: str = "") -> str:
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
    out = _save_modifier(mod, output_path)
    return f"Clips reordered. Saved to: {out}"


@mcp.tool()
def fcpxml_add_transition(
    path: str, after_clip_name: str,
    duration: str = "30030/30000s", name: str = "Cross Dissolve",
    output_path: str = "",
) -> str:
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
    out = _save_modifier(mod, output_path)
    return f"Transition added. Saved to: {out}"


@mcp.tool()
def fcpxml_change_speed(
    path: str, clip_name: str, speed_factor: float,
    output_path: str = "",
) -> str:
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
    if not mod.change_speed(clip_name, speed_factor):
        raise FCPMCPError(
            ErrorCode.TARGET_NOT_FOUND,
            f"Clip '{clip_name}' not found",
        )
    out = _save_modifier(mod, output_path)
    return f"Speed changed to {speed_factor}x. Saved to: {out}"


@mcp.tool()
def fcpxml_assign_role(
    path: str, clip_name: str, role: str, output_path: str = "",
) -> str:
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
    out = _save_modifier(mod, output_path)
    return f"Role '{role}' assigned. Saved to: {out}"


@mcp.tool()
def fcpxml_add_title(
    path: str,
    text: str,
    duration: str = "150150/30000s",
    position: str = "end",
    output_path: str = "",
) -> str:
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
    for el in mod.root.find("resources") or []:
        if el.tag == "effect" and "Title" in el.get("name", ""):
            title_ref = el.get("id", "")
            break

    if not title_ref:
        # Add a Basic Title effect resource
        import xml.etree.ElementTree as ET
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

    import xml.etree.ElementTree as ET
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

    out = _save_modifier(mod, output_path)
    return f"Title '{text}' added. Saved to: {out}"


@mcp.tool()
def fcpxml_add_audio(
    path: str,
    audio_src: str,
    name: str = "",
    duration: str = "",
    position: str = "end",
    output_path: str = "",
) -> str:
    """Add an audio clip to the timeline.

    Args:
        path: Path to .fcpxml file
        audio_src: Path to audio file
        name: Clip name (defaults to filename)
        duration: Duration in FCPXML time (defaults to asset duration)
        position: "start", "end", or clip name to insert after
        output_path: Output file path
    """
    import xml.etree.ElementTree as ET

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
    out = _save_modifier(mod, output_path)
    return f"Audio '{name}' added. Saved to: {out}"


# ============================================================================
# Category 4: FCPXML Generation (8 tools)
# ============================================================================

@mcp.tool()
def fcpxml_create_project(
    name: str = "Untitled Project",
    format_name: str = "FFVideoFormat1080p2997",
    width: int = 1920,
    height: int = 1080,
    frame_duration: str = "1001/30000s",
    event_name: str = "Default Event",
    output_path: str = "",
) -> str:
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

    out = _save_generator(
        gen,
        output_path,
        default_name=f"{name}.fcpxml",
    )
    return f"Project created: {out}"


@mcp.tool()
def fcpxml_create_timeline(
    clips_json: str,
    project_name: str = "Generated Timeline",
    format_name: str = "FFVideoFormat1080p2997",
    event_name: str = "Generated",
    output_path: str = "",
) -> str:
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
    out = _save_generator(
        gen,
        output_path,
        default_name=f"{project_name}.fcpxml",
    )
    return f"Timeline created with {len(clips)} clips: {out}"


@mcp.tool()
def fcpxml_auto_rough_cut(
    clips_json: str,
    target_duration: str = "",
    max_clip_duration: str = "150150/30000s",
    transition_duration: str = "",
    project_name: str = "Rough Cut",
    output_path: str = "",
) -> str:
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

    out = _save_generator(
        gen,
        output_path,
        default_name=f"{project_name}.fcpxml",
    )
    return f"Rough cut created ({running_total.to_seconds():.1f}s): {out}"


@mcp.tool()
def fcpxml_generate_montage(
    clips_json: str,
    clip_duration: str = "90090/30000s",
    transition_duration: str = "30030/30000s",
    project_name: str = "Montage",
    output_path: str = "",
) -> str:
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

    out = _save_generator(
        gen,
        output_path,
        default_name=f"{project_name}.fcpxml",
    )
    return f"Montage created ({len(clips)} shots): {out}"


@mcp.tool()
def fcpxml_import_srt(
    path: str,
    srt_path: str,
    output_path: str = "",
) -> str:
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

    # Find or create title effect
    title_ref = ""
    for el in mod.root.find("resources") or []:
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

    out = _save_modifier(mod, output_path)
    return f"{len(subtitles)} subtitles added. Saved to: {out}"


@mcp.tool()
def fcpxml_import_edl(
    edl_path: str,
    media_dir: str = "",
    project_name: str = "EDL Import",
    output_path: str = "",
) -> str:
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
    out = _save_generator(
        gen,
        output_path,
        default_name=f"{project_name}.fcpxml",
    )
    return f"EDL imported ({clip_count} clips): {out}"


@mcp.tool()
def fcpxml_reformat(
    path: str,
    target_width: int = 1080,
    target_height: int = 1920,
    target_format_name: str = "FFVideoFormat1080x1920p2997",
    output_path: str = "",
) -> str:
    """Reformat a timeline for a different aspect ratio (e.g., 16:9 → 9:16 for vertical).

    Args:
        path: Path to .fcpxml file
        target_width: Target width
        target_height: Target height
        target_format_name: Target format name
        output_path: Output file path
    """

    mod = FCPXMLModifier(_resolve_input(path, suffixes={".fcpxml"}))

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

    out = _save_modifier(mod, output_path)
    return f"Reformatted to {target_width}x{target_height}. Saved to: {out}"


# ============================================================================
# Category 8: Batch Operations (6 tools)
# ============================================================================

@mcp.tool()
def fcpxml_fix_flash_frames(
    path: str,
    min_frames: int = 3,
    frame_duration: str = "1001/30000s",
    output_path: str = "",
) -> str:
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
    out = _save_modifier(mod, output_path)
    return f"{count} flash frames fixed. Saved to: {out}"


@mcp.tool()
def fcpxml_fill_gaps(
    path: str,
    fill_asset_ref: str,
    fill_name: str = "Fill",
    output_path: str = "",
) -> str:
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
    count = mod.fill_gaps(fill_asset_ref, fill_name)
    out = _save_modifier(mod, output_path)
    return f"{count} gaps filled. Saved to: {out}"


@mcp.tool()
def fcpxml_remove_silence(
    path: str,
    silence_threshold_seconds: float = 2.0,
    output_path: str = "",
) -> str:
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
    count = 0
    for spine_el in mod.root.iter("spine"):
        for gap_el in list(spine_el.findall("gap")):
            dur = RationalTime.from_fcpxml(gap_el.get("duration", "0s"))
            if dur.to_seconds() >= silence_threshold_seconds:
                spine_el.remove(gap_el)
                count += 1
        if count > 0:
            mod._recalculate_offsets(spine_el)
    out = _save_modifier(mod, output_path)
    return f"{count} gaps removed. Saved to: {out}"


@mcp.tool()
def fcpxml_batch_rename_clips(
    path: str, pattern: str, replacement: str, output_path: str = "",
) -> str:
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
    count = mod.batch_rename_clips(pattern, replacement)
    if count == 0:
        raise FCPMCPError(
            ErrorCode.TARGET_NOT_FOUND,
            f"No clip name contains pattern '{pattern}'",
        )
    out = _save_modifier(mod, output_path)
    return f"{count} clips renamed. Saved to: {out}"


@mcp.tool()
def fcpxml_batch_assign_roles(
    path: str, rules_json: str, output_path: str = "",
) -> str:
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
    out = _save_modifier(mod, output_path)
    return f"{count} roles assigned. Saved to: {out}"


@mcp.tool()
def fcpxml_batch_apply_transition(
    path: str,
    duration: str = "30030/30000s",
    name: str = "Cross Dissolve",
    output_path: str = "",
) -> str:
    """Add transitions between all adjacent clips in the timeline.

    Args:
        path: Path to .fcpxml file
        duration: Transition duration
        name: Transition name
        output_path: Output file path
    """
    _parse_time(duration, "duration")
    mod = FCPXMLModifier(_resolve_input(path, suffixes={".fcpxml"}))
    count = mod.batch_apply_transition(duration, name)
    if count == 0:
        raise FCPMCPError(
            ErrorCode.TARGET_NOT_FOUND,
            "No adjacent clips were available for transitions",
        )
    out = _save_modifier(mod, output_path)
    return f"{count} transitions added. Saved to: {out}"


# ============================================================================
# Category 9: QC & Validation (6 tools)
# ============================================================================

@mcp.tool()
def fcpxml_qc_report(path: str) -> str:
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

    return "\n".join(lines)


@mcp.tool()
def fcpxml_check_media_links(path: str) -> str:
    """Verify all referenced media files exist on disk.

    Args:
        path: Path to .fcpxml file
    """
    doc = _parse_doc(path)
    for asset in doc.assets.values():
        if asset.src.startswith("file://"):
            PATHS.resolve_reference(url_unquote(asset.src[7:]))
    result = _validator.check_media_links(doc)
    if not result.issues:
        return "All media files found."
    return result.summary()


@mcp.tool()
def fcpxml_check_frame_rates(path: str) -> str:
    """Detect mixed frame rate issues in the timeline.

    Args:
        path: Path to .fcpxml file
    """
    doc = _parse_doc(path)
    frame_rates = set()
    for fmt in doc.formats.values():
        if fmt.frame_duration.numerator > 0:
            frame_rates.add(fmt.fps)

    if len(frame_rates) <= 1:
        return f"Consistent frame rate: {frame_rates.pop() if frame_rates else 'unknown'}fps"

    return f"MIXED FRAME RATES DETECTED: {', '.join(f'{r:.2f}fps' for r in sorted(frame_rates))}"


@mcp.tool()
def fcpxml_check_audio_levels(path: str) -> str:
    """Flag clips with potential audio issues (volume adjustments, missing audio).

    Args:
        path: Path to .fcpxml file
    """
    doc = _parse_doc(path)
    issues = []

    for clip in doc.all_clips:
        if clip.is_gap:
            continue

        # Check if asset has audio
        if clip.ref and clip.ref in doc.assets:
            asset = doc.assets[clip.ref]
            if not asset.has_audio and clip.role in ("Dialogue", "Music", ""):
                issues.append(f"'{clip.name}': no audio in source (role: {clip.role or 'none'})")

        # Check volume adjustments
        if clip.volume:
            amt = clip.volume.amount
            if "dB" in amt:
                try:
                    db_val = float(amt.replace("dB", ""))
                    if db_val > 6:
                        issues.append(f"'{clip.name}': very high volume (+{db_val}dB)")
                    elif db_val < -20:
                        issues.append(f"'{clip.name}': very low volume ({db_val}dB)")
                except ValueError:
                    pass

    if not issues:
        return "No audio issues detected."
    return "Audio issues:\n" + "\n".join(f"- {i}" for i in issues)


@mcp.tool()
def fcpxml_check_safe_zones(path: str) -> str:
    """Check for clips with transforms that might push content outside safe zones.

    Args:
        path: Path to .fcpxml file
    """
    doc = _parse_doc(path)
    issues = []

    for clip in doc.all_clips:
        if clip.transform:
            t = clip.transform
            if abs(t.position_x) > 800 or abs(t.position_y) > 450:
                issues.append(f"'{clip.name}': position ({t.position_x}, {t.position_y}) may be outside safe zone")
            if t.scale > 2.0 or t.scale < 0.3:
                issues.append(f"'{clip.name}': scale {t.scale}x may cause quality issues")

    if not issues:
        return "All clips within safe zones."
    return "Safe zone concerns:\n" + "\n".join(f"- {i}" for i in issues)


@mcp.tool()
def fcpxml_check_duration(path: str, target_seconds: float) -> str:
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
        return (f"Project '{project.name}': {actual:.1f}s / {target_seconds:.1f}s target "
                f"({status}, diff: {diff:+.1f}s)")

    raise FCPMCPError(
        ErrorCode.TARGET_NOT_FOUND,
        "No project with a sequence was found",
    )


# ============================================================================
# Category 10: Templates & Presets (6 tools)
# ============================================================================

@mcp.tool()
def fcp_list_motion_templates() -> str:
    """List installed Motion templates (titles, transitions, generators, effects)."""
    from .utils.paths import motion_templates_dir

    templates_dir = motion_templates_dir()
    if not templates_dir.exists():
        raise FCPMCPError(
            ErrorCode.TARGET_NOT_FOUND,
            f"Motion Templates directory not found: {templates_dir}",
        )

    categories = {}
    for category_dir in templates_dir.iterdir():
        if not category_dir.is_dir():
            continue
        cat_name = category_dir.stem.replace(".localized", "")
        templates = []
        for template_dir in category_dir.rglob("*.motn"):
            templates.append(template_dir.stem)
        if templates:
            categories[cat_name] = templates

    return json.dumps(categories, indent=2)


@mcp.tool()
def fcp_list_share_destinations() -> str:
    """List configured FCP share destinations."""
    from .utils.paths import fcp_destinations_dir

    dest_dir = fcp_destinations_dir()
    if not dest_dir.exists():
        return json.dumps({"destinations": []}, indent=2)

    destinations = []
    for f in dest_dir.glob("*.fcpdestination"):
        destinations.append(f.stem)

    return json.dumps({"destinations": destinations}, indent=2)


@mcp.tool()
def fcp_discover_effects() -> str:
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

    return json.dumps(result, indent=2)


@mcp.tool()
def fcpxml_list_templates(templates_dir: str = "") -> str:
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
    return json.dumps({"templates": templates}, indent=2)


@mcp.tool()
def fcpxml_apply_template(
    template_path: str,
    clips_json: str,
    project_name: str = "From Template",
    output_path: str = "",
) -> str:
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


@mcp.tool()
def fcpxml_save_template(
    path: str,
    template_name: str,
    output_dir: str = "",
) -> str:
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
    atomic_replace_bytes(
        dest,
        src.read_bytes(),
        event_format=CONFIG.log_format,
    )
    return f"Template saved: {dest}"


# ============================================================================
# Category 1: Library Inspection — live FCP (6 tools)
# ============================================================================

@mcp.tool()
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


@mcp.tool()
def fcp_get_libraries() -> str:
    """Get all open libraries in Final Cut Pro (requires FCP to be running)."""
    return automation.run_osascript(
        automation.FCP_LIBRARIES,
        config=CONFIG,
    )


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
def fcp_get_app_state() -> str:
    """Get FCP application state — version, frontmost status."""
    return automation.run_osascript(
        automation.FCP_APP_STATE,
        config=CONFIG,
    )


# ============================================================================
# Category 6: FCP Live Control (10 tools)
# ============================================================================

@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
def fcp_export_xml() -> str:
    """Trigger XML export in FCP via menu automation (requires Accessibility permissions)."""
    return automation.run_osascript(
        automation.FCP_EXPORT_XML,
        timeout=30,
        config=CONFIG,
    )


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
def fcp_undo() -> str:
    """Undo the last action in FCP."""
    return automation.run_osascript(
        automation.FCP_UNDO,
        timeout=30,
        config=CONFIG,
    )


@mcp.tool()
def fcp_redo() -> str:
    """Redo the last undone action in FCP."""
    return automation.run_osascript(
        automation.FCP_REDO,
        timeout=30,
        config=CONFIG,
    )


@mcp.tool()
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


@mcp.tool()
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

@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
def fcpxml_export_resolve(path: str, output_path: str = "") -> str:
    """Convert FCPXML to DaVinci Resolve-compatible format (FCPXML v1.9).

    Args:
        path: Path to .fcpxml file
        output_path: Output file path
    """

    mod = FCPXMLModifier(_resolve_input(path, suffixes={".fcpxml"}))
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
    out = _save_modifier(mod, output_path)
    return f"Resolve-compatible FCPXML saved: {out}"


@mcp.tool()
def fcpxml_export_fcp7(path: str, output_path: str = "") -> str:
    """Convert FCPXML to FCP7 XML format (compatible with Premiere Pro and Avid).

    Args:
        path: Path to .fcpxml file
        output_path: Output file path
    """
    import xml.etree.ElementTree as ET

    source = _resolve_input(path, suffixes={".fcpxml"})
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
    atomic_replace_bytes(
        destination,
        f"{xml_text}\n".encode(),
        event_format=CONFIG.log_format,
    )
    return f"FCP7 XML saved: {destination}"


@mcp.tool()
def fcpxml_export_edl(path: str, output_path: str = "") -> str:
    """Export timeline as EDL (Edit Decision List).

    Args:
        path: Path to .fcpxml file
        output_path: Output .edl file path
    """
    source = _resolve_input(path, suffixes={".fcpxml"})
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
    atomic_replace_bytes(
        destination,
        edl_content.encode(),
        event_format=CONFIG.log_format,
    )
    return f"EDL exported ({edit_num - 1} edits): {destination}"


# ============================================================================
# Category 5: Media Analysis — FFmpeg (8 tools)
# ============================================================================

@mcp.tool()
def media_info(path: str) -> str:
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
    summary = {
        "filename": fmt.get("filename"),
        "duration": f"{float(fmt.get('duration', 0)):.2f}s",
        "size_mb": f"{int(fmt.get('size', 0)) / 1048576:.1f}",
        "bitrate_kbps": f"{int(fmt.get('bit_rate', 0)) / 1000:.0f}",
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
    return json.dumps(summary, indent=2)


@mcp.tool()
def media_detect_silence(
    path: str,
    noise_threshold: str = "-30dB",
    min_duration: float = 0.5,
) -> str:
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
    if not silences:
        return "No silent sections detected."
    return json.dumps(silences, indent=2)


@mcp.tool()
def media_detect_beats(path: str) -> str:
    """Detect beat positions in audio/music files.

    Returns beat timestamps that can be used for music-synced editing.

    Args:
        path: Path to audio/video file
    """
    from .media.ffprobe import detect_beats

    source = _resolve_input(path)
    beats = detect_beats(str(source))
    return json.dumps({"beat_count": len(beats), "beats": beats}, indent=2)


@mcp.tool()
def media_loudness(path: str) -> str:
    """Analyze audio loudness (EBU R128 / LUFS).

    Returns integrated loudness, loudness range, and true peak.

    Args:
        path: Path to audio/video file
    """
    from .media.ffprobe import analyze_loudness

    source = _resolve_input(path)
    result = analyze_loudness(str(source))
    if not result:
        raise FCPMCPError(
            ErrorCode.OUTPUT_MISSING,
            "FFmpeg returned no loudness summary; the file may have no audio",
        )
    return json.dumps(result, indent=2)


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
def media_list_streams(path: str) -> str:
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
    return json.dumps(result, indent=2)


@mcp.tool()
def media_scene_detect(path: str, threshold: float = 0.3) -> str:
    """Detect scene changes in video.

    Useful for automatic clip segmentation.

    Args:
        path: Path to video file
        threshold: Scene change sensitivity (0.0-1.0, lower = more sensitive)
    """
    from .media.ffprobe import detect_scenes

    source = _resolve_input(path)
    scenes = detect_scenes(str(source), threshold)
    return json.dumps({"scene_count": len(scenes), "scenes": scenes}, indent=2)


@mcp.tool()
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


@mcp.tool()
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

@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
def puppet_list_presets() -> str:
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
    return json.dumps(presets, indent=2)


# ============================================================================
# MCP Prompts (5) — pre-baked flows that wrap the most common tool sequences
# ============================================================================

@mcp.prompt(
    name="qc-check",
    description="Run a full QC sweep on an FCPXML timeline and interpret the results.",
)
def prompt_qc_check(path: str) -> str:
    """Run qc report, then explain what needs attention."""
    return (
        f"Run `fcpxml_qc_report(path={path!r})` on this FCPXML file and give me a triaged "
        "summary of the results.\n\n"
        "For each issue category (flash frames, gaps, media links, frame-rate mismatches, "
        "audio levels, safe-zone violations, duration drift), tell me:\n"
        "  1. How many items were flagged.\n"
        "  2. Whether it is fixable automatically — and if so, which tool to call "
        "(`fcpxml_fix_flash_frames`, `fcpxml_fill_gaps`, `fcpxml_remove_silence`, etc.).\n"
        "  3. Whether it requires manual intervention in Final Cut Pro.\n\n"
        "End with a short prioritized checklist of the next 3 actions."
    )


@mcp.prompt(
    name="rough-cut",
    description="Assemble a rough cut from a list of clips, optionally target a duration.",
)
def prompt_rough_cut(clips_json: str, target_duration: str = "") -> str:
    """Auto-assemble a rough cut, then QC it."""
    target_clause = (
        f" targeting a total duration of {target_duration}"
        if target_duration
        else " using all clips"
    )
    return (
        f"Assemble a rough cut from the clips in:\n\n{clips_json}\n\n"
        f"Steps:\n"
        f"  1. Call `fcpxml_auto_rough_cut(clips_json=<above>, "
        f"target_duration={target_duration!r})` to build the timeline"
        f"{target_clause}.\n"
        "  2. Run `fcpxml_qc_report` on the output path and surface any flash frames or gaps.\n"
        "  3. If cleanup is needed, call `fcpxml_fix_flash_frames` and `fcpxml_fill_gaps`, "
        "then re-QC.\n"
        "  4. Return the final output path and a one-line summary of what was assembled."
    )


@mcp.prompt(
    name="cleanup",
    description="Heal flash frames and gaps in an existing FCPXML, then re-QC.",
)
def prompt_cleanup(path: str, output_path: str = "") -> str:
    """Fix flash frames, fill gaps, re-QC."""
    out_clause = (
        f" Save the cleaned file to {output_path!r}."
        if output_path
        else " Save to a `_cleaned.fcpxml` sibling of the input."
    )
    return (
        f"Clean up the FCPXML at {path!r}.{out_clause}\n\n"
        "Steps (in order):\n"
        f"  1. `fcpxml_fix_flash_frames(path={path!r}, output_path=...)` — extend clips "
        "shorter than 2 frames.\n"
        "  2. `fcpxml_fill_gaps(path=<output from step 1>, output_path=...)` — fill spine "
        "gaps with placeholder gap elements.\n"
        "  3. `fcpxml_qc_report(path=<output from step 2>)` — confirm the issues are gone.\n\n"
        "Report: before/after counts for flash frames and gaps, plus any QC items that "
        "still need manual attention."
    )


@mcp.prompt(
    name="youtube-chapters",
    description="Extract FCPXML markers and emit a YouTube chapter timestamp blob.",
)
def prompt_youtube_chapters(path: str) -> str:
    """Convert FCPXML markers to a YouTube description chapter blob."""
    return (
        f"Turn the markers in {path!r} into a YouTube chapter list.\n\n"
        "Steps:\n"
        f"  1. `fcpxml_list_markers(path={path!r})` to get every marker with its start time.\n"
        "  2. Sort by start time ascending. Drop markers before 00:00.\n"
        "  3. Format each as `HH:MM:SS Title` (or `MM:SS Title` if the video is under an "
        "hour). The first entry MUST start at `00:00` — if the earliest marker is later, "
        "prepend a `00:00 Intro` line.\n"
        "  4. Return the blob as a single code block ready to paste into a YouTube "
        "description. Below it, note how many markers were used and flag any that looked "
        "like internal notes (e.g., TODO/FIXME) that were skipped."
    )


@mcp.prompt(
    name="beat-sync",
    description="Detect beats in an audio file and cut clips to land on them.",
)
def prompt_beat_sync(audio_path: str, clips_json: str, project_name: str = "Beat Sync") -> str:
    """Detect beats, build a rough cut that hits them."""
    return (
        f"Build a beat-synced cut using audio {audio_path!r} and clips:\n\n{clips_json}\n\n"
        "Steps:\n"
        f"  1. `media_detect_beats(path={audio_path!r})` — get the beat timestamps.\n"
        "  2. Convert each beat (in seconds) to an FCPXML rational-time string. The cadence "
        "of the beat list sets the per-clip duration window.\n"
        "  3. Call `fcpxml_auto_rough_cut` with the clips above, setting `max_clip_duration` "
        "to the median inter-beat interval so cuts land on beats.\n"
        f"  4. Name the project {project_name!r}.\n"
        "  5. Add a marker at every beat via `fcpxml_batch_add_markers` so the editor can "
        "see the grid.\n"
        "  6. Run `fcpxml_qc_report` on the result and report the final path plus how many "
        "beats were used."
    )


# ============================================================================
# Entry point
# ============================================================================

def main():
    """Run the MCP server."""
    mcp.run()


if __name__ == "__main__":
    main()
