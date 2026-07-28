"""FCPXML diff engine — compare two FCPXML documents."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from .models import Clip, FCPXMLDocument
from .parser import FCPXMLParser


@dataclass
class ClipChange:
    """A change detected between two timelines."""
    change_type: str  # "added", "removed", "moved", "trimmed", "renamed"
    clip_name: str = ""
    details: str = ""


@dataclass
class DiffResult:
    """Result of comparing two FCPXML documents."""
    project_a: str = ""
    project_b: str = ""
    changes: list[ClipChange] = field(default_factory=list)
    clips_added: int = 0
    clips_removed: int = 0
    clips_moved: int = 0
    clips_trimmed: int = 0
    clips_unchanged: int = 0

    def summary(self) -> str:
        lines = [
            f"Diff: '{self.project_a}' vs '{self.project_b}'",
            f"  Added: {self.clips_added}",
            f"  Removed: {self.clips_removed}",
            f"  Moved: {self.clips_moved}",
            f"  Trimmed: {self.clips_trimmed}",
            f"  Unchanged: {self.clips_unchanged}",
        ]
        if self.changes:
            lines.append("  Changes:")
            for c in self.changes:
                lines.append(f"    [{c.change_type}] {c.clip_name}: {c.details}")
        return "\n".join(lines)


def diff_documents(doc_a: FCPXMLDocument, doc_b: FCPXMLDocument) -> list[DiffResult]:
    """Compare two FCPXML documents, matching projects by name."""
    results = []

    projects_a = {p.name: p for p in doc_a.all_projects}
    projects_b = {p.name: p for p in doc_b.all_projects}

    all_names = set(projects_a.keys()) | set(projects_b.keys())

    for name in sorted(all_names):
        result = DiffResult(project_a=name, project_b=name)

        if name not in projects_a:
            result.changes.append(ClipChange("added", name, "Entire project added"))
            results.append(result)
            continue
        if name not in projects_b:
            result.changes.append(ClipChange("removed", name, "Entire project removed"))
            results.append(result)
            continue

        pa = projects_a[name]
        pb = projects_b[name]

        clips_a = _clip_fingerprints(pa)
        clips_b = _clip_fingerprints(pb)

        keys_a = set(clips_a.keys())
        keys_b = set(clips_b.keys())

        # Removed clips
        for key in sorted(keys_a - keys_b):
            info = clips_a[key]
            result.changes.append(ClipChange("removed", info["name"],
                                             f"at {info['offset']:.2f}s"))
            result.clips_removed += 1

        # Added clips
        for key in sorted(keys_b - keys_a):
            info = clips_b[key]
            result.changes.append(ClipChange("added", info["name"],
                                             f"at {info['offset']:.2f}s"))
            result.clips_added += 1

        # Check for modifications in common clips
        for key in sorted(keys_a & keys_b):
            a = clips_a[key]
            b = clips_b[key]

            if abs(a["offset"] - b["offset"]) > 0.001:
                result.changes.append(ClipChange(
                    "moved", a["name"],
                    f"from {a['offset']:.2f}s to {b['offset']:.2f}s"))
                result.clips_moved += 1
            elif abs(a["duration"] - b["duration"]) > 0.001:
                result.changes.append(ClipChange(
                    "trimmed", a["name"],
                    f"duration {a['duration']:.2f}s → {b['duration']:.2f}s"))
                result.clips_trimmed += 1
            else:
                result.clips_unchanged += 1

        results.append(result)

    return results


def diff_files(path_a: str | Path, path_b: str | Path) -> list[DiffResult]:
    """Compare two FCPXML files."""
    parser = FCPXMLParser()
    doc_a = parser.parse(path_a)
    doc_b = parser.parse(path_b)
    return diff_documents(doc_a, doc_b)


def _clip_fingerprint(clip: Clip) -> str:
    """Create a unique-ish fingerprint for a clip based on ref + source range."""
    return f"{clip.ref}:{clip.start.to_fcpxml()}:{clip.clip_type.value}"


def _clip_fingerprints(project) -> dict[str, dict]:
    """Build fingerprint → info map for all clips in a project."""
    result = {}
    if not project.sequence or not project.sequence.spine:
        return result

    for i, clip in enumerate(project.sequence.spine.clips):
        if clip.is_gap:
            continue
        key = f"{_clip_fingerprint(clip)}_{i}"
        result[key] = {
            "name": clip.name,
            "offset": clip.offset.to_seconds(),
            "duration": clip.duration.to_seconds(),
            "start": clip.start.to_seconds(),
        }
    return result


WorkflowChangeType = Literal["added", "removed", "changed"]
_MAX_WORKFLOW_DIFF_TEXT = 4096


def _workflow_diff_text(value: object, *, fallback: str = "") -> str:
    text = value if isinstance(value, str) else str(value)
    return (text or fallback)[:_MAX_WORKFLOW_DIFF_TEXT]


@dataclass(frozen=True)
class WorkflowDiffChange:
    """One deterministic, duplicate-aware semantic FCPXML change."""

    project_name: str
    project_occurrence: int
    source_project_index: int | None
    candidate_project_index: int | None
    entity_kind: str
    entity_name: str
    entity_occurrence: int
    source_entity_index: int | None
    candidate_entity_index: int | None
    change_type: WorkflowChangeType
    field: str
    before: str | None = None
    after: str | None = None

    def sort_key(self) -> tuple[object, ...]:
        missing = 2**31 - 1
        return (
            self.project_name,
            self.project_occurrence,
            self.source_project_index
            if self.source_project_index is not None
            else missing,
            self.candidate_project_index
            if self.candidate_project_index is not None
            else missing,
            self.entity_kind,
            self.entity_name,
            self.entity_occurrence,
            self.source_entity_index
            if self.source_entity_index is not None
            else missing,
            self.candidate_entity_index
            if self.candidate_entity_index is not None
            else missing,
            self.change_type,
            self.field,
            self.before or "",
            self.after or "",
        )

    def to_mapping(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class WorkflowDocumentDiff:
    """Complete typed workflow semantic diff, independent from legacy text."""

    schema_version: str
    project_count_source: int
    project_count_candidate: int
    change_count: int
    changes: tuple[WorkflowDiffChange, ...]

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "project_count_source": self.project_count_source,
            "project_count_candidate": self.project_count_candidate,
            "change_count": self.change_count,
            "changes": [change.to_mapping() for change in self.changes],
        }


def append_operation_effects(
    document_diff: WorkflowDocumentDiff,
    receipts: Iterable[object],
) -> WorkflowDocumentDiff:
    """Bind verified executor effects into the typed diff.

    The document model deliberately omits some FCPXML constructs, including
    transitions and time maps. Successful operation receipts are produced only
    after the executor verifies the exact requested mutation, so their bounded
    value changes provide deterministic coverage for those effects as well as
    an audit link for modeled effects.
    """
    changes = list(document_diff.changes)
    for receipt_index, receipt in enumerate(receipts, start=1):
        operation_id = _workflow_diff_text(
            getattr(receipt, "operation_id", ""),
            fallback=f"operation-{receipt_index}",
        )
        kind = _workflow_diff_text(
            getattr(receipt, "kind", ""),
            fallback="unknown",
        )
        for change_index, value_change in enumerate(
            getattr(receipt, "changes", ()),
            start=1,
        ):
            before = getattr(value_change, "before", None)
            after = getattr(value_change, "after", None)
            changes.append(
                _entity_change(
                    project_name="<workflow>",
                    project_occurrence=1,
                    source_project_index=None,
                    candidate_project_index=None,
                    entity_kind="operation_effect",
                    entity_name=operation_id,
                    entity_occurrence=receipt_index,
                    source_entity_index=None,
                    candidate_entity_index=change_index - 1,
                    change_type=(
                        "added"
                        if before is None
                        else "removed"
                        if after is None
                        else "changed"
                    ),
                    field=f"{kind}.{getattr(value_change, 'field', 'effect')}",
                    before=before,
                    after=after,
                )
            )
    ordered = tuple(sorted(changes, key=WorkflowDiffChange.sort_key))
    return WorkflowDocumentDiff(
        schema_version=document_diff.schema_version,
        project_count_source=document_diff.project_count_source,
        project_count_candidate=document_diff.project_count_candidate,
        change_count=len(ordered),
        changes=ordered,
    )


def _project_occurrences(
    document: FCPXMLDocument,
) -> list[tuple[int, str, int, object]]:
    counts: dict[str, int] = {}
    result = []
    for index, project in enumerate(document.all_projects):
        name = _workflow_diff_text(project.name, fallback="<unnamed-project>")
        occurrence = counts.get(name, 0) + 1
        counts[name] = occurrence
        result.append((index, name, occurrence, project))
    return result


def _project_clips(project: object) -> list[Clip]:
    sequence = getattr(project, "sequence", None)
    spine = getattr(sequence, "spine", None)
    if spine is None:
        return []
    return list(spine.clips)


def _clip_occurrences(clips: list[Clip]) -> list[tuple[int, str, int, Clip]]:
    counts: dict[tuple[str, str], int] = {}
    result = []
    for index, clip in enumerate(clips):
        name = _workflow_diff_text(clip.name, fallback="<unnamed-entity>")
        key = (clip.clip_type.value, name)
        occurrence = counts.get(key, 0) + 1
        counts[key] = occurrence
        result.append((index, name, occurrence, clip))
    return result


def _clip_fields(clip: Clip) -> tuple[tuple[str, str], ...]:
    # Rational strings are the identity. Deliberately never convert to floats.
    return (
        ("kind", clip.clip_type.value),
        ("name", _workflow_diff_text(clip.name)),
        ("ref", _workflow_diff_text(clip.ref)),
        ("offset", clip.offset.to_fcpxml()),
        ("start", clip.start.to_fcpxml()),
        ("duration", clip.duration.to_fcpxml()),
        ("role", _workflow_diff_text(clip.role)),
        ("lane", str(clip.lane)),
        ("enabled", "true" if clip.enabled else "false"),
    )


def _nested_entities(
    clip: Clip,
    kind: Literal["marker", "keyword"],
) -> dict[tuple[tuple[str, ...], int], tuple[int, str, str]]:
    items = clip.markers if kind == "marker" else clip.keywords
    counts: dict[tuple[str, ...], int] = {}
    result: dict[tuple[tuple[str, ...], int], tuple[int, str, str]] = {}
    for index, item in enumerate(items):
        if kind == "marker":
            identity = (
                item.start.to_fcpxml(),
                item.duration.to_fcpxml(),
                _workflow_diff_text(item.value),
                _workflow_diff_text(item.note),
                item.marker_type.value,
                "true" if item.completed else "false",
            )
            rendered = (
                f"start={identity[0]};duration={identity[1]};value={identity[2]};"
                f"note={identity[3]};type={identity[4]};completed={identity[5]}"
            )
            name = identity[2] or "<unnamed-marker>"
        else:
            identity = (
                item.start.to_fcpxml(),
                item.duration.to_fcpxml(),
                _workflow_diff_text(item.value),
            )
            rendered = (
                f"start={identity[0]};duration={identity[1]};value={identity[2]}"
            )
            name = identity[2] or "<unnamed-keyword>"
        occurrence = counts.get(identity, 0) + 1
        counts[identity] = occurrence
        result[(identity, occurrence)] = (
            index,
            name,
            _workflow_diff_text(rendered),
        )
    return result


def _nested_changes(
    *,
    before_clip: Clip,
    after_clip: Clip,
    kind: Literal["marker", "keyword"],
    project_name: str,
    project_occurrence: int,
    source_project_index: int,
    candidate_project_index: int,
) -> list[WorkflowDiffChange]:
    before = _nested_entities(before_clip, kind)
    after = _nested_entities(after_clip, kind)
    changes: list[WorkflowDiffChange] = []
    for identity, occurrence in sorted(set(before) | set(after)):
        before_entry = before.get((identity, occurrence))
        after_entry = after.get((identity, occurrence))
        if before_entry is not None and after_entry is not None:
            continue
        entry = before_entry or after_entry
        assert entry is not None
        changes.append(
            _entity_change(
                project_name=project_name,
                project_occurrence=project_occurrence,
                source_project_index=source_project_index,
                candidate_project_index=candidate_project_index,
                entity_kind=kind,
                entity_name=entry[1],
                entity_occurrence=occurrence,
                source_entity_index=(
                    before_entry[0] if before_entry is not None else None
                ),
                candidate_entity_index=(
                    after_entry[0] if after_entry is not None else None
                ),
                change_type="added" if before_entry is None else "removed",
                field="entity",
                before=before_entry[2] if before_entry is not None else None,
                after=after_entry[2] if after_entry is not None else None,
            )
        )
    return changes


def _entity_change(
    *,
    project_name: str,
    project_occurrence: int,
    source_project_index: int | None,
    candidate_project_index: int | None,
    entity_kind: str,
    entity_name: str,
    entity_occurrence: int,
    source_entity_index: int | None,
    candidate_entity_index: int | None,
    change_type: WorkflowChangeType,
    field: str,
    before: str | None = None,
    after: str | None = None,
) -> WorkflowDiffChange:
    return WorkflowDiffChange(
        project_name=project_name,
        project_occurrence=project_occurrence,
        source_project_index=source_project_index,
        candidate_project_index=candidate_project_index,
        entity_kind=entity_kind,
        entity_name=entity_name,
        entity_occurrence=entity_occurrence,
        source_entity_index=source_entity_index,
        candidate_entity_index=candidate_entity_index,
        change_type=change_type,
        field=field,
        before=before,
        after=after,
    )


def build_workflow_diff(
    source: FCPXMLDocument,
    candidate: FCPXMLDocument,
) -> WorkflowDocumentDiff:
    """Build a complete deterministic diff over the modeled workflow domain."""
    source_projects = {
        (name, occurrence): (index, project)
        for index, name, occurrence, project in _project_occurrences(source)
    }
    candidate_projects = {
        (name, occurrence): (index, project)
        for index, name, occurrence, project in _project_occurrences(candidate)
    }
    changes: list[WorkflowDiffChange] = []
    for project_name, project_occurrence in sorted(
        set(source_projects) | set(candidate_projects)
    ):
        source_entry = source_projects.get((project_name, project_occurrence))
        candidate_entry = candidate_projects.get((project_name, project_occurrence))
        source_project_index = source_entry[0] if source_entry is not None else None
        candidate_project_index = candidate_entry[0] if candidate_entry is not None else None
        if source_entry is None or candidate_entry is None:
            changes.append(
                _entity_change(
                    project_name=project_name,
                    project_occurrence=project_occurrence,
                    source_project_index=source_project_index,
                    candidate_project_index=candidate_project_index,
                    entity_kind="project",
                    entity_name=project_name,
                    entity_occurrence=project_occurrence,
                    source_entity_index=None,
                    candidate_entity_index=None,
                    change_type="added" if source_entry is None else "removed",
                    field="project",
                    before=None if source_entry is None else project_name,
                    after=project_name if source_entry is None else None,
                )
            )
            continue

        source_clips = {
            (clip.clip_type.value, name, occurrence): (index, clip)
            for index, name, occurrence, clip in _clip_occurrences(
                _project_clips(source_entry[1])
            )
        }
        candidate_clips = {
            (clip.clip_type.value, name, occurrence): (index, clip)
            for index, name, occurrence, clip in _clip_occurrences(
                _project_clips(candidate_entry[1])
            )
        }
        for entity_kind, entity_name, entity_occurrence in sorted(
            set(source_clips) | set(candidate_clips)
        ):
            before_entry = source_clips.get(
                (entity_kind, entity_name, entity_occurrence)
            )
            after_entry = candidate_clips.get(
                (entity_kind, entity_name, entity_occurrence)
            )
            if before_entry is None or after_entry is None:
                changes.append(
                    _entity_change(
                        project_name=project_name,
                        project_occurrence=project_occurrence,
                        source_project_index=source_project_index,
                        candidate_project_index=candidate_project_index,
                        entity_kind=entity_kind,
                        entity_name=entity_name,
                        entity_occurrence=entity_occurrence,
                        source_entity_index=(
                            before_entry[0] if before_entry is not None else None
                        ),
                        candidate_entity_index=(
                            after_entry[0] if after_entry is not None else None
                        ),
                        change_type="added" if before_entry is None else "removed",
                        field="entity",
                        before=None if before_entry is None else entity_name,
                        after=entity_name if before_entry is None else None,
                    )
                )
                continue
            before_fields = dict(_clip_fields(before_entry[1]))
            after_fields = dict(_clip_fields(after_entry[1]))
            if before_entry[0] != after_entry[0]:
                changes.append(
                    _entity_change(
                        project_name=project_name,
                        project_occurrence=project_occurrence,
                        source_project_index=source_project_index,
                        candidate_project_index=candidate_project_index,
                        entity_kind=entity_kind,
                        entity_name=entity_name,
                        entity_occurrence=entity_occurrence,
                        source_entity_index=before_entry[0],
                        candidate_entity_index=after_entry[0],
                        change_type="changed",
                        field="position",
                        before=str(before_entry[0]),
                        after=str(after_entry[0]),
                    )
                )
            for field_name in sorted(before_fields.keys() | after_fields.keys()):
                before = before_fields.get(field_name)
                after = after_fields.get(field_name)
                if before == after:
                    continue
                changes.append(
                    _entity_change(
                        project_name=project_name,
                        project_occurrence=project_occurrence,
                        source_project_index=source_project_index,
                        candidate_project_index=candidate_project_index,
                        entity_kind=entity_kind,
                        entity_name=entity_name,
                        entity_occurrence=entity_occurrence,
                        source_entity_index=before_entry[0],
                        candidate_entity_index=after_entry[0],
                        change_type="changed",
                        field=field_name,
                        before=before,
                        after=after,
                    )
                )
            for nested_kind in ("marker", "keyword"):
                changes.extend(
                    _nested_changes(
                        before_clip=before_entry[1],
                        after_clip=after_entry[1],
                        kind=nested_kind,
                        project_name=project_name,
                        project_occurrence=project_occurrence,
                        source_project_index=source_project_index,
                        candidate_project_index=candidate_project_index,
                    )
                )
    ordered = tuple(sorted(changes, key=WorkflowDiffChange.sort_key))
    return WorkflowDocumentDiff(
        schema_version="1",
        project_count_source=len(source.all_projects),
        project_count_candidate=len(candidate.all_projects),
        change_count=len(ordered),
        changes=ordered,
    )
