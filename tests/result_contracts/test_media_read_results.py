from __future__ import annotations

import importlib
from collections.abc import Callable
from pathlib import Path
from subprocess import CompletedProcess

import pytest
from pydantic import BaseModel, ValidationError

from fcp_mcp import server
from fcp_mcp.config import RuntimeConfig
from fcp_mcp.media import ffprobe
from fcp_mcp.security.paths import PathPolicy

FFPROBE_STDOUT = """{
  "format": {
    "filename": "/fixtures/complete.mov",
    "duration": "12.500",
    "size": "15728640",
    "bit_rate": "1536000",
    "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
    "format_long_name": "QuickTime / MOV"
  },
  "streams": [
    {
      "index": 3,
      "codec_name": "h264",
      "codec_long_name": "H.264 / AVC / MPEG-4 AVC / MPEG-4 part 10",
      "codec_type": "video",
      "width": 1920,
      "height": 1080,
      "pix_fmt": "yuv420p",
      "r_frame_rate": "30000/1001",
      "avg_frame_rate": "30000/1001",
      "duration": "12.500",
      "bit_rate": "1200000",
      "tags": {"language": "eng", "handler_name": "VideoHandler"}
    },
    {
      "index": 7,
      "codec_name": "aac",
      "codec_long_name": "AAC (Advanced Audio Coding)",
      "codec_type": "audio",
      "sample_rate": "48000",
      "channels": 2,
      "channel_layout": "stereo",
      "duration": "12.480",
      "bit_rate": "192000",
      "tags": {"language": "eng", "handler_name": "SoundHandler"}
    },
    {
      "index": 11,
      "codec_name": "mov_text",
      "codec_long_name": "MOV text",
      "codec_type": "subtitle",
      "duration": "11.000",
      "tags": {"language": "spa", "title": "Spanish"}
    }
  ]
}
"""

SILENCE_STDERR = """ffmpeg version fixture
[silencedetect @ 0x1] silence_start: 1.25
[silencedetect @ 0x1] silence_end: 2.75 | silence_duration: 1.5
[silencedetect @ 0x1] silence_start: 8
[silencedetect @ 0x1] silence_end: 9.125 | silence_duration: 1.125
"""

BEAT_STDERR = """ffmpeg version fixture
[Parsed_ametadata_1 @ 0x1] lavfi.astats.Overall.RMS_level=-45
[Parsed_ametadata_1 @ 0x1] lavfi.astats.Overall.RMS_level=-20
[Parsed_ametadata_1 @ 0x1] lavfi.astats.Overall.RMS_level=-19
[Parsed_ametadata_1 @ 0x1] lavfi.astats.Overall.RMS_level=-10
"""

LOUDNESS_STDERR = """ffmpeg version fixture
[Parsed_ebur128_0 @ 0x1] Summary:
  I:         -16.4 LUFS
  LRA:         5.2 LU
  Peak:       -1.3 dBFS
"""

SCENE_STDERR = """ffmpeg version fixture
[Parsed_showinfo_1 @ 0x1] n:0 pts:75075 pts_time:2.5025 lavfi.scene_score:0.412
[Parsed_showinfo_1 @ 0x1] n:1 pts:210210 pts_time:7.007 lavfi.scene_score:0.731
"""

MEDIA_INFO_TEXT = """{
  "filename": "/fixtures/complete.mov",
  "duration": "12.50s",
  "size_mb": "15.0",
  "bitrate_kbps": "1536",
  "format": "QuickTime / MOV",
  "streams": [
    {
      "type": "video",
      "codec": "h264",
      "width": 1920,
      "height": 1080,
      "fps": "30000/1001",
      "pix_fmt": "yuv420p"
    },
    {
      "type": "audio",
      "codec": "aac",
      "sample_rate": "48000",
      "channels": 2,
      "channel_layout": "stereo"
    },
    {
      "type": "subtitle",
      "codec": "mov_text"
    }
  ]
}"""

SILENCE_TEXT = """[
  {
    "start": 1.25,
    "end": 2.75,
    "duration": 1.5
  },
  {
    "start": 8.0,
    "end": 9.125,
    "duration": 1.125
  }
]"""

