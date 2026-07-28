"""Deterministic, SDK-independent workflow plan normalization and execution."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from enum import Enum
from itertools import pairwise
from pathlib import Path
from types import MappingProxyType
from typing import Literal, cast

from fcp_mcp.contracts import ErrorCode
from fcp_mcp.fcpxml.time_utils import RationalTime
from fcp_mcp.fcpxml.writer import FCPXMLModifier, assigned_role
from fcp_mcp.workflow.models import (
    AddKeywordOperation,
    AddMarkerOperation,
    AddTransitionOperation,
    AssignRoleOperation,
    BatchApplyTransitionOperation,
    BatchAssignRolesOperation,
    BatchRenameClipsOperation,
    ChangeSpeedOperation,
    DeleteClipsOperation,
    FillGapsOperation,
    FixFlashFramesOperation,
    OperationDisposition,
    OperationReceiptV1,
    OperationValueChangeV1,
    ReorderClipsOperation,
    SplitClipOperation,
    TrimClipOperation,
    WorkflowOperation,
    WorkflowPlanV1,
    sha256_canonical,
)

_CLIP_TAGS = frozenset(
    {
        "asset-clip",
        "clip",
        "gap",
        "title",
        "generator",
        "compound-clip",
        "mc-clip",
        "sync-clip",
        "audition",
        "video",
        "audio",
        "ref-clip",
    }
)
_BATCH_ROLE_TAGS = frozenset({"asset-clip", "title", "audio", "video"})
_TIME_PATTERN = re.compile(r"^(-?\d+)(?:/(\d+))?s$")
_MAX_TIME_NUMERATOR = (1 << 63) - 1
_MIN_TIME_NUMERATOR = -(1 << 63)
_MAX_TIME_DENOMINATOR = (1 << 31) - 1
_MAX_RECEIPT_ENTITIES = 1000
_MAX_RECEIPT_CHANGES = 100


@dataclass(frozen=True)
class ResourceInventoryV1:
    resource_id: str
    kind: str
    name: str
    uid: str | None
    has_video: bool | None
    has_audio: bool | None


@dataclass(frozen=True)
class ClipInventoryV1:
    identity: str
    kind: str
    name: str
    ref: str | None
    start: str
    duration: str
    offset: str
    spine_index: int | None
    child_index: int | None


@dataclass(frozen=True)
class SpineInventoryV1:
    identity: str
    items: tuple[ClipInventoryV1, ...]


@dataclass(frozen=True)
class SourceInventoryV1:
    resources: tuple[ResourceInventoryV1, ...]
    clips: tuple[ClipInventoryV1, ...]
    spines: tuple[SpineInventoryV1, ...]


@dataclass(frozen=True)
class NormalizedOperationV1:
    operation_id: str
    operation: WorkflowOperation


@dataclass(frozen=True)
class NormalizedPlanV1:
    schema_version: Literal["1"]
    operations: tuple[NormalizedOperationV1, ...]
    caller_plan_sha256: str


class CandidateDisposition(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True)
class CandidateExecution:
    normalized_plan: NormalizedPlanV1
    receipts: tuple[OperationReceiptV1, ...]
    disposition: CandidateDisposition
    candidate_bytes: bytes | None = None
    error_code: ErrorCode | None = None
    error_summary: str | None = None

    def __post_init__(self) -> None:
        if self.disposition is CandidateDisposition.SUCCEEDED:
            if (
                self.candidate_bytes is None
                or self.error_code is not None
                or self.error_summary is not None
            ):
                raise ValueError("successful candidate execution requires candidate bytes only")
            if not self.receipts or any(
                receipt.disposition is not OperationDisposition.SUCCEEDED
                for receipt in self.receipts
            ):
                raise ValueError("successful candidate execution requires successful receipts")
        elif (
            self.candidate_bytes is not None
            or self.error_code is None
            or self.error_summary is None
        ):
            raise ValueError("failed candidate execution requires a coded failure only")
        elif (
            not self.receipts
            or self.receipts[-1].disposition is not OperationDisposition.FAILED
            or self.receipts[-1].error_code is not self.error_code
            or any(
                receipt.disposition is not OperationDisposition.SUCCEEDED
                for receipt in self.receipts[:-1]
            )
        ):
            raise ValueError("failed candidate execution requires one final matching failure")


class OperationPreflightError(ValueError):
    """A source-dependent plan validation failure with stable receipt context."""

    def __init__(
        self,
        *,
        normalized_plan: NormalizedPlanV1,
        operation_index: int,
        code: ErrorCode,
        summary: str,
    ) -> None:
        self.normalized_plan = normalized_plan
        self.operation_index = operation_index
        self.code = code
        self.summary = _bounded_summary(summary)
        super().__init__(self.summary)


class _ExecutionError(RuntimeError):
    def __init__(self, code: ErrorCode, summary: str, affected_count: int = 0) -> None:
        self.code = code
        self.summary = _bounded_summary(summary)
        self.affected_count = affected_count
        super().__init__(self.summary)


def _bounded_summary(summary: str) -> str:
    cleaned = " ".join(summary.split())
    return (cleaned or "operation failed")[:4096]


def _resource_boolean(value: str | None) -> bool | None:
    if value is None:
        return None
    return value == "1"


def build_source_inventory(modifier: FCPXMLModifier) -> SourceInventoryV1:
    """Build immutable inspection evidence from one safely parsed source."""
    resources: list[ResourceInventoryV1] = []
    seen_resource_ids: set[str] = set()
    resources_element = modifier.root.find("./resources")
    if resources_element is not None:
        for element in resources_element:
            resource_id = element.get("id", "")
            if not resource_id or resource_id in seen_resource_ids:
                raise ValueError("source resource IDs must be non-empty and unique")
            seen_resource_ids.add(resource_id)
            resources.append(
                ResourceInventoryV1(
                    resource_id=resource_id,
                    kind=element.tag,
                    name=element.get("name", ""),
                    uid=element.get("uid"),
                    has_video=_resource_boolean(element.get("hasVideo")),
                    has_audio=_resource_boolean(element.get("hasAudio")),
                )
            )

    spine_locations: dict[int, tuple[int, int]] = {}
    spine_elements = list(modifier.root.iter("spine"))
    for spine_index, spine in enumerate(spine_elements):
        for child_index, child in enumerate(spine):
            spine_locations[id(child)] = (spine_index, child_index)

    clips: list[ClipInventoryV1] = []
    by_element: dict[int, ClipInventoryV1] = {}
    for element_index, element in enumerate(modifier.root.iter(), start=1):
        location = spine_locations.get(id(element))
        is_spine_transition = element.tag == "transition" and location is not None
        if element.tag not in _CLIP_TAGS and not is_spine_transition:
            continue
        clip = ClipInventoryV1(
            identity=f"element-{element_index:06d}",
            kind=element.tag,
            name=element.get("name", ""),
            ref=element.get("ref"),
            start=element.get("start", "0s"),
            duration=element.get("duration", "0s"),
            offset=element.get("offset", "0s"),
            spine_index=location[0] if location is not None else None,
            child_index=location[1] if location is not None else None,
        )
        if not is_spine_transition:
            clips.append(clip)
        by_element[id(element)] = clip

    spines = tuple(
        SpineInventoryV1(
            identity=f"spine-{spine_index + 1:03d}",
            items=tuple(
                by_element[id(child)]
                for child in spine
                if id(child) in by_element
            ),
        )
        for spine_index, spine in enumerate(spine_elements)
    )
    return SourceInventoryV1(
        resources=tuple(resources),
        clips=tuple(clips),
        spines=spines,
    )


def _parse_time(
    value: str,
    *,
    positive: bool = False,
    nonnegative: bool = False,
) -> RationalTime:
    match = _TIME_PATTERN.fullmatch(value)
    if match is None:
        raise ValueError("time must use an integer FCPXML rational")
    numerator = int(match.group(1))
    denominator = int(match.group(2) or "1")
    if not _MIN_TIME_NUMERATOR <= numerator <= _MAX_TIME_NUMERATOR:
        raise ValueError("time numerator is outside the signed 64-bit domain")
    if not 1 <= denominator <= _MAX_TIME_DENOMINATOR:
        raise ValueError("time denominator is outside the positive 32-bit domain")
    if positive and numerator <= 0:
        raise ValueError("duration must be positive")
    if nonnegative and numerator < 0:
        raise ValueError("time must be nonnegative")
    return RationalTime(numerator, denominator)


def _validate_result_time(
    value: RationalTime,
    *,
    positive: bool = False,
    nonnegative: bool = False,
) -> RationalTime:
    if not _MIN_TIME_NUMERATOR <= value.numerator <= _MAX_TIME_NUMERATOR:
        raise ValueError("result time numerator is outside the signed 64-bit domain")
    if not 1 <= value.denominator <= _MAX_TIME_DENOMINATOR:
        raise ValueError("result time denominator is outside the positive 32-bit domain")
    if positive and value.numerator <= 0:
        raise ValueError("result duration must be positive")
    if nonnegative and value.numerator < 0:
        raise ValueError("result time must be nonnegative")
    return value


def _clip(inventory: SourceInventoryV1, name: str) -> ClipInventoryV1 | None:
    return next((clip for clip in inventory.clips if clip.name == name), None)


def _resource(
    inventory: SourceInventoryV1,
    resource_id: str,
    kind: str,
) -> ResourceInventoryV1 | None:
    return next(
        (
            resource
            for resource in inventory.resources
            if resource.resource_id == resource_id and resource.kind == kind
        ),
        None,
    )


def _transition_resource(
    inventory: SourceInventoryV1,
    resource_id: str,
) -> ResourceInventoryV1 | None:
    resource = _resource(inventory, resource_id, "effect")
    if resource is None or resource.uid is None:
        return None
    return resource if resource.uid.lower().endswith(".motr") else None


def _visual_fill_resource(
    inventory: SourceInventoryV1,
    resource_id: str,
) -> ResourceInventoryV1 | None:
    resource = _resource(inventory, resource_id, "asset")
    if resource is None or resource.has_video is not True:
        return None
    return resource


def _require_clip(inventory: SourceInventoryV1, name: str) -> ClipInventoryV1:
    clip = _clip(inventory, name)
    if clip is None:
        raise _ExecutionError(
            ErrorCode.TARGET_NOT_FOUND,
            "requested named clip target was not found",
        )
    return clip


def _preflight_operation(
    operation: WorkflowOperation,
    inventory: SourceInventoryV1,
) -> None:
    try:
        if isinstance(operation, AddMarkerOperation):
            _require_clip(inventory, operation.clip_name)
            _parse_time(operation.start, nonnegative=True)
            _parse_time(operation.duration, positive=True)
            if operation.marker_type not in {"standard", "chapter"}:
                raise ValueError("marker type must be standard or chapter")
        elif isinstance(operation, AddKeywordOperation):
            _require_clip(inventory, operation.clip_name)
            _parse_time(operation.start, nonnegative=True)
            if operation.duration is not None:
                _parse_time(operation.duration, positive=True)
        elif isinstance(operation, TrimClipOperation):
            target = _require_clip(inventory, operation.clip_name)
            old_start = _parse_time(target.start, nonnegative=True)
            old_duration = _parse_time(target.duration, positive=True)
            new_start = (
                _parse_time(operation.new_start, nonnegative=True)
                if operation.new_start is not None
                else old_start
            )
            new_duration = (
                _parse_time(operation.new_duration, positive=True)
                if operation.new_duration is not None
                else old_duration
            )
            if new_start == old_start and new_duration == old_duration:
                raise ValueError("trim must change start or duration")
        elif isinstance(operation, SplitClipOperation):
            target = _require_clip(inventory, operation.clip_name)
            duration = _parse_time(target.duration, positive=True)
            start = _parse_time(target.start, nonnegative=True)
            offset = _parse_time(target.offset)
            split = _parse_time(operation.split_at, positive=True)
            if split >= duration:
                raise ValueError("split point must be strictly inside the clip")
            _validate_result_time(duration - split, positive=True)
            _validate_result_time(start + split, nonnegative=True)
            _validate_result_time(offset + split)
        elif isinstance(operation, DeleteClipsOperation):
            for name in operation.clip_names:
                _require_clip(inventory, name)
        elif isinstance(operation, ReorderClipsOperation):
            if not inventory.spines:
                raise _ExecutionError(
                    ErrorCode.TARGET_NOT_FOUND,
                    "requested spine target was not found",
                )
            primary = inventory.spines[0]
            current_offset = RationalTime.zero()
            for item in primary.items:
                _validate_result_time(current_offset, nonnegative=True)
                if item.kind == "transition":
                    continue
                duration = _parse_time(item.duration, nonnegative=True)
                current_offset = _validate_result_time(
                    current_offset + duration,
                    nonnegative=True,
                )
            for name in operation.clip_names:
                matches = [
                    item
                    for item in primary.items
                    if item.kind != "transition" and item.name == name
                ]
                if not matches:
                    raise _ExecutionError(
                        ErrorCode.TARGET_NOT_FOUND,
                        "requested named spine target was not found",
                    )
                if len(matches) > 1:
                    raise ValueError("reorder target name is ambiguous")
                _parse_time(matches[0].duration, positive=True)
        elif isinstance(operation, AddTransitionOperation):
            target = _require_clip(inventory, operation.after_clip_name)
            if target.kind == "gap":
                raise _ExecutionError(
                    ErrorCode.TARGET_NOT_FOUND,
                    "requested transition adjacency was not found",
                )
            _parse_time(operation.duration, positive=True)
            offset = _parse_time(target.offset)
            duration = _parse_time(target.duration, positive=True)
            _validate_result_time(offset + duration)
            if (
                operation.ref
                and _transition_resource(inventory, operation.ref) is None
            ):
                raise _ExecutionError(
                    ErrorCode.TARGET_NOT_FOUND,
                    "requested transition resource was not found",
                )
            if target.spine_index is None or target.child_index is None:
                raise _ExecutionError(
                    ErrorCode.TARGET_NOT_FOUND,
                    "requested transition adjacency was not found",
                )
            spine = inventory.spines[target.spine_index]
            target_position = next(
                index
                for index, item in enumerate(spine.items)
                if item.identity == target.identity
            )
            following = tuple(
                item
                for item in spine.items[target_position + 1 :]
                if item.kind != "transition"
            )
            if not following or following[0].kind == "gap":
                raise _ExecutionError(
                    ErrorCode.TARGET_NOT_FOUND,
                    "requested transition adjacency was not found",
                )
        elif isinstance(operation, ChangeSpeedOperation):
            target = _require_clip(inventory, operation.clip_name)
            duration = _parse_time(target.duration, positive=True)
            new_numerator = round(duration.numerator / operation.speed_factor)
            _validate_result_time(
                RationalTime(new_numerator, duration.denominator),
                positive=True,
            )
        elif isinstance(operation, AssignRoleOperation):
            _require_clip(inventory, operation.clip_name)
        elif isinstance(
            operation,
            (BatchAssignRolesOperation, BatchRenameClipsOperation),
        ):
            return
        elif isinstance(operation, FillGapsOperation):
            if _visual_fill_resource(inventory, operation.fill_ref) is None:
                raise _ExecutionError(
                    ErrorCode.TARGET_NOT_FOUND,
                    "requested fill resource was not found",
                )
            for spine in inventory.spines:
                for item in spine.items:
                    if item.kind == "gap":
                        _parse_time(item.offset)
                        _parse_time(item.duration, positive=True)
        elif isinstance(operation, FixFlashFramesOperation):
            frame_duration = _parse_time(operation.frame_duration, positive=True)
            minimum = _validate_result_time(
                frame_duration * operation.min_frames,
                positive=True,
            )
            for spine in inventory.spines:
                current_offset = RationalTime.zero()
                for item in spine.items:
                    _validate_result_time(current_offset, nonnegative=True)
                    if item.kind == "transition":
                        continue
                    duration = _parse_time(item.duration, nonnegative=True)
                    projected_duration = duration
                    if (
                        item.kind not in {"gap", "transition"}
                        and duration < minimum
                        and not duration.is_zero
                    ):
                        projected_duration = minimum
                    current_offset = _validate_result_time(
                        current_offset + projected_duration,
                        nonnegative=True,
                    )
        elif isinstance(operation, BatchApplyTransitionOperation):
            _parse_time(operation.duration, positive=True)
            if (
                operation.ref
                and _transition_resource(inventory, operation.ref) is None
            ):
                raise _ExecutionError(
                    ErrorCode.TARGET_NOT_FOUND,
                    "requested transition resource was not found",
                )
            for spine in inventory.spines:
                for item in spine.items:
                    if item.kind not in {"gap", "transition"}:
                        offset = _parse_time(item.offset)
                        duration = _parse_time(item.duration, positive=True)
                        _validate_result_time(offset + duration)
        else:
            raise TypeError("unsupported validated workflow operation")
    except _ExecutionError:
        raise
    except (ArithmeticError, ValueError) as error:
        raise _ExecutionError(
            ErrorCode.INVALID_ARGUMENTS,
            "operation contains invalid time or value semantics",
        ) from error


def _map_inventory_clips(
    inventory: SourceInventoryV1,
    transform: Callable[[ClipInventoryV1], ClipInventoryV1],
) -> SourceInventoryV1:
    source_items = {clip.identity: clip for clip in inventory.clips}
    for spine in inventory.spines:
        for item in spine.items:
            source_items.setdefault(item.identity, item)
    transformed = {
        identity: transform(clip)
        for identity, clip in source_items.items()
    }
    return replace(
        inventory,
        clips=tuple(transformed[clip.identity] for clip in inventory.clips),
        spines=tuple(
            replace(
                spine,
                items=tuple(transformed[item.identity] for item in spine.items),
            )
            for spine in inventory.spines
        ),
    )


def _advance_inventory(
    inventory: SourceInventoryV1,
    item: NormalizedOperationV1,
) -> SourceInventoryV1:
    """Project deterministic operation effects for ordered preflight."""
    operation = item.operation
    if isinstance(operation, TrimClipOperation):
        target = _require_clip(inventory, operation.clip_name)
        return _map_inventory_clips(
            inventory,
            lambda clip: (
                replace(
                    clip,
                    start=operation.new_start or clip.start,
                    duration=operation.new_duration or clip.duration,
                )
                if clip.identity == target.identity
                else clip
            ),
        )
    if isinstance(operation, SplitClipOperation):
        target = _require_clip(inventory, operation.clip_name)
        split = _parse_time(operation.split_at, positive=True)
        start = _parse_time(target.start, nonnegative=True)
        duration = _parse_time(target.duration, positive=True)
        offset = _parse_time(target.offset)
        first = replace(target, duration=split.to_fcpxml())
        second = replace(
            target,
            identity=f"{target.identity}:{item.operation_id}:split",
            name=f"{operation.clip_name}_split",
            start=(start + split).to_fcpxml(),
            duration=(duration - split).to_fcpxml(),
            offset=(offset + split).to_fcpxml(),
            child_index=(
                target.child_index + 1
                if target.child_index is not None
                else None
            ),
        )
        clips: list[ClipInventoryV1] = []
        for clip in inventory.clips:
            clips.append(first if clip.identity == target.identity else clip)
            if clip.identity == target.identity:
                clips.append(second)
        spines = []
        for spine in inventory.spines:
            projected: list[ClipInventoryV1] = []
            for clip in spine.items:
                projected.append(first if clip.identity == target.identity else clip)
                if clip.identity == target.identity:
                    projected.append(second)
            spines.append(replace(spine, items=tuple(projected)))
        return replace(inventory, clips=tuple(clips), spines=tuple(spines))
    if isinstance(operation, DeleteClipsOperation):
        removed: set[str] = set()
        available = list(inventory.clips)
        for name in operation.clip_names:
            target = next(clip for clip in available if clip.name == name)
            removed.add(target.identity)
            available.remove(target)
        return replace(
            inventory,
            clips=tuple(
                clip for clip in inventory.clips if clip.identity not in removed
            ),
            spines=tuple(
                replace(
                    spine,
                    items=tuple(
                        clip
                        for clip in spine.items
                        if clip.identity not in removed
                    ),
                )
                for spine in inventory.spines
            ),
        )
    if isinstance(operation, ReorderClipsOperation):
        primary = inventory.spines[0]
        by_name = {
            clip.name: clip
            for clip in primary.items
            if clip.kind != "transition" and clip.name in operation.clip_names
        }
        selected_identities = {clip.identity for clip in by_name.values()}
        projected = [
            clip
            for clip in primary.items
            if clip.identity not in selected_identities
        ]
        for insert_index, name in enumerate(operation.clip_names):
            projected.insert(insert_index, by_name[name])
        current_offset = RationalTime.zero()
        normalized_items: list[ClipInventoryV1] = []
        for clip in projected:
            normalized_items.append(
                replace(clip, offset=current_offset.to_fcpxml())
            )
            if clip.kind == "transition":
                continue
            duration = _parse_time(clip.duration, nonnegative=True)
            current_offset = _validate_result_time(
                current_offset + duration,
                nonnegative=True,
            )
        normalized_by_identity = {
            clip.identity: clip for clip in normalized_items
        }
        return replace(
            inventory,
            clips=tuple(
                normalized_by_identity.get(clip.identity, clip)
                for clip in inventory.clips
            ),
            spines=(
                replace(primary, items=tuple(normalized_items)),
                *inventory.spines[1:],
            ),
        )
    if isinstance(operation, ChangeSpeedOperation):
        target = _require_clip(inventory, operation.clip_name)
        duration = _parse_time(target.duration, positive=True)
        new_duration = RationalTime(
            round(duration.numerator / operation.speed_factor),
            duration.denominator,
        ).to_fcpxml()
        return _map_inventory_clips(
            inventory,
            lambda clip: (
                replace(clip, duration=new_duration)
                if clip.identity == target.identity
                else clip
            ),
        )
    if isinstance(operation, BatchRenameClipsOperation):
        projected = _map_inventory_clips(
            inventory,
            lambda clip: replace(
                clip,
                name=clip.name.replace(operation.pattern, operation.replacement),
            ),
        )
        return replace(
            projected,
            resources=tuple(
                replace(
                    resource,
                    name=resource.name.replace(
                        operation.pattern,
                        operation.replacement,
                    ),
                )
                for resource in projected.resources
            ),
        )
    if isinstance(operation, FillGapsOperation):
        direct_gap_ids = {
            clip.identity
            for spine in inventory.spines
            for clip in spine.items
            if clip.kind == "gap"
        }
        return _map_inventory_clips(
            inventory,
            lambda clip: (
                replace(
                    clip,
                    kind="asset-clip",
                    name=operation.fill_name,
                    ref=operation.fill_ref,
                    start="0s",
                )
                if clip.identity in direct_gap_ids
                else clip
            ),
        )
    if isinstance(operation, FixFlashFramesOperation):
        minimum = (
            _parse_time(operation.frame_duration, positive=True)
            * operation.min_frames
        )
        projected: dict[str, ClipInventoryV1] = {}
        changed = False
        for spine in inventory.spines:
            current_offset = RationalTime.zero()
            for clip in spine.items:
                if clip.kind == "transition":
                    projected[clip.identity] = replace(
                        clip,
                        offset=current_offset.to_fcpxml(),
                    )
                    continue
                duration = _parse_time(clip.duration, nonnegative=True)
                projected_duration = duration
                if (
                    clip.kind not in {"gap", "transition"}
                    and duration < minimum
                    and not duration.is_zero
                ):
                    projected_duration = minimum
                    changed = True
                projected[clip.identity] = replace(
                    clip,
                    duration=(
                        projected_duration.to_fcpxml()
                        if projected_duration != duration
                        else clip.duration
                    ),
                    offset=current_offset.to_fcpxml(),
                )
                current_offset = current_offset + projected_duration
        if not changed:
            return inventory
        return _map_inventory_clips(
            inventory,
            lambda clip: projected.get(clip.identity, clip),
        )
    return inventory


def normalize_plan(
    plan: WorkflowPlanV1,
    inventory: SourceInventoryV1,
) -> NormalizedPlanV1:
    """Assign server operation IDs, hash caller input, and run source preflight."""
    if not isinstance(plan, WorkflowPlanV1):
        raise TypeError("plan must be a WorkflowPlanV1")
    if not isinstance(inventory, SourceInventoryV1):
        raise TypeError("inventory must be a SourceInventoryV1")
    normalized = NormalizedPlanV1(
        schema_version="1",
        operations=tuple(
            NormalizedOperationV1(
                operation_id=f"op-{index:03d}",
                operation=operation,
            )
            for index, operation in enumerate(plan.operations, start=1)
        ),
        caller_plan_sha256=sha256_canonical(plan),
    )
    projected_inventory = inventory
    for index, item in enumerate(normalized.operations):
        try:
            _preflight_operation(item.operation, projected_inventory)
            projected_inventory = _advance_inventory(projected_inventory, item)
        except _ExecutionError as error:
            raise OperationPreflightError(
                normalized_plan=normalized,
                operation_index=index,
                code=error.code,
                summary=error.summary,
            ) from error
    return normalized


def _named_element(modifier: FCPXMLModifier, name: str) -> ET.Element | None:
    return next(
        (
            element
            for element in modifier.root.iter()
            if element.tag in _CLIP_TAGS and element.get("name") == name
        ),
        None,
    )


def _editable_elements(modifier: FCPXMLModifier) -> list[ET.Element]:
    return [
        element
        for element in modifier.root.iter()
        if element.tag in _BATCH_ROLE_TAGS
    ]


def _entity_name(element: ET.Element, index: int) -> str:
    return element.get("name") or f"element-{index:06d}"


def _tree_state(
    root: ET.Element,
    *,
    excluded_ids: frozenset[int] = frozenset(),
    ignored_attributes: Mapping[int, frozenset[str]] | None = None,
    ignored_children: frozenset[int] = frozenset(),
) -> tuple[
    tuple[
        int,
        str,
        tuple[tuple[str, str], ...],
        str | None,
        str | None,
        tuple[int, ...],
    ],
    ...,
]:
    """Snapshot live element identity and state with explicit allowed differences."""
    ignored = ignored_attributes or {}
    state = []
    for element in root.iter():
        identity = id(element)
        if identity in excluded_ids:
            continue
        skipped_attributes = ignored.get(identity, frozenset())
        children = (
            ()
            if identity in ignored_children
            else tuple(
                id(child)
                for child in element
                if id(child) not in excluded_ids
            )
        )
        state.append(
            (
                identity,
                element.tag,
                tuple(
                    sorted(
                        (name, value)
                        for name, value in element.attrib.items()
                        if name not in skipped_attributes
                    )
                ),
                element.text,
                element.tail,
                children,
            )
        )
    return tuple(sorted(state, key=lambda record: record[0]))


def _bounded_entities(names: list[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    warnings: list[str] = []
    bounded_names = []
    for name in names[:_MAX_RECEIPT_ENTITIES]:
        bounded = (name or "unnamed entity")[:255]
        if bounded != name:
            warnings.append("affected entity identities were bounded")
        bounded_names.append(bounded)
    if len(names) > _MAX_RECEIPT_ENTITIES:
        warnings.append("affected entity identities were truncated")
    return tuple(bounded_names), tuple(dict.fromkeys(warnings))


def _change(field: str, before: str | None, after: str | None) -> OperationValueChangeV1:
    return OperationValueChangeV1(
        field=(field or "value")[:255],
        before=before[:4096] if before is not None else None,
        after=after[:4096] if after is not None else None,
    )


def _success_receipt(
    item: NormalizedOperationV1,
    *,
    affected_entities: list[str],
    changes: list[OperationValueChangeV1] | None = None,
    warnings: tuple[str, ...] = (),
    affected_count: int | None = None,
) -> OperationReceiptV1:
    entities, truncation_warnings = _bounded_entities(affected_entities)
    return OperationReceiptV1(
        operation_id=item.operation_id,
        kind=item.operation.kind,
        disposition=OperationDisposition.SUCCEEDED,
        affected_count=(
            len(affected_entities) if affected_count is None else affected_count
        ),
        affected_entities=entities,
        changes=tuple((changes or [])[:_MAX_RECEIPT_CHANGES]),
        warnings=warnings + truncation_warnings,
    )


def _failed_receipt(
    item: NormalizedOperationV1,
    *,
    code: ErrorCode,
    summary: str,
    affected_count: int = 0,
) -> OperationReceiptV1:
    return OperationReceiptV1(
        operation_id=item.operation_id,
        kind=item.operation.kind,
        disposition=OperationDisposition.FAILED,
        affected_count=affected_count,
        error_code=code,
        error_summary=_bounded_summary(summary),
    )


def _execute_add_marker(
    modifier: FCPXMLModifier,
    item: NormalizedOperationV1,
) -> OperationReceiptV1:
    operation = cast(AddMarkerOperation, item.operation)
    target = _named_element(modifier, operation.clip_name)
    if target is None:
        raise _ExecutionError(ErrorCode.OPERATION_FAILED, "preflight target disappeared")
    tag = "chapter-marker" if operation.marker_type == "chapter" else "marker"
    before = list(target.findall(tag))
    result = modifier.add_marker(
        operation.clip_name,
        operation.start,
        operation.value,
        operation.note,
        operation.marker_type,
        operation.duration,
    )
    after = list(target.findall(tag))
    expected = {
        "start": operation.start,
        "duration": operation.duration,
        "value": operation.value,
    }
    if operation.note:
        expected["note"] = operation.note
    if (
        result is not True
        or len(after) != len(before) + 1
        or after[-1].attrib != expected
    ):
        raise _ExecutionError(
            ErrorCode.OPERATION_FAILED,
            "marker operation did not produce the requested effect",
        )
    return _success_receipt(
        item,
        affected_entities=[operation.clip_name],
        changes=[_change("marker_count", str(len(before)), str(len(after)))],
    )


def _execute_add_keyword(
    modifier: FCPXMLModifier,
    item: NormalizedOperationV1,
) -> OperationReceiptV1:
    operation = cast(AddKeywordOperation, item.operation)
    target = _named_element(modifier, operation.clip_name)
    if target is None:
        raise _ExecutionError(ErrorCode.OPERATION_FAILED, "preflight target disappeared")
    before = list(target.findall("keyword"))
    result = modifier.add_keyword(
        operation.clip_name,
        operation.value,
        operation.start,
        operation.duration,
    )
    after = list(target.findall("keyword"))
    expected = {"value": operation.value, "start": operation.start}
    if operation.duration is not None:
        expected["duration"] = operation.duration
    if (
        result is not True
        or len(after) != len(before) + 1
        or after[-1].attrib != expected
    ):
        raise _ExecutionError(
            ErrorCode.OPERATION_FAILED,
            "keyword operation did not produce the requested effect",
        )
    return _success_receipt(
        item,
        affected_entities=[operation.clip_name],
        changes=[_change("keyword_count", str(len(before)), str(len(after)))],
    )


def _execute_trim_clip(
    modifier: FCPXMLModifier,
    item: NormalizedOperationV1,
) -> OperationReceiptV1:
    operation = cast(TrimClipOperation, item.operation)
    target = _named_element(modifier, operation.clip_name)
    if target is None:
        raise _ExecutionError(ErrorCode.OPERATION_FAILED, "preflight target disappeared")
    before_start = target.get("start", "0s")
    before_duration = target.get("duration", "0s")
    result = modifier.trim_clip(
        operation.clip_name,
        operation.new_start,
        operation.new_duration,
    )
    expected_start = operation.new_start or before_start
    expected_duration = operation.new_duration or before_duration
    if (
        result is not True
        or target.get("start", "0s") != expected_start
        or target.get("duration", "0s") != expected_duration
    ):
        raise _ExecutionError(
            ErrorCode.OPERATION_FAILED,
            "trim operation did not produce the requested effect",
        )
    changes = []
    if before_start != expected_start:
        changes.append(_change("start", before_start, expected_start))
    if before_duration != expected_duration:
        changes.append(_change("duration", before_duration, expected_duration))
    return _success_receipt(
        item,
        affected_entities=[operation.clip_name],
        changes=changes,
    )


def _execute_split_clip(
    modifier: FCPXMLModifier,
    item: NormalizedOperationV1,
) -> OperationReceiptV1:
    operation = cast(SplitClipOperation, item.operation)
    target = _named_element(modifier, operation.clip_name)
    if target is None:
        raise _ExecutionError(ErrorCode.OPERATION_FAILED, "preflight target disappeared")
    parent = modifier._find_parent(target)
    if parent is None:
        raise _ExecutionError(ErrorCode.OPERATION_FAILED, "preflight parent disappeared")
    before_duration = target.get("duration", "0s")
    before_children = list(parent)
    target_index = before_children.index(target)
    old_start = _parse_time(target.get("start", "0s"), nonnegative=True)
    old_duration = _parse_time(before_duration, positive=True)
    old_offset = _parse_time(target.get("offset", "0s"))
    split = _parse_time(operation.split_at, positive=True)
    result = modifier.split_clip(operation.clip_name, operation.split_at)
    children = list(parent)
    second = children[target_index + 1] if len(children) > target_index + 1 else None
    if (
        result is not True
        or len(children) != len(before_children) + 1
        or target.get("duration") != split.to_fcpxml()
        or second is None
        or second.get("name") != f"{operation.clip_name}_split"
        or second.get("start") != (old_start + split).to_fcpxml()
        or second.get("duration") != (old_duration - split).to_fcpxml()
        or second.get("offset") != (old_offset + split).to_fcpxml()
        or any(second.find(tag) is not None for tag in ("marker", "chapter-marker", "keyword"))
    ):
        raise _ExecutionError(
            ErrorCode.OPERATION_FAILED,
            "split operation did not produce the requested effect",
        )
    second_name = f"{operation.clip_name}_split"
    return _success_receipt(
        item,
        affected_entities=[operation.clip_name, second_name],
        changes=[
            _change("duration", before_duration, split.to_fcpxml()),
            _change("created_clip", None, second_name),
        ],
    )


def _execute_delete_clips(
    modifier: FCPXMLModifier,
    item: NormalizedOperationV1,
) -> OperationReceiptV1:
    operation = cast(DeleteClipsOperation, item.operation)
    targets = [_named_element(modifier, name) for name in operation.clip_names]
    if any(target is None for target in targets):
        raise _ExecutionError(
            ErrorCode.TARGET_NOT_FOUND,
            "requested named clip target was not found",
        )
    removed_subtree_ids: set[int] = set()
    for target in targets:
        assert target is not None
        parent = modifier._find_parent(target)
        if parent is None:
            raise _ExecutionError(
                ErrorCode.TARGET_NOT_FOUND,
                "requested named clip parent was not found",
            )
        removed_subtree_ids.update(id(element) for element in target.iter())
    expected_tree = _tree_state(
        modifier.root,
        excluded_ids=frozenset(removed_subtree_ids),
    )
    result = modifier.delete_clips(list(operation.clip_names))
    live_element_ids = {id(element) for element in modifier.root.iter()}
    if result != len(operation.clip_names) or any(
        id(target) in live_element_ids for target in targets
    ) or _tree_state(modifier.root) != expected_tree:
        raise _ExecutionError(
            ErrorCode.OPERATION_FAILED,
            "delete operation count or membership did not match",
            affected_count=max(result, 0),
        )
    return _success_receipt(
        item,
        affected_entities=list(operation.clip_names),
        changes=[
            _change("membership", "present", "absent")
            for _ in operation.clip_names
        ],
    )


def _execute_reorder_clips(
    modifier: FCPXMLModifier,
    item: NormalizedOperationV1,
) -> OperationReceiptV1:
    operation = cast(ReorderClipsOperation, item.operation)
    spine = modifier.root.find(".//spine")
    if spine is None:
        raise _ExecutionError(ErrorCode.OPERATION_FAILED, "preflight spine disappeared")
    selected = set(operation.clip_names)
    before_children = list(spine)
    ignored_offsets = {
        id(child): frozenset({"offset"}) for child in before_children
    }
    unchanged_tree = _tree_state(
        modifier.root,
        ignored_attributes=ignored_offsets,
        ignored_children=frozenset({id(spine)}),
    )
    selected_elements = {
        child.get("name", ""): child
        for child in before_children
        if child.tag != "transition" and child.get("name", "") in selected
    }
    selected_ids = {id(element) for element in selected_elements.values()}
    expected_children = [
        child
        for child in before_children
        if id(child) not in selected_ids
    ]
    for insert_index, name in enumerate(operation.clip_names):
        expected_children.insert(insert_index, selected_elements[name])
    expected_offsets: dict[int, str] = {}
    current_offset = RationalTime.zero()
    for child in expected_children:
        expected_offsets[id(child)] = current_offset.to_fcpxml()
        if child.tag == "transition":
            continue
        duration = _parse_time(child.get("duration", "0s"), nonnegative=True)
        current_offset = _validate_result_time(
            current_offset + duration,
            nonnegative=True,
        )
    before_offsets = {
        id(child): child.get("offset") for child in before_children
    }
    result = modifier.reorder_clips(list(operation.clip_names))
    after_children = list(spine)
    if (
        result is not True
        or after_children != expected_children
        or any(
            child.get("offset") != expected_offsets[id(child)]
            for child in after_children
        )
        or _tree_state(
            modifier.root,
            ignored_attributes=ignored_offsets,
            ignored_children=frozenset({id(spine)}),
        )
        != unchanged_tree
    ):
        raise _ExecutionError(
            ErrorCode.OPERATION_FAILED,
            "reorder operation changed tree state inconsistently",
        )
    order_changed_elements = [
        element
        for name in operation.clip_names
        for element in (selected_elements[name],)
        if before_children.index(element) != after_children.index(element)
    ]
    offset_changed_elements = [
        child
        for child in after_children
        if before_offsets[id(child)] != expected_offsets[id(child)]
    ]
    changed_elements: list[ET.Element] = []
    changed_ids: set[int] = set()
    for child in [*order_changed_elements, *offset_changed_elements]:
        if id(child) not in changed_ids:
            changed_ids.add(id(child))
            changed_elements.append(child)
    base_labels = [
        child.get("name") or _entity_name(child, index)
        for index, child in enumerate(after_children, start=1)
    ]
    entity_labels = {
        id(child): (
            f"{label} [{index}]"
            if base_labels.count(label) > 1
            else label
        )
        for index, (child, label) in enumerate(
            zip(after_children, base_labels, strict=True),
            start=1,
        )
    }
    changed_names = [entity_labels[id(child)] for child in changed_elements]
    before = [child.get("name") or child.tag for child in before_children]
    after = [child.get("name") or child.tag for child in after_children]
    changes = []
    if before_children != after_children:
        changes.append(_change("order", ",".join(before), ",".join(after)))
    changes.extend(
        _change(
            f"offset:{entity_labels[id(child)]}",
            before_offsets[id(child)],
            expected_offsets[id(child)],
        )
        for child in offset_changed_elements
    )
    return _success_receipt(
        item,
        affected_entities=changed_names,
        changes=changes,
        warnings=(
            ("requested order and offsets were already satisfied",)
            if not changed_names
            else ()
        ),
    )


def _execute_add_transition(
    modifier: FCPXMLModifier,
    item: NormalizedOperationV1,
) -> OperationReceiptV1:
    operation = cast(AddTransitionOperation, item.operation)
    target = _named_element(modifier, operation.after_clip_name)
    if target is None:
        raise _ExecutionError(ErrorCode.OPERATION_FAILED, "preflight target disappeared")
    parent = modifier._find_parent(target)
    if parent is None:
        raise _ExecutionError(ErrorCode.OPERATION_FAILED, "preflight parent disappeared")
    before_transitions = {id(element) for element in parent.findall("transition")}
    before_children = list(parent)
    target_index = before_children.index(target)
    result = modifier.add_transition(
        operation.after_clip_name,
        operation.duration,
        operation.name,
        operation.ref,
    )
    added = [
        element
        for element in parent.findall("transition")
        if id(element) not in before_transitions
    ]
    expected_offset = (
        _parse_time(target.get("offset", "0s"))
        + _parse_time(target.get("duration", "0s"), positive=True)
    ).to_fcpxml()
    expected = {
        "offset": expected_offset,
        "duration": operation.duration,
        "name": operation.name,
    }
    if operation.ref:
        expected["ref"] = operation.ref
    if (
        result is not True
        or len(added) != 1
        or added[0].attrib != expected
        or list(parent)
        != [
            *before_children[: target_index + 1],
            added[0],
            *before_children[target_index + 1 :],
        ]
    ):
        raise _ExecutionError(
            ErrorCode.OPERATION_FAILED,
            "transition operation did not produce the requested effect",
        )
    return _success_receipt(
        item,
        affected_entities=[operation.after_clip_name],
        changes=[_change("transition_count", "0", "1")],
    )


def _execute_change_speed(
    modifier: FCPXMLModifier,
    item: NormalizedOperationV1,
) -> OperationReceiptV1:
    operation = cast(ChangeSpeedOperation, item.operation)
    target = _named_element(modifier, operation.clip_name)
    if target is None:
        raise _ExecutionError(ErrorCode.OPERATION_FAILED, "preflight target disappeared")
    before_duration = target.get("duration", "0s")
    old_duration = _parse_time(before_duration, positive=True)
    expected_duration = RationalTime(
        round(old_duration.numerator / operation.speed_factor),
        old_duration.denominator,
    ).to_fcpxml()
    result = modifier.change_speed(operation.clip_name, operation.speed_factor)
    time_map = target.find("timeMap")
    points = list(time_map.findall("timept")) if time_map is not None else []
    if (
        result is not True
        or target.get("duration") != expected_duration
        or len(points) != 2
        or points[0].attrib
        != {"time": "0s", "value": "0s", "interp": "smooth2"}
        or points[1].attrib
        != {
            "time": expected_duration,
            "value": old_duration.to_fcpxml(),
            "interp": "smooth2",
        }
    ):
        raise _ExecutionError(
            ErrorCode.OPERATION_FAILED,
            "speed operation did not produce the requested duration and time map",
        )
    return _success_receipt(
        item,
        affected_entities=[operation.clip_name],
        changes=[
            _change("duration", before_duration, expected_duration),
            _change("speed_factor", None, str(operation.speed_factor)),
        ],
    )


def _execute_assign_role(
    modifier: FCPXMLModifier,
    item: NormalizedOperationV1,
) -> OperationReceiptV1:
    operation = cast(AssignRoleOperation, item.operation)
    target = _named_element(modifier, operation.clip_name)
    if target is None:
        raise _ExecutionError(ErrorCode.OPERATION_FAILED, "preflight target disappeared")
    before = assigned_role(target)
    result = modifier.assign_role(operation.clip_name, operation.role)
    if result is not True or assigned_role(target) != operation.role:
        raise _ExecutionError(
            ErrorCode.OPERATION_FAILED,
            "role operation did not produce the requested effect",
        )
    changed = before != operation.role
    return _success_receipt(
        item,
        affected_entities=[operation.clip_name] if changed else [],
        changes=[_change("role", before, operation.role)] if changed else [],
        warnings=("requested role was already assigned",) if not changed else (),
    )


def _execute_batch_assign_roles(
    modifier: FCPXMLModifier,
    item: NormalizedOperationV1,
) -> OperationReceiptV1:
    operation = cast(BatchAssignRolesOperation, item.operation)
    editable_before = _editable_elements(modifier)
    before_roles = {
        id(element): assigned_role(element) for element in editable_before
    }
    matches: list[tuple[ET.Element, str, str | None, str]] = []
    for index, element in enumerate(editable_before, start=1):
        name = element.get("name", "")
        for rule in operation.rules:
            if rule.match.lower() in name.lower():
                matches.append(
                    (element, _entity_name(element, index), assigned_role(element), rule.role)
                )
                break
    result = modifier.batch_assign_roles(
        [{"match": rule.match, "role": rule.role} for rule in operation.rules]
    )
    expected_roles = dict(before_roles)
    for element, _, _, role in matches:
        expected_roles[id(element)] = role
    editable_after = _editable_elements(modifier)
    if (
        result != len(matches)
        or [id(element) for element in editable_after]
        != [id(element) for element in editable_before]
        or any(
            assigned_role(element) != expected_roles[id(element)]
            for element in editable_after
        )
    ):
        raise _ExecutionError(
            ErrorCode.OPERATION_FAILED,
            "batch role count or requested values did not match",
            affected_count=max(result, 0),
        )
    changes = [
        _change(f"role:{name}", before, after)
        for _, name, before, after in matches
        if before != after
    ]
    names = [
        name
        for _, name, before, after in matches
        if before != after
    ]
    if not matches:
        warnings = ("no clip names matched role rules",)
    elif not names:
        warnings = ("matching clips already had requested roles",)
    else:
        warnings = ()
    return _success_receipt(
        item,
        affected_entities=names,
        changes=changes,
        warnings=warnings,
    )


def _execute_batch_rename_clips(
    modifier: FCPXMLModifier,
    item: NormalizedOperationV1,
) -> OperationReceiptV1:
    operation = cast(BatchRenameClipsOperation, item.operation)
    matches = [
        (element, element.get("name", ""), element.get("name", "").replace(
            operation.pattern, operation.replacement
        ))
        for element in modifier.root.iter()
        if operation.pattern in element.get("name", "")
    ]
    result = modifier.batch_rename_clips(operation.pattern, operation.replacement)
    if result != len(matches) or any(element.get("name") != after for element, _, after in matches):
        raise _ExecutionError(
            ErrorCode.OPERATION_FAILED,
            "batch rename count or requested values did not match",
            affected_count=max(result, 0),
        )
    changed = [(before, after) for _, before, after in matches if before != after]
    return _success_receipt(
        item,
        affected_entities=[after for _, after in changed],
        changes=[
            _change("name", before, after)
            for before, after in changed
        ],
        warnings=("no clip names changed",) if not changed else (),
    )


def _execute_fill_gaps(
    modifier: FCPXMLModifier,
    item: NormalizedOperationV1,
) -> OperationReceiptV1:
    operation = cast(FillGapsOperation, item.operation)
    gaps = [
        gap
        for spine in modifier.root.iter("spine")
        for gap in spine.findall("gap")
    ]
    expected = [
        (
            gap.get("offset", "0s"),
            gap.get("duration", "0s"),
        )
        for gap in gaps
    ]
    before_ids = {id(element) for element in modifier.root.iter("asset-clip")}
    result = modifier.fill_gaps(operation.fill_ref, operation.fill_name)
    added = [
        element
        for element in modifier.root.iter("asset-clip")
        if id(element) not in before_ids
    ]
    if (
        result != len(gaps)
        or len(added) != len(gaps)
        or any(
            spine.find("gap") is not None
            for spine in modifier.root.iter("spine")
        )
        or any(
            element.attrib
            != {
                "ref": operation.fill_ref,
                "name": operation.fill_name,
                "offset": offset,
                "start": "0s",
                "duration": duration,
            }
            for element, (offset, duration) in zip(added, expected, strict=True)
        )
    ):
        raise _ExecutionError(
            ErrorCode.OPERATION_FAILED,
            "gap fill count or requested values did not match",
            affected_count=max(result, 0),
        )
    return _success_receipt(
        item,
        affected_entities=[operation.fill_name for _ in added],
        changes=[_change("gap_count", str(len(gaps)), "0")],
        warnings=("source contained no gaps",) if not gaps else (),
    )


def _execute_fix_flash_frames(
    modifier: FCPXMLModifier,
    item: NormalizedOperationV1,
) -> OperationReceiptV1:
    operation = cast(FixFlashFramesOperation, item.operation)
    minimum = _parse_time(operation.frame_duration, positive=True) * operation.min_frames
    matches: list[tuple[ET.Element, str, str]] = []
    spine_snapshots: list[tuple[ET.Element, list[ET.Element]]] = []
    element_index = 0
    for spine in modifier.root.iter("spine"):
        spine_snapshots.append((spine, list(spine)))
        for element in spine:
            element_index += 1
            if element.tag in {"gap", "transition"}:
                continue
            duration_text = element.get("duration", "0s")
            duration = _parse_time(duration_text, nonnegative=True)
            if duration < minimum and not duration.is_zero:
                matches.append((element, _entity_name(element, element_index), duration_text))
    match_ids = {id(element) for element, _, _ in matches}
    expected_attributes = {
        id(element): dict(element.attrib)
        for _, children in spine_snapshots
        for element in children
    }
    if matches:
        for _, children in spine_snapshots:
            current_offset = RationalTime.zero()
            for element in children:
                expected_attributes[id(element)]["offset"] = current_offset.to_fcpxml()
                if element.tag == "transition":
                    continue
                duration = _parse_time(
                    element.get("duration", "0s"),
                    nonnegative=True,
                )
                if id(element) in match_ids:
                    duration = minimum
                    expected_attributes[id(element)]["duration"] = minimum.to_fcpxml()
                current_offset = current_offset + duration
    result = modifier.fix_flash_frames(
        min_frames=operation.min_frames,
        frame_duration_str=operation.frame_duration,
    )
    expected_duration = minimum.to_fcpxml()
    if (
        result != len(matches)
        or any(list(spine) != children for spine, children in spine_snapshots)
        or any(
            element.attrib != expected_attributes[id(element)]
            for _, children in spine_snapshots
            for element in children
        )
    ):
        raise _ExecutionError(
            ErrorCode.OPERATION_FAILED,
            "flash-frame count or requested durations did not match",
            affected_count=max(result, 0),
        )
    return _success_receipt(
        item,
        affected_entities=[name for _, name, _ in matches],
        changes=[
            _change(f"duration:{name}", before, expected_duration)
            for _, name, before in matches
        ],
        warnings=("no flash frames required changes",) if not matches else (),
    )


def _execute_batch_apply_transition(
    modifier: FCPXMLModifier,
    item: NormalizedOperationV1,
) -> OperationReceiptV1:
    operation = cast(BatchApplyTransitionOperation, item.operation)
    expected_names: list[str] = []
    expected_pairs: list[
        tuple[ET.Element, ET.Element, dict[str, str]]
    ] = []
    spine_snapshots: dict[int, tuple[ET.Element, list[ET.Element]]] = {}
    before_ids = {id(element) for element in modifier.root.iter("transition")}
    for spine_index, spine in enumerate(modifier.root.iter("spine"), start=1):
        spine_snapshots[id(spine)] = (spine, list(spine))
        clips = [element for element in spine if element.tag != "transition"]
        for pair_index, (first, second) in enumerate(pairwise(clips), start=1):
            if first.tag == "gap" or second.tag == "gap":
                continue
            expected_names.append(
                first.get("name")
                or f"spine-{spine_index:03d}:adjacency-{pair_index:03d}"
            )
            attributes = {
                "offset": (
                    _parse_time(first.get("offset", "0s"))
                    + _parse_time(first.get("duration", "0s"), positive=True)
                ).to_fcpxml(),
                "duration": operation.duration,
                "name": operation.name,
            }
            if operation.ref:
                attributes["ref"] = operation.ref
            expected_pairs.append((spine, first, attributes))
    result = modifier.batch_apply_transition(
        operation.duration,
        operation.name,
        operation.ref,
    )
    added = [
        element
        for element in modifier.root.iter("transition")
        if id(element) not in before_ids
    ]
    added_ids = {id(element) for element in added}
    observed_ids: set[int] = set()
    placement_valid = True
    for spine, first, attributes in expected_pairs:
        children = list(spine)
        first_index = children.index(first)
        if first_index + 1 >= len(children):
            placement_valid = False
            break
        transition = children[first_index + 1]
        if (
            id(transition) not in added_ids
            or id(transition) in observed_ids
            or transition.attrib != attributes
        ):
            placement_valid = False
            break
        observed_ids.add(id(transition))
    membership_valid = all(
        [
            child
            for child in spine
            if id(child) not in added_ids
        ]
        == before
        for spine, before in spine_snapshots.values()
    )
    if (
        result != len(expected_names)
        or len(added) != len(expected_names)
        or observed_ids != added_ids
        or not placement_valid
        or not membership_valid
    ):
        raise _ExecutionError(
            ErrorCode.OPERATION_FAILED,
            "batch transition count or requested values did not match",
            affected_count=max(result, 0),
        )
    return _success_receipt(
        item,
        affected_entities=expected_names,
        changes=[_change("added_transition_count", "0", str(len(added)))],
        warnings=("source had no compatible clip adjacencies",) if not added else (),
    )


OperationExecutor = Callable[[FCPXMLModifier, NormalizedOperationV1], OperationReceiptV1]

OPERATION_EXECUTORS: Mapping[str, OperationExecutor] = MappingProxyType(
    {
        "add_marker": _execute_add_marker,
        "add_keyword": _execute_add_keyword,
        "trim_clip": _execute_trim_clip,
        "split_clip": _execute_split_clip,
        "delete_clips": _execute_delete_clips,
        "reorder_clips": _execute_reorder_clips,
        "add_transition": _execute_add_transition,
        "change_speed": _execute_change_speed,
        "assign_role": _execute_assign_role,
        "batch_assign_roles": _execute_batch_assign_roles,
        "batch_rename_clips": _execute_batch_rename_clips,
        "fill_gaps": _execute_fill_gaps,
        "fix_flash_frames": _execute_fix_flash_frames,
        "batch_apply_transition": _execute_batch_apply_transition,
    }
)


def execute_plan(
    source: str | Path,
    plan: WorkflowPlanV1,
) -> CandidateExecution:
    """Apply one validated plan in memory and return bytes only on full success."""
    modifier = FCPXMLModifier(source)
    inventory = build_source_inventory(modifier)
    try:
        normalized = normalize_plan(plan, inventory)
    except OperationPreflightError as error:
        item = error.normalized_plan.operations[error.operation_index]
        receipt = _failed_receipt(
            item,
            code=error.code,
            summary=error.summary,
        )
        return CandidateExecution(
            normalized_plan=error.normalized_plan,
            receipts=(receipt,),
            disposition=CandidateDisposition.FAILED,
            error_code=error.code,
            error_summary=error.summary,
        )

    receipts: list[OperationReceiptV1] = []
    for item in normalized.operations:
        executor = OPERATION_EXECUTORS[item.operation.kind]
        try:
            receipt = executor(modifier, item)
        except _ExecutionError as error:
            failed = _failed_receipt(
                item,
                code=error.code,
                summary=error.summary,
                affected_count=error.affected_count,
            )
            receipts.append(failed)
            return CandidateExecution(
                normalized_plan=normalized,
                receipts=tuple(receipts),
                disposition=CandidateDisposition.FAILED,
                error_code=error.code,
                error_summary=error.summary,
            )
        receipts.append(receipt)

    candidate = modifier.serialize()
    return CandidateExecution(
        normalized_plan=normalized,
        receipts=tuple(receipts),
        disposition=CandidateDisposition.SUCCEEDED,
        candidate_bytes=candidate,
    )


__all__ = [
    "OPERATION_EXECUTORS",
    "CandidateDisposition",
    "CandidateExecution",
    "ClipInventoryV1",
    "NormalizedOperationV1",
    "NormalizedPlanV1",
    "OperationPreflightError",
    "ResourceInventoryV1",
    "SourceInventoryV1",
    "SpineInventoryV1",
    "build_source_inventory",
    "execute_plan",
    "normalize_plan",
]
