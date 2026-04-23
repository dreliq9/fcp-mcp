"""Tests for FCPXML analysis."""

import pytest

from fcp_mcp.fcpxml.models import FCPXMLDocument
from fcp_mcp.fcpxml.analysis import (
    analyze_timeline_stats,
    analyze_pacing,
    detect_gaps,
    detect_flash_frames,
    detect_duplicates,
)


class TestTimelineStats:
    def test_basic_stats(self, sample_doc: FCPXMLDocument):
        stats_list = analyze_timeline_stats(sample_doc)
        assert len(stats_list) == 1
        stats = stats_list[0]
        assert stats.project_name == "Travel Vlog v1"
        assert stats.clip_count == 6
        assert stats.non_gap_clip_count == 5
        assert stats.gap_count == 1
        assert stats.resolution == "1920x1080"
        assert stats.fps == pytest.approx(29.97, abs=0.01)

    def test_marker_count(self, sample_doc: FCPXMLDocument):
        stats = analyze_timeline_stats(sample_doc)[0]
        assert stats.marker_count == 3  # 2 standard + 1 chapter

    def test_keyword_count(self, sample_doc: FCPXMLDocument):
        stats = analyze_timeline_stats(sample_doc)[0]
        assert stats.keyword_count == 2  # intro + outro

    def test_roles(self, sample_doc: FCPXMLDocument):
        stats = analyze_timeline_stats(sample_doc)[0]
        assert "Dialogue" in stats.roles_used
        assert "Video" in stats.roles_used

    def test_connected_clips(self, sample_doc: FCPXMLDocument):
        stats = analyze_timeline_stats(sample_doc)[0]
        assert stats.connected_clip_count == 1  # Beach title


class TestPacing:
    def test_pacing_analysis(self, sample_doc: FCPXMLDocument):
        pacing_list = analyze_pacing(sample_doc)
        assert len(pacing_list) == 1
        pacing = pacing_list[0]
        assert pacing.average_shot_length > 0
        assert pacing.shortest_shot > 0
        assert pacing.longest_shot >= pacing.shortest_shot
        assert len(pacing.pacing_curve) == 5  # 5 non-gap clips

    def test_histogram(self, sample_doc: FCPXMLDocument):
        pacing = analyze_pacing(sample_doc)[0]
        total = sum(pacing.histogram.values())
        assert total == 5


class TestGapDetection:
    def test_detect_gaps(self, sample_doc: FCPXMLDocument):
        gaps = detect_gaps(sample_doc)
        assert len(gaps) == 1
        gap = gaps[0]
        assert gap.duration_seconds == pytest.approx(3003 / 30000, abs=0.001)
        assert gap.before_clip == "Broll_Beach"
        assert gap.after_clip == "Broll_City"


class TestFlashFrames:
    def test_detect_flash_frames(self, sample_doc: FCPXMLDocument):
        flashes = detect_flash_frames(sample_doc, max_frames=2)
        assert len(flashes) == 1
        flash = flashes[0]
        assert flash.clip_name == "Broll_Beach_Flash"
        assert flash.frame_count == 2

    def test_no_flash_frames_high_threshold(self, sample_doc: FCPXMLDocument):
        flashes = detect_flash_frames(sample_doc, max_frames=100)
        # With a very high threshold, more clips should be flagged
        assert len(flashes) >= 1


class TestDuplicates:
    def test_detect_duplicates(self, sample_doc: FCPXMLDocument):
        dupes = detect_duplicates(sample_doc)
        # r2 (Interview_A) is used twice, r3 (Broll_Beach) is used twice
        assert len(dupes) == 2
        refs = {d.asset_ref for d in dupes}
        assert "r2" in refs
        assert "r3" in refs