BEAT_TEXT = """{
  "beat_count": 2,
  "beats": [
    0.023,
    0.07
  ]
}"""

LOUDNESS_TEXT = """{
  "integrated_lufs": -16.4,
  "loudness_range_lu": 5.2,
  "true_peak_dbfs": -1.3
}"""

STREAMS_TEXT = """[
  {
    "index": 0,
    "type": "video",
    "codec": "h264",
    "language": "eng",
    "duration": "12.500"
  },
  {
    "index": 1,
    "type": "audio",
    "codec": "aac",
    "language": "eng",
    "duration": "12.480"
  },
  {
    "index": 2,
    "type": "subtitle",
    "codec": "mov_text",
    "language": "spa",
    "duration": "11.000"
  }
]"""

SCENE_TEXT = """{
  "scene_count": 2,
  "scenes": [
    {
      "time": 2.5025
    },
    {
      "time": 7.007
    }
  ]
}"""

PUPPET_TEXT = """{
  "idle": {
    "description": "Subtle breathing and sway \\u2014 keeps the character looking alive",
    "parts_used": [
      "body/torso",
      "head"
    ],
    "parameters": {
      "breathe_amount": 8.0,
      "sway_amount": 3.0
    },
    "good_for": "Background characters, dialogue scenes, any time a character is standing still"
  },
  "bounce": {
    "description": "Vertical bouncing motion on a single part",
    "parts_used": [
      "body/torso (or first available part)"
    ],
    "parameters": {
      "amplitude": 20.0,
      "cycles": 3
    },
    "good_for": "Excited characters, reactions, comedic emphasis"
  },
  "walk": {
    "description": "Full walk cycle with arm swing, leg swing, and body bob",
    "parts_used": [
      "body/torso",
      "head",
      "left_arm",
      "right_arm",
      "left_leg",
      "right_leg"
    ],
    "parameters": {
      "stride": 100.0,
      "bob_height": 15.0,
      "arm_swing": 30.0,
      "leg_swing": 35.0,
      "cycles": 3
    },
    "good_for": "Characters walking in place (combine with position keyframes for actual movement)"
  },
  "talk": {
    "description": "Talking animation with mouth movement and subtle head bob",
    "parts_used": [
      "mouth/jaw",
      "head"
    ],
    "parameters": {
      "jaw_range": 15.0,
      "head_bob": 5.0,
      "tempo": 4.0
    },
    "good_for": "Dialogue scenes, narration, any speaking character"
  },
  "wave": {
    "description": "Arm waving rotation",
    "parts_used": [
      "left_arm (or right_arm)"
    ],
    "parameters": {
      "angle_range": 45.0,
      "cycles": 2
    },
    "good_for": "Greetings, goodbyes, getting attention"
  }
}"""


def _assert_media_info(model: BaseModel) -> None:
    assert model.format == "QuickTime / MOV"
    assert model.duration_seconds == 12.5
    assert model.bitrate_kbps == 1536.0
    assert model.streams[0].codec_type == "video"
    assert model.streams[0].index == 3
    assert model.streams[0].frame_rate == 30000 / 1001
    assert model.streams[1].codec_type == "audio"
    assert model.streams[1].index == 7
    assert model.streams[1].sample_rate_hz == 48000
    assert model.streams[2].codec_type == "subtitle"
    assert model.streams[2].index == 11
    assert model.streams[2].title == "Spanish"


def _assert_silence(model: BaseModel) -> None:
    assert model.noise_threshold == "-35dB"
    assert model.minimum_duration_seconds == 0.75
    assert [(item.start_seconds, item.end_seconds) for item in model.ranges] == [
        (1.25, 2.75),
        (8.0, 9.125),
    ]


def _assert_beats(model: BaseModel) -> None:
    assert model.beat_count == 2
    assert model.beats[0].seconds == 0.023
    assert model.beats[1].timecode == "00:00:00.070"
    assert model.cadence.intervals_seconds == [0.047]
    assert model.cadence.estimated_bpm == pytest.approx(1276.595744680851)


