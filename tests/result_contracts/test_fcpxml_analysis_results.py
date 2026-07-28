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
from fcp_mcp.security.paths import PathPolicy

SUMMARY = {
    "version": "1.11",
    "formats": 1,
    "assets": 4,
    "effects": 2,
    "projects": [
        {
            "name": "Travel Vlog v1",
            "has_sequence": True,
            "clip_count": 6,
            "duration": "390390/30000s",
        }
    ],
}

CLIPS = [
    {
        "index": 0,
        "name": "Interview_A",
        "type": "asset-clip",
        "offset": "00:00:00:00",
        "offset_raw": "0s",
        "start": "30030/30000s",
        "duration": "5.005s",
        "duration_raw": "150150/30000s",
        "role": "Dialogue",
        "ref": "r2",
        "connected_clips": 0,
        "markers": 1,
    },
    {
        "index": 1,
        "name": "Broll_Beach",
        "type": "asset-clip",
        "offset": "00:00:05:00",
        "offset_raw": "150150/30000s",
        "start": "0s",
        "duration": "4.004s",
        "duration_raw": "120120/30000s",
        "role": "Video",
        "ref": "r3",
        "connected_clips": 1,
        "markers": 1,
    },
    {
        "index": 2,
        "name": "Gap",
        "type": "gap",
        "offset": "00:00:09:00",
        "offset_raw": "270270/30000s",
        "start": "0s",
        "duration": "0.100s",
        "duration_raw": "3003/30000s",
        "role": "",
        "ref": "",
        "connected_clips": 0,
        "markers": 0,
    },
    {
        "index": 3,
        "name": "Broll_City",
        "type": "asset-clip",
        "offset": "00:00:09:03",
        "offset_raw": "273273/30000s",
        "start": "0s",
        "duration": "3.003s",
        "duration_raw": "90090/30000s",
        "role": "Video",
        "ref": "r4",
        "connected_clips": 0,
        "markers": 1,
    },
    {
        "index": 4,
        "name": "Broll_Beach_Flash",
        "type": "asset-clip",
        "offset": "00:00:12:03",
        "offset_raw": "363363/30000s",
        "start": "120120/30000s",
        "duration": "0.067s",
        "duration_raw": "2002/30000s",
        "role": "Video",
        "ref": "r3",
        "connected_clips": 0,
        "markers": 0,
    },
    {
        "index": 5,
        "name": "Interview_A_Outro",
        "type": "asset-clip",
        "offset": "00:00:12:05",
        "offset_raw": "365365/30000s",
        "start": "240240/30000s",
        "duration": "0.834s",
        "duration_raw": "25025/30000s",
        "role": "Dialogue",
        "ref": "r2",
        "connected_clips": 0,
        "markers": 0,
    },
]

MARKERS = [
    {
        "clip": "Interview_A",
        "type": "marker",
        "value": "Good take",
        "note": "Use this section",
        "start": "00:00:02:00",
        "start_raw": "60060/30000s",
    },
    {
        "clip": "Interview_A",
        "type": "keyword",
        "value": "intro",
        "start": "30030/30000s",
        "duration": "60060/30000s",
    },
    {
        "clip": "Broll_Beach",
        "type": "marker",
        "value": "Sunset",
        "note": "Best sunset shot",
        "start": "00:00:01:00",
        "start_raw": "30030/30000s",
    },
    {
        "clip": "Broll_City",
        "type": "chapter-marker",
        "value": "City Section",
        "note": "",
        "start": "00:00:00:00",
        "start_raw": "0s",
    },
    {
        "clip": "Interview_A_Outro",
        "type": "keyword",
        "value": "outro",
        "start": "240240/30000s",
        "duration": "25025/30000s",
    },
]

PACING = [
    {
        "average_shot_length": 2.5825799999999997,
        "median_shot_length": 3.003,
        "std_deviation": 1.8682473114422273,
        "shortest_shot": 0.06673333333333334,
        "longest_shot": 5.005,
        "pacing_curve": [
            5.005,
            4.004,
            3.003,
            0.06673333333333334,
            0.8341666666666666,
        ],
        "histogram": {
            "< 1s": 2,
            "1-3s": 0,
            "3-5s": 2,
            "5-10s": 1,
            "10-30s": 0,
            "30s+": 0,
        },
    }
]

GAPS = [
    {
        "offset_seconds": 9.009,
        "offset_timecode": "00:00:09:00",
        "duration_seconds": 0.1001,
        "before_clip": "Broll_Beach",
        "after_clip": "Broll_City",
    }
]

FLASH_FRAMES = [
    {
        "clip_name": "Broll_Beach_Flash",
        "offset_seconds": 12.1121,
        "offset_timecode": "00:00:12:03",
        "duration_seconds": 0.06673333333333334,
        "frame_count": 2,
    }
]

