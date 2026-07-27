"""Tests for puppet animation engine."""

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from fcp_mcp.fcpxml.puppet import (
    Keyframe,
    PartAnimation,
    PuppetPart,
    PuppetRig,
    PuppetSceneBuilder,
    add_keyframed_param,
    add_transform_to_clip,
    preset_bounce,
    preset_idle,
    preset_talk,
    preset_walk,
    preset_wave,
    rig_from_json,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def simple_rig():
    """A minimal 3-part rig for testing."""
    rig = PuppetRig(name="test_char", position=(0, 0))
    rig.add_part(PuppetPart(
        name="head", image_path="/tmp/head.png",
        position=(0, 200), anchor=(0, -80), z_order=3,
    ))
    rig.add_part(PuppetPart(
        name="body", image_path="/tmp/body.png",
        position=(0, 0), z_order=2,
    ))
    rig.add_part(PuppetPart(
        name="left_arm", image_path="/tmp/left_arm.png",
        position=(-100, 50), anchor=(40, 80), z_order=1,
    ))
    return rig


@pytest.fixture
def full_humanoid_rig():
    """A full 6-part humanoid rig."""
    rig = PuppetRig(name="full_char", position=(0, 0))
    for name, pos, anchor, z in [
        ("head", (0, 200), (0, -80), 6),
        ("body", (0, 0), (0, 0), 3),
        ("left_arm", (-100, 50), (40, 80), 2),
        ("right_arm", (100, 50), (-40, 80), 4),
        ("left_leg", (-40, -180), (0, 80), 1),
        ("right_leg", (40, -180), (0, 80), 1),
    ]:
        rig.add_part(PuppetPart(
            name=name, image_path=f"/tmp/{name}.png",
            position=pos, anchor=anchor, z_order=z,
        ))
    return rig


# ---------------------------------------------------------------------------
# PuppetRig tests
# ---------------------------------------------------------------------------

class TestPuppetRig:
    def test_create_rig(self, simple_rig):
        assert simple_rig.name == "test_char"
        assert len(simple_rig.parts) == 3

    def test_get_part(self, simple_rig):
        head = simple_rig.get_part("head")
        assert head is not None
        assert head.position == (0, 200)

    def test_get_missing_part(self, simple_rig):
        assert simple_rig.get_part("tail") is None

    def test_part_names(self, simple_rig):
        assert simple_rig.part_names == ["head", "body", "left_arm"]

    def test_rig_from_json(self):
        data = {
            "name": "json_char",
            "position": [100, -50],
            "parts": [
                {
                    "name": "head",
                    "image": "/tmp/head.png",
                    "position": [0, 200],
                    "scale": 0.8,
                    "z_order": 3,
                },
                {
                    "name": "body",
                    "image": "/tmp/body.png",
                    "z_order": 1,
                },
            ],
        }
        rig = rig_from_json(data)
        assert rig.name == "json_char"
        assert rig.position == (100, -50)
        assert len(rig.parts) == 2
        assert rig.get_part("head").scale == 0.8
        assert rig.get_part("body").position == (0, 0)  # default


# ---------------------------------------------------------------------------
# Transform XML generation
# ---------------------------------------------------------------------------

class TestTransformXML:
    def test_static_transform(self):
        clip = ET.Element("asset-clip")
        transform = add_transform_to_clip(
            clip, position=(100, -50), scale=(1.5, 1.5), rotation=45.0
        )
        assert transform.get("position") == "100.0 -50.0"
        assert transform.get("scale") == "1.5000 1.5000"
        assert transform.get("rotation") == "45.0"

    def test_keyframed_param(self):
        transform = ET.Element("adjust-transform")
        keyframes = [
            Keyframe(time="0s", value=(0, 200), interp="linear"),
            Keyframe(time="150150/30000s", value=(50, 210), interp="smooth2"),
            Keyframe(time="300300/30000s", value=(0, 200), interp="smooth2"),
        ]
        param = add_keyframed_param(transform, "position", keyframes)
        assert param.get("name") == "position"
        kf_elems = param.findall("keyframe")
        assert len(kf_elems) == 3
        assert kf_elems[0].get("time") == "0s"
        assert kf_elems[0].get("value") == "0.0 200.0"
        assert kf_elems[1].get("interp") == "smooth2"

    def test_scalar_keyframes(self):
        transform = ET.Element("adjust-transform")
        keyframes = [
            Keyframe(time="0s", value=0.0),
            Keyframe(time="300300/30000s", value=360.0),
        ]
        param = add_keyframed_param(transform, "rotation", keyframes)
        kf_elems = param.findall("keyframe")
        assert kf_elems[0].get("value") == "0.0"
        assert kf_elems[1].get("value") == "360.0"


# ---------------------------------------------------------------------------
# Scene builder
# ---------------------------------------------------------------------------

class TestPuppetSceneBuilder:
    def test_build_static_scene(self, simple_rig):
        builder = PuppetSceneBuilder(duration="300300/30000s")
        builder.add_rig(simple_rig)
        gen = builder.build(project_name="Test Scene")

        xml_str = gen.to_string()
        root = ET.fromstring(xml_str)

        # Should have a gap on the spine
        spine = root.find(".//spine")
        assert spine is not None
        gap = spine.find("gap")
        assert gap is not None

        # Should have connected clips for each part
        connected = gap.findall("asset-clip")
        assert len(connected) == 3  # head, body, left_arm

        # Check lanes are assigned (sorted by z_order)
        lanes = [int(c.get("lane", 0)) for c in connected]
        assert all(l > 0 for l in lanes)

        # Check transforms exist
        for clip in connected:
            transform = clip.find("adjust-transform")
            assert transform is not None

    def test_build_with_animation(self, simple_rig):
        builder = PuppetSceneBuilder(duration="300300/30000s")
        builder.add_rig(simple_rig)
        builder.add_animation(PartAnimation(
            part_name="head",
            property_name="position",
            keyframes=[
                Keyframe(time="0s", value=(0, 200)),
                Keyframe(time="300300/30000s", value=(20, 210)),
            ],
        ))
        gen = builder.build()
        xml_str = gen.to_string()
        root = ET.fromstring(xml_str)

        # Find the head clip and check for animated param
        for clip in root.iter("asset-clip"):
            if "head" in clip.get("name", ""):
                transform = clip.find("adjust-transform")
                param = transform.find("param")
                assert param is not None
                assert param.get("name") == "position"
                kfs = param.findall("keyframe")
                assert len(kfs) == 2

    def test_save_to_file(self, simple_rig, tmp_path):
        builder = PuppetSceneBuilder(duration="300300/30000s")
        builder.add_rig(simple_rig)
        gen = builder.build()

        path = gen.save(tmp_path / "puppet.fcpxml")
        assert Path(path).exists()
        content = Path(path).read_text()
        assert '<?xml version="1.0"' in content
        assert "fcpxml" in content

    def test_multi_rig_scene(self, simple_rig):
        rig2 = PuppetRig(name="char2", position=(400, 0))
        rig2.add_part(PuppetPart(
            name="head", image_path="/tmp/c2_head.png",
            position=(0, 200), z_order=1,
        ))

        builder = PuppetSceneBuilder(duration="300300/30000s")
        builder.add_rig(simple_rig)
        builder.add_rig(rig2)
        gen = builder.build()

        xml_str = gen.to_string()
        root = ET.fromstring(xml_str)
        gap = root.find(".//gap")
        connected = gap.findall("asset-clip")
        assert len(connected) == 4  # 3 from rig1 + 1 from rig2

    def test_rig_position_offset(self):
        """Character base position should offset all part positions."""
        rig = PuppetRig(name="offset_char", position=(300, -100))
        rig.add_part(PuppetPart(
            name="body", image_path="/tmp/body.png",
            position=(0, 0), z_order=1,
        ))
        builder = PuppetSceneBuilder(duration="300300/30000s")
        builder.add_rig(rig)
        gen = builder.build()

        xml_str = gen.to_string()
        root = ET.fromstring(xml_str)
        clip = root.find(".//gap/asset-clip")
        transform = clip.find("adjust-transform")
        pos = transform.get("position")
        assert pos == "300.0 -100.0"


# ---------------------------------------------------------------------------
# Preset motions
# ---------------------------------------------------------------------------

class TestPresets:
    def test_preset_bounce(self, simple_rig):
        body = simple_rig.get_part("body")
        anim = preset_bounce(body, "300300/30000s", amplitude=20, cycles=2)
        assert anim.part_name == "body"
        assert anim.property_name == "position"
        assert len(anim.keyframes) > 0
        # First and last keyframes should be near the original position
        first_val = anim.keyframes[0].value
        assert isinstance(first_val, tuple)

    def test_preset_wave(self, simple_rig):
        arm = simple_rig.get_part("left_arm")
        anim = preset_wave(arm, "300300/30000s", angle_range=45, cycles=2)
        assert anim.part_name == "left_arm"
        assert anim.property_name == "rotation"
        assert len(anim.keyframes) > 0
        # Values should be floats (rotation angles)
        for kf in anim.keyframes:
            assert isinstance(kf.value, float)

    def test_preset_walk(self, full_humanoid_rig):
        anims = preset_walk(full_humanoid_rig, "300300/30000s", cycles=2)
        animated_parts = {a.part_name for a in anims}
        # Should animate body, head, both arms, both legs
        assert "body" in animated_parts
        assert "head" in animated_parts
        assert "left_arm" in animated_parts
        assert "right_arm" in animated_parts
        assert "left_leg" in animated_parts
        assert "right_leg" in animated_parts

    def test_preset_walk_missing_parts(self, simple_rig):
        """Walk should silently skip missing parts."""
        anims = preset_walk(simple_rig, "300300/30000s")
        # simple_rig has head, body, left_arm — no right_arm or legs
        animated_parts = {a.part_name for a in anims}
        assert "right_arm" not in animated_parts
        assert "left_leg" not in animated_parts

    def test_preset_talk(self, full_humanoid_rig):
        # Add a mouth part
        full_humanoid_rig.add_part(PuppetPart(
            name="mouth", image_path="/tmp/mouth.png",
            position=(0, 150), z_order=7,
        ))
        anims = preset_talk(full_humanoid_rig, "300300/30000s")
        animated_parts = {a.part_name for a in anims}
        assert "mouth" in animated_parts
        assert "head" in animated_parts

    def test_preset_idle(self, full_humanoid_rig):
        anims = preset_idle(full_humanoid_rig, "300300/30000s")
        animated_parts = {a.part_name for a in anims}
        assert "body" in animated_parts
        assert "head" in animated_parts

    def test_preset_builds_valid_scene(self, full_humanoid_rig):
        """Full round-trip: preset → scene → valid FCPXML."""
        builder = PuppetSceneBuilder(duration="300300/30000s")
        builder.add_rig(full_humanoid_rig)

        for anim in preset_walk(full_humanoid_rig, "300300/30000s"):
            builder.add_animation(anim)

        gen = builder.build()
        xml_str = gen.to_string()

        # Should parse as valid XML
        root = ET.fromstring(xml_str)
        assert root.tag == "fcpxml"

        # Should have keyframe params on animated clips
        params_found = list(root.iter("param"))
        assert len(params_found) > 0

        keyframes_found = list(root.iter("keyframe"))
        assert len(keyframes_found) > 0