def _assert_loudness(model: BaseModel) -> None:
    assert model.integrated_lufs == -16.4
    assert model.loudness_range_lu == 5.2
    assert model.true_peak_dbfs == -1.3
    assert model.warnings == []
    assert model.raw_summary is None


def _assert_streams(model: BaseModel) -> None:
    assert [stream.codec_type for stream in model.streams] == [
        "video",
        "audio",
        "subtitle",
    ]
    assert model.streams[0].width == 1920
    assert [stream.index for stream in model.streams] == [3, 7, 11]
    assert model.streams[1].channel_layout == "stereo"
    assert model.streams[2].language == "spa"
    assert model.streams[2].title == "Spanish"


def _assert_scenes(model: BaseModel) -> None:
    assert model.threshold == 0.4
    assert model.scene_count == 2
    assert model.scene_changes[0].time_seconds == 2.5025
    assert model.scene_changes[0].score == 0.412
    assert model.scene_changes[1].timecode == "00:00:07.007"
    assert model.warnings == []
    assert model.raw_summary is None


def _assert_presets(model: BaseModel) -> None:
    assert [preset.name for preset in model.presets] == [
        "idle",
        "bounce",
        "walk",
        "talk",
        "wave",
    ]
    walk = model.presets[2]
    assert walk.parts == [
        "body/torso",
        "head",
        "left_arm",
        "right_arm",
        "left_leg",
        "right_leg",
    ]
    assert [(parameter.name, parameter.default) for parameter in walk.parameters] == [
        ("stride", 100.0),
        ("bob_height", 15.0),
        ("arm_swing", 30.0),
        ("leg_swing", 35.0),
        ("cycles", 3),
    ]
    assert walk.usage.startswith("Characters walking in place")


ResultAssertion = Callable[[BaseModel], None]
CASES: tuple[tuple[str, str, dict[str, object], str, ResultAssertion], ...] = (
    (
        "media_info",
        "MediaInfoResult",
        {"path": ""},
        MEDIA_INFO_TEXT,
        _assert_media_info,
    ),
    (
        "media_detect_silence",
        "SilenceDetectionResult",
        {"path": "", "noise_threshold": "-35dB", "min_duration": 0.75},
        SILENCE_TEXT,
        _assert_silence,
    ),
    (
        "media_detect_beats",
        "BeatDetectionResult",
        {"path": ""},
        BEAT_TEXT,
        _assert_beats,
    ),
    (
        "media_loudness",
        "LoudnessResult",
        {"path": ""},
        LOUDNESS_TEXT,
        _assert_loudness,
    ),
    (
        "media_list_streams",
        "StreamListResult",
        {"path": ""},
        STREAMS_TEXT,
        _assert_streams,
    ),
    (
        "media_scene_detect",
        "SceneDetectionResult",
        {"path": "", "threshold": 0.4},
        SCENE_TEXT,
        _assert_scenes,
    ),
    (
        "puppet_list_presets",
        "PuppetPresetListResult",
        {},
        PUPPET_TEXT,
        _assert_presets,
    ),
)