DUPLICATES = [
    {
        "asset_ref": "r2",
        "asset_name": "Interview_A",
        "occurrences": [
            {
                "name": "Interview_A",
                "offset_seconds": 0.0,
                "duration_seconds": 5.005,
            },
            {
                "name": "Interview_A_Outro",
                "offset_seconds": 12.178833333333333,
                "duration_seconds": 0.8341666666666666,
            },
        ],
    },
    {
        "asset_ref": "r3",
        "asset_name": "Broll_Beach",
        "occurrences": [
            {
                "name": "Broll_Beach",
                "offset_seconds": 5.005,
                "duration_seconds": 4.004,
            },
            {
                "name": "Broll_Beach_Flash",
                "offset_seconds": 12.1121,
                "duration_seconds": 0.06673333333333334,
            },
        ],
    },
]

EFFECTS = {
    "applied": [],
    "available": [
        {
            "id": "r6",
            "name": "Basic Title",
            "uid": (
                ".../Titles.localized/Build In:Out.localized/"
                "Basic Title.localized/Basic Title.moti"
            ),
        },
        {
            "id": "r7",
            "name": "Cross Dissolve",
            "uid": (
                ".../Transitions.localized/Dissolves.localized/"
                "Cross Dissolve.localized/Cross Dissolve.motr"
            ),
        },
    ],
}

ROLES = {"roles": ["Dialogue", "Titles", "Video"]}

TIMELINE_STATS = [
    {
        "project_name": "Travel Vlog v1",
        "total_duration_seconds": 13.013,
        "total_duration_timecode": "00:00:13:00",
        "clip_count": 6,
        "non_gap_clip_count": 5,
        "gap_count": 1,
        "total_gap_duration_seconds": 0.1001,
        "transition_count": 0,
        "marker_count": 3,
        "keyword_count": 2,
        "connected_clip_count": 1,
        "roles_used": ["Dialogue", "Video"],
        "fps": 29.97002997002997,
        "resolution": "1920x1080",
        "average_clip_duration_seconds": 2.5825799999999997,
        "shortest_clip_seconds": 0.06673333333333334,
        "longest_clip_seconds": 5.005,
    }
]

VALIDATION_TEXT = (
    "VALID\n"
    "  0 errors, 1 warnings, 0 info\n"
    "  WARN: Very short clip (0.067s) — possible flash frame "
    "[Project 'Travel Vlog v1' > Clip #5 'Broll_Beach_Flash']"
)
DIFF_TEXT = (
    "Diff: 'Travel Vlog v1' vs 'Travel Vlog v1'\n"
    "  Added: 0\n"
    "  Removed: 0\n"
    "  Moved: 0\n"
    "  Trimmed: 0\n"
    "  Unchanged: 5"
)

LEGACY_TEXT = {
    "fcpxml_parse": json.dumps(SUMMARY, indent=2),
    "fcpxml_list_clips": json.dumps(CLIPS, indent=2),
    "fcpxml_list_markers": json.dumps(MARKERS, indent=2),
    "fcpxml_analyze_pacing": json.dumps(PACING, indent=2),
    "fcpxml_detect_gaps": json.dumps(GAPS, indent=2),
    "fcpxml_detect_flash_frames": json.dumps(FLASH_FRAMES, indent=2),
    "fcpxml_detect_duplicates": json.dumps(DUPLICATES, indent=2),
    "fcpxml_validate": VALIDATION_TEXT,
    "fcpxml_list_effects": json.dumps(EFFECTS, indent=2),
    "fcpxml_list_roles": json.dumps(ROLES, indent=2),
    "fcpxml_timeline_stats": json.dumps(TIMELINE_STATS, indent=2),
    "fcpxml_diff": DIFF_TEXT,
}


def _assert_summary(model: BaseModel) -> None:
    assert model.version == "1.11"
    assert model.formats == 1
    assert model.projects[0].clip_count == 6


def _assert_clips(model: BaseModel) -> None:
    assert len(model.clips) == 6
    assert model.clips[0].duration_seconds == 5.005
    assert model.clips[1].connected_clip_count == 1
    assert model.clips[2].clip_type == "gap"


def _assert_markers(model: BaseModel) -> None:
    assert len(model.markers) == 3
    assert model.markers[2].marker_type == "chapter-marker"
    assert len(model.keywords) == 2
    assert model.keywords[1].duration_raw == "25025/30000s"


def _assert_pacing(model: BaseModel) -> None:
    assert len(model.analyses) == 1
    assert model.analyses[0].histogram.under_one_second == 2
    assert model.analyses[0].pacing_curve[-1] == 0.8341666666666666


def _assert_gaps(model: BaseModel) -> None:
    assert model.count == 1
    assert model.gaps[0].after_clip == "Broll_City"


def _assert_flash_frames(model: BaseModel) -> None:
    assert model.max_frames == 2
    assert model.count == 1
    assert model.items[0].frame_count == 2


def _assert_duplicates(model: BaseModel) -> None:
    assert len(model.groups) == 2
    assert model.groups[1].occurrences[1].name == "Broll_Beach_Flash"


