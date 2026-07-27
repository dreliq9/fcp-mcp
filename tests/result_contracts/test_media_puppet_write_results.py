from __future__ import annotations

import hashlib
import importlib
import json
import sys
import types
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from subprocess import CompletedProcess

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import ValidationError

from fcp_mcp import server
from fcp_mcp.config import RuntimeConfig
from fcp_mcp.security.paths import PathPolicy

WRITE_CASES = (
    ("media_extract_thumbnail", "MediaArtifactResult"),
    ("media_extract_thumbnails", "MediaArtifactListResult"),
    ("media_extract_audio", "MediaArtifactResult"),
    ("media_audio_to_midi", "MediaArtifactResult"),
    ("puppet_create_rig", "PuppetRigResult"),
    ("puppet_create_humanoid_rig", "PuppetRigResult"),
    ("puppet_build_scene", "PuppetBuildResult"),
    ("puppet_animate", "PuppetBuildResult"),
    ("puppet_preset_motion", "PuppetBuildResult"),
    ("puppet_multi_scene", "PuppetMultiSceneResult"),
)

FCPXML_MEDIA_TYPE = "application/vnd.apple.fcpxml+xml"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _artifact(path: Path, media_type: str) -> dict[str, object]:
    return {
        "path": str(path.resolve()),
        "media_type": media_type,
        "sha256": _sha256(path),
        "size_bytes": path.stat().st_size,
    }


def _assert_receipt(payload: dict[str, object], destination: Path) -> None:
    receipt = payload["receipt"]
    assert isinstance(receipt, dict)
    uuid.UUID(str(receipt["transaction_id"]))
    assert receipt["source"] is None
    assert receipt["destination"] == str(destination.resolve())
    assert receipt["output_sha256"] == _sha256(destination)
    assert receipt["disposition"] == "committed"
    assert receipt["elapsed_ms"] >= 0
    assert payload["destination"] == _artifact(destination, FCPXML_MEDIA_TYPE)
    assert ET.parse(destination).getroot().tag == "fcpxml"


@pytest.fixture(autouse=True)
def isolated_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    config = RuntimeConfig.from_env(
        {
            "FCP_MCP_OUTPUT_DIR": str(tmp_path),
            "FCP_MCP_ALLOWED_ROOTS": str(tmp_path),
            "FCP_MCP_STATE_DIR": str(state_dir),
            "FCP_MCP_PROFILE": "full",
        },
        home=tmp_path,
    )
    monkeypatch.setattr(server, "CONFIG", config)
    monkeypatch.setattr(server, "PATHS", PathPolicy(config))


def _install_checked_media_runner(
    monkeypatch: pytest.MonkeyPatch,
    *,
    thumbnail_count: int = 2,
    omit_outputs: bool = False,
) -> list[list[str]]:
    ffprobe = importlib.import_module("fcp_mcp.media.ffprobe")
    original_run_checked = ffprobe._run_checked
    commands: list[list[str]] = []

    def runner(command: list[str], **kwargs: object) -> CompletedProcess[str]:
        argv = list(command)
        commands.append(argv)
        if len(argv) == 2 and argv[1] == "-version":
            return CompletedProcess(argv, 0, "ffmpeg version contract\n", "")

        destination = Path(argv[-1])
        if "-vframes" in argv:
            assert argv[1:2] == ["-y"]
            assert argv[2] == "-ss"
            assert argv[3] in {"0.0", "1.25"}
            assert argv[4] == "-i"
            assert argv[-3] == "-vf"
            assert argv[-2] in {"scale=320:-1", "scale=640:-1"}
            if not omit_outputs:
                destination.write_bytes(b"\xff\xd8thumbnail-contract\xff\xd9")
            return CompletedProcess(argv, 0, "", "")

        if any(item.startswith("fps=1/") for item in argv):
            assert argv[1:3] == ["-y", "-i"]
            assert argv[-2] == "fps=1/3.0,scale=480:-1"
            if not omit_outputs:
                for index in range(1, thumbnail_count + 1):
                    Path(str(destination).replace("%04d", f"{index:04d}")).write_bytes(
                        f"thumbnail-{index}".encode()
                    )
            return CompletedProcess(argv, 0, "", "")

        if "-vn" in argv:
            assert argv[1] == "-y"
            assert argv[2] == "-i"
            if destination.suffix == ".flac":
                assert argv[-4:] == ["-vn", "-c:a", "flac", str(destination)]
            elif destination.suffix == ".wav":
                assert argv[-4:] == [
                    "-vn",
                    "-c:a",
                    "pcm_s16le",
                    str(destination),
                ]
            else:
                raise AssertionError(
                    f"unexpected audio command: {argv!r}"
                )
            if not omit_outputs:
                destination.write_bytes(b"RIFFaudio-contract")
            return CompletedProcess(argv, 0, "", "")

        raise AssertionError(f"unexpected external command: {argv!r}")

    def checked(command: list[str], **kwargs: object) -> CompletedProcess[str]:
        kwargs.pop("runner", None)
        return original_run_checked(command, runner=runner, **kwargs)

    monkeypatch.setattr(ffprobe, "_run_checked", checked)
    return commands


