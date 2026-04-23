"""Puppet animation engine for FCPXML.

Builds cutout-style character animations: separate body part images
positioned and keyframed on a timeline. Think South Park / paper doll animation.

Workflow:
1. Define a rig (character parts + positions)
2. Build a scene (generates FCPXML with layered clips)
3. Add motions (keyframe animations on parts)
4. Apply presets (walk, wave, bounce, talk)
"""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .generator import FCPXMLGenerator
from .time_utils import RationalTime


@dataclass
class Keyframe:
    """Single keyframe in an animation curve."""
    time: str  # FCPXML rational time, e.g. "150150/30000s"
    value: float | tuple[float, float]  # scalar or (x, y)
    interp: str = "smooth2"  # "linear", "smooth2" (ease), "hold"


@dataclass
class PuppetPart:
    """One piece of a puppet character (e.g., head, arm, leg)."""
    name: str
    image_path: str  # absolute path to PNG
    position: tuple[float, float] = (0.0, 0.0)  # (x, y) from center
    scale: float = 1.0
    rotation: float = 0.0  # degrees
    anchor: tuple[float, float] = (0.0, 0.0)  # pivot point
    z_order: int = 0  # higher = in front (maps to lane number)
    # Optional: image dimensions for overlap calculations
    width: int = 0
    height: int = 0


@dataclass
class PuppetRig:
    """A complete character made of parts."""
    name: str
    parts: list[PuppetPart] = field(default_factory=list)
    # Base position of the whole character on screen
    position: tuple[float, float] = (0.0, 0.0)

    def add_part(self, part: PuppetPart) -> None:
        self.parts.append(part)

    def get_part(self, name: str) -> Optional[PuppetPart]:
        for p in self.parts:
            if p.name == name:
                return p
        return None

    @property
    def part_names(self) -> list[str]:
        return [p.name for p in self.parts]


@dataclass
class PartAnimation:
    """Keyframe animation for one property of one part."""
    part_name: str
    property_name: str  # "position", "scale", "rotation"
    keyframes: list[Keyframe] = field(default_factory=list)


def _time_seconds(time_str: str) -> float:
    """Convert FCPXML time string to seconds."""
    return RationalTime.from_fcpxml(time_str).to_seconds()


def _time_from_seconds(seconds: float) -> str:
    """Convert seconds to FCPXML time string at 30000 timescale."""
    return RationalTime.from_seconds(seconds, 30000).to_fcpxml()


def _lerp(a: float, b: float, t: float) -> float:
    """Linear interpolation."""
    return a + (b - a) * t


# ---------------------------------------------------------------------------
# FCPXML keyframe XML generation
# ---------------------------------------------------------------------------

def add_transform_to_clip(
    clip_element: ET.Element,
    position: tuple[float, float] = (0.0, 0.0),
    scale: tuple[float, float] = (1.0, 1.0),
    rotation: float = 0.0,
    anchor: tuple[float, float] = (0.0, 0.0),
) -> ET.Element:
    """Add a static adjust-transform to a clip element."""
    transform = ET.SubElement(clip_element, "adjust-transform")
    transform.set("position", f"{position[0]:.1f} {position[1]:.1f}")
    transform.set("scale", f"{scale[0]:.4f} {scale[1]:.4f}")
    transform.set("rotation", f"{rotation:.1f}")
    transform.set("anchor", f"{anchor[0]:.1f} {anchor[1]:.1f}")
    return transform


def add_keyframed_param(
    transform_element: ET.Element,
    param_name: str,
    keyframes: list[Keyframe],
) -> ET.Element:
    """Add an animated param with keyframes inside an adjust-transform.

    param_name: "position", "scale", or "rotation"
    For position/scale, keyframe values should be (x, y) tuples.
    For rotation, keyframe values should be floats.
    """
    param = ET.SubElement(transform_element, "param")
    param.set("name", param_name)

    for kf in keyframes:
        kf_elem = ET.SubElement(param, "keyframe")
        kf_elem.set("time", kf.time)
        if isinstance(kf.value, tuple):
            kf_elem.set("value", f"{kf.value[0]:.1f} {kf.value[1]:.1f}")
        else:
            kf_elem.set("value", f"{kf.value:.1f}")
        kf_elem.set("interp", kf.interp)

    return param


