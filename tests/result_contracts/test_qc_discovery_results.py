from __future__ import annotations

import importlib
import json
import os
from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import BaseModel

from fcp_mcp import server
from fcp_mcp.config import RuntimeConfig
from fcp_mcp.contracts import DoctorReport
from fcp_mcp.profiles import Profile
from fcp_mcp.security.paths import PathPolicy
from fcp_mcp.utils import paths as utility_paths

QC_MARKDOWN = """# QC Report

## Validation
VALID
  0 errors, 1 warnings, 0 info
  WARN: Very short clip (0.067s) — possible flash frame [Project 'Travel Vlog v1' > Clip #5 'Broll_Beach_Flash']

## Stats: Travel Vlog v1
Duration: 00:00:13:00 (13.0s)
Clips: 5 (+1 gaps)
Resolution: 1920x1080 @ 29.97fps
Avg clip: 2.58s
Markers: 3, Keywords: 2
Roles: Dialogue, Video

## Gaps (1)
- 00:00:09:00: 0.100s (between 'Broll_Beach' and 'Broll_City')

## Flash Frames (1)
- 'Broll_Beach_Flash' at 00:00:12:03: 2 frames (0.067s)

## Duplicate Sources (2)
- 'Interview_A' used 2x
- 'Broll_Beach' used 2x

## Pacing
Avg shot: 2.58s, Median: 3.00s
Shortest: 0.067s, Longest: 5.00s
Distribution: {"< 1s": 2, "1-3s": 0, "3-5s": 2, "5-10s": 1, "10-30s": 0, "30s+": 0}"""

BUILT_IN_TRANSITIONS = [
    "Cross Dissolve",
    "Fade to Color",
    "Fade to Black",
    "Wipe",
    "Band Wipe",
    "Center Wipe",
    "Checker Wipe",
    "Clock Wipe",
    "Edge Wipe",
    "Gradient Wipe",
    "Inset Wipe",
    "Jaws Wipe",
    "Barn Door",
    "Cube",
    "Doorway",
    "Mosaic",
    "Page Curl",
    "Puzzle",
    "Ripple",
    "Spin",
    "Swap",
    "Swing",
    "Zoom & Pan",
]
BUILT_IN_TITLES = [
    "Basic Title",
    "Basic Lower Third",
    "Custom Lower Third",
    "Bumper/Opener",
    "Centered Title",
    "Credits",
    "Focus",
    "Gradient",
    "Line Title",
    "Scrolling Credits",
]


def _assert_doctor(model: BaseModel) -> None:
    report = DoctorReport.model_validate(model)
    assert report.profile is Profile.FULL
    assert report.tool_names == sorted(report.tool_names)
    assert len(report.tool_names) == 93
    assert report.resource_count == 0
    assert report.tool_count == len(report.tool_names)
    catalog = next(check for check in report.checks if check.id == "mcp_catalog")
    assert catalog.details["tool_names"] == report.tool_names


def _assert_qc(model: BaseModel) -> None:
    assert model.markdown == QC_MARKDOWN
    assert model.validation.warning_count == 1
    assert model.stats[0].project_name == "Travel Vlog v1"
    assert model.gaps[0].after_clip == "Broll_City"
    assert model.flash_frames[0].frame_count == 2
    assert model.duplicates[0].occurrences[1].name == "Interview_A_Outro"
    assert model.pacing[0].histogram.under_one_second == 2


def _assert_media_links(model: BaseModel) -> None:
    assert model.missing_count == 1
    assert model.issues[0].severity == "warning"
    assert model.issues[0].location == "Asset 'Silent' (r3)"


def _assert_frame_rates(model: BaseModel) -> None:
    assert [item.format_id for item in model.formats] == ["r1", "r2"]
    assert model.formats[1].frame_duration == "1/24s"
    assert len(model.mismatches) == 1
    assert model.mismatches[0].actual_fps == 24.0
    assert model.mismatches[0].expected_fps == 30.0


def _assert_audio(model: BaseModel) -> None:
    assert model.verified is False
    assert [item.kind for item in model.observations] == [
        "missing_audio",
        "high_volume",
    ]
    assert model.observations[1].amount_db == 7.5
    assert model.limitations


def _assert_safe_zones(model: BaseModel) -> None:
    assert model.verified is False
    assert [item.kind for item in model.observations] == [
        "position",
        "scale",
    ]
    assert model.observations[0].position_x == 900.0
    assert model.observations[1].scale == 2.5
    assert model.limitations


def _assert_duration(model: BaseModel) -> None:
    assert model.project_name == "Diagnostics"
    assert model.target_seconds == 3.0
    assert model.actual_seconds == 2.0
    assert model.delta_seconds == -1.0
    assert model.within_tolerance is False


def _assert_motion_templates(model: BaseModel) -> None:
    assert len(model.templates) == 1
    assert model.templates[0].category == "Titles"
    assert model.templates[0].name == "Lower Third"


def _assert_share_destinations(model: BaseModel) -> None:
    assert model.destinations == ["Archive"]