def _write_rig_images(tmp_path: Path) -> dict[str, Path]:
    images = {}
    for name in (
        "head",
        "body",
        "left_arm",
        "right_arm",
        "left_leg",
        "right_leg",
    ):
        image = tmp_path / f"{name}.png"
        image.write_bytes(f"png-{name}".encode())
        images[name] = image
    return images


def _rig_json(images: dict[str, Path]) -> str:
    return json.dumps(
        {
            "name": "Ada",
            "position": [12.5, -4.0],
            "parts": [
                {
                    "name": "head",
                    "image": str(images["head"]),
                    "position": [1.0, 200.0],
                    "scale": 0.75,
                    "rotation": 2.5,
                    "anchor": [0.0, -80.0],
                    "z_order": 6,
                    "width": 640,
                    "height": 480,
                },
                {
                    "name": "body",
                    "image": str(images["body"]),
                    "position": [0.0, 0.0],
                    "z_order": 3,
                },
                {
                    "name": "left_arm",
                    "image": str(images["left_arm"]),
                    "position": [-100.0, 50.0],
                    "anchor": [40.0, 80.0],
                    "z_order": 2,
                },
            ],
        }
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "model_name"),
    WRITE_CASES,
    ids=[case[0] for case in WRITE_CASES],
)
async def test_exact_write_group_advertises_named_nonlegacy_schemas(
    tool_name: str,
    model_name: str,
) -> None:
    definition = server.TOOLS.definitions[tool_name]
    assert definition.result_model.__name__ == model_name
    tools = {tool.name: tool for tool in await server.mcp.list_tools()}
    assert set(tools[tool_name].outputSchema["properties"]) != {"result"}