# ---------------------------------------------------------------------------
# Scene builder — assembles rig into FCPXML
# ---------------------------------------------------------------------------

class PuppetSceneBuilder:
    """Build an FCPXML timeline from puppet rigs and animations."""

    def __init__(
        self,
        duration: str = "300300/30000s",  # 10 seconds default
        format_name: str = "FFVideoFormat1080p2997",
        width: int = 1920,
        height: int = 1080,
        frame_duration: str = "1001/30000s",
    ):
        self.duration = duration
        self.format_name = format_name
        self.width = width
        self.height = height
        self.frame_duration = frame_duration
        self.rigs: list[PuppetRig] = []
        self.animations: list[PartAnimation] = []
        self._rig_lane_base: dict[str, int] = {}  # rig_name → starting lane

    def add_rig(self, rig: PuppetRig) -> None:
        """Add a character rig to the scene."""
        # Assign lane base: each rig gets a block of lanes
        if self.rigs:
            last_rig = self.rigs[-1]
            last_base = self._rig_lane_base[last_rig.name]
            self._rig_lane_base[rig.name] = last_base + len(last_rig.parts) + 1
        else:
            self._rig_lane_base[rig.name] = 1
        self.rigs.append(rig)

    def add_animation(self, anim: PartAnimation) -> None:
        """Add a keyframe animation to a part."""
        self.animations.append(anim)

    def _get_animations_for_part(self, part_name: str) -> list[PartAnimation]:
        return [a for a in self.animations if a.part_name == part_name]

    def build(
        self,
        project_name: str = "Puppet Animation",
        event_name: str = "Animation",
    ) -> FCPXMLGenerator:
        """Generate the FCPXML document."""
        gen = FCPXMLGenerator()
        fmt_ref = gen.add_format(
            name=self.format_name,
            width=self.width,
            height=self.height,
            frame_duration=self.frame_duration,
        )

        spine = gen.create_project(
            name=project_name,
            format_ref=fmt_ref,
            duration=self.duration,
            event_name=event_name,
        )

        # Primary storyline: a gap for the full duration (empty background)
        gap = gen.add_gap_to_spine(spine, duration=self.duration)

        # Each rig's parts become connected clips on the gap
        for rig in self.rigs:
            base_lane = self._rig_lane_base[rig.name]
            # Sort parts by z_order so lane assignment respects depth
            sorted_parts = sorted(rig.parts, key=lambda p: p.z_order)

            for i, part in enumerate(sorted_parts):
                lane = base_lane + i

                # Register the image as an asset
                # For still images, duration = scene duration (FCP holds stills)
                asset_ref = gen.add_asset(
                    src=part.image_path,
                    name=f"{rig.name}_{part.name}",
                    duration=self.duration,
                    has_video=True,
                    has_audio=False,
                    format_ref=fmt_ref,
                )

                # Add as connected clip on the gap
                clip = gen.add_connected_clip(
                    parent_clip=gap,
                    asset_ref=asset_ref,
                    name=f"{rig.name}_{part.name}",
                    offset="0s",
                    start="0s",
                    duration=self.duration,
                    lane=lane,
                )

                # Calculate absolute position (rig base + part offset)
                abs_x = rig.position[0] + part.position[0]
                abs_y = rig.position[1] + part.position[1]

                # Check for animations on this part
                part_anims = self._get_animations_for_part(part.name)

                # Add transform (static or animated)
                transform = add_transform_to_clip(
                    clip,
                    position=(abs_x, abs_y),
                    scale=(part.scale, part.scale),
                    rotation=part.rotation,
                    anchor=part.anchor,
                )

                # Add keyframed params if animated
                for anim in part_anims:
                    if anim.property_name == "position":
                        # Offset keyframe values by rig base position
                        offset_kfs = []
                        for kf in anim.keyframes:
                            if isinstance(kf.value, tuple):
                                offset_val = (
                                    kf.value[0] + rig.position[0],
                                    kf.value[1] + rig.position[1],
                                )
                            else:
                                offset_val = kf.value
                            offset_kfs.append(Keyframe(
                                time=kf.time, value=offset_val, interp=kf.interp
                            ))
                        add_keyframed_param(transform, "position", offset_kfs)
                    elif anim.property_name == "rotation":
                        add_keyframed_param(transform, "rotation", anim.keyframes)
                    elif anim.property_name == "scale":
                        add_keyframed_param(transform, "scale", anim.keyframes)

        return gen


