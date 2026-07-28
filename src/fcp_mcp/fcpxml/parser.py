"""FCPXML parser — converts FCPXML files into Python data models.

Supports FCPXML versions 1.6–1.11.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from typing import ClassVar

from ..utils.safe_xml import parse_fcpxml, parse_string
from .models import (
    AppliedEffect,
    Asset,
    Clip,
    ClipType,
    Effect,
    Event,
    FCPXMLDocument,
    Format,
    Keyword,
    Library,
    Marker,
    MarkerType,
    Project,
    Sequence,
    Spine,
    TimecodeFormat,
    TransformParams,
    VolumeParams,
)
from .time_utils import RationalTime


class FCPXMLParser:
    """Parse FCPXML files into FCPXMLDocument objects."""

    # Map XML tag names to ClipType
    TAG_TO_CLIP_TYPE: ClassVar[dict[str, ClipType]] = {
        "asset-clip": ClipType.ASSET_CLIP,
        "clip": ClipType.CLIP,
        "gap": ClipType.GAP,
        "title": ClipType.TITLE,
        "generator": ClipType.GENERATOR,
        "compound-clip": ClipType.COMPOUND_CLIP,
        "mc-clip": ClipType.MULTICAM_CLIP,
        "sync-clip": ClipType.SYNC_CLIP,
        "audition": ClipType.AUDITION,
        "video": ClipType.VIDEO,
        "audio": ClipType.AUDIO,
        "ref-clip": ClipType.REF_CLIP,
    }

    CLIP_TAGS: ClassVar[frozenset[str]] = frozenset(TAG_TO_CLIP_TYPE)

    def parse(self, path: str | Path) -> FCPXMLDocument:
        """Parse an FCPXML file and return a document model."""
        tree = parse_fcpxml(path)
        return self._parse_root(tree.getroot())

    def parse_bytes(self, payload: bytes) -> FCPXMLDocument:
        """Parse in-memory FCPXML bytes without creating a temporary file."""
        if not isinstance(payload, bytes):
            raise TypeError("FCPXML payload must be bytes")
        return self._parse_root(parse_string(payload))

    def _parse_root(self, root: ET.Element) -> FCPXMLDocument:
        doc = FCPXMLDocument()
        doc.version = root.get("version", "1.11")

        # Parse resources
        resources_el = root.find("resources")
        if resources_el is not None:
            self._parse_resources(resources_el, doc)

        # Parse library
        library_el = root.find("library")
        if library_el is not None:
            doc.library = self._parse_library(library_el, doc)

        # Parse standalone events (no library wrapper)
        for event_el in root.findall("event"):
            doc.events.append(self._parse_event(event_el, doc))

        # Parse standalone projects
        for project_el in root.findall("project"):
            doc.projects.append(self._parse_project(project_el, doc))

        return doc

    def _parse_resources(self, resources_el: ET.Element, doc: FCPXMLDocument) -> None:
        """Parse all resource elements."""
        for el in resources_el:
            tag = el.tag
            rid = el.get("id", "")

            if tag == "format":
                fmt = self._parse_format(el)
                doc.formats[rid] = fmt
                doc.resources[rid] = fmt

            elif tag == "asset":
                asset = self._parse_asset(el)
                doc.assets[rid] = asset
                doc.resources[rid] = asset

            elif tag == "effect":
                effect = self._parse_effect(el)
                doc.effects[rid] = effect
                doc.resources[rid] = effect

    def _parse_format(self, el: ET.Element) -> Format:
        """Parse a format resource."""
        frame_dur_str = el.get("frameDuration", "1001/30000s")
        return Format(
            id=el.get("id", ""),
            name=el.get("name", ""),
            width=int(el.get("width", "0")),
            height=int(el.get("height", "0")),
            frame_duration=RationalTime.from_fcpxml(frame_dur_str),
            color_space=el.get("colorSpace", ""),
        )

    def _parse_asset(self, el: ET.Element) -> Asset:
        """Parse an asset resource."""
        return Asset(
            id=el.get("id", ""),
            src=el.get("src", ""),
            name=el.get("name", ""),
            start=RationalTime.from_fcpxml(el.get("start", "0s")),
            duration=RationalTime.from_fcpxml(el.get("duration", "0s")),
            has_video=el.get("hasVideo", "1") == "1",
            has_audio=el.get("hasAudio", "1") == "1",
            format_ref=el.get("format", ""),
            audio_sources=int(el.get("audioSources", "1")),
            audio_channels=int(el.get("audioChannels", "2")),
        )

    def _parse_effect(self, el: ET.Element) -> Effect:
        """Parse an effect resource."""
        return Effect(
            id=el.get("id", ""),
            name=el.get("name", ""),
            uid=el.get("uid", ""),
            src=el.get("src", ""),
        )

    def _parse_library(self, el: ET.Element, doc: FCPXMLDocument) -> Library:
        """Parse a library element."""
        lib = Library(
            name=el.get("name", ""),
            location=el.get("location", ""),
        )
        for event_el in el.findall("event"):
            lib.events.append(self._parse_event(event_el, doc))
        return lib

    def _parse_event(self, el: ET.Element, doc: FCPXMLDocument) -> Event:
        """Parse an event element."""
        event = Event(
            name=el.get("name", ""),
            uid=el.get("uid", ""),
        )
        for project_el in el.findall("project"):
            event.projects.append(self._parse_project(project_el, doc))
        return event

    def _parse_project(self, el: ET.Element, doc: FCPXMLDocument) -> Project:
        """Parse a project element."""
        project = Project(
            name=el.get("name", ""),
            uid=el.get("uid", ""),
            mod_date=el.get("modDate", ""),
        )
        seq_el = el.find("sequence")
        if seq_el is not None:
            project.sequence = self._parse_sequence(seq_el, doc)
        return project

    def _parse_sequence(self, el: ET.Element, doc: FCPXMLDocument) -> Sequence:
        """Parse a sequence element."""
        tc_fmt_str = el.get("tcFormat", "NDF")
        tc_format = TimecodeFormat.DROP_FRAME if tc_fmt_str == "DF" else TimecodeFormat.NON_DROP_FRAME

        seq = Sequence(
            name=el.get("name", ""),
            uid=el.get("uid", ""),
            format_ref=el.get("format", ""),
            duration=RationalTime.from_fcpxml(el.get("duration", "0s")),
            tc_start=RationalTime.from_fcpxml(el.get("tcStart", "0s")),
            tc_format=tc_format,
        )

        # Resolve frame duration from format
        if seq.format_ref and seq.format_ref in doc.formats:
            seq._frame_duration = doc.formats[seq.format_ref].frame_duration

        spine_el = el.find("spine")
        if spine_el is not None:
            seq.spine = self._parse_spine(spine_el, doc)

        return seq

    def _parse_spine(self, el: ET.Element, doc: FCPXMLDocument) -> Spine:
        """Parse a spine element (primary storyline)."""
        spine = Spine()
        for child in el:
            if child.tag in self.CLIP_TAGS:
                clip = self._parse_clip(child, doc)
                spine.clips.append(clip)
            elif child.tag == "transition":
                # Transitions are between clips; we track them but they don't take timeline space
                # in the same way. Some workflows treat them as clips.
                pass  # TODO: handle transitions in spine
        return spine

    def _parse_clip(self, el: ET.Element, doc: FCPXMLDocument) -> Clip:
        """Parse any clip-type element."""
        clip_type = self.TAG_TO_CLIP_TYPE.get(el.tag, ClipType.CLIP)

        clip = Clip(
            clip_type=clip_type,
            name=el.get("name", ""),
            ref=el.get("ref", ""),
            offset=RationalTime.from_fcpxml(el.get("offset", "0s")),
            start=RationalTime.from_fcpxml(el.get("start", "0s")),
            duration=RationalTime.from_fcpxml(el.get("duration", "0s")),
            role=el.get("audioRole") or el.get("videoRole") or el.get("role", ""),
            lane=int(el.get("lane", "0")),
            enabled=el.get("enabled", "1") != "0",
            _raw_attribs=dict(el.attrib),
        )

        # Parse nested elements
        for child in el:
            if child.tag == "marker":
                clip.markers.append(self._parse_marker(child, MarkerType.STANDARD))
            elif child.tag == "chapter-marker":
                clip.markers.append(self._parse_marker(child, MarkerType.CHAPTER))
            elif child.tag == "keyword":
                clip.keywords.append(self._parse_keyword(child))
            elif child.tag == "filter-video" or child.tag == "filter-audio":
                clip.effects.append(self._parse_applied_effect(child))
            elif child.tag == "adjust-transform":
                clip.transform = self._parse_transform(child)
            elif child.tag == "adjust-volume":
                clip.volume = VolumeParams(amount=child.get("amount", "0dB"))
            elif child.tag in self.CLIP_TAGS:
                # Connected clip
                connected = self._parse_clip(child, doc)
                clip.connected_clips.append(connected)
            elif child.tag == "spine":
                # Nested spine (in compound clips, auditions)
                nested_spine = self._parse_spine(child, doc)
                clip.spine_clips.extend(nested_spine.clips)

        return clip

    def _parse_marker(self, el: ET.Element, marker_type: MarkerType) -> Marker:
        """Parse a marker element."""
        return Marker(
            start=RationalTime.from_fcpxml(el.get("start", "0s")),
            duration=RationalTime.from_fcpxml(el.get("duration", "0s")),
            value=el.get("value", ""),
            note=el.get("note", ""),
            marker_type=marker_type,
            completed=el.get("completed", "0") == "1",
        )

    def _parse_keyword(self, el: ET.Element) -> Keyword:
        """Parse a keyword element."""
        return Keyword(
            start=RationalTime.from_fcpxml(el.get("start", "0s")),
            duration=RationalTime.from_fcpxml(el.get("duration", "0s")),
            value=el.get("value", ""),
        )

    def _parse_applied_effect(self, el: ET.Element) -> AppliedEffect:
        """Parse an applied effect (filter-video or filter-audio)."""
        params = {}
        for param_el in el.findall(".//param"):
            key = param_el.get("key", param_el.get("name", ""))
            params[key] = param_el.get("value", "")
        return AppliedEffect(
            ref=el.get("ref", ""),
            name=el.get("name", ""),
            enabled=el.get("enabled", "1") != "0",
            parameters=params,
        )

    def _parse_transform(self, el: ET.Element) -> TransformParams:
        """Parse adjust-transform element."""
        position = el.get("position", "0 0").split()
        anchor = el.get("anchor", "0 0").split()
        scale = el.get("scale", "1 1").split()
        scale_x = float(scale[0]) if scale else 1.0
        scale_y = float(scale[1]) if len(scale) > 1 else scale_x
        return TransformParams(
            position_x=float(position[0]) if len(position) > 0 else 0.0,
            position_y=float(position[1]) if len(position) > 1 else 0.0,
            scale=scale_x,
            rotation=float(el.get("rotation", "0")),
            anchor_x=float(anchor[0]) if len(anchor) > 0 else 0.0,
            anchor_y=float(anchor[1]) if len(anchor) > 1 else 0.0,
            scale_y=scale_y,
        )