def _assert_effects(model: BaseModel) -> None:
    assert model.effects == ["Film Look"]
    assert model.transitions == BUILT_IN_TRANSITIONS
    assert model.titles == BUILT_IN_TITLES


def _assert_templates(model: BaseModel) -> None:
    assert len(model.templates) == 1
    assert model.templates[0].endswith("promo_template.fcpxml")
    assert model.directory == str(Path(model.templates[0]).parent)


ResultAssertion = Callable[[BaseModel], None]
CASES: tuple[tuple[str, str, ResultAssertion], ...] = (
    ("fcp_doctor", "DoctorReport", _assert_doctor),
    ("fcpxml_qc_report", "QCReportResult", _assert_qc),
    ("fcpxml_check_media_links", "MediaLinkCheckResult", _assert_media_links),
    ("fcpxml_check_frame_rates", "FrameRateCheckResult", _assert_frame_rates),
    ("fcpxml_check_audio_levels", "AudioLevelCheckResult", _assert_audio),
    ("fcpxml_check_safe_zones", "SafeZoneCheckResult", _assert_safe_zones),
    ("fcpxml_check_duration", "DurationCheckResult", _assert_duration),
    ("fcp_list_motion_templates", "MotionTemplateListResult", _assert_motion_templates),
    (
        "fcp_list_share_destinations",
        "ShareDestinationListResult",
        _assert_share_destinations,
    ),
    ("fcp_discover_effects", "InstalledEffectListResult", _assert_effects),
    ("fcpxml_list_templates", "TemplateListResult", _assert_templates),
)


@pytest.fixture
def qc_discovery_cases(
    monkeypatch: pytest.MonkeyPatch,
    sample_fcpxml_path: Path,
    tmp_path: Path,
) -> dict[str, tuple[dict[str, object], str | None]]:
    config = RuntimeConfig.from_env(
        {
            "FCP_MCP_OUTPUT_DIR": str(tmp_path),
            "FCP_MCP_ALLOWED_ROOTS": os.pathsep.join(
                [str(sample_fcpxml_path.parent), str(tmp_path)]
            ),
            "FCP_MCP_PROFILE": "full",
        },
        home=tmp_path,
    )
    monkeypatch.setattr(server, "CONFIG", config)
    monkeypatch.setattr(server, "PATHS", PathPolicy(config))

    missing_media = tmp_path / "missing.mov"
    diagnostic_fcpxml = tmp_path / "diagnostics.fcpxml"
    diagnostic_fcpxml.write_text(
        f"""<?xml version="1.0" encoding="UTF-8"?>
<fcpxml version="1.11">
  <resources>
    <format id="r1" name="Thirty" frameDuration="1/30s" width="1920" height="1080"/>
    <format id="r2" name="Twenty Four" frameDuration="1/24s" width="1920" height="1080"/>
    <asset id="r3" name="Silent" src="{missing_media.as_uri()}" duration="2s"
           hasVideo="1" hasAudio="0" format="r1"/>
  </resources>
  <project name="Diagnostics">
    <sequence format="r1" duration="2s">
      <spine>
        <asset-clip ref="r3" name="Silent Clip" duration="2s" role="Dialogue">
          <adjust-transform position="900 500" scale="2.5 2.5"/>
          <adjust-volume amount="7.5dB"/>
        </asset-clip>
      </spine>
    </sequence>
  </project>
</fcpxml>
""",
        encoding="utf-8",
    )

    motion_dir = tmp_path / "Motion Templates.localized"
    title_dir = motion_dir / "Titles.localized" / "Fixture Pack"
    title_dir.mkdir(parents=True)
    (title_dir / "Lower Third.motn").write_text("fixture", encoding="utf-8")
    effect_dir = motion_dir / "Effects.localized" / "Fixture Pack"
    effect_dir.mkdir(parents=True)
    (effect_dir / "Film Look.moef").write_text("fixture", encoding="utf-8")
    monkeypatch.setattr(utility_paths, "motion_templates_dir", lambda: motion_dir)

    destinations_dir = tmp_path / "Destinations"
    destinations_dir.mkdir()
    (destinations_dir / "Archive.fcpdestination").write_text(
        "fixture",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        utility_paths,
        "fcp_destinations_dir",
        lambda: destinations_dir,
    )

    template = tmp_path / "promo_template.fcpxml"
    template.write_text("<fcpxml version='1.11'/>", encoding="utf-8")
    media_summary = (
        "VALID\n"
        "  0 errors, 1 warnings, 0 info\n"
        f"  WARN: Media file not found: {missing_media} "
        "[Asset 'Silent' (r3)]"
    )
    effects_text = json.dumps(
        {
            "built_in_transitions": BUILT_IN_TRANSITIONS,
            "built_in_titles": BUILT_IN_TITLES,
            "custom_effects": ["Film Look"],
        },
        indent=2,
    )
    path = str(diagnostic_fcpxml)
    return {
        "fcp_doctor": ({}, None),
        "fcpxml_qc_report": (
            {"path": str(sample_fcpxml_path)},
            QC_MARKDOWN,
        ),
        "fcpxml_check_media_links": ({"path": path}, media_summary),
        "fcpxml_check_frame_rates": (
            {"path": path},
            "MIXED FRAME RATES DETECTED: 24.00fps, 30.00fps",
        ),
        "fcpxml_check_audio_levels": (
            {"path": path},
            (
                "Audio issues:\n"
                "- 'Silent Clip': no audio in source (role: Dialogue)\n"
                "- 'Silent Clip': very high volume (+7.5dB)"
            ),
        ),
        "fcpxml_check_safe_zones": (
            {"path": path},
            (
                "Safe zone concerns:\n"
                "- 'Silent Clip': position (900.0, 500.0) may be outside safe zone\n"
                "- 'Silent Clip': scale 2.5x may cause quality issues"
            ),
        ),
        "fcpxml_check_duration": (
            {"path": path, "target_seconds": 3.0},
            (
                "Project 'Diagnostics': 2.0s / 3.0s target "
                "(UNDER, diff: -1.0s)"
            ),
        ),
        "fcp_list_motion_templates": (
            {},
            json.dumps({"Titles": ["Lower Third"]}, indent=2),
        ),
        "fcp_list_share_destinations": (
            {},
            json.dumps({"destinations": ["Archive"]}, indent=2),
        ),
        "fcp_discover_effects": ({}, effects_text),
        "fcpxml_list_templates": (
            {"templates_dir": str(tmp_path)},
            json.dumps({"templates": [str(template)]}, indent=2),
        ),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "model_name", "assert_payload"),
    CASES,
    ids=[case[0] for case in CASES],
)
async def test_qc_and_discovery_tools_publish_typed_dual_channel_results(
    tool_name: str,
    model_name: str,
    assert_payload: ResultAssertion,
    qc_discovery_cases: dict[str, tuple[dict[str, object], str | None]],
) -> None:
    tools = {tool.name: tool for tool in await server.mcp.list_tools()}
    arguments, expected_text = qc_discovery_cases[tool_name]

    assert set(tools[tool_name].output_schema["properties"]) != {"result"}
    result = await server.mcp.call_tool(tool_name, arguments)

    assert result.is_error is False
    if expected_text is None:
        assert result.content[0].text == json.dumps(
            result.structured_content,
            indent=2,
        )
    else:
        assert result.content[0].text == expected_text

    if tool_name == "fcp_doctor":
        expected_model = DoctorReport
    else:
        result_models = importlib.import_module("fcp_mcp.result_models.fcpxml")
        expected_model = getattr(result_models, model_name)
        assert expected_model.model_config["frozen"] is True
        assert expected_model.model_config["extra"] == "forbid"
    model = expected_model.model_validate(result.structured_content)
    assert model.schema_version == "1"
    assert_payload(model)