# ---------------------------------------------------------------------------
# Motion presets — generate keyframes for common animations
# ---------------------------------------------------------------------------

def preset_bounce(
    part: PuppetPart,
    duration: str,
    amplitude: float = 20.0,
    cycles: int = 3,
    fps: float = 29.97,
) -> PartAnimation:
    """Vertical bounce animation (like breathing or idle bob)."""
    dur_sec = _time_seconds(duration)
    keyframes = []
    # Generate keyframes at key points in the sine wave
    num_points = cycles * 4 + 1  # 4 points per cycle + endpoint
    for i in range(num_points):
        t = i / (num_points - 1)
        time_sec = t * dur_sec
        angle = t * cycles * 2 * math.pi
        y_offset = part.position[1] + amplitude * math.sin(angle)
        keyframes.append(Keyframe(
            time=_time_from_seconds(time_sec),
            value=(part.position[0], y_offset),
            interp="smooth2",
        ))

    return PartAnimation(
        part_name=part.name,
        property_name="position",
        keyframes=keyframes,
    )


def preset_wave(
    part: PuppetPart,
    duration: str,
    angle_range: float = 45.0,
    cycles: int = 2,
) -> PartAnimation:
    """Rotation wave animation (e.g., hand waving)."""
    dur_sec = _time_seconds(duration)
    keyframes = []
    num_points = cycles * 4 + 1
    for i in range(num_points):
        t = i / (num_points - 1)
        time_sec = t * dur_sec
        angle = part.rotation + angle_range * math.sin(t * cycles * 2 * math.pi)
        keyframes.append(Keyframe(
            time=_time_from_seconds(time_sec),
            value=angle,
            interp="smooth2",
        ))

    return PartAnimation(
        part_name=part.name,
        property_name="rotation",
        keyframes=keyframes,
    )


