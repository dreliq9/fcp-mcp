"""Tests for FCPXML generator."""

from pathlib import Path

import pytest

from fcp_mcp.fcpxml.generator import FCPXMLGenerator
from fcp_mcp.fcpxml.parser import FCPXMLParser


@pytest.fixture
def gen():
    return FCPXMLGenerator()


@pytest.fixture
def parser():
    return FCPXMLParser()


class TestResources:
    def test_add_format(self, gen):
        rid = gen.add_format()
        assert rid.startswith("r")

    def test_add_format_deduplicates(self, gen):
        r1 = gen.add_format(name="FFVideoFormat1080p2997")
        r2 = gen.add_format(name="FFVideoFormat1080p2997")
        assert r1 == r2

    def test_add_asset(self, gen):
        rid = gen.add_asset("/tmp/test.mov", name="Test Clip", duration="300300/30000s")
        assert rid.startswith("r")

    def test_add_asset_normalizes_path(self, gen):
        rid = gen.add_asset("/tmp/test.mov")
        xml = gen.to_string()
        assert "file://" in xml

    def test_add_effect(self, gen):
        rid = gen.add_effect("Cross Dissolve")
        assert rid.startswith("r")


class TestProjectCreation:
    def test_create_project(self, gen):
        spine = gen.create_project(name="Test Project", event_name="Test Event")
        assert spine is not None
        assert spine.tag == "spine"

    def test_create_project_with_library(self, gen):
        spine = gen.create_project(name="Test", library_name="MyLib")
        xml = gen.to_string()
        assert "MyLib" in xml
        assert "library" in xml


class TestClipBuilding:
    def test_add_clip(self, gen):
        fmt = gen.add_format()
        asset = gen.add_asset("/tmp/test.mov", duration="300300/30000s", format_ref=fmt)
        spine = gen.create_project(name="Test", format_ref=fmt)

        clip = gen.add_clip_to_spine(spine, asset, name="Clip 1",
                                      duration="150150/30000s")
        assert clip is not None
        assert clip.get("name") == "Clip 1"
        assert clip.get("offset") == "0s"

    def test_auto_offset(self, gen):
        fmt = gen.add_format()
        asset = gen.add_asset("/tmp/test.mov", duration="300300/30000s", format_ref=fmt)
        spine = gen.create_project(name="Test", format_ref=fmt)

        gen.add_clip_to_spine(spine, asset, name="C1", duration="150150/30000s")
        clip2 = gen.add_clip_to_spine(spine, asset, name="C2", duration="150150/30000s")
        # Generator uses simplified fractions, so check value equivalence
        from fcp_mcp.fcpxml.time_utils import RationalTime
        expected = RationalTime.from_fcpxml("150150/30000s")
        actual = RationalTime.from_fcpxml(clip2.get("offset"))
        assert actual == expected

    def test_add_gap(self, gen):
        spine = gen.create_project(name="Test")
        gap = gen.add_gap_to_spine(spine, duration="30030/30000s")
        assert gap.tag == "gap"

    def test_add_title(self, gen):
        effect_ref = gen.add_effect("Basic Title")
        spine = gen.create_project(name="Test")
        title = gen.add_title_to_spine(spine, effect_ref, name="My Title",
                                        duration="150150/30000s")
        assert title.tag == "title"
        assert title.get("role") == "Titles"

    def test_add_connected_clip(self, gen):
        fmt = gen.add_format()
        asset = gen.add_asset("/tmp/test.mov", duration="300300/30000s", format_ref=fmt)
        spine = gen.create_project(name="Test", format_ref=fmt)
        primary = gen.add_clip_to_spine(spine, asset, name="Primary",
                                         duration="150150/30000s")
        connected = gen.add_connected_clip(primary, asset, name="Overlay",
                                            duration="60060/30000s", lane=1)
        assert connected.get("lane") == "1"

    def test_add_marker(self, gen):
        fmt = gen.add_format()
        asset = gen.add_asset("/tmp/test.mov", duration="300300/30000s", format_ref=fmt)
        spine = gen.create_project(name="Test", format_ref=fmt)
        clip = gen.add_clip_to_spine(spine, asset, name="C1", duration="150150/30000s")
        marker = gen.add_marker(clip, "30030/30000s", "Mark Here", note="Important")
        assert marker.tag == "marker"