@pytest.mark.asyncio
async def test_media_thumbnail_result_binds_real_bytes_and_exact_text(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    commands = _install_checked_media_runner(monkeypatch)
    source = tmp_path / "source.mov"
    source.write_bytes(b"source-video")
    destination = tmp_path / "thumb.jpg"

    result = await server.mcp.call_tool(
        "media_extract_thumbnail",
        {
            "path": str(source),
            "time": 1.25,
            "output_path": str(destination),
            "width": 640,
        },
    )

    assert result.content[0].text == f"Thumbnail saved: {destination}"
    assert result.structuredContent == {
        "schema_version": "1",
        "operation": "extract_thumbnail",
        "source": _artifact(source, "video/quicktime"),
        "artifact": _artifact(destination, "image/jpeg"),
    }
    assert len(commands) == 2


@pytest.mark.asyncio
async def test_media_thumbnail_missing_promised_file_is_coded_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_checked_media_runner(monkeypatch, omit_outputs=True)
    source = tmp_path / "source.mov"
    source.write_bytes(b"source-video")

    with pytest.raises(ToolError, match="output_missing"):
        await server.mcp.call_tool(
            "media_extract_thumbnail",
            {
                "path": str(source),
                "output_path": str(tmp_path / "missing.jpg"),
            },
        )


@pytest.mark.asyncio
async def test_media_thumbnail_list_binds_count_to_verified_artifacts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_checked_media_runner(monkeypatch, thumbnail_count=2)
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source-video")
    destination = tmp_path / "thumbs"

    result = await server.mcp.call_tool(
        "media_extract_thumbnails",
        {
            "path": str(source),
            "interval": 3.0,
            "output_dir": str(destination),
            "width": 480,
        },
    )
    first = destination / "thumb_0001.jpg"
    second = destination / "thumb_0002.jpg"

    assert result.content[0].text == (
        "{\n"
        '  "count": 2,\n'
        '  "thumbnails": [\n'
        f'    "{first}",\n'
        f'    "{second}"\n'
        "  ]\n"
        "}"
    )
    assert result.structuredContent == {
        "schema_version": "1",
        "operation": "extract_thumbnails",
        "source": _artifact(source, "video/mp4"),
        "requested_count": 2,
        "artifacts": [
            _artifact(first, "image/jpeg"),
            _artifact(second, "image/jpeg"),
        ],
    }


@pytest.mark.asyncio
async def test_media_audio_result_uses_honest_mime_and_exact_text(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_checked_media_runner(monkeypatch)
    source = tmp_path / "source.mov"
    source.write_bytes(b"source-video")
    destination = tmp_path / "audio.flac"

    result = await server.mcp.call_tool(
        "media_extract_audio",
        {
            "path": str(source),
            "output_path": str(destination),
            "format": "flac",
        },
    )

    assert result.content[0].text == (
        "{\n"
        f'  "audio_file": "{destination}",\n'
        '  "format": "flac",\n'
        '  "size_mb": "0.0",\n'
        f'  "source": "{source}"\n'
        "}"
    )
    assert result.structuredContent == {
        "schema_version": "1",
        "operation": "extract_audio",
        "source": _artifact(source, "video/quicktime"),
        "artifact": _artifact(destination, "audio/flac"),
    }


@pytest.mark.asyncio
async def test_media_audio_to_midi_binds_written_midi_bytes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.wav"
    source.write_bytes(b"RIFFsource-audio")
    destination = tmp_path / "notes.mid"

    basic_pitch = types.ModuleType("basic_pitch")
    basic_pitch.ICASSP_2022_MODEL_PATH = "/contract/model"
    inference = types.ModuleType("basic_pitch.inference")

    class MidiData:
        def write(self, path: str) -> None:
            Path(path).write_bytes(b"MThd-midi-contract")

    def predict(
        path: str,
        *,
        model_or_model_path: str,
    ) -> tuple[dict[str, object], MidiData, list[tuple[float, float, int]]]:
        assert path == str(source)
        assert model_or_model_path == "/contract/model"
        return {}, MidiData(), [(0.5, 2.0, 60), (2.0, 3.5, 72)]

    inference.predict = predict
    monkeypatch.setitem(sys.modules, "basic_pitch", basic_pitch)
    monkeypatch.setitem(sys.modules, "basic_pitch.inference", inference)

    result = await server.mcp.call_tool(
        "media_audio_to_midi",
        {"path": str(source), "output_path": str(destination)},
    )

    assert result.content[0].text == (
        "{\n"
        f'  "midi_file": "{destination}",\n'
        '  "notes_detected": 2,\n'
        '  "pitch_range": "MIDI 60-72",\n'
        '  "duration_seconds": "3.0",\n'
        f'  "source": "{source}"\n'
        "}"
    )
    assert result.structuredContent == {
        "schema_version": "1",
        "operation": "audio_to_midi",
        "source": _artifact(source, "audio/wav"),
        "artifact": _artifact(destination, "audio/midi"),
    }


@pytest.mark.asyncio
async def test_puppet_create_rig_returns_complete_service_data_and_persists_it(
    tmp_path: Path,
) -> None:
    images = _write_rig_images(tmp_path)

    result = await server.mcp.call_tool(
        "puppet_create_rig",
        {"rig_json": _rig_json(images)},
    )

    assert result.content[0].text == json.dumps(
        {
            "name": "Ada",
            "position": [12.5, -4.0],
            "parts": [
                {
                    "name": "head",
                    "image": str(images["head"]),
                    "position": [1.0, 200.0],
                    "scale": 0.75,
                    "rotation": 2.5,
                    "anchor": [0.0, -80.0],
                    "z_order": 6,
                },
                {
                    "name": "body",
                    "image": str(images["body"]),
                    "position": [0.0, 0.0],
                    "scale": 1.0,
                    "rotation": 0.0,
                    "anchor": [0, 0],
                    "z_order": 3,
                },
                {
                    "name": "left_arm",
                    "image": str(images["left_arm"]),
                    "position": [-100.0, 50.0],
                    "scale": 1.0,
                    "rotation": 0.0,
                    "anchor": [40.0, 80.0],
                    "z_order": 2,
                },
            ],
            "status": "rig_valid",
        },
        indent=2,
    )
    rig = result.structuredContent["rig"]
    assert rig["name"] == "Ada"
    assert rig["position"] == [12.5, -4.0]
    assert rig["parts"][0] == {
        "name": "head",
        "image": str(images["head"]),
        "position": [1.0, 200.0],
        "scale": 0.75,
        "rotation": 2.5,
        "anchor": [0.0, -80.0],
        "z_order": 6,
        "width": 640,
        "height": 480,
    }

    persisted = list((server.CONFIG.state_dir / "puppet_rigs").glob("*.json"))
    assert len(persisted) == 1
    assert json.loads(persisted[0].read_text()) == result.structuredContent


@pytest.mark.asyncio
async def test_puppet_humanoid_returns_every_real_part_and_persists_it(
    tmp_path: Path,
) -> None:
    images = _write_rig_images(tmp_path)

    result = await server.mcp.call_tool(
        "puppet_create_humanoid_rig",
        {
            "name": "Humanoid",
            "image_dir": str(tmp_path),
            "position_x": 25.0,
            "position_y": -10.0,
            "scale": 0.5,
        },
    )

    assert result.content[0].text == json.dumps(
        {
            "name": "Humanoid",
            "position": [25.0, -10.0],
            "parts_found": [
                "head",
                "body",
                "left_arm",
                "right_arm",
                "left_leg",
                "right_leg",
            ],
            "parts_missing": [],
            "status": "rig_valid",
        },
        indent=2,
    )
    assert [part["name"] for part in result.structuredContent["rig"]["parts"]] == [
        "head",
        "body",
        "left_arm",
        "right_arm",
        "left_leg",
        "right_leg",
    ]
    assert result.structuredContent["rig"]["parts"][0]["image"] == str(images["head"])
    persisted = list((server.CONFIG.state_dir / "puppet_rigs").glob("*.json"))
    assert len(persisted) == 1
    assert json.loads(persisted[0].read_text()) == result.structuredContent


@pytest.mark.asyncio
async def test_puppet_build_scene_reparses_committed_project_and_has_receipt(
    tmp_path: Path,
) -> None:
    images = _write_rig_images(tmp_path)
    destination = tmp_path / "scene.fcpxml"

    result = await server.mcp.call_tool(
        "puppet_build_scene",
        {
            "rigs_json": _rig_json(images),
            "duration": "2s",
            "project_name": "Contract Scene",
            "output_path": str(destination),
        },
    )

    assert result.content[0].text == (
        "{\n"
        f'  "file": "{destination}",\n'
        '  "rigs": 1,\n'
        '  "total_parts": 3,\n'
        '  "duration": "2s",\n'
        '  "status": "scene_built"\n'
        "}"
    )
    assert result.structuredContent["project"] == "Contract Scene"
    assert len(result.structuredContent["rigs"]) == 1
    assert result.structuredContent["animations"] == []
    _assert_receipt(result.structuredContent, destination)


@pytest.mark.asyncio
async def test_puppet_animate_returns_typed_service_animations_and_receipt(
    tmp_path: Path,
) -> None:
    images = _write_rig_images(tmp_path)
    destination = tmp_path / "animated.fcpxml"
    animations = json.dumps(
        [
            {
                "part": "head",
                "property": "rotation",
                "keyframes": [
                    {"time": "0s", "value": 0.0, "interp": "linear"},
                    {"time": "2s", "value": 15.0, "interp": "smooth2"},
                ],
            }
        ]
    )

    result = await server.mcp.call_tool(
        "puppet_animate",
        {
            "rigs_json": _rig_json(images),
            "animations_json": animations,
            "duration": "2s",
            "project_name": "Animated Contract",
            "output_path": str(destination),
        },
    )

    assert result.content[0].text == (
        "{\n"
        f'  "file": "{destination}",\n'
        '  "rigs": 1,\n'
        '  "animations": 1,\n'
        '  "duration": "2s",\n'
        '  "status": "animated_scene_built"\n'
        "}"
    )
    assert result.structuredContent["animations"] == [
        {
            "part_name": "head",
            "property_name": "rotation",
            "keyframes": [
                {"time": "0s", "value": 0.0, "interp": "linear"},
                {"time": "2s", "value": 15.0, "interp": "smooth2"},
            ],
        }
    ]
    _assert_receipt(result.structuredContent, destination)


@pytest.mark.asyncio
async def test_puppet_preset_uses_generated_service_animations_and_receipt(
    tmp_path: Path,
) -> None:
    images = _write_rig_images(tmp_path)
    destination = tmp_path / "preset.fcpxml"

    result = await server.mcp.call_tool(
        "puppet_preset_motion",
        {
            "rig_json": _rig_json(images),
            "preset": "bounce",
            "duration": "2s",
            "project_name": "Preset Contract",
            "output_path": str(destination),
            "cycles": 2,
            "intensity": 0.5,
        },
    )

    assert result.content[0].text == (
        "{\n"
        f'  "file": "{destination}",\n'
        '  "preset": "bounce",\n'
        '  "animations_applied": 1,\n'
        '  "parts_animated": [\n'
        '    "body"\n'
        "  ],\n"
        '  "duration": "2s",\n'
        '  "status": "preset_applied"\n'
        "}"
    )
    assert result.structuredContent["project"] == "Preset Contract"
    assert result.structuredContent["animations"][0]["part_name"] == "body"
    assert result.structuredContent["animations"][0]["property_name"] == "position"
    _assert_receipt(result.structuredContent, destination)


@pytest.mark.asyncio
async def test_puppet_multi_scene_has_one_verified_artifact_per_scene(
    tmp_path: Path,
) -> None:
    images = _write_rig_images(tmp_path)
    scenes = json.dumps(
        [
            {"name": "intro", "duration": "2s", "preset": "idle"},
            {
                "name": "greeting",
                "duration": "3s",
                "preset": "wave",
                "cycles": 2,
            },
        ]
    )

    result = await server.mcp.call_tool(
        "puppet_multi_scene",
        {
            "rigs_json": _rig_json(images),
            "scenes_json": scenes,
            "project_name": "Contract",
            "output_path": str(tmp_path),
        },
    )
    intro = tmp_path / "Contract_intro.fcpxml"
    greeting = tmp_path / "Contract_greeting.fcpxml"

    assert result.content[0].text == (
        "{\n"
        '  "scenes_created": 2,\n'
        '  "files": [\n'
        "    {\n"
        '      "scene": "intro",\n'
        f'      "file": "{intro}",\n'
        '      "preset": "idle"\n'
        "    },\n"
        "    {\n"
        '      "scene": "greeting",\n'
        f'      "file": "{greeting}",\n'
        '      "preset": "wave"\n'
        "    }\n"
        "  ],\n"
        '  "status": "multi_scene_built"\n'
        "}"
    )
    assert result.structuredContent["scene_count"] == 2
    artifacts = result.structuredContent["artifacts"]
    assert [artifact["scene"] for artifact in artifacts] == ["intro", "greeting"]
    for payload, destination in zip(artifacts, (intro, greeting), strict=True):
        _assert_receipt(payload, destination)


@pytest.mark.asyncio
async def test_malformed_puppet_candidate_preserves_existing_destination(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    images = _write_rig_images(tmp_path)
    destination = tmp_path / "existing.fcpxml"
    prior_bytes = b"<?xml version='1.0'?><fcpxml version='1.11'/>"
    destination.write_bytes(prior_bytes)

    class MalformedGenerator:
        def to_string(self) -> str:
            return "<not-fcpxml>"

    monkeypatch.setattr(
        server.PuppetSceneBuilder,
        "build",
        lambda *_args, **_kwargs: MalformedGenerator(),
    )

    with pytest.raises(ToolError, match="validation_failed"):
        await server.mcp.call_tool(
            "puppet_build_scene",
            {
                "rigs_json": _rig_json(images),
                "output_path": str(destination),
            },
        )
    assert destination.read_bytes() == prior_bytes


@pytest.mark.asyncio
async def test_invalid_multi_scene_name_creates_no_artifact(
    tmp_path: Path,
) -> None:
    images = _write_rig_images(tmp_path)

    with pytest.raises(ToolError, match="invalid_arguments"):
        await server.mcp.call_tool(
            "puppet_multi_scene",
            {
                "rigs_json": _rig_json(images),
                "scenes_json": json.dumps(
                    [{"name": 42, "duration": "2s", "preset": "idle"}]
                ),
                "project_name": "Contract",
                "output_path": str(tmp_path),
            },
        )

    assert list(tmp_path.glob("Contract_*.fcpxml")) == []


def test_media_list_model_rejects_wrong_requested_artifact_count() -> None:
    models = importlib.import_module("fcp_mcp.result_models.media")
    reference = {
        "path": "/tmp/thumb.jpg",
        "media_type": "image/jpeg",
        "sha256": "0" * 64,
        "size_bytes": 1,
    }
    with pytest.raises(ValidationError):
        models.MediaArtifactListResult(
            operation="extract_thumbnails",
            source=reference,
            requested_count=2,
            artifacts=[reference],
        )


def test_task9_models_are_frozen_strict_forbid_extra_and_use_no_any() -> None:
    media_models = importlib.import_module("fcp_mcp.result_models.media")
    puppet_models = importlib.import_module("fcp_mcp.result_models.puppet")
    for module, names in (
        (
            media_models,
            ("MediaArtifactResult", "MediaArtifactListResult"),
        ),
        (
            puppet_models,
            (
                "PuppetPartRecord",
                "PuppetRigRecord",
                "PuppetKeyframeRecord",
                "PuppetAnimationRecord",
                "PuppetRigResult",
                "PuppetBuildResult",
                "PuppetSceneArtifactRecord",
                "PuppetMultiSceneResult",
            ),
        ),
    ):
        for name in names:
            model = getattr(module, name)
            assert model.model_config["frozen"] is True
            assert model.model_config["strict"] is True
            assert model.model_config["extra"] == "forbid"
            assert "Any" not in repr(model.__annotations__)

    with pytest.raises(ValidationError):
        puppet_models.PuppetPartRecord(
            name="head",
            image="/tmp/head.png",
            position=[0.0, 0.0],
            scale=1.0,
            rotation=0.0,
            anchor=[0.0, 0.0],
            z_order="1",
            width=1,
            height=1,
        )