def preset_walk(
    rig: PuppetRig,
    duration: str,
    stride: float = 100.0,
    bob_height: float = 15.0,
    arm_swing: float = 30.0,
    leg_swing: float = 35.0,
    cycles: int = 3,
) -> list[PartAnimation]:
    """Walk cycle preset. Generates animations for standard body parts.

    Expects parts named: body/torso, head, left_arm, right_arm, left_leg, right_leg.
    Parts that don't exist are silently skipped.
    """
    dur_sec = _time_seconds(duration)
    animations = []
    num_points = cycles * 4 + 1

    def _time_at(t: float) -> str:
        return _time_from_seconds(t * dur_sec)

    # Body bob (vertical sine, half frequency of legs)
    body = rig.get_part("body") or rig.get_part("torso")
    if body:
        kfs = []
        for i in range(num_points):
            t = i / (num_points - 1)
            y = body.position[1] + bob_height * abs(math.sin(t * cycles * 2 * math.pi))
            kfs.append(Keyframe(time=_time_at(t), value=(body.position[0], y), interp="smooth2"))
        animations.append(PartAnimation(part_name=body.name, property_name="position", keyframes=kfs))

    # Head follows body bob (slightly delayed/dampened)
    head = rig.get_part("head")
    if head:
        kfs = []
        for i in range(num_points):
            t = i / (num_points - 1)
            y = head.position[1] + bob_height * 0.7 * abs(math.sin(t * cycles * 2 * math.pi + 0.2))
            kfs.append(Keyframe(time=_time_at(t), value=(head.position[0], y), interp="smooth2"))
        animations.append(PartAnimation(part_name=head.name, property_name="position", keyframes=kfs))

    # Arm swing (rotation, opposite phase)
    for arm_name, phase in [("left_arm", 0.0), ("right_arm", math.pi)]:
        arm = rig.get_part(arm_name)
        if arm:
            kfs = []
            for i in range(num_points):
                t = i / (num_points - 1)
                angle = arm.rotation + arm_swing * math.sin(t * cycles * 2 * math.pi + phase)
                kfs.append(Keyframe(time=_time_at(t), value=angle, interp="smooth2"))
            animations.append(PartAnimation(part_name=arm_name, property_name="rotation", keyframes=kfs))

    # Leg swing (rotation, opposite phase to each other, same phase as opposite arm)
    for leg_name, phase in [("left_leg", math.pi), ("right_leg", 0.0)]:
        leg = rig.get_part(leg_name)
        if leg:
            kfs = []
            for i in range(num_points):
                t = i / (num_points - 1)
                angle = leg.rotation + leg_swing * math.sin(t * cycles * 2 * math.pi + phase)
                kfs.append(Keyframe(time=_time_at(t), value=angle, interp="smooth2"))
            animations.append(PartAnimation(part_name=leg_name, property_name="rotation", keyframes=kfs))

    return animations


def preset_talk(
    rig: PuppetRig,
    duration: str,
    jaw_range: float = 15.0,
    head_bob: float = 5.0,
    tempo: float = 4.0,
) -> list[PartAnimation]:
    """Talking animation. Animates jaw/mouth and subtle head movement.

    Expects parts named: head, mouth/jaw.
    Uses irregular rhythm to look more natural than a simple sine.
    """
    dur_sec = _time_seconds(duration)
    animations = []
    num_points = max(int(dur_sec * tempo * 4), 12)

    def _time_at(t: float) -> str:
        return _time_from_seconds(t * dur_sec)

    # Mouth/jaw — scale Y to simulate opening/closing
    mouth = rig.get_part("mouth") or rig.get_part("jaw")
    if mouth:
        kfs = []
        for i in range(num_points):
            t = i / (num_points - 1)
            # Combine two frequencies for irregular mouth movement
            open_amount = (
                0.6 * abs(math.sin(t * tempo * 2 * math.pi))
                + 0.4 * abs(math.sin(t * tempo * 3.7 * math.pi))
            )
            scale_y = 1.0 + (jaw_range / 100.0) * open_amount
            kfs.append(Keyframe(
                time=_time_at(t),
                value=(mouth.scale, mouth.scale * scale_y),
                interp="smooth2",
            ))
        animations.append(PartAnimation(
            part_name=mouth.name, property_name="scale", keyframes=kfs
        ))

    # Subtle head bob while talking
    head = rig.get_part("head")
    if head:
        kfs = []
        for i in range(num_points):
            t = i / (num_points - 1)
            y = head.position[1] + head_bob * math.sin(t * tempo * 1.3 * math.pi)
            x = head.position[0] + head_bob * 0.3 * math.sin(t * tempo * 0.7 * math.pi)
            kfs.append(Keyframe(
                time=_time_at(t),
                value=(x, y),
                interp="smooth2",
            ))
        animations.append(PartAnimation(
            part_name=head.name, property_name="position", keyframes=kfs
        ))

    return animations