@pytest.fixture
def multimedia_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[Path, dict[str, str]]:
    source = tmp_path / "complete.mov"
    source.write_bytes(b"fixture")
    config = RuntimeConfig.from_env(
        {
            "FCP_MCP_ALLOWED_ROOTS": str(tmp_path),
            "FCP_MCP_OUTPUT_DIR": str(tmp_path),
            "FCP_MCP_PROFILE": "full",
        },
        home=tmp_path,
    )
    monkeypatch.setattr(server, "CONFIG", config)
    monkeypatch.setattr(server, "PATHS", PathPolicy(config))

    command_output = {
        "ffprobe_stdout": FFPROBE_STDOUT,
        "silence_stderr": SILENCE_STDERR,
        "beat_stderr": BEAT_STDERR,
        "loudness_stderr": LOUDNESS_STDERR,
        "scene_stderr": SCENE_STDERR,
    }

    def run_fixture(
        command: list[str],
        *,
        timeout: float,
        expected_outputs: tuple[str | Path, ...] = (),
        runner: object = None,
    ) -> CompletedProcess[str]:
        del timeout, expected_outputs, runner
        if command[-1] == "-version":
            return CompletedProcess(command, 0, "ffmpeg version fixture\n", "")
        if "-show_format" in command:
            return CompletedProcess(
                command,
                0,
                command_output["ffprobe_stdout"],
                "",
            )
        if any("silencedetect=" in item for item in command):
            return CompletedProcess(
                command,
                0,
                "",
                command_output["silence_stderr"],
            )
        if any("astats=" in item for item in command):
            return CompletedProcess(
                command,
                0,
                "",
                command_output["beat_stderr"],
            )
        if any("ebur128=" in item for item in command):
            return CompletedProcess(
                command,
                0,
                "",
                command_output["loudness_stderr"],
            )
        if any("select=" in item for item in command):
            return CompletedProcess(
                command,
                0,
                "",
                command_output["scene_stderr"],
            )
        raise AssertionError(f"Unexpected multimedia command: {command!r}")

    monkeypatch.setattr(ffprobe, "_run_checked", run_fixture)
    return source, command_output


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "model_name", "arguments", "legacy_text", "assert_payload"),
    CASES,
    ids=[case[0] for case in CASES],
)
async def test_media_reads_publish_typed_dual_channel_results(
    tool_name: str,
    model_name: str,
    arguments: dict[str, object],
    legacy_text: str,
    assert_payload: ResultAssertion,
    multimedia_boundary: tuple[Path, dict[str, str]],
) -> None:
    source, _ = multimedia_boundary
    arguments = {**arguments}
    if "path" in arguments:
        arguments["path"] = str(source)
    tools = {tool.name: tool for tool in await server.mcp.list_tools()}

    assert set(tools[tool_name].outputSchema["properties"]) != {"result"}
    result = await server.mcp.call_tool(tool_name, arguments)

    assert result.isError is False
    assert result.content[0].text == legacy_text
    module_name = "puppet" if tool_name == "puppet_list_presets" else "media"
    result_models = importlib.import_module(f"fcp_mcp.result_models.{module_name}")
    expected_model = getattr(result_models, model_name)
    model = expected_model.model_validate(result.structuredContent)
    assert model.schema_version == "1"
    assert_payload(model)


@pytest.mark.asyncio
async def test_media_info_and_stream_list_publish_real_discriminated_unions(
    multimedia_boundary: tuple[Path, dict[str, str]],
) -> None:
    source, _ = multimedia_boundary
    tools = {tool.name: tool for tool in await server.mcp.list_tools()}

    for tool_name in ("media_info", "media_list_streams"):
        schema = tools[tool_name].outputSchema
        stream_items = schema["properties"]["streams"]["items"]
        assert stream_items["discriminator"]["propertyName"] == "codec_type"
        assert len(stream_items["oneOf"]) == 4
        result = await server.mcp.call_tool(tool_name, {"path": str(source)})
        assert [
            stream["codec_type"] for stream in result.structuredContent["streams"]
        ] == ["video", "audio", "subtitle"]


@pytest.mark.asyncio
async def test_silence_empty_result_keeps_threshold_context(
    multimedia_boundary: tuple[Path, dict[str, str]],
) -> None:
    source, command_output = multimedia_boundary
    command_output["silence_stderr"] = "ffmpeg version fixture\n"

    result = await server.mcp.call_tool(
        "media_detect_silence",
        {
            "path": str(source),
            "noise_threshold": "-42dB",
            "min_duration": 1.25,
        },
    )

    assert result.content[0].text == "No silent sections detected."
    assert result.structuredContent == {
        "schema_version": "1",
        "noise_threshold": "-42dB",
        "minimum_duration_seconds": 1.25,
        "ranges": [],
    }


