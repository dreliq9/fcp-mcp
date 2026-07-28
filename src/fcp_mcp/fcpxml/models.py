"""Data models for FCPXML elements.

All timeline data is represented as frozen dataclasses with RationalTime values.
These models are the internal representation — parsers create them, writers consume them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .time_utils import RationalTime


class ClipType(Enum):
    ASSET_CLIP = "asset-clip"
    CLIP = "clip"
    GAP = "gap"
    TITLE = "title"
    GENERATOR = "generator"
    COMPOUND_CLIP = "compound-clip"
    MULTICAM_CLIP = "mc-clip"
    SYNC_CLIP = "sync-clip"
    AUDITION = "audition"
    VIDEO = "video"
    AUDIO = "audio"
    REF_CLIP = "ref-clip"


class MarkerType(Enum):
    STANDARD = "marker"
    CHAPTER = "chapter-marker"
    TODO = "todo"


class TimecodeFormat(Enum):
    NON_DROP_FRAME = "NDF"
    DROP_FRAME = "DF"


@dataclass
class Format:
    """Video format definition."""
    id: str
    name: str = ""
    width: int = 0
    height: int = 0
    frame_duration: RationalTime = field(default_factory=lambda: RationalTime(1001, 30000))
    color_space: str = ""

    @property
    def fps(self) -> float:
        if self.frame_duration.numerator == 0:
            return 0.0
        return self.frame_duration.denominator / self.frame_duration.numerator


@dataclass
class Asset:
    """Source media asset."""
    id: str
    src: str = ""
    name: str = ""
    start: RationalTime = field(default_factory=RationalTime.zero)
    duration: RationalTime = field(default_factory=RationalTime.zero)
    has_video: bool = True
    has_audio: bool = True
    format_ref: str = ""
    audio_sources: int = 1
    audio_channels: int = 2


@dataclass
class Effect:
    """Effect/transition/generator resource."""
    id: str
    name: str = ""
    uid: str = ""
    src: str = ""


@dataclass
class Marker:
    """Timeline marker."""
    start: RationalTime = field(default_factory=RationalTime.zero)
    duration: RationalTime = field(default_factory=RationalTime.zero)
    value: str = ""
    note: str = ""
    marker_type: MarkerType = MarkerType.STANDARD
    completed: bool = False  # for TODO markers


@dataclass
class Keyword:
    """Keyword range on a clip."""
    start: RationalTime = field(default_factory=RationalTime.zero)
    duration: RationalTime = field(default_factory=RationalTime.zero)
    value: str = ""


@dataclass
class TransformParams:
    """Spatial transform parameters."""
    position_x: float = 0.0
    position_y: float = 0.0
    scale: float = 1.0
    rotation: float = 0.0
    anchor_x: float = 0.0
    anchor_y: float = 0.0
    scale_y: float | None = None

    def __post_init__(self) -> None:
        if self.scale_y is None:
            self.scale_y = self.scale

    @property
    def scale_x(self) -> float:
        """Horizontal scale, preserving the original scalar API as an alias."""
        return self.scale


@dataclass
class VolumeParams:
    """Audio volume settings."""
    amount: str = "0dB"  # in dB, e.g. "0dB", "-6dB", "+3dB"


@dataclass
class AppliedEffect:
    """An effect applied to a clip."""
    ref: str = ""
    name: str = ""
    enabled: bool = True
    parameters: dict[str, Any] = field(default_factory=dict)


@dataclass
class Transition:
    """A transition between clips."""
    offset: RationalTime = field(default_factory=RationalTime.zero)
    duration: RationalTime = field(default_factory=RationalTime.zero)
    name: str = ""
    ref: str = ""


@dataclass
class Clip:
    """A clip on the timeline (any type)."""
    clip_type: ClipType = ClipType.ASSET_CLIP
    name: str = ""
    ref: str = ""  # reference to asset/effect
    offset: RationalTime = field(default_factory=RationalTime.zero)
    start: RationalTime = field(default_factory=RationalTime.zero)
    duration: RationalTime = field(default_factory=RationalTime.zero)
    role: str = ""
    lane: int = 0  # 0 = primary storyline, positive = above, negative = below
    enabled: bool = True

    # Nested content
    markers: list[Marker] = field(default_factory=list)
    keywords: list[Keyword] = field(default_factory=list)
    effects: list[AppliedEffect] = field(default_factory=list)
    transform: TransformParams | None = None
    volume: VolumeParams | None = None
    connected_clips: list[Clip] = field(default_factory=list)
    spine_clips: list[Clip] = field(default_factory=list)  # for compound clips

    # Raw XML element reference (for round-tripping attributes we don't model)
    _raw_attribs: dict[str, str] = field(default_factory=dict)

    @property
    def end_offset(self) -> RationalTime:
        """Calculate end position on timeline."""
        return self.offset + self.duration

    @property
    def source_end(self) -> RationalTime:
        """Calculate source end point."""
        return self.start + self.duration

    @property
    def duration_seconds(self) -> float:
        return self.duration.to_seconds()

    @property
    def is_gap(self) -> bool:
        return self.clip_type == ClipType.GAP


@dataclass
class Spine:
    """Primary storyline — ordered sequence of clips."""
    clips: list[Clip] = field(default_factory=list)

    @property
    def duration(self) -> RationalTime:
        """Total duration of all clips in spine."""
        if not self.clips:
            return RationalTime.zero()
        total = RationalTime.zero()
        for clip in self.clips:
            total = total + clip.duration
        return total

    @property
    def clip_count(self) -> int:
        return len(self.clips)

    @property
    def non_gap_clips(self) -> list[Clip]:
        return [c for c in self.clips if not c.is_gap]


@dataclass
class Sequence:
    """A timeline/sequence containing a spine."""
    name: str = ""
    uid: str = ""
    format_ref: str = ""
    duration: RationalTime = field(default_factory=RationalTime.zero)
    tc_start: RationalTime = field(default_factory=RationalTime.zero)
    tc_format: TimecodeFormat = TimecodeFormat.NON_DROP_FRAME
    spine: Spine = field(default_factory=Spine)

    @property
    def frame_duration(self) -> RationalTime | None:
        """Get frame duration from format reference (set during parsing)."""
        return getattr(self, "_frame_duration", None)


@dataclass
class Project:
    """A project containing a sequence."""
    name: str = ""
    uid: str = ""
    mod_date: str = ""
    sequence: Sequence | None = None


@dataclass
class Event:
    """An event containing projects."""
    name: str = ""
    uid: str = ""
    projects: list[Project] = field(default_factory=list)


@dataclass
class Library:
    """A library containing events."""
    name: str = ""
    location: str = ""
    events: list[Event] = field(default_factory=list)


@dataclass
class FCPXMLDocument:
    """Root document representing a parsed FCPXML file."""
    version: str = "1.11"
    resources: dict[str, Any] = field(default_factory=dict)  # id → Format/Asset/Effect
    formats: dict[str, Format] = field(default_factory=dict)
    assets: dict[str, Asset] = field(default_factory=dict)
    effects: dict[str, Effect] = field(default_factory=dict)
    library: Library | None = None

    # If the FCPXML contains standalone events/projects (no library wrapper)
    events: list[Event] = field(default_factory=list)
    projects: list[Project] = field(default_factory=list)

    @property
    def all_projects(self) -> list[Project]:
        """Get all projects from library or standalone."""
        result = list(self.projects)
        if self.library:
            for event in self.library.events:
                result.extend(event.projects)
        for event in self.events:
            result.extend(event.projects)
        return result

    @property
    def all_clips(self) -> list[Clip]:
        """Get all clips from all timelines."""
        clips = []
        for project in self.all_projects:
            if project.sequence and project.sequence.spine:
                clips.extend(project.sequence.spine.clips)
        return clips
