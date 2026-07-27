from __future__ import annotations

import importlib
from collections.abc import Callable
from pathlib import Path
from subprocess import CompletedProcess

import pytest
from pydantic import BaseModel

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
      "index": 0,
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
      "index": 1,
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
      "index": 2,
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
    assert model.streams[0].frame_rate == 30000 / 1001
    assert model.streams[1].codec_type == "audio"
    assert model.streams[1].sample_rate_hz == 48000
    assert model.streams[2].codec_type == "subtitle"
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
        assert len(stream_items["oneOf"]) == 3
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
    assert result.structuredContent["raw_summary"] == (
        "LRA:        wide LU\n"
        "[Parsed_ebur128_0 @ 0x1] warning: gated measurement unavailable"
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
        }
    ]
    assert [warning["kind"] for warning in result.structuredContent["warnings"]] == [
        "malformed_value",
        "command_warning",
    ]
    assert result.structuredContent["raw_summary"] == (
        "[Parsed_showinfo_1 @ 0x1] n:1 pts:bad pts_time:not-a-number "
        "lavfi.scene_score:0.61\n"
        "[Parsed_showinfo_1 @ 0x1] warning: corrupt timestamp metadata"
    )
    assert len(result.structuredContent["raw_summary"]) <= 2000


def test_media_and_puppet_models_are_frozen_strict_and_opaque_free() -> None:
    media_models = importlib.import_module("fcp_mcp.result_models.media")
    puppet_models = importlib.import_module("fcp_mcp.result_models.puppet")
    model_names = (
        "MediaStreamRecord",
        "VideoStreamRecord",
        "AudioStreamRecord",
        "SubtitleStreamRecord",
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
