"""Tests for FCPXML writer/modifier."""

import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

import pytest

from fcp_mcp.fcpxml.parser import FCPXMLParser
from fcp_mcp.fcpxml.writer import FCPXMLModifier
from fcp_mcp.utils.safe_xml import parse_string

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def modifier():
    return FCPXMLModifier(FIXTURES_DIR / "sample.fcpxml")


@pytest.fixture
def parser():
    return FCPXMLParser()


class TestMarkers:
    def test_add_marker(self, modifier):
        result = modifier.add_marker("Interview_A", "90090/30000s", "New Marker", "test note")
        assert result is True

    def test_add_marker_missing_clip(self, modifier):
        result = modifier.add_marker("NonExistent", "0s", "fail")
        assert result is False

    def test_batch_add_markers(self, modifier):
        markers = [
            {"clip_name": "Interview_A", "start": "0s", "value": "M1"},
            {"clip_name": "Broll_Beach", "start": "0s", "value": "M2"},
            {"clip_name": "NonExistent", "start": "0s", "value": "M3"},
        ]
        count = modifier.batch_add_markers(markers)
        assert count == 2

    def test_delete_markers(self, modifier):
        count = modifier.delete_markers("Interview_A")
        assert count == 1  # sample has 1 marker on Interview_A

    def test_delete_markers_with_filter(self, modifier):
        count = modifier.delete_markers("Interview_A", value_filter="Nonexistent")
        assert count == 0


class TestClipOperations:
    def test_trim_clip(self, modifier):
        sequence = modifier.root.find(".//sequence")
        assert sequence is not None
        before = sequence.get("duration")

        result = modifier.trim_clip("Interview_A", new_duration="120120/30000s")

        assert result is True
        assert sequence.get("duration") == before

    def test_trim_last_storyline_clip_updates_sequence_duration(self, modifier):
        result = modifier.trim_clip(
            "Interview_A_Outro",
            new_duration="1001/30000s",
        )

        assert result is True
        sequence = modifier.root.find(".//sequence")
        assert sequence is not None
        assert sequence.get("duration") == "61061/5000s"

    def test_trim_storyline_duration_is_relative_to_sequence_timecode(
        self,
        tmp_path,
    ):
        source = tmp_path / "nonzero-timecode.fcpxml"
        source.write_text(
            """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE fcpxml>
<fcpxml version="1.11">
    <resources><format id="r1" frameDuration="1/30s"/></resources>
    <library><event name="Event"><project name="Project">
        <sequence format="r1" duration="10s" tcStart="3600s">
            <spine><gap name="Storyline" offset="3600s" duration="10s"/></spine>
        </sequence>
    </project></event></library>
</fcpxml>
"""
        )
        modifier = FCPXMLModifier(source)

        result = modifier.trim_clip("Storyline", new_duration="5s")

        assert result is True
        sequence = modifier.root.find(".//sequence")
        assert sequence is not None
        assert sequence.get("duration") == "5s"

    def test_trim_connected_clip_does_not_change_sequence_duration(self, modifier):
        sequence = modifier.root.find(".//sequence")
        assert sequence is not None
        before = sequence.get("duration")

        result = modifier.trim_clip(
            "Beach Scene",
            new_duration="600600/30000s",
        )

        assert result is True
        assert sequence.get("duration") == before

    def test_split_clip(self, modifier):
        result = modifier.split_clip("Interview_A", "60060/30000s")
        assert result is True

    def test_split_clip_at_zero(self, modifier):
        result = modifier.split_clip("Interview_A", "0s")
        assert result is False

    def test_delete_clips(self, modifier):
        count = modifier.delete_clips(["Broll_Beach_Flash"])
        assert count == 1

    def test_assign_role(self, modifier):
        result = modifier.assign_role("Interview_A", "Narration")
        assert result is True
        clip = next(
            element
            for element in modifier.root.iter("asset-clip")
            if element.get("name") == "Interview_A"
        )
        assert clip.get("audioRole") == "Narration"
        assert "role" not in clip.attrib


class TestTransitions:
    def test_add_transition(self, modifier):
        result = modifier.add_transition("Interview_A")
        assert result is True


