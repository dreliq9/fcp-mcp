"""FCPXML writer/modifier — modifies existing FCPXML files.

Loads an FCPXML, applies modifications, writes to a new file.
Never overwrites the original.
"""

from __future__ import annotations

import copy
import xml.etree.ElementTree as ET
from pathlib import Path

from ..utils.safe_xml import parse_fcpxml
from .time_utils import RationalTime
from .transaction import commit_fcpxml


class FCPXMLModifier:
    """Load and modify FCPXML files."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.tree = parse_fcpxml(self.path)
        self.root = self.tree.getroot()

    def save(
        self,
        output_path: str | Path | None = None,
        *,
        event_format: str = "text",
    ) -> Path:
        """Save modified FCPXML. Defaults to original_name_modified.fcpxml."""
        if output_path is None:
            stem = self.path.stem
            output_path = self.path.parent / f"{stem}_modified.fcpxml"
        output_path = Path(output_path)
        ET.indent(self.root, space="    ")
        xml_text = ET.tostring(self.root, encoding="unicode", xml_declaration=True)
        receipt = commit_fcpxml(
            source=self.path,
            destination=output_path,
            xml_text=xml_text,
            event_format=event_format,
            operation="modifier_save",
        )
        return receipt.destination

    # --- Markers ---

    def add_marker(
        self,
        clip_name: str,
        start: str,
        value: str,
        note: str = "",
        marker_type: str = "standard",
        duration: str = "1/1s",
    ) -> bool:
        """Add a marker to a clip by name. Returns True if clip found."""
        clip_el = self._find_clip_by_name(clip_name)
        if clip_el is None:
            return False

        tag = "chapter-marker" if marker_type == "chapter" else "marker"
        marker_el = ET.SubElement(clip_el, tag)
        marker_el.set("start", start)
        marker_el.set("duration", duration)
        marker_el.set("value", value)
        if note:
            marker_el.set("note", note)
        return True

    def batch_add_markers(
        self, markers: list[dict],
    ) -> int:
        """Add multiple markers. Each dict: {clip_name, start, value, note?, type?}.
        Returns count of successfully added markers.
        """
        count = 0
        for m in markers:
            if self.add_marker(
                clip_name=m["clip_name"],
                start=m["start"],
                value=m["value"],
                note=m.get("note", ""),
                marker_type=m.get("type", "standard"),
            ):
                count += 1
        return count

    def delete_markers(self, clip_name: str, value_filter: str | None = None) -> int:
        """Remove markers from a clip. If value_filter set, only remove matching."""
        clip_el = self._find_clip_by_name(clip_name)
        if clip_el is None:
            return 0
        count = 0
        for tag in ("marker", "chapter-marker"):
            for marker_el in list(clip_el.findall(tag)):
                if value_filter is None or marker_el.get("value", "") == value_filter:
                    clip_el.remove(marker_el)
                    count += 1
        return count

    # --- Keywords ---

    def add_keyword(
        self, clip_name: str, value: str, start: str = "0s", duration: str | None = None,
    ) -> bool:
        """Add a keyword to a clip."""
        clip_el = self._find_clip_by_name(clip_name)
        if clip_el is None:
            return False
        kw_el = ET.SubElement(clip_el, "keyword")
        kw_el.set("value", value)
        kw_el.set("start", start)
        if duration:
            kw_el.set("duration", duration)
        return True

    # --- Clip Operations ---

    def trim_clip(
        self,
        clip_name: str,
        new_start: str | None = None,
        new_duration: str | None = None,
    ) -> bool:
        """Trim a clip's source in/out points."""
        clip_el = self._find_clip_by_name(clip_name)
        if clip_el is None:
            return False
        if new_start is not None:
            clip_el.set("start", new_start)
        if new_duration is not None:
            clip_el.set("duration", new_duration)
        return True

    def split_clip(self, clip_name: str, split_at: str) -> bool:
        """Split a clip at a given offset within the clip.

        Creates two clips: original up to split point, remainder after.
        """
        clip_el = self._find_clip_by_name(clip_name)
        if clip_el is None:
            return False

        parent = self._find_parent(clip_el)
        if parent is None:
            return False

        orig_start = RationalTime.from_fcpxml(clip_el.get("start", "0s"))
        orig_duration = RationalTime.from_fcpxml(clip_el.get("duration", "0s"))
        orig_offset = RationalTime.from_fcpxml(clip_el.get("offset", "0s"))
        split_time = RationalTime.from_fcpxml(split_at)

        if split_time >= orig_duration or split_time.is_zero:
            return False

        # First part: original start, duration = split_time
        clip_el.set("duration", split_time.to_fcpxml())

        # Second part: start = orig_start + split_time, duration = remainder
        second = copy.deepcopy(clip_el)
        second.set("start", (orig_start + split_time).to_fcpxml())
        second.set("duration", (orig_duration - split_time).to_fcpxml())
        second.set("offset", (orig_offset + split_time).to_fcpxml())
        second.set("name", f"{clip_name}_split")

        # Remove markers/keywords from second part (they belong to first by default)
        for tag in ("marker", "chapter-marker", "keyword"):
            for el in list(second.findall(tag)):
                second.remove(el)

        # Insert after original
        children = list(parent)
        idx = children.index(clip_el)
        parent.insert(idx + 1, second)
        return True

    def delete_clips(self, clip_names: list[str]) -> int:
        """Remove clips by name. Returns count removed."""
        count = 0
        for name in clip_names:
            clip_el = self._find_clip_by_name(name)
            if clip_el is not None:
                parent = self._find_parent(clip_el)
                if parent is not None:
                    parent.remove(clip_el)
                    count += 1
        return count

    def reorder_clips(self, clip_names: list[str]) -> bool:
        """Reorder clips in the primary spine to match the given name order.

        Only reorders clips that are in the provided list. Others stay in place.
        """
        spine_el = self.root.find(".//spine")
        if spine_el is None:
            return False

        # Collect clip elements by name
        name_to_el = {}
        for child in list(spine_el):
            name = child.get("name", "")
            if name in clip_names:
                name_to_el[name] = child
                spine_el.remove(child)

        # Re-insert in requested order
        insert_idx = 0
        for name in clip_names:
            if name in name_to_el:
                spine_el.insert(insert_idx, name_to_el[name])
                insert_idx += 1

        # Recalculate offsets
        self._recalculate_offsets(spine_el)
        return True

    # --- Transitions ---

    def add_transition(
        self,
        after_clip_name: str,
        duration: str = "30030/30000s",
        name: str = "Cross Dissolve",
        ref: str = "",
    ) -> bool:
        """Insert a transition after the named clip."""
        clip_el = self._find_clip_by_name(after_clip_name)
        if clip_el is None:
            return False

        parent = self._find_parent(clip_el)
        if parent is None:
            return False

        transition_el = ET.Element("transition")
        clip_offset = RationalTime.from_fcpxml(clip_el.get("offset", "0s"))
        clip_duration = RationalTime.from_fcpxml(clip_el.get("duration", "0s"))
        transition_el.set("offset", (clip_offset + clip_duration).to_fcpxml())
        transition_el.set("duration", duration)
        transition_el.set("name", name)
        if ref:
            transition_el.set("ref", ref)

        children = list(parent)
        idx = children.index(clip_el)
        parent.insert(idx + 1, transition_el)
        return True

    # --- Speed ---

    def change_speed(self, clip_name: str, speed_factor: float) -> bool:
        """Change clip speed. speed_factor: 2.0 = 2x fast, 0.5 = half speed."""
        clip_el = self._find_clip_by_name(clip_name)
        if clip_el is None or speed_factor <= 0:
            return False

        orig_duration = RationalTime.from_fcpxml(clip_el.get("duration", "0s"))
        new_duration = RationalTime(
            round(orig_duration.numerator / speed_factor),
            orig_duration.denominator,
        )
        clip_el.set("duration", new_duration.to_fcpxml())

        # Add/update timeMap for the speed change
        # Simple linear remap
        time_map = clip_el.find("timeMap")
        if time_map is not None:
            clip_el.remove(time_map)

        time_map = ET.SubElement(clip_el, "timeMap")
        # Start point
        tp1 = ET.SubElement(time_map, "timept")
        tp1.set("time", "0s")
        tp1.set("value", "0s")
        tp1.set("interp", "smooth2")
        # End point
        tp2 = ET.SubElement(time_map, "timept")
        tp2.set("time", new_duration.to_fcpxml())
        orig_source_dur = RationalTime.from_fcpxml(clip_el.get("duration", "0s"))
        tp2.set("value", orig_duration.to_fcpxml())
        tp2.set("interp", "smooth2")

        return True

    # --- Roles ---

    def assign_role(self, clip_name: str, role: str) -> bool:
        """Set role on a clip."""
        clip_el = self._find_clip_by_name(clip_name)
        if clip_el is None:
            return False
        clip_el.set("role", role)
        return True

    # --- Batch Operations ---

    def batch_assign_roles(self, rules: list[dict]) -> int:
        """Assign roles based on rules. Each rule: {match: str, role: str}.
        match is substring matched against clip name.
        """
        count = 0
        for clip_el in self.root.iter():
            if clip_el.tag not in ("asset-clip", "clip", "title", "audio", "video"):
                continue
            name = clip_el.get("name", "")
            for rule in rules:
                if rule["match"].lower() in name.lower():
                    clip_el.set("role", rule["role"])
                    count += 1
                    break
        return count

    def batch_rename_clips(self, pattern: str, replacement: str) -> int:
        """Rename clips matching pattern (substring replace). Returns count."""
        count = 0
        for clip_el in self.root.iter():
            name = clip_el.get("name", "")
            if pattern in name:
                clip_el.set("name", name.replace(pattern, replacement))
                count += 1
        return count

    def fill_gaps(self, fill_ref: str, fill_name: str = "Fill") -> int:
        """Replace all gaps with clips referencing the given asset. Returns count."""
        count = 0
        for spine_el in self.root.iter("spine"):
            for gap_el in list(spine_el.findall("gap")):
                idx = list(spine_el).index(gap_el)
                new_clip = ET.Element("asset-clip")
                new_clip.set("ref", fill_ref)
                new_clip.set("name", fill_name)
                new_clip.set("offset", gap_el.get("offset", "0s"))
                new_clip.set("start", "0s")
                new_clip.set("duration", gap_el.get("duration", "0s"))
                spine_el.remove(gap_el)
                spine_el.insert(idx, new_clip)
                count += 1
        return count

    def fix_flash_frames(self, min_frames: int = 3, frame_duration_str: str = "1001/30000s") -> int:
        """Extend clips shorter than min_frames by absorbing from source.
        Returns count of fixed clips.
        """
        frame_dur = RationalTime.from_fcpxml(frame_duration_str)
        min_duration = frame_dur * min_frames
        count = 0

        for spine_el in self.root.iter("spine"):
            for clip_el in list(spine_el):
                if clip_el.tag == "gap" or clip_el.tag == "transition":
                    continue
                duration = RationalTime.from_fcpxml(clip_el.get("duration", "0s"))
                if duration < min_duration and not duration.is_zero:
                    clip_el.set("duration", min_duration.to_fcpxml())
                    count += 1

        if count > 0:
            for spine_el in self.root.iter("spine"):
                self._recalculate_offsets(spine_el)
        return count

    def batch_apply_transition(
        self,
        duration: str = "30030/30000s",
        name: str = "Cross Dissolve",
        ref: str = "",
    ) -> int:
        """Add transitions between all adjacent clips in spine. Returns count added."""
        count = 0
        for spine_el in self.root.iter("spine"):
            clips = [c for c in spine_el if c.tag not in ("transition",)]
            for i in range(len(clips) - 1):
                clip_a = clips[i]
                if clip_a.tag == "gap":
                    continue
                clip_b = clips[i + 1]
                if clip_b.tag == "gap":
                    continue

                transition_el = ET.Element("transition")
                a_offset = RationalTime.from_fcpxml(clip_a.get("offset", "0s"))
                a_duration = RationalTime.from_fcpxml(clip_a.get("duration", "0s"))
                transition_el.set("offset", (a_offset + a_duration).to_fcpxml())
                transition_el.set("duration", duration)
                transition_el.set("name", name)
                if ref:
                    transition_el.set("ref", ref)

                # Insert transition after clip_a
                children = list(spine_el)
                idx = children.index(clip_a)
                spine_el.insert(idx + 1, transition_el)
                count += 1
        return count

    # --- Helpers ---

    def _find_clip_by_name(self, name: str) -> ET.Element | None:
        """Find first clip element with matching name."""
        for el in self.root.iter():
            if el.get("name") == name and el.tag in (
                "asset-clip", "clip", "gap", "title", "generator",
                "compound-clip", "mc-clip", "sync-clip", "audition",
                "video", "audio", "ref-clip",
            ):
                return el
        return None

    def _find_parent(self, target: ET.Element) -> ET.Element | None:
        """Find parent of an element."""
        for parent in self.root.iter():
            for child in parent:
                if child is target:
                    return parent
        return None

    def _recalculate_offsets(self, spine_el: ET.Element) -> None:
        """Recalculate offsets for all clips in a spine sequentially."""
        current_offset = RationalTime.zero()
        for child in spine_el:
            if child.tag == "transition":
                # Transitions overlap — don't advance offset
                child.set("offset", current_offset.to_fcpxml())
                continue
            child.set("offset", current_offset.to_fcpxml())
            duration = RationalTime.from_fcpxml(child.get("duration", "0s"))
            current_offset = current_offset + duration
