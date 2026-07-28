"""FCPXML timeline analysis — pacing, gaps, flash frames, duplicates, stats."""

from __future__ import annotations

from dataclasses import dataclass, field
from math import fsum

from .models import Clip, FCPXMLDocument


@dataclass
class TimelineStats:
    """Summary statistics for a timeline."""
    project_name: str = ""
    total_duration_seconds: float = 0.0
    total_duration_timecode: str = "00:00:00:00"
    clip_count: int = 0
    non_gap_clip_count: int = 0
    gap_count: int = 0
    total_gap_duration_seconds: float = 0.0
    transition_count: int = 0
    marker_count: int = 0
    keyword_count: int = 0
    connected_clip_count: int = 0
    roles_used: list[str] = field(default_factory=list)
    fps: float = 0.0
    resolution: str = ""
    average_clip_duration_seconds: float = 0.0
    shortest_clip_seconds: float = 0.0
    longest_clip_seconds: float = 0.0


@dataclass
class PacingAnalysis:
    """Shot length distribution and pacing metrics."""
    average_shot_length: float = 0.0
    median_shot_length: float = 0.0
    std_deviation: float = 0.0
    shortest_shot: float = 0.0
    longest_shot: float = 0.0
    pacing_curve: list[float] = field(default_factory=list)  # shot durations in order
    histogram: dict[str, int] = field(default_factory=dict)  # duration bucket → count


@dataclass
class GapInfo:
    """Information about a gap in the timeline."""
    offset_seconds: float = 0.0
    offset_timecode: str = ""
    duration_seconds: float = 0.0
    before_clip: str = ""
    after_clip: str = ""


@dataclass
class FlashFrame:
    """A potentially problematic very-short clip."""
    clip_name: str = ""
    offset_seconds: float = 0.0
    offset_timecode: str = ""
    duration_seconds: float = 0.0
    frame_count: int = 0


@dataclass
class DuplicateGroup:
    """A group of clips using the same source media."""
    asset_ref: str = ""
    asset_name: str = ""
    occurrences: list[dict] = field(default_factory=list)  # [{name, offset, duration}, ...]


def analyze_timeline_stats(doc: FCPXMLDocument) -> list[TimelineStats]:
    """Generate statistics for all timelines in the document."""
    results = []
    for project in doc.all_projects:
        if not project.sequence or not project.sequence.spine:
            continue

        seq = project.sequence
        spine = seq.spine
        fps = 29.97

        # Get format info
        fmt = doc.formats.get(seq.format_ref)
        resolution = ""
        if fmt:
            fps = fmt.fps
            resolution = f"{fmt.width}x{fmt.height}"

        clips = spine.clips
        non_gaps = spine.non_gap_clips
        durations = [c.duration.to_seconds() for c in non_gaps]

        stats = TimelineStats(
            project_name=project.name,
            total_duration_seconds=seq.duration.to_seconds() if not seq.duration.is_zero else spine.duration.to_seconds(),
            total_duration_timecode=seq.duration.to_timecode(fps) if not seq.duration.is_zero else spine.duration.to_timecode(fps),
            clip_count=len(clips),
            non_gap_clip_count=len(non_gaps),
            gap_count=sum(1 for c in clips if c.is_gap),
            total_gap_duration_seconds=sum(c.duration.to_seconds() for c in clips if c.is_gap),
            marker_count=sum(len(c.markers) for c in clips),
            keyword_count=sum(len(c.keywords) for c in clips),
            connected_clip_count=sum(len(c.connected_clips) for c in clips),
            roles_used=sorted({c.role for c in clips if c.role}),
            fps=fps,
            resolution=resolution,
            average_clip_duration_seconds=fsum(durations) / len(durations) if durations else 0,
            shortest_clip_seconds=min(durations) if durations else 0,
            longest_clip_seconds=max(durations) if durations else 0,
        )
        results.append(stats)
    return results