class TestSpeed:
    def test_change_speed(self, modifier):
        result = modifier.change_speed("Broll_Beach", 2.0)
        assert result is True

    def test_change_speed_zero(self, modifier):
        result = modifier.change_speed("Broll_Beach", 0)
        assert result is False


class TestBatchOps:
    def test_batch_rename(self, modifier):
        count = modifier.batch_rename_clips("Broll_", "B-Roll_")
        assert count >= 2  # Beach and City at minimum

    def test_batch_assign_roles(self, modifier):
        rules = [
            {"match": "Interview", "role": "Narration"},
            {"match": "Broll", "role": "B-Roll"},
        ]
        count = modifier.batch_assign_roles(rules)
        assert count >= 3
        matched = [
            element
            for element in modifier.root.iter("asset-clip")
            if "interview" in element.get("name", "").lower()
            or "broll" in element.get("name", "").lower()
        ]
        assert matched
        assert all(element.get("audioRole") for element in matched)
        assert all("role" not in element.attrib for element in matched)

    def test_fill_gaps(self, modifier):
        count = modifier.fill_gaps("r3", "Fill Clip")
        assert count == 1  # sample has 1 gap

    def test_fix_flash_frames(self, modifier):
        count = modifier.fix_flash_frames(min_frames=3)
        assert count == 1  # sample has 1 flash frame (2 frames)


class TestSaveRoundtrip:
    def test_serialize_is_deterministic_and_non_mutating(
        self, modifier, sample_fcpxml_path
    ):
        modifier.add_marker("Interview_A", "0s", "Début 東京")
        root_before = ET.tostring(modifier.root, encoding="utf-8")
        source_before = sample_fcpxml_path.read_bytes()

        first = modifier.serialize()
        second = modifier.serialize()

        assert first == second
        assert first.startswith(b"<?xml version='1.0' encoding='utf-8'?>")
        assert b"    <resources>" in first
        assert "Début 東京".encode() in first
        assert ET.tostring(modifier.root, encoding="utf-8") == root_before
        assert sample_fcpxml_path.read_bytes() == source_before
        assert parse_string(first).tag == "fcpxml"

    def test_equivalent_fresh_modifiers_serialize_identically(
        self, sample_fcpxml_path
    ):
        first = FCPXMLModifier(sample_fcpxml_path)
        second = FCPXMLModifier(sample_fcpxml_path)

        assert first.serialize() == second.serialize()

    def test_serialize_supports_fcpxmld_bundle(
        self, sample_fcpxml_path, tmp_path
    ):
        bundle = tmp_path / "Example.fcpxmld"
        bundle.mkdir()
        info = bundle / "Info.fcpxml"
        info.write_bytes(sample_fcpxml_path.read_bytes())

        modifier = FCPXMLModifier(bundle)

        assert parse_string(modifier.serialize()).tag == "fcpxml"
        assert info.read_bytes() == sample_fcpxml_path.read_bytes()

    def test_save_passes_serialize_bytes_once_to_transaction_boundary(
        self, modifier, monkeypatch, tmp_path
    ):
        destination = tmp_path / "exact.fcpxml"
        expected = modifier.serialize()
        calls = []

        def record_commit(**kwargs):
            calls.append(kwargs)
            return SimpleNamespace(destination=destination)

        monkeypatch.setattr(
            "fcp_mcp.fcpxml.writer.commit_fcpxml_bytes",
            record_commit,
        )

        receipt = modifier.save_with_receipt(destination, event_format="json")

        assert receipt.destination == destination
        assert len(calls) == 1
        assert calls[0] == {
            "source": modifier.path,
            "destination": destination,
            "xml_bytes": expected,
            "event_format": "json",
            "operation": "modifier_save",
        }

    def test_save_and_reparse(self, modifier, parser, tmp_path):
        output = tmp_path / "roundtrip.fcpxml"
        modifier.add_marker("Interview_A", "0s", "Test Marker")
        modifier.save(output)

        doc = parser.parse(output)
        clips = doc.all_projects[0].sequence.spine.clips
        interview = clips[0]
        marker_values = [m.value for m in interview.markers]
        assert "Test Marker" in marker_values