@pytest.mark.asyncio
async def test_media_info_bounds_raw_values_when_numbers_do_not_normalize(
    multimedia_boundary: tuple[Path, dict[str, str]],
) -> None:
    source, command_output = multimedia_boundary
    command_output["ffprobe_stdout"] = """{
  "format": {
    "filename": "/fixtures/malformed.mov",
    "duration": "N/A",
    "size": "unknown",
    "bit_rate": "-"
  },
  "streams": [
    {
      "index": 0,
      "codec_type": "video",
      "codec_name": "h264",
      "duration": "N/A",
      "bit_rate": "unknown",
      "r_frame_rate": "0/0"
    }
  ]
}
"""

    result = await server.mcp.call_tool("media_info", {"path": str(source)})

    assert result.content[0].text == """{
  "filename": "/fixtures/malformed.mov",
  "duration": "0.00s",
  "size_mb": "0.0",
  "bitrate_kbps": "0",
  "format": null,
  "streams": [
    {
      "type": "video",
      "codec": "h264",
      "width": null,
      "height": null,
      "fps": "0/0",
      "pix_fmt": null
    }
  ]
}"""
    assert result.structuredContent["duration_seconds"] is None
    assert result.structuredContent["size_mb"] is None
    assert result.structuredContent["bitrate_kbps"] is None
    assert result.structuredContent["raw_summary"] == (
        "duration: N/A\nsize: unknown\nbit_rate: -"
    )
    stream = result.structuredContent["streams"][0]
    assert stream["duration_seconds"] is None
    assert stream["bitrate_kbps"] is None
    assert stream["frame_rate"] is None
    assert stream["raw_summary"] == (
        "duration: N/A\nbit_rate: unknown\nr_frame_rate: 0/0"
    )
    assert len(result.structuredContent["raw_summary"]) <= 2000
    assert len(stream["raw_summary"]) <= 2000


@pytest.mark.asyncio
async def test_nonfinite_media_numbers_never_reach_structured_content(
    multimedia_boundary: tuple[Path, dict[str, str]],
) -> None:
    source, command_output = multimedia_boundary
    command_output["ffprobe_stdout"] = """{
  "format": {
    "filename": "/fixtures/nonfinite.mov",
    "duration": "NaN",
    "size": "+inf",
    "bit_rate": "-inf"
  },
  "streams": [
    {
      "index": 27,
      "codec_type": "audio",
      "codec_name": "aac",
      "duration": "NaN",
      "bit_rate": "+inf",
      "sample_rate": "-inf",
      "channels": 2,
      "channel_layout": "stereo"
    }
  ]
}
"""

    result = await server.mcp.call_tool("media_info", {"path": str(source)})

    assert result.content[0].text == """{
  "filename": "/fixtures/nonfinite.mov",
  "duration": "nans",
  "size_mb": "inf",
  "bitrate_kbps": "-inf",
  "format": null,
  "streams": [
    {
      "type": "audio",
      "codec": "aac",
      "sample_rate": "-inf",
      "channels": 2,
      "channel_layout": "stereo"
    }
  ]
}"""
    assert result.structuredContent["duration_seconds"] is None
    assert result.structuredContent["size_mb"] is None
    assert result.structuredContent["bitrate_kbps"] is None
    assert result.structuredContent["raw_summary"] == (
        "duration: NaN\nsize: +inf\nbit_rate: -inf"
    )
    stream = result.structuredContent["streams"][0]
    assert stream["index"] == 27
    assert stream["duration_seconds"] is None
    assert stream["bitrate_kbps"] is None
    assert stream["sample_rate_hz"] is None
    assert stream["raw_summary"] == (
        "duration: NaN\nbit_rate: +inf\nsample_rate: -inf"
    )
    assert len(result.structuredContent["raw_summary"]) <= 2000
    assert len(stream["raw_summary"]) <= 2000


@pytest.mark.asyncio
async def test_loudness_retains_parsed_values_and_all_parser_warnings(
    multimedia_boundary: tuple[Path, dict[str, str]],
) -> None:
    source, command_output = multimedia_boundary
    command_output["loudness_stderr"] = """ffmpeg version fixture
[Parsed_ebur128_0 @ 0x1] Summary:
  I:         -18.2 LUFS
  LRA:        wide LU
  Peak:       -1.1 dBFS
[Parsed_ebur128_0 @ 0x1] warning: gated measurement unavailable
"""

    result = await server.mcp.call_tool("media_loudness", {"path": str(source)})

    assert result.content[0].text == """{
  "integrated_lufs": -18.2,
  "true_peak_dbfs": -1.1
}"""
    assert result.structuredContent["integrated_lufs"] == -18.2
    assert result.structuredContent["loudness_range_lu"] is None
    assert result.structuredContent["true_peak_dbfs"] == -1.1
    assert [warning["kind"] for warning in result.structuredContent["warnings"]] == [
        "malformed_value",
        "command_warning",
    ]
    assert result.structuredContent["warnings"][1]["message"] == (
        "[Parsed_ebur128_0 @ 0x1] warning: gated measurement unavailable"
    )
    assert result.structuredContent["raw_summary"] == "LRA:        wide LU"
    assert len(result.structuredContent["raw_summary"]) <= 2000