def analyze_pacing(doc: FCPXMLDocument) -> list[PacingAnalysis]:
    """Analyze shot pacing for all timelines."""
    results = []
    for project in doc.all_projects:
        if not project.sequence or not project.sequence.spine:
            continue

        non_gaps = project.sequence.spine.non_gap_clips
        durations = [c.duration.to_seconds() for c in non_gaps]

        if not durations:
            results.append(PacingAnalysis())
            continue

        sorted_durs = sorted(durations)
        n = len(sorted_durs)
        median = sorted_durs[n // 2] if n % 2 == 1 else (sorted_durs[n // 2 - 1] + sorted_durs[n // 2]) / 2
        avg = fsum(durations) / n
        variance = sum((d - avg) ** 2 for d in durations) / n
        std_dev = variance ** 0.5

        # Histogram buckets
        buckets = {"< 1s": 0, "1-3s": 0, "3-5s": 0, "5-10s": 0, "10-30s": 0, "30s+": 0}
        for d in durations:
            if d < 1:
                buckets["< 1s"] += 1
            elif d < 3:
                buckets["1-3s"] += 1
            elif d < 5:
                buckets["3-5s"] += 1
            elif d < 10:
                buckets["5-10s"] += 1
            elif d < 30:
                buckets["10-30s"] += 1
            else:
                buckets["30s+"] += 1

        results.append(PacingAnalysis(
            average_shot_length=avg,
            median_shot_length=median,
            std_deviation=std_dev,
            shortest_shot=sorted_durs[0],
            longest_shot=sorted_durs[-1],
            pacing_curve=durations,
            histogram=buckets,
        ))
    return results


def detect_gaps(doc: FCPXMLDocument, fps: float = 29.97) -> list[GapInfo]:
    """Find all gaps in timelines."""
    gaps = []
    for project in doc.all_projects:
        if not project.sequence or not project.sequence.spine:
            continue

        clips = project.sequence.spine.clips
        for i, clip in enumerate(clips):
            if clip.is_gap:
                before = clips[i - 1].name if i > 0 else "(start)"
                after = clips[i + 1].name if i < len(clips) - 1 else "(end)"
                gaps.append(GapInfo(
                    offset_seconds=clip.offset.to_seconds(),
                    offset_timecode=clip.offset.to_timecode(fps),
                    duration_seconds=clip.duration.to_seconds(),
                    before_clip=before,
                    after_clip=after,
                ))
    return gaps


def detect_flash_frames(
    doc: FCPXMLDocument,
    max_frames: int = 2,
    fps: float = 29.97,
) -> list[FlashFrame]:
    """Find clips shorter than max_frames."""
    max_duration = max_frames / fps
    flashes = []
    for project in doc.all_projects:
        if not project.sequence or not project.sequence.spine:
            continue

        for clip in project.sequence.spine.non_gap_clips:
            dur_s = clip.duration.to_seconds()
            if dur_s <= max_duration and dur_s > 0:
                flashes.append(FlashFrame(
                    clip_name=clip.name,
                    offset_seconds=clip.offset.to_seconds(),
                    offset_timecode=clip.offset.to_timecode(fps),
                    duration_seconds=dur_s,
                    frame_count=round(dur_s * fps),
                ))
    return flashes


def detect_duplicates(doc: FCPXMLDocument) -> list[DuplicateGroup]:
    """Find clips that reference the same source asset."""
    ref_groups: dict[str, list[Clip]] = {}
    for project in doc.all_projects:
        if not project.sequence or not project.sequence.spine:
            continue

        for clip in project.sequence.spine.non_gap_clips:
            if clip.ref:
                ref_groups.setdefault(clip.ref, []).append(clip)

    duplicates = []
    for ref, clips in ref_groups.items():
        if len(clips) > 1:
            asset = doc.assets.get(ref)
            duplicates.append(DuplicateGroup(
                asset_ref=ref,
                asset_name=asset.name if asset else ref,
                occurrences=[
                    {
                        "name": c.name,
                        "offset_seconds": c.offset.to_seconds(),
                        "duration_seconds": c.duration.to_seconds(),
                    }
                    for c in clips
                ],
            ))
    return duplicates
