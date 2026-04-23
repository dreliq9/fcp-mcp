"""FCPXML diff engine — compare two FCPXML documents."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Union

from .parser import FCPXMLParser
from .models import Clip, FCPXMLDocument


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
        for key in keys_a - keys_b:
            info = clips_a[key]
            result.changes.append(ClipChange("removed", info["name"],
                                             f"at {info['offset']:.2f}s"))
            result.clips_removed += 1

        # Added clips
        for key in keys_b - keys_a:
            info = clips_b[key]
            result.changes.append(ClipChange("added", info["name"],
                                             f"at {info['offset']:.2f}s"))
            result.clips_added += 1

        # Check for modifications in common clips
        for key in keys_a & keys_b:
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


def diff_files(path_a: Union[str, Path], path_b: Union[str, Path]) -> list[DiffResult]:
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
