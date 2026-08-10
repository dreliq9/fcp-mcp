"""FCPXML generator — create FCPXML files from scratch.

Builds complete, valid FCPXML documents programmatically.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import quote as url_quote

from .time_utils import FORMAT_FRAME_DURATIONS, RationalTime
from .transaction import FCPXMLTransactionReceipt, commit_fcpxml


class FCPXMLGenerator:
    """Build FCPXML documents from scratch."""

    def __init__(self, version: str = "1.11"):
        self.version = version
        self.root = ET.Element("fcpxml")
        self.root.set("version", version)
        self.resources = ET.SubElement(self.root, "resources")
        self._resource_counter = 0
        self._formats: dict[str, str] = {}  # format_name → resource_id
        self._assets: dict[str, str] = {}   # asset_key → resource_id

    def _next_id(self) -> str:
        self._resource_counter += 1
        return f"r{self._resource_counter}"

    @staticmethod
    def _normalize_asset_source(src: str) -> str:
        if src.startswith("file://"):
            return src
        return "file://" + url_quote(str(Path(src).resolve()), safe="/:@")

    # --- Resources ---

    def add_format(
        self,
        name: str = "FFVideoFormat1080p2997",
        width: int = 1920,
        height: int = 1080,
        frame_duration: str = "",
        color_space: str = "1-1-1 (Rec. 709)",
    ) -> str:
        """Add a format resource. Returns resource ID.

        If frame_duration is empty, it is auto-detected from the format name.
        """
        if name in self._formats:
            return self._formats[name]

        # Auto-detect frame duration from format name if not specified
        if not frame_duration:
            if name in FORMAT_FRAME_DURATIONS:
                frame_duration = FORMAT_FRAME_DURATIONS[name].to_fcpxml()
            else:
                frame_duration = "1001/30000s"  # fallback to 29.97

        rid = self._next_id()
        fmt = ET.SubElement(self.resources, "format")
        fmt.set("id", rid)
        fmt.set("name", name)
        fmt.set("width", str(width))
        fmt.set("height", str(height))
        fmt.set("frameDuration", frame_duration)
        fmt.set("colorSpace", color_space)
        self._formats[name] = rid
        return rid

    def add_asset(
        self,
        src: str,
        name: str | None = None,
        duration: str = "0s",
        has_video: bool = True,
        has_audio: bool = True,
        format_ref: str | None = None,
        audio_channels: int = 2,
    ) -> str:
        """Add an asset resource. Returns resource ID.

        src should be a file path or file:// URL.
        """
        # Normalize to file:// URL with percent-encoding
        src = self._normalize_asset_source(src)

        if src in self._assets:
            return self._assets[src]

        rid = self._next_id()
        if name is None:
            name = Path(src.replace("file://", "")).stem

        asset = ET.SubElement(self.resources, "asset")
        asset.set("id", rid)
        asset.set("name", name)
        asset.set("start", "0s")
        asset.set("duration", duration)
        asset.set("hasVideo", "1" if has_video else "0")
        asset.set("hasAudio", "1" if has_audio else "0")
        if format_ref:
            asset.set("format", format_ref)
        asset.set("audioChannels", str(audio_channels))

        # media-rep child element (required by FCPXML v1.10+ DTD)
        media_rep = ET.SubElement(asset, "media-rep")
        media_rep.set("kind", "original-media")
        media_rep.set("src", src)

        self._assets[src] = rid
        return rid

    def add_effect(
        self,
        name: str,
        uid: str = "",
    ) -> str:
        """Add an effect resource. Returns resource ID."""
        rid = self._next_id()
        effect = ET.SubElement(self.resources, "effect")
        effect.set("id", rid)
        effect.set("name", name)
        if uid:
            effect.set("uid", uid)
        return rid

    # --- Structure ---

    def create_project(
        self,
        name: str = "Untitled Project",
        format_ref: str | None = None,
        duration: str = "0s",
        tc_start: str = "0s",
        tc_format: str = "NDF",
        library_name: str = "",
        event_name: str = "Default Event",
    ) -> ET.Element:
        """Create a project with library/event wrapper. Returns the spine element."""
        if format_ref is None:
            format_ref = self.add_format()

        if library_name:
            library = ET.SubElement(self.root, "library")
            library.set("name", library_name)
            event = ET.SubElement(library, "event")
        else:
            event = ET.SubElement(self.root, "event")
        event.set("name", event_name)

        project = ET.SubElement(event, "project")
        project.set("name", name)

        sequence = ET.SubElement(project, "sequence")
        sequence.set("format", format_ref)
        sequence.set("duration", duration)
        sequence.set("tcStart", tc_start)
        sequence.set("tcFormat", tc_format)

        spine = ET.SubElement(sequence, "spine")
        return spine

    # --- Clip Building ---

    def add_clip_to_spine(
        self,
        spine: ET.Element,
        asset_ref: str,
        name: str = "",
        start: str = "0s",
        duration: str = "0s",
        role: str = "",
        offset: str | None = None,
    ) -> ET.Element:
        """Add an asset-clip to a spine. Auto-calculates offset if not provided."""
        if offset is None:
            offset = self._calculate_next_offset(spine)

        clip = ET.SubElement(spine, "asset-clip")
        clip.set("ref", asset_ref)
        if name:
            clip.set("name", name)
        clip.set("offset", offset)
        clip.set("start", start)
        clip.set("duration", duration)
        if role:
            clip.set("audioRole", role)
        return clip

    def add_gap_to_spine(
        self,
        spine: ET.Element,
        duration: str,
        offset: str | None = None,
    ) -> ET.Element:
        """Add a gap to a spine."""
        if offset is None:
            offset = self._calculate_next_offset(spine)

        gap = ET.SubElement(spine, "gap")
        gap.set("offset", offset)
        gap.set("duration", duration)
        gap.set("name", "Gap")
        return gap

    def add_title_to_spine(
        self,
        spine: ET.Element,
        effect_ref: str,
        name: str = "Title",
        duration: str = "150150/30000s",
        text_params: dict[str, str] | None = None,
        offset: str | None = None,
    ) -> ET.Element:
        """Add a title generator clip to a spine."""
        if offset is None:
            offset = self._calculate_next_offset(spine)

        title = ET.SubElement(spine, "title")
        title.set("ref", effect_ref)
        title.set("name", name)
        title.set("offset", offset)
        title.set("duration", duration)
        title.set("role", "Titles")

        if text_params:
            for key, value in text_params.items():
                param = ET.SubElement(title, "param")
                param.set("name", key)
                param.set("key", key)
                param.set("value", value)

        return title

    def add_connected_clip(
        self,
        parent_clip: ET.Element,
        asset_ref: str,
        name: str = "",
        offset: str = "0s",
        start: str = "0s",
        duration: str = "0s",
        lane: int = 1,
        role: str = "",
    ) -> ET.Element:
        """Add a connected clip (e.g., B-roll over primary storyline)."""
        clip = ET.SubElement(parent_clip, "asset-clip")
        clip.set("ref", asset_ref)
        if name:
            clip.set("name", name)
        clip.set("offset", offset)
        clip.set("start", start)
        clip.set("duration", duration)
        clip.set("lane", str(lane))
        if role:
            clip.set("audioRole", role)
        return clip

    def add_transition(
        self,
        spine: ET.Element,
        duration: str = "30030/30000s",
        name: str = "Cross Dissolve",
        effect_ref: str = "",
        offset: str | None = None,
    ) -> ET.Element:
        """Add a transition to the spine."""
        if offset is None:
            offset = self._calculate_next_offset(spine)

        transition = ET.SubElement(spine, "transition")
        transition.set("offset", offset)
        transition.set("duration", duration)
        transition.set("name", name)
        if effect_ref:
            transition.set("ref", effect_ref)
        return transition

    def add_marker(
        self,
        clip: ET.Element,
        start: str,
        value: str,
        note: str = "",
        duration: str = "1/1s",
        marker_type: str = "standard",
    ) -> ET.Element:
        """Add a marker to a clip element."""
        tag = "chapter-marker" if marker_type == "chapter" else "marker"
        marker = ET.SubElement(clip, tag)
        marker.set("start", start)
        marker.set("duration", duration)
        marker.set("value", value)
        if note:
            marker.set("note", note)
        return marker

    # --- Timeline Builders ---

    def build_timeline_from_clips(
        self,
        clips: list[dict],
        project_name: str = "Generated Timeline",
        format_name: str = "FFVideoFormat1080p2997",
        event_name: str = "Generated",
    ) -> ET.Element:
        """Build a complete timeline from a list of clip definitions.

        Each clip dict:
            src: str (file path)
            name: str (optional)
            start: str (source start, optional, default "0s")
            duration: str (required)
            asset_duration: str (optional full source duration)
            role: str (optional)
            has_video: bool (optional, default True)
            has_audio: bool (optional, default True)

        Returns the root element.
        """
        format_ref = self.add_format(name=format_name)

        # Resolve one safe asset duration for every source before adding assets.
        # Keep the original rational spelling for zero-start and explicit values.
        asset_specs: dict[str, dict] = {}
        for clip_def in clips:
            src = clip_def["src"]
            source_key = self._normalize_asset_source(src)
            start_text = clip_def.get("start", "0s")
            duration_text = clip_def["duration"]
            start = RationalTime.from_fcpxml(start_text)
            duration = RationalTime.from_fcpxml(duration_text)
            source_end = duration if start.is_zero else start + duration
            source_end_text = (
                duration_text if start.is_zero else source_end.to_fcpxml()
            )
            spec = asset_specs.setdefault(
                source_key,
                {
                    "clip": clip_def,
                    "source_end": source_end,
                    "source_end_text": source_end_text,
                    "explicit_durations": [],
                },
            )
            if source_end > spec["source_end"]:
                spec["source_end"] = source_end
                spec["source_end_text"] = source_end_text
            if "asset_duration" in clip_def:
                explicit_text = clip_def["asset_duration"]
                spec["explicit_durations"].append(
                    (RationalTime.from_fcpxml(explicit_text), explicit_text)
                )

        asset_refs: dict[str, str] = {}
        for source_key, spec in asset_specs.items():
            explicit_durations = spec["explicit_durations"]
            for explicit, explicit_text in explicit_durations:
                if explicit < spec["source_end"]:
                    raise ValueError(
                        "asset_duration "
                        f"{explicit_text} must cover selected source range "
                        f"ending at {spec['source_end_text']} for "
                        f"{spec['clip']['src']}"
                    )
            if explicit_durations:
                _, asset_duration = max(
                    explicit_durations,
                    key=lambda item: item[0],
                )
            else:
                asset_duration = spec["source_end_text"]
            first_clip = spec["clip"]
            asset_refs[source_key] = self.add_asset(
                src=first_clip["src"],
                name=first_clip.get("name"),
                duration=asset_duration,
                has_video=first_clip.get("has_video", True),
                has_audio=first_clip.get("has_audio", True),
                format_ref=format_ref,
            )

        # Calculate total duration
        total = RationalTime.zero()
        for clip_def in clips:
            total = total + RationalTime.from_fcpxml(clip_def["duration"])

        # Create project
        spine = self.create_project(
            name=project_name,
            format_ref=format_ref,
            duration=total.to_fcpxml(),
            event_name=event_name,
        )

        # Add clips
        for clip_def in clips:
            self.add_clip_to_spine(
                spine=spine,
                asset_ref=asset_refs[
                    self._normalize_asset_source(clip_def["src"])
                ],
                name=clip_def.get("name", ""),
                start=clip_def.get("start", "0s"),
                duration=clip_def["duration"],
                role=clip_def.get("role", ""),
            )

        return self.root

    # --- Output ---

    def save(
        self,
        path: str | Path,
        *,
        event_format: str = "text",
    ) -> Path:
        """Write FCPXML to file."""
        return self.save_with_receipt(
            path,
            event_format=event_format,
        ).destination

    def save_with_receipt(
        self,
        path: str | Path,
        *,
        event_format: str = "text",
    ) -> FCPXMLTransactionReceipt:
        """Write FCPXML atomically and return its complete receipt."""
        return commit_fcpxml(
            source=None,
            destination=Path(path),
            xml_text=f"{self.to_string()}\n",
            event_format=event_format,
            operation="generator_save",
        )

    def to_string(self) -> str:
        """Return FCPXML as string."""
        ET.indent(self.root, space="    ")
        xml_str = ET.tostring(self.root, encoding="unicode")
        return f'<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n{xml_str}'

    # --- Helpers ---

    def _calculate_next_offset(self, spine: ET.Element) -> str:
        """Calculate the next offset in a spine (after all existing clips)."""
        offset = RationalTime.zero()
        for child in spine:
            if child.tag == "transition":
                continue  # transitions don't advance the offset
            duration = RationalTime.from_fcpxml(child.get("duration", "0s"))
            offset = offset + duration
        return offset.to_fcpxml()