def preset_idle(
    rig: PuppetRig,
    duration: str,
    breathe_amount: float = 8.0,
    sway_amount: float = 3.0,
) -> list[PartAnimation]:
    """Subtle idle/breathing animation to keep character alive.

    Applies gentle vertical breathing to body and slight sway to head.
    """
    dur_sec = _time_seconds(duration)
    animations = []
    num_points = 13  # Smooth sine, not too many keyframes

    def _time_at(t: float) -> str:
        return _time_from_seconds(t * dur_sec)

    body = rig.get_part("body") or rig.get_part("torso")
    if body:
        kfs = []
        for i in range(num_points):
            t = i / (num_points - 1)
            y = body.position[1] + breathe_amount * math.sin(t * 2 * math.pi)
            kfs.append(Keyframe(
                time=_time_at(t),
                value=(body.position[0], y),
                interp="smooth2",
            ))
        animations.append(PartAnimation(
            part_name=body.name, property_name="position", keyframes=kfs
        ))

    head = rig.get_part("head")
    if head:
        kfs = []
        for i in range(num_points):
            t = i / (num_points - 1)
            x = head.position[0] + sway_amount * math.sin(t * 1.5 * math.pi)
            y = head.position[1] + breathe_amount * 0.5 * math.sin(t * 2 * math.pi)
            kfs.append(Keyframe(
                time=_time_at(t),
                value=(x, y),
                interp="smooth2",
            ))
        animations.append(PartAnimation(
            part_name=head.name, property_name="position", keyframes=kfs
        ))

    return animations


# ---------------------------------------------------------------------------
# Rig builder helpers — create rigs from JSON or standard templates
# ---------------------------------------------------------------------------

def rig_from_json(data: dict) -> PuppetRig:
    """Build a PuppetRig from a JSON-compatible dict.

    Expected format:
    {
        "name": "character_name",
        "position": [x, y],
        "parts": [
            {
                "name": "head",
                "image": "/path/to/head.png",
                "position": [0, 200],
                "scale": 1.0,
                "rotation": 0,
                "anchor": [0, -50],
                "z_order": 5
            },
            ...
        ]
    }
    """
    rig = PuppetRig(
        name=data["name"],
        position=tuple(data.get("position", [0, 0])),
    )
    for part_data in data.get("parts", []):
        rig.add_part(PuppetPart(
            name=part_data["name"],
            image_path=part_data["image"],
            position=tuple(part_data.get("position", [0, 0])),
            scale=part_data.get("scale", 1.0),
            rotation=part_data.get("rotation", 0.0),
            anchor=tuple(part_data.get("anchor", [0, 0])),
            z_order=part_data.get("z_order", 0),
            width=part_data.get("width", 0),
            height=part_data.get("height", 0),
        ))
    return rig


def standard_humanoid_rig(
    name: str,
    image_dir: str,
    position: tuple[float, float] = (0.0, 0.0),
    scale: float = 1.0,
) -> PuppetRig:
    """Create a standard 6-part humanoid rig from a folder of images.

    Expects files named: head.png, body.png, left_arm.png, right_arm.png,
    left_leg.png, right_leg.png in image_dir.

    Positions are for a ~1080p frame with character roughly centered.
    """
    d = Path(image_dir)
    rig = PuppetRig(name=name, position=position)

    # Standard humanoid layout (y-positive = up in FCP coordinate system)
    parts_config = [
        ("head",      (0, 200),   (0, -80),  6),  # name, pos, anchor, z_order
        ("body",      (0, 0),     (0, 0),    3),
        ("left_arm",  (-100, 50), (40, 80),  2),
        ("right_arm", (100, 50),  (-40, 80), 4),
        ("left_leg",  (-40, -180),(0, 80),   1),
        ("right_leg", (40, -180), (0, 80),   1),
    ]

    for part_name, pos, anchor, z in parts_config:
        img = d / f"{part_name}.png"
        if img.exists():
            rig.add_part(PuppetPart(
                name=part_name,
                image_path=str(img),
                position=(pos[0] * scale, pos[1] * scale),
                scale=scale,
                anchor=(anchor[0] * scale, anchor[1] * scale),
                z_order=z,
            ))

    return rig