@pytest.mark.asyncio
async def test_loudness_rejects_all_nonfinite_ffmpeg_forms(
    multimedia_boundary: tuple[Path, dict[str, str]],
) -> None:
    source, command_output = multimedia_boundary
    command_output["loudness_stderr"] = """ffmpeg version fixture
[Parsed_ebur128_0 @ 0x1] Summary:
  I:         -18.2 LUFS
  I:           NaN LUFS
  LRA:        +inf LU
  Peak:       -inf dBFS
"""

    result = await server.mcp.call_tool("media_loudness", {"path": str(source)})

    assert result.content[0].text == """{
  "integrated_lufs": NaN,
  "loudness_range_lu": Infinity,
  "true_peak_dbfs": -Infinity
}"""
    assert result.structuredContent["integrated_lufs"] == -18.2
    assert result.structuredContent["loudness_range_lu"] is None
    assert result.structuredContent["true_peak_dbfs"] is None
    assert [warning["field"] for warning in result.structuredContent["warnings"]] == [
        "integrated_lufs",
        "loudness_range_lu",
        "true_peak_dbfs",
    ]
    assert result.structuredContent["raw_summary"] == (
        "I:           NaN LUFS\n"
        "LRA:        +inf LU\n"
        "Peak:       -inf dBFS"
    )
    assert len(result.structuredContent["raw_summary"]) <= 2000


@pytest.mark.asyncio
async def test_scene_detection_retains_scores_and_all_parser_warnings(
    multimedia_boundary: tuple[Path, dict[str, str]],
) -> None:
    source, command_output = multimedia_boundary
    command_output["scene_stderr"] = """ffmpeg version fixture
[Parsed_showinfo_1 @ 0x1] n:0 pts:37500 pts_time:1.25 lavfi.scene_score:0.52
[Parsed_showinfo_1 @ 0x1] n:1 pts:bad pts_time:not-a-number lavfi.scene_score:0.61
[Parsed_showinfo_1 @ 0x1] warning: corrupt timestamp metadata
"""

    result = await server.mcp.call_tool(
        "media_scene_detect",
        {"path": str(source), "threshold": 0.45},
    )

    assert result.content[0].text == """{
  "scene_count": 1,
  "scenes": [
    {
      "time": 1.25
    }
  ]
}"""
    assert result.structuredContent["threshold"] == 0.45
    assert result.structuredContent["scene_changes"] == [
        {
            "time_seconds": 1.25,
            "timecode": "00:00:01.250",
            "score": 0.52,
        },
        {
            "time_seconds": None,
            "timecode": None,
            "score": 0.61,
        },
    ]
    assert [warning["kind"] for warning in result.structuredContent["warnings"]] == [
        "malformed_value",
        "command_warning",
    ]
    assert result.structuredContent["warnings"][1]["message"] == (
        "[Parsed_showinfo_1 @ 0x1] warning: corrupt timestamp metadata"
    )
    assert result.structuredContent["raw_summary"] == (
        "[Parsed_showinfo_1 @ 0x1] n:1 pts:bad pts_time:not-a-number "
        "lavfi.scene_score:0.61"
    )
    assert len(result.structuredContent["raw_summary"]) <= 2000


