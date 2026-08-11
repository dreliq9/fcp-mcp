"""Tests for FCPXML parser."""

from pathlib import Path

import pytest

from fcp_mcp.fcpxml.models import (
    ClipType,
    FCPXMLDocument,
    MarkerType,
    TimecodeFormat,
)
from fcp_mcp.fcpxml.parser import FCPXMLParser
from fcp_mcp.fcpxml.time_utils import RationalTime


class TestParserBasic:
    def test_parse_version(self, sample_doc: FCPXMLDocument):
        assert sample_doc.version == "1.11"

    def test_parse_formats(self, sample_doc: FCPXMLDocument):
        assert "r1" in sample_doc.formats
        fmt = sample_doc.formats["r1"]
        assert fmt.name == "FFVideoFormat1080p2997"
        assert fmt.width == 1920
        assert fmt.height == 1080
        assert fmt.frame_duration == RationalTime(1001, 30000)

    def test_parse_assets(self, sample_doc: FCPXMLDocument):
        assert len(sample_doc.assets) == 4
        interview = sample_doc.assets["r2"]
        assert interview.name == "Interview_A"
        assert interview.has_video is True
        assert interview.has_audio is True

        music = sample_doc.assets["r5"]
        assert music.name == "Music_Track"
        assert music.has_video is False
        assert music.has_audio is True

    def test_parse_effects(self, sample_doc: FCPXMLDocument):
        assert "r6" in sample_doc.effects
        assert sample_doc.effects["r6"].name == "Basic Title"
        assert "r7" in sample_doc.effects
        assert sample_doc.effects["r7"].name == "Cross Dissolve"

    def test_parse_transform_preserves_xy_scale(self, tmp_path: Path):
        path = tmp_path / "xy-scale.fcpxml"
        path.write_text(
            """<?xml version="1.0" encoding="UTF-8"?>
<fcpxml version="1.11">
  <resources>
    <format id="r1" frameDuration="1/30s" width="1920" height="1080"/>
    <asset id="r2" name="Clip" duration="1s"/>
  </resources>
  <event name="Event">
    <project name="Project">
      <sequence format="r1" duration="1s">
        <spine>
          <asset-clip ref="r2" name="Clip" duration="1s">
            <adjust-transform scale="1.25 0.75"/>
          </asset-clip>
        </spine>
      </sequence>
    </project>
  </event>
</fcpxml>
""",
            encoding="utf-8",
        )

        transform = (
            FCPXMLParser()
            .parse(path)
            .all_projects[0]
            .sequence.spine.clips[0]
            .transform
        )

        assert transform is not None
        assert transform.scale == 1.25
        assert transform.scale_y == 0.75

    def test_parse_fcpxml_114_search_collections(self, parser: FCPXMLParser):
        document = parser.parse_bytes(
            b'''<?xml version="1.0" encoding="UTF-8"?>
<fcpxml version="1.14">
  <event name="Searches">
    <smart-collection name="Related dialogue" match="all">
      <match-text enabled="1" rule="isRelatedTo" value="cloud outage" scope="transcript"/>
      <match-analysis-type enabled="1" rule="isAvailable" value="transcript"/>
    </smart-collection>
    <smart-collection name="Beach shots" match="all">
      <match-text enabled="1" rule="includes" value="beach at sunset" scope="visual"/>
      <match-analysis-type enabled="1" rule="isAvailable" value="visual"/>
    </smart-collection>
  </event>
</fcpxml>'''
        )

        assert document.version == "1.14"
        assert [item.name for item in document.smart_collections] == [
            "Related dialogue",
            "Beach shots",
        ]
        assert document.smart_collections[0].predicates[0].kind == "match-text"
        assert document.smart_collections[0].predicates[0].attributes == {
            "enabled": "1",
            "rule": "isRelatedTo",
            "value": "cloud outage",
            "scope": "transcript",
        }
        assert document.smart_collections[1].predicates[1].attributes["value"] == "visual"