class TestTimelineBuilder:
    def test_build_from_clips(self, gen, parser, tmp_path):
        clips = [
            {"src": "/tmp/a.mov", "name": "A", "duration": "150150/30000s"},
            {"src": "/tmp/b.mov", "name": "B", "duration": "90090/30000s"},
            {"src": "/tmp/c.mov", "name": "C", "duration": "60060/30000s", "role": "B-Roll"},
        ]
        root = gen.build_timeline_from_clips(clips, project_name="Auto Timeline")

        output = tmp_path / "timeline.fcpxml"
        gen.save(output)

        doc = parser.parse(output)
        assert len(doc.all_projects) == 1
        assert doc.all_projects[0].name == "Auto Timeline"
        spine_clips = doc.all_projects[0].sequence.spine.clips
        assert len(spine_clips) == 3
        assert spine_clips[0].name == "A"
        assert spine_clips[2].role == "B-Roll"

    def test_nonzero_start_preserves_explicit_full_asset_duration(self, gen):
        gen.build_timeline_from_clips(
            [
                {
                    "src": "/tmp/source.mov",
                    "name": "Source range",
                    "start": "900900/30000s",
                    "duration": "150150/30000s",
                    "asset_duration": "5735730/30000s",
                }
            ]
        )

        asset = gen.root.find("./resources/asset")
        clip = gen.root.find(".//asset-clip")
        assert asset is not None
        assert clip is not None
        assert asset.get("duration") == "5735730/30000s"
        assert clip.get("start") == "900900/30000s"
        assert clip.get("duration") == "150150/30000s"

    def test_nonzero_start_derives_asset_duration_from_source_end(self, gen):
        gen.build_timeline_from_clips(
            [
                {
                    "src": "/tmp/source.mov",
                    "start": "900900/30000s",
                    "duration": "150150/30000s",
                }
            ]
        )

        asset = gen.root.find("./resources/asset")
        assert asset is not None
        assert asset.get("duration") == "7007/200s"

    def test_repeated_source_uses_maximum_selected_source_end(self, gen):
        gen.build_timeline_from_clips(
            [
                {
                    "src": "/tmp/source.mov",
                    "start": "2s",
                    "duration": "3s",
                },
                {
                    "src": "/tmp/source.mov",
                    "start": "10s",
                    "duration": "2s",
                },
            ]
        )

        assets = gen.root.findall("./resources/asset")
        clips = gen.root.findall(".//asset-clip")
        assert len(assets) == 1
        assert assets[0].get("duration") == "12s"
        assert [clip.get("ref") for clip in clips] == [
            assets[0].get("id"),
            assets[0].get("id"),
        ]

    def test_explicit_asset_duration_rejects_shorter_selected_range(self, gen):
        with pytest.raises(
            ValueError,
            match="asset_duration.*must cover selected source range",
        ):
            gen.build_timeline_from_clips(
                [
                    {
                        "src": "/tmp/source.mov",
                        "start": "10s",
                        "duration": "2s",
                        "asset_duration": "11s",
                    }
                ]
            )

    def test_zero_start_keeps_selected_duration_as_asset_duration(self, gen):
        gen.build_timeline_from_clips(
            [
                {
                    "src": "/tmp/source.mov",
                    "start": "0s",
                    "duration": "150150/30000s",
                }
            ]
        )

        asset = gen.root.find("./resources/asset")
        assert asset is not None
        assert asset.get("duration") == "150150/30000s"


class TestOutput:
    def test_save_creates_file(self, gen, tmp_path):
        gen.create_project(name="Test")
        output = tmp_path / "generated.fcpxml"
        gen.save(output)
        assert output.exists()
        content = output.read_text()
        assert "fcpxml" in content
        assert 'version="1.11"' in content

    def test_to_string(self, gen):
        gen.create_project(name="Test")
        xml = gen.to_string()
        assert "fcpxml" in xml
        assert "Test" in xml