@pytest.mark.asyncio
async def test_scene_detection_rejects_nonfinite_values_without_losing_finite_scores(
    multimedia_boundary: tuple[Path, dict[str, str]],
) -> None:
    source, command_output = multimedia_boundary
    command_output["scene_stderr"] = """ffmpeg version fixture
[Parsed_showinfo_1 @ 0x1] n:0 pts:1 pts_time:1.5 lavfi.scene_score:NaN
[Parsed_showinfo_1 @ 0x1] n:1 pts:2 pts_time:+inf lavfi.scene_score:0.62
[Parsed_showinfo_1 @ 0x1] n:2 pts:3 pts_time:-inf lavfi.scene_score:-inf
"""

    result = await server.mcp.call_tool(
        "media_scene_detect",
        {"path": str(source), "threshold": 0.5},
    )

    assert result.content[0].text == """{
  "scene_count": 3,
  "scenes": [
    {
      "time": 1.5
    },
    {
      "time": Infinity
    },
    {
      "time": -Infinity
    }
  ]
}"""
    assert result.structuredContent["scene_changes"] == [
        {
            "time_seconds": 1.5,
            "timecode": "00:00:01.500",
            "score": None,
        },
        {
            "time_seconds": None,
            "timecode": None,
            "score": 0.62,
        },
        {
            "time_seconds": None,
            "timecode": None,
            "score": None,
        },
    ]
    assert [warning["field"] for warning in result.structuredContent["warnings"]] == [
        "score",
        "time_seconds",
        "time_seconds",
        "score",
    ]
    assert result.structuredContent["raw_summary"] == (
        "[Parsed_showinfo_1 @ 0x1] n:0 pts:1 pts_time:1.5 "
        "lavfi.scene_score:NaN\n"
        "[Parsed_showinfo_1 @ 0x1] n:1 pts:2 pts_time:+inf "
        "lavfi.scene_score:0.62\n"
        "[Parsed_showinfo_1 @ 0x1] n:2 pts:3 pts_time:-inf "
        "lavfi.scene_score:-inf"
    )
    assert len(result.structuredContent["raw_summary"]) <= 2000


@pytest.mark.asyncio
async def test_streams_preserve_real_indices_and_never_relabel_unsupported_types(
    multimedia_boundary: tuple[Path, dict[str, str]],
) -> None:
    source, command_output = multimedia_boundary
    command_output["ffprobe_stdout"] = """{
  "format": {},
  "streams": [
    {"index": 4, "codec_type": "video", "codec_name": "h264"},
    {"index": 9, "codec_type": "subtitle", "codec_name": "mov_text"},
    {"index": 20, "codec_type": "data", "codec_name": "bin_data"},
    {"index": 21, "codec_type": "attachment", "codec_name": "ttf"},
    {"index": 22, "codec_type": "mystery", "codec_name": "private"},
    {"index": 23, "codec_name": "missing_type"}
  ]
}
"""

    result = await server.mcp.call_tool(
        "media_list_streams",
        {"path": str(source)},
    )

    assert result.content[0].text == """[
  {
    "index": 0,
    "type": "video",
    "codec": "h264",
    "language": "",
    "duration": null
  },
  {
    "index": 1,
    "type": "subtitle",
    "codec": "mov_text",
    "language": "",
    "duration": null
  },
  {
    "index": 2,
    "type": "data",
    "codec": "bin_data",
    "language": "",
    "duration": null
  },
  {
    "index": 3,
    "type": "attachment",
    "codec": "ttf",
    "language": "",
    "duration": null
  },
  {
    "index": 4,
    "type": "mystery",
    "codec": "private",
    "language": "",
    "duration": null
  },
  {
    "index": 5,
    "type": null,
    "codec": "missing_type",
    "language": "",
    "duration": null
  }
]"""
    streams = result.structuredContent["streams"]
    assert [stream["index"] for stream in streams] == [4, 9, 20, 21, 22, 23]
    assert [stream["codec_type"] for stream in streams] == [
        "video",
        "subtitle",
        "data",
        "attachment",
        "unknown",
        "unknown",
    ]
    assert [stream.get("reason") for stream in streams[2:]] == [
        "unsupported_codec_type",
        "unsupported_codec_type",
        "unrecognized_codec_type",
        "missing_codec_type",
    ]
    assert streams[2]["raw_summary"] is None
    assert streams[3]["raw_summary"] is None
    assert streams[4]["raw_summary"] == "codec_type: mystery"
    assert streams[5]["raw_summary"] == "codec_type: <missing>"