@pytest.mark.asyncio
async def test_metadata_checks_keep_empty_observations_honest(
    sample_fcpxml_path: Path,
    qc_discovery_cases: dict[str, tuple[dict[str, object], str | None]],
) -> None:
    del qc_discovery_cases
    path = str(sample_fcpxml_path)

    audio = await server.mcp.call_tool(
        "fcpxml_check_audio_levels",
        {"path": path},
    )
    safe_zones = await server.mcp.call_tool(
        "fcpxml_check_safe_zones",
        {"path": path},
    )

    assert audio.content[0].text == "No audio issues detected."
    assert audio.structured_content["observations"] == []
    assert audio.structured_content["verified"] is False
    assert audio.structured_content["limitations"]
    assert safe_zones.content[0].text == "All clips within safe zones."
    assert safe_zones.structured_content["observations"] == []
    assert safe_zones.structured_content["verified"] is False
    assert safe_zones.structured_content["limitations"]


@pytest.mark.asyncio
async def test_frame_rate_mismatches_use_the_referenced_sequence_format(
    tmp_path: Path,
    qc_discovery_cases: dict[str, tuple[dict[str, object], str | None]],
) -> None:
    del qc_discovery_cases
    path = tmp_path / "later-declared-timeline-format.fcpxml"
    path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<fcpxml version="1.11">
  <resources>
    <format id="r24" name="Non Timeline" frameDuration="1/24s"
            width="1920" height="1080"/>
    <format id="r30" name="Timeline" frameDuration="1/30s"
            width="1920" height="1080"/>
  </resources>
  <project name="Referenced Format">
    <sequence format="r30" duration="1s"><spine/></sequence>
  </project>
</fcpxml>
""",
        encoding="utf-8",
    )

    result = await server.mcp.call_tool(
        "fcpxml_check_frame_rates",
        {"path": str(path)},
    )

    assert result.content[0].text == (
        "MIXED FRAME RATES DETECTED: 24.00fps, 30.00fps"
    )
    assert result.structured_content["mismatches"] == [
        {
            "format_id": "r24",
            "expected_fps": 30.0,
            "actual_fps": 24.0,
        }
    ]