def _assert_validation(model: BaseModel) -> None:
    assert model.valid is True
    assert model.error_count == 0
    assert model.warning_count == 1
    assert model.issues[0].severity == "warning"
    assert model.summary == VALIDATION_TEXT


def _assert_effects(model: BaseModel) -> None:
    assert model.applied == []
    assert model.available[1].name == "Cross Dissolve"


def _assert_roles(model: BaseModel) -> None:
    assert model.roles == ["Dialogue", "Titles", "Video"]


def _assert_timeline_stats(model: BaseModel) -> None:
    assert len(model.projects) == 1
    assert model.projects[0].resolution == "1920x1080"
    assert model.projects[0].keyword_count == 2


def _assert_diff(model: BaseModel) -> None:
    assert model.projects[0].project_a == "Travel Vlog v1"
    assert model.projects[0].counts.unchanged == 5
    assert model.changes == []
    assert model.counts.added == 0
    assert model.summary == DIFF_TEXT


ResultAssertion = Callable[[BaseModel], None]
CASES: tuple[tuple[str, str, ResultAssertion], ...] = (
    ("fcpxml_parse", "FCPXMLSummaryResult", _assert_summary),
    ("fcpxml_list_clips", "ClipListResult", _assert_clips),
    ("fcpxml_list_markers", "MarkerListResult", _assert_markers),
    ("fcpxml_analyze_pacing", "PacingAnalysisResult", _assert_pacing),
    ("fcpxml_detect_gaps", "GapDetectionResult", _assert_gaps),
    (
        "fcpxml_detect_flash_frames",
        "FlashFrameDetectionResult",
        _assert_flash_frames,
    ),
    (
        "fcpxml_detect_duplicates",
        "DuplicateDetectionResult",
        _assert_duplicates,
    ),
    ("fcpxml_validate", "FCPXMLValidationResult", _assert_validation),
    ("fcpxml_list_effects", "EffectInventoryResult", _assert_effects),
    ("fcpxml_list_roles", "RoleListResult", _assert_roles),
    ("fcpxml_timeline_stats", "TimelineStatsResult", _assert_timeline_stats),
    ("fcpxml_diff", "FCPXMLDiffResult", _assert_diff),
)


@pytest.fixture
def analysis_path_policy(
    monkeypatch: pytest.MonkeyPatch,
    sample_fcpxml_path: Path,
    tmp_path: Path,
) -> None:
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
    monkeypatch.setattr(server, "PATHS", PathPolicy(config))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "model_name", "assert_payload"),
    CASES,
    ids=[case[0] for case in CASES],
)
async def test_fcpxml_analysis_tools_publish_typed_domain_results(
    tool_name: str,
    model_name: str,
    assert_payload: ResultAssertion,
    sample_fcpxml_path: Path,
    analysis_path_policy: None,
) -> None:
    path = str(sample_fcpxml_path)
    arguments = (
        {"path_a": path, "path_b": path}
        if tool_name == "fcpxml_diff"
        else {"path": path}
    )
    tools = {tool.name: tool for tool in await server.mcp.list_tools()}

    result = await server.mcp.call_tool(tool_name, arguments)

    assert result.is_error is False
    assert result.content[0].text == LEGACY_TEXT[tool_name]
    assert set(tools[tool_name].output_schema["properties"]) != {"result"}

    result_models = importlib.import_module("fcp_mcp.result_models.fcpxml")
    expected_model = getattr(result_models, model_name)
    model = expected_model.model_validate(result.structured_content)
    assert model.schema_version == "1"
    assert_payload(model)


@pytest.mark.asyncio
async def test_pacing_types_an_empty_histogram_without_changing_legacy_text(
    tmp_path: Path,
    analysis_path_policy: None,
) -> None:
    empty_timeline = tmp_path / "empty-timeline.fcpxml"
    empty_timeline.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<fcpxml version="1.11">
  <resources>
    <format id="r1" frameDuration="1/30s" width="1920" height="1080"/>
  </resources>
  <project name="Empty">
    <sequence format="r1" duration="0s"><spine/></sequence>
  </project>
</fcpxml>
""",
        encoding="utf-8",
    )
    legacy_payload = [
        {
            "average_shot_length": 0.0,
            "median_shot_length": 0.0,
            "std_deviation": 0.0,
            "shortest_shot": 0.0,
            "longest_shot": 0.0,
            "pacing_curve": [],
            "histogram": {},
        }
    ]

    result = await server.mcp.call_tool(
        "fcpxml_analyze_pacing",
        {"path": str(empty_timeline)},
    )

    assert result.is_error is False
    assert result.content[0].text == json.dumps(legacy_payload, indent=2)
    assert result.structured_content["analyses"][0]["pacing_curve"] == []
    assert result.structured_content["analyses"][0]["histogram"] == {
        "under_one_second": 0,
        "one_to_three_seconds": 0,
        "three_to_five_seconds": 0,
        "five_to_ten_seconds": 0,
        "ten_to_thirty_seconds": 0,
        "thirty_seconds_or_more": 0,
    }