@pytest.mark.asyncio
async def test_scene_timecodes_round_before_second_minute_and_hour_decomposition(
    multimedia_boundary: tuple[Path, dict[str, str]],
) -> None:
    source, command_output = multimedia_boundary
    command_output["scene_stderr"] = """ffmpeg version fixture
[Parsed_showinfo_1 @ 0x1] n:0 pts_time:0.9994
[Parsed_showinfo_1 @ 0x1] n:1 pts_time:0.9996
[Parsed_showinfo_1 @ 0x1] n:2 pts_time:1.0004
[Parsed_showinfo_1 @ 0x1] n:3 pts_time:59.9994
[Parsed_showinfo_1 @ 0x1] n:4 pts_time:59.9996
[Parsed_showinfo_1 @ 0x1] n:5 pts_time:60.0004
[Parsed_showinfo_1 @ 0x1] n:6 pts_time:3599.9996
[Parsed_showinfo_1 @ 0x1] n:7 pts_time:3600.0004
"""

    result = await server.mcp.call_tool(
        "media_scene_detect",
        {"path": str(source), "threshold": 0.3},
    )

    assert result.content[0].text == """{
  "scene_count": 8,
  "scenes": [
    {
      "time": 0.9994
    },
    {
      "time": 0.9996
    },
    {
      "time": 1.0004
    },
    {
      "time": 59.9994
    },
    {
      "time": 59.9996
    },
    {
      "time": 60.0004
    },
    {
      "time": 3599.9996
    },
    {
      "time": 3600.0004
    }
  ]
}"""
    assert [
        change["timecode"]
        for change in result.structuredContent["scene_changes"]
    ] == [
        "00:00:00.999",
        "00:00:01.000",
        "00:00:01.000",
        "00:00:59.999",
        "00:01:00.000",
        "00:01:00.000",
        "01:00:00.000",
        "01:00:00.000",
    ]


def test_media_models_reject_nonfinite_values_at_validation_boundary() -> None:
    result_models = importlib.import_module("fcp_mcp.result_models.media")

    with pytest.raises(ValidationError):
        result_models.MediaInfoResult(
            filename=None,
            format=None,
            duration_seconds=float("nan"),
            size_mb=None,
            bitrate_kbps=None,
            streams=[],
        )
    with pytest.raises(ValidationError):
        result_models.SceneChangeRecord(
            time_seconds=1.0,
            timecode="00:00:01.000",
            score=float("inf"),
        )
    with pytest.raises(ValidationError):
        result_models.LoudnessResult(
            integrated_lufs=-18.0,
            loudness_range_lu=float("-inf"),
            true_peak_dbfs=-1.0,
            warnings=[],
        )


def test_media_and_puppet_models_are_frozen_strict_and_opaque_free() -> None:
    media_models = importlib.import_module("fcp_mcp.result_models.media")
    puppet_models = importlib.import_module("fcp_mcp.result_models.puppet")
    model_names = (
        "MediaStreamRecord",
        "VideoStreamRecord",
        "AudioStreamRecord",
        "SubtitleStreamRecord",
        "UnsupportedStreamRecord",
        "MediaInfoResult",
        "SilenceRangeRecord",
        "SilenceDetectionResult",
        "BeatRecord",
        "BeatCadenceRecord",
        "BeatDetectionResult",
        "ParserWarningRecord",
        "LoudnessResult",
        "StreamListResult",
        "SceneChangeRecord",
        "SceneDetectionResult",
        "PuppetPresetParameterRecord",
        "PuppetPresetRecord",
        "PuppetPresetListResult",
    )

    for model_name in model_names:
        module = puppet_models if model_name.startswith("Puppet") else media_models
        model = getattr(module, model_name)
        assert model.model_config["frozen"] is True
        assert model.model_config["extra"] == "forbid"
        schema = model.model_json_schema()
        pending: list[object] = [schema]
        while pending:
            item = pending.pop()
            if isinstance(item, dict):
                additional = item.get("additionalProperties", False)
                assert additional is False
                pending.extend(item.values())
            elif isinstance(item, list):
                pending.extend(item)
        assert "typing.Any" not in str(model.model_fields)
