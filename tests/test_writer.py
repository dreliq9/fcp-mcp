"""Tests for FCPXML writer/modifier."""

from pathlib import Path

import pytest

from fcp_mcp.fcpxml.parser import FCPXMLParser
from fcp_mcp.fcpxml.writer import FCPXMLModifier

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
        result = modifier.trim_clip("Interview_A", new_duration="120120/30000s")
        assert result is True

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

    def test_fill_gaps(self, modifier):
        count = modifier.fill_gaps("r3", "Fill Clip")
        assert count == 1  # sample has 1 gap

    def test_fix_flash_frames(self, modifier):
        count = modifier.fix_flash_frames(min_frames=3)
        assert count == 1  # sample has 1 flash frame (2 frames)


class TestSaveRoundtrip:
    def test_save_and_reparse(self, modifier, parser, tmp_path):
        output = tmp_path / "roundtrip.fcpxml"
        modifier.add_marker("Interview_A", "0s", "Test Marker")
        modifier.save(output)

        doc = parser.parse(output)
        clips = doc.all_projects[0].sequence.spine.clips
        interview = clips[0]
        marker_values = [m.value for m in interview.markers]
        assert "Test Marker" in marker_values