class TestParserLibrary:
    def test_library_exists(self, sample_doc: FCPXMLDocument):
        assert sample_doc.library is not None
        assert "MyLibrary" in sample_doc.library.location

    def test_event(self, sample_doc: FCPXMLDocument):
        events = sample_doc.library.events
        assert len(events) == 1
        assert events[0].name == "Travel Video"

    def test_project(self, sample_doc: FCPXMLDocument):
        projects = sample_doc.all_projects
        assert len(projects) == 1
        assert projects[0].name == "Travel Vlog v1"

    def test_sequence(self, sample_doc: FCPXMLDocument):
        seq = sample_doc.all_projects[0].sequence
        assert seq is not None
        assert seq.tc_format == TimecodeFormat.NON_DROP_FRAME
        assert seq.format_ref == "r1"


class TestParserClips:
    def test_clip_count(self, sample_doc: FCPXMLDocument):
        spine = sample_doc.all_projects[0].sequence.spine
        # 6 clips in spine: interview, beach, gap, city, flash, outro
        # (transition is not counted as a clip currently)
        assert spine.clip_count == 6

    def test_first_clip(self, sample_doc: FCPXMLDocument):
        clip = sample_doc.all_projects[0].sequence.spine.clips[0]
        assert clip.clip_type == ClipType.ASSET_CLIP
        assert clip.name == "Interview_A"
        assert clip.ref == "r2"
        assert clip.role == "Dialogue"
        assert clip.offset == RationalTime(0, 1)
        assert clip.duration == RationalTime(150150, 30000)

    def test_gap_clip(self, sample_doc: FCPXMLDocument):
        gap = sample_doc.all_projects[0].sequence.spine.clips[2]
        assert gap.is_gap is True
        assert gap.clip_type == ClipType.GAP
        assert gap.duration == RationalTime(3003, 30000)

    def test_non_gap_clips(self, sample_doc: FCPXMLDocument):
        spine = sample_doc.all_projects[0].sequence.spine
        non_gaps = spine.non_gap_clips
        assert len(non_gaps) == 5  # all except the gap

    def test_clip_roles(self, sample_doc: FCPXMLDocument):
        clips = sample_doc.all_projects[0].sequence.spine.clips
        assert clips[0].role == "Dialogue"  # Interview
        assert clips[1].role == "Video"     # Beach
        assert clips[3].role == "Video"     # City


class TestParserMarkers:
    def test_markers_on_clip(self, sample_doc: FCPXMLDocument):
        clip = sample_doc.all_projects[0].sequence.spine.clips[0]  # Interview
        assert len(clip.markers) == 1
        marker = clip.markers[0]
        assert marker.value == "Good take"
        assert marker.note == "Use this section"
        assert marker.marker_type == MarkerType.STANDARD

    def test_chapter_marker(self, sample_doc: FCPXMLDocument):
        clip = sample_doc.all_projects[0].sequence.spine.clips[3]  # City
        assert len(clip.markers) == 1
        assert clip.markers[0].marker_type == MarkerType.CHAPTER
        assert clip.markers[0].value == "City Section"


class TestParserKeywords:
    def test_keywords(self, sample_doc: FCPXMLDocument):
        clip = sample_doc.all_projects[0].sequence.spine.clips[0]  # Interview
        assert len(clip.keywords) == 1
        assert clip.keywords[0].value == "intro"

    def test_outro_keyword(self, sample_doc: FCPXMLDocument):
        clip = sample_doc.all_projects[0].sequence.spine.clips[5]  # Outro
        assert len(clip.keywords) == 1
        assert clip.keywords[0].value == "outro"


class TestParserConnectedClips:
    def test_connected_title(self, sample_doc: FCPXMLDocument):
        beach = sample_doc.all_projects[0].sequence.spine.clips[1]  # Beach
        assert len(beach.connected_clips) == 1
        title = beach.connected_clips[0]
        assert title.clip_type == ClipType.TITLE
        assert title.name == "Beach Scene"
        assert title.ref == "r6"


class TestParserEdgeCases:
    def test_flash_frame_detected(self, sample_doc: FCPXMLDocument):
        """The sample has a 2-frame clip that should be parseable."""
        flash = sample_doc.all_projects[0].sequence.spine.clips[4]
        assert flash.name == "Broll_Beach_Flash"
        # 2002/30000s = ~0.067s = 2 frames at 29.97
        assert flash.duration == RationalTime(2002, 30000)
        assert flash.duration_seconds < 0.1

    def test_file_not_found(self, parser: FCPXMLParser):
        with pytest.raises(FileNotFoundError):
            parser.parse("/nonexistent/file.fcpxml")
