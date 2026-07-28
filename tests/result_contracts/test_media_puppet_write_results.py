from __future__ import annotations

import base64
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
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import ValidationError

from fcp_mcp import server
from fcp_mcp.config import RuntimeConfig
from fcp_mcp.contracts import FCPMCPError
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
JPEG_BYTES = base64.b64decode(
    "/9j/4AAQSkZJRgABAgAAAQABAAD//gAQTGF2YzYyLjI4LjEwMAD/2wBDAAgEBAQE"
    "BAUFBQUFBQYGBgYGBgYGBgYGBgYHBwcICAgHBwcGBgcHCAgICAkJCQgICAgJCQoK"
    "CgwMCwsODg4RERT/xABMAAEBAAAAAAAAAAAAAAAAAAAABgEBAQAAAAAAAAAAAAAA"
    "AAAABgcQAQAAAAAAAAAAAAAAAAAAAAARAQAAAAAAAAAAAAAAAAAAAAD/wAARCAAC"
    "AAIDASIAAhEAAxEA/9oADAMBAAIRAxEAPwCLAE1/f//Z"
)
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAACXBIWXMAAAABAAAA"
    "AQBPJcTWAAAAEElEQVR4nGP8wwACLGCSAQANBAECv1AVswAAAABJRU5ErkJggg=="
)
FLAC_BYTES = base64.b64decode(
    "ZkxhQwAAACICQAJAAAAAAASVAfQA8AAAAAAAAAAAAAAAAAAAAAAAAAAAhAAALg0A"
    "AABMYXZmNjIuMTIuMTAwAQAAABUAAABlbmNvZGVyPUxhdmY2Mi4xMi4xMDD/+GQI"
    "AE8JTgAABWsKMg3FD7YPzQ4FCpTlmJO/JGtbXhj3jqRBkn77gI6TffVzFBQs4ace"
    "tz6Lr5N1u+UFHChZ57mLqrkyL7PZ4UIEDDxBDFUVyrJlNWNFmChQkQ8lnYAQIg=="
)
_WAV_SAMPLES = b"\x00\x00\x20\x03\xe0\xfc\x00\x00"
WAV_BYTES = (
    b"RIFF"
    + (36 + len(_WAV_SAMPLES)).to_bytes(4, "little")
    + b"WAVEfmt \x10\x00\x00\x00"
    + b"\x01\x00\x01\x00\x40\x1f\x00\x00\x80\x3e\x00\x00"
    + b"\x02\x00\x10\x00data"
    + len(_WAV_SAMPLES).to_bytes(4, "little")
    + _WAV_SAMPLES
)
MP3_BYTES = base64.b64decode(
    "/+MoxAAdMKqIX08AAAubclAAfv379+/f3vSA8ePHjylLv379+/34bJBL+F+A7gLYEMMM463o"
    "8ePAQrB8/lDnygf6PfrBw5iAH31g4GMgD76wIc0A/yju/B8HAQBAEAQB8HwfB8CAgCAIBgHw"
    "fB+UBAMb//B8HwICAIAg4Dg+D76gQcAB/f+eHNRqGpTYxzfy/+MoxA4g+Z6g8ZlIAFMZlupT"
    "DMqf6MWM6m4ZiTlLmfU9hgcFLE6z5KREf5fIADBQIhDIpWRIm1GCdvQGDTUlkRCKRSsiIlUM"
    "12yRjIQISXFUJCs0rUpXGNSlBufnPYZ7lJFEJAqEwVEQVBVRQMHy5+oeIjyhKd/KOl9FYiqJ"
    "f//16tYB4ATEoSj72VtZrXDo/+MoxA0ZgP4gFcwYAMj5KCIAJsOIERFUphCBsDYxiaMj65JA"
    "FABVDiDVSdCUqSiSqSmJit5o6PiDATVSDAQCBo8CobBUYeBUeCrhEe4NKDv4lnfxF/o/xF//"
    "8Gr6j3EvWdK/z3+dTEFNRTMuMTAwqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq"
)
MIDI_BYTES = (
    b"MThd\x00\x00\x00\x06\x00\x00\x00\x01\x00\x60"
    b"MTrk\x00\x00\x00\x0c"
    b"\x00\x90\x3c\x40\x60\x80\x3c\x40\x00\xff\x2f\x00"
)
QUICKTIME_BYTES = b"\x00\x00\x00\x14ftypqt  \x00\x00\x00\x00qt  "
MP4_BYTES = b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00isommp42"

DECODABLE_MEDIA_BYTES = {
    JPEG_BYTES,
    PNG_BYTES,
    FLAC_BYTES,
    WAV_BYTES,
    MP3_BYTES,
}


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
    thumbnail_bytes: bytes = JPEG_BYTES,
    audio_bytes: bytes | None = None,
    media_returncode: int = 0,
) -> list[list[str]]:
    ffprobe = importlib.import_module("fcp_mcp.media.ffprobe")
    original_run_checked = ffprobe._run_checked
    commands: list[list[str]] = []

    def runner(command: list[str], **kwargs: object) -> CompletedProcess[str]:
        argv = list(command)
        commands.append(argv)
        if len(argv) == 2 and argv[1] == "-version":
            return CompletedProcess(argv, 0, "ffmpeg version contract\n", "")

        if "-abort_on" in argv:
            source = Path(argv[argv.index("-i") + 1])
            media_type = server._detected_media_type(source)
            stream_map = (
                "0:v:0"
                if media_type in {"image/jpeg", "image/png"}
                else "0:a:0"
            )
            assert argv == [
                argv[0],
                "-v",
                "error",
                "-xerror",
                "-err_detect",
                "explode",
                "-i",
                str(source),
                "-map",
                stream_map,
                "-abort_on",
                "empty_output",
                "-f",
                "null",
                "-",
            ]
            if source.read_bytes() in DECODABLE_MEDIA_BYTES:
                return CompletedProcess(argv, 0, "", "")
            return CompletedProcess(argv, 1, "", "strict decode failed")

        destination = Path(argv[-1])
        if "-vframes" in argv:
            assert argv[1:2] == ["-y"]
            assert argv[2] == "-ss"
            assert argv[3] in {"0.0", "1.25"}
            assert argv[4] == "-i"
            assert argv[-3] == "-vf"
            assert argv[-2] in {"scale=320:-1", "scale=640:-1"}
            if media_returncode:
                return CompletedProcess(
                    argv,
                    media_returncode,
                    "",
                    "contract media failure",
                )
            if not omit_outputs:
                destination.write_bytes(thumbnail_bytes)
            return CompletedProcess(argv, 0, "", "")

        if any(item.startswith("fps=1/") for item in argv):
            assert argv[1:3] == ["-y", "-i"]
            assert argv[-2] == "fps=1/3.0,scale=480:-1"
            if media_returncode:
                return CompletedProcess(
                    argv,
                    media_returncode,
                    "",
                    "contract media failure",
                )
            if not omit_outputs:
                for index in range(1, thumbnail_count + 1):
                    Path(
                        str(destination).replace("%04d", f"{index:04d}")
                    ).write_bytes(thumbnail_bytes)
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
            elif destination.suffix == ".mp3":
                assert argv[-6:] == [
                    "-vn",
                    "-c:a",
                    "libmp3lame",
                    "-q:a",
                    "2",
                    str(destination),
                ]
            else:
                raise AssertionError(
                    f"unexpected audio command: {argv!r}"
                )
            if media_returncode:
                return CompletedProcess(
                    argv,
                    media_returncode,
                    "",
                    "contract media failure",
                )
            if not omit_outputs:
                destination.write_bytes(
                    audio_bytes
                    if audio_bytes is not None
                    else {
                        ".flac": FLAC_BYTES,
                        ".mp3": MP3_BYTES,
                        ".wav": WAV_BYTES,
                    }[destination.suffix]
                )
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
    assert set(tools[tool_name].output_schema["properties"]) != {"result"}


@pytest.mark.asyncio
async def test_media_thumbnail_result_binds_real_bytes_and_exact_text(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    commands = _install_checked_media_runner(monkeypatch)
    source = tmp_path / "source.mov"
    source.write_bytes(QUICKTIME_BYTES)
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
    assert result.structured_content == {
        "schema_version": "1",
        "operation": "extract_thumbnail",
        "source": _artifact(source, "video/quicktime"),
        "artifact": _artifact(destination, "image/jpeg"),
    }
    assert len(commands) == 4


@pytest.mark.asyncio
async def test_media_thumbnail_missing_promised_file_is_coded_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_checked_media_runner(monkeypatch, omit_outputs=True)
    source = tmp_path / "source.mov"
    source.write_bytes(QUICKTIME_BYTES)

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
    source.write_bytes(MP4_BYTES)
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
    assert result.structured_content == {
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
    source.write_bytes(QUICKTIME_BYTES)
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
    assert result.structured_content == {
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
    source.write_bytes(WAV_BYTES)
    destination = tmp_path / "notes.mid"

    basic_pitch = types.ModuleType("basic_pitch")
    basic_pitch.ICASSP_2022_MODEL_PATH = "/contract/model"
    inference = types.ModuleType("basic_pitch.inference")

    class MidiData:
        def write(self, path: str) -> None:
            Path(path).write_bytes(MIDI_BYTES)

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
    assert result.structured_content == {
        "schema_version": "1",
        "operation": "audio_to_midi",
        "source": _artifact(source, "audio/wav"),
        "artifact": _artifact(destination, "audio/midi"),
    }


@pytest.mark.asyncio
async def test_media_thumbnail_rejects_text_with_jpeg_suffix(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_checked_media_runner(
        monkeypatch,
        thumbnail_bytes=b"not a jpeg",
    )
    source = tmp_path / "source.mov"
    source.write_bytes(QUICKTIME_BYTES)
    destination = tmp_path / "fake.jpg"

    with pytest.raises(ToolError, match="validation_failed"):
        await server.mcp.call_tool(
            "media_extract_thumbnail",
            {
                "path": str(source),
                "output_path": str(destination),
            },
        )


@pytest.mark.asyncio
async def test_media_audio_rejects_riff_bytes_with_flac_suffix(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_checked_media_runner(
        monkeypatch,
        audio_bytes=WAV_BYTES,
    )
    source = tmp_path / "source.mov"
    source.write_bytes(QUICKTIME_BYTES)
    destination = tmp_path / "fake.flac"

    with pytest.raises(ToolError, match="validation_failed"):
        await server.mcp.call_tool(
            "media_extract_audio",
            {
                "path": str(source),
                "output_path": str(destination),
                "format": "flac",
            },
        )


@pytest.mark.asyncio
async def test_empty_media_source_reaches_prior_command_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    commands = _install_checked_media_runner(
        monkeypatch,
        media_returncode=9,
    )
    source = tmp_path / "empty.mov"
    source.touch()

    with pytest.raises(ToolError, match="command_failed"):
        await server.mcp.call_tool(
            "media_extract_thumbnail",
            {
                "path": str(source),
                "output_path": str(tmp_path / "unused.jpg"),
            },
        )

    assert any("-vframes" in command for command in commands)


@pytest.mark.parametrize(
    ("suffix", "header_only"),
    (
        pytest.param(".jpg", b"\xff\xd8\xff\xd9", id="jpeg"),
        pytest.param(".png", b"\x89PNG\r\n\x1a\n", id="png"),
        pytest.param(".wav", b"RIFF\x04\x00\x00\x00WAVE", id="wav"),
        pytest.param(".mp3", b"ID3", id="mp3"),
        pytest.param(".flac", b"fLaC", id="flac"),
        pytest.param(
            ".mid",
            b"MThd\x00\x00\x00\x06",
            id="midi",
        ),
    ),
)
def test_media_boundary_rejects_header_only_artifacts(
    suffix: str,
    header_only: bytes,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_checked_media_runner(monkeypatch)
    artifact = tmp_path / f"header-only{suffix}"
    artifact.write_bytes(header_only)

    with pytest.raises(FCPMCPError, match="validation_failed"):
        server._media_artifact_reference(artifact)


@pytest.mark.parametrize(
    ("suffix", "corrupt_bytes"),
    (
        pytest.param(
            ".jpg",
            (
                b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00"
                b"\x00\x01\x00\x01\x00\x00\xff\xd9"
            ),
            id="corrupt-jpeg-segments",
        ),
        pytest.param(
            ".flac",
            b"fLaC\x80\x00\x00\x22" + (b"\x00" * 34),
            id="metadata-only-flac",
        ),
        pytest.param(
            ".mid",
            (
                b"MThd\x00\x00\x00\x06\x00\x00\x00\x01\x00\x60"
                b"MTrk\x00\x00\x00\x04\x00\xff"
            ),
            id="truncated-midi-track",
        ),
        pytest.param(
            ".mid",
            (
                b"MThd\x00\x00\x00\x06\x00\x00\x00\x01\x00\x60"
                b"MTrk\x00\x00\x00\x04\x00\x90\x3c\x40"
            ),
            id="midi-missing-end-of-track",
        ),
        pytest.param(
            ".mid",
            (
                b"MThd\x00\x00\x00\x06\x00\x00\x00\x01\x00\x60"
                b"MTrk\x00\x00\x00\x08"
                b"\x81\x80\x80\x80\x00\x90\x3c\x40"
            ),
            id="midi-oversized-vlq",
        ),
        pytest.param(
            ".mid",
            (
                b"MThd\x00\x00\x00\x06\x00\x00\x00\x01\x00\x60"
                b"MTrk\x00\x00\x00\x05\x00\xff\x01\x05\xaa"
            ),
            id="midi-truncated-meta",
        ),
        pytest.param(
            ".mid",
            (
                b"MThd\x00\x00\x00\x06\x00\x00\x00\x01\x00\x60"
                b"MTrk\x00\x00\x00\x04\x00\xf0\x05\x01"
            ),
            id="midi-truncated-sysex",
        ),
        pytest.param(
            ".mid",
            (
                b"MThd\x00\x00\x00\x07\x00\x00\x00\x01\x00\x60\x00"
                b"MTrk\x00\x00\x00\x04\x00\xff\x2f\x00"
            ),
            id="midi-nonstandard-header-length",
        ),
    ),
)
def test_media_boundary_rejects_plausible_but_corrupt_artifacts(
    suffix: str,
    corrupt_bytes: bytes,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_checked_media_runner(monkeypatch)
    artifact = tmp_path / f"corrupt{suffix}"
    artifact.write_bytes(corrupt_bytes)

    with pytest.raises(FCPMCPError, match="validation_failed"):
        server._media_artifact_reference(artifact)


@pytest.mark.parametrize(
    ("suffix", "valid_bytes", "media_type"),
    (
        (".jpg", JPEG_BYTES, "image/jpeg"),
        (".png", PNG_BYTES, "image/png"),
        (".wav", WAV_BYTES, "audio/wav"),
        (".mp3", MP3_BYTES, "audio/mpeg"),
        (".flac", FLAC_BYTES, "audio/flac"),
        (".mid", MIDI_BYTES, "audio/midi"),
    ),
)
def test_media_boundary_accepts_complete_decodable_artifacts(
    suffix: str,
    valid_bytes: bytes,
    media_type: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_checked_media_runner(monkeypatch)
    artifact = tmp_path / f"valid{suffix}"
    artifact.write_bytes(valid_bytes)

    assert server._media_artifact_reference(artifact).model_dump() == (
        _artifact(artifact, media_type)
    )


def test_media_boundary_accepts_valid_midi_running_status(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "running-status.mid"
    artifact.write_bytes(
        b"MThd\x00\x00\x00\x06\x00\x00\x00\x01\x00\x60"
        b"MTrk\x00\x00\x00\x0f"
        b"\x00\x90\x3c\x40"
        b"\x60\x3e\x40"
        b"\x60\x80\x3c\x40"
        b"\x00\xff\x2f\x00"
    )

    assert server._media_artifact_reference(artifact).media_type == "audio/midi"


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
    rig = result.structured_content["rig"]
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
    assert json.loads(persisted[0].read_text()) == result.structured_content


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
    assert [part["name"] for part in result.structured_content["rig"]["parts"]] == [
        "head",
        "body",
        "left_arm",
        "right_arm",
        "left_leg",
        "right_leg",
    ]
    assert result.structured_content["rig"]["parts"][0]["image"] == str(images["head"])
    persisted = list((server.CONFIG.state_dir / "puppet_rigs").glob("*.json"))
    assert len(persisted) == 1
    assert json.loads(persisted[0].read_text()) == result.structured_content


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
    assert result.structured_content["project"] == "Contract Scene"
    assert len(result.structured_content["rigs"]) == 1
    assert result.structured_content["animations"] == []
    _assert_receipt(result.structured_content, destination)


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
    assert result.structured_content["animations"] == [
        {
            "rig_name": "Ada",
            "part_name": "head",
            "property_name": "rotation",
            "keyframes": [
                {"time": "0s", "value": 0.0, "interp": "linear"},
                {"time": "2s", "value": 15.0, "interp": "smooth2"},
            ],
        }
    ]
    _assert_receipt(result.structured_content, destination)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    ("property", "keyframe_count", "time", "value", "interpolation"),
)
async def test_puppet_animation_evidence_rejects_candidate_mutation(
    mutation: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    images = _write_rig_images(tmp_path)
    destination = tmp_path / f"mutated-{mutation}.fcpxml"
    prior_bytes = b"<?xml version='1.0'?><fcpxml version='1.11'/>"
    destination.write_bytes(prior_bytes)
    original_build = server.PuppetSceneBuilder.build

    def mutated_build(
        builder: server.PuppetSceneBuilder,
        *args: object,
        **kwargs: object,
    ) -> object:
        generator = original_build(builder, *args, **kwargs)
        parameter = generator.root.find(".//param")
        assert parameter is not None
        keyframes = parameter.findall("keyframe")
        assert len(keyframes) == 2
        if mutation == "property":
            parameter.set("name", "scale")
        elif mutation == "keyframe_count":
            parameter.remove(keyframes[-1])
        elif mutation == "time":
            keyframes[0].set("time", "1s")
        elif mutation == "value":
            keyframes[0].set("value", "99.0")
        else:
            keyframes[0].set("interp", "hold")
        return generator

    monkeypatch.setattr(server.PuppetSceneBuilder, "build", mutated_build)
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

    with pytest.raises(ToolError, match="validation_failed"):
        await server.mcp.call_tool(
            "puppet_animate",
            {
                "rigs_json": _rig_json(images),
                "animations_json": animations,
                "duration": "2s",
                "output_path": str(destination),
            },
        )

    assert destination.read_bytes() == prior_bytes


@pytest.mark.asyncio
@pytest.mark.parametrize("duration", ("0s", "-1s"))
async def test_puppet_build_rejects_nonpositive_duration_before_commit(
    duration: str,
    tmp_path: Path,
) -> None:
    images = _write_rig_images(tmp_path)
    destination = tmp_path / "nonpositive.fcpxml"
    prior_bytes = b"prior destination"
    destination.write_bytes(prior_bytes)

    with pytest.raises(ToolError, match="invalid_arguments"):
        await server.mcp.call_tool(
            "puppet_build_scene",
            {
                "rigs_json": _rig_json(images),
                "duration": duration,
                "output_path": str(destination),
            },
        )

    assert destination.read_bytes() == prior_bytes


def _assert_expected_transaction_residue(
    destination: Path,
    *,
    backup_path: str | None,
) -> None:
    assert list(destination.parent.glob(f".{destination.name}.*.tmp")) == []
    backups = list(destination.parent.glob(f"{destination.name}.bak.*"))
    if backup_path is None:
        assert backups == []
    else:
        assert backups == [Path(backup_path)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw_duration", "preexisting"),
    (
        pytest.param("+1s", False, id="plus-new"),
        pytest.param(" 1s ", True, id="whitespace-overwrite"),
    ),
)
async def test_puppet_build_canonicalizes_accepted_duration_without_text_drift(
    raw_duration: str,
    preexisting: bool,
    tmp_path: Path,
) -> None:
    images = _write_rig_images(tmp_path)
    destination = tmp_path / "canonical-duration.fcpxml"
    if preexisting:
        destination.write_bytes(b"prior destination")

    result = await server.mcp.call_tool(
        "puppet_build_scene",
        {
            "rigs_json": _rig_json(images),
            "duration": raw_duration,
            "project_name": "Canonical Duration",
            "output_path": str(destination),
        },
    )

    assert result.content[0].text == (
        "{\n"
        f'  "file": "{destination}",\n'
        '  "rigs": 1,\n'
        '  "total_parts": 3,\n'
        f'  "duration": "{raw_duration}",\n'
        '  "status": "scene_built"\n'
        "}"
    )
    assert result.structured_content["duration"] == "1s"
    assert ET.parse(destination).find(".//sequence").get("duration") == "1s"
    _assert_expected_transaction_residue(
        destination,
        backup_path=result.structured_content["receipt"]["backup_path"],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("preexisting", (False, True), ids=("new", "overwrite"))
async def test_puppet_animate_canonicalizes_duration_and_keyframe_times(
    preexisting: bool,
    tmp_path: Path,
) -> None:
    images = _write_rig_images(tmp_path)
    destination = tmp_path / "canonical-keyframes.fcpxml"
    if preexisting:
        destination.write_bytes(b"prior destination")
    animations = json.dumps(
        [
            {
                "part": "head",
                "property": "rotation",
                "keyframes": [
                    {"time": "+0s", "value": 0.0, "interp": "linear"},
                    {"time": " 1s ", "value": 15.0, "interp": "smooth2"},
                ],
            }
        ]
    )

    result = await server.mcp.call_tool(
        "puppet_animate",
        {
            "rigs_json": _rig_json(images),
            "animations_json": animations,
            "duration": " +2s ",
            "project_name": "Canonical Keyframes",
            "output_path": str(destination),
        },
    )

    assert result.content[0].text == (
        "{\n"
        f'  "file": "{destination}",\n'
        '  "rigs": 1,\n'
        '  "animations": 1,\n'
        '  "duration": " +2s ",\n'
        '  "status": "animated_scene_built"\n'
        "}"
    )
    assert result.structured_content["duration"] == "2s"
    assert [
        keyframe["time"]
        for keyframe in result.structured_content["animations"][0]["keyframes"]
    ] == ["0s", "1s"]
    assert [
        keyframe.get("time")
        for keyframe in ET.parse(destination).findall(".//param/keyframe")
    ] == ["0s", "1s"]
    _assert_expected_transaction_residue(
        destination,
        backup_path=result.structured_content["receipt"]["backup_path"],
    )


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
    assert result.structured_content["project"] == "Preset Contract"
    assert result.structured_content["animations"][0]["part_name"] == "body"
    assert result.structured_content["animations"][0]["property_name"] == "position"
    _assert_receipt(result.structured_content, destination)


@pytest.mark.asyncio
async def test_puppet_preset_canonicalizes_duration_without_text_drift(
    tmp_path: Path,
) -> None:
    images = _write_rig_images(tmp_path)
    destination = tmp_path / "canonical-preset.fcpxml"

    result = await server.mcp.call_tool(
        "puppet_preset_motion",
        {
            "rig_json": _rig_json(images),
            "preset": "bounce",
            "duration": "+2s",
            "project_name": "Canonical Preset",
            "output_path": str(destination),
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
        '  "duration": "+2s",\n'
        '  "status": "preset_applied"\n'
        "}"
    )
    assert result.structured_content["duration"] == "2s"
    assert ET.parse(destination).find(".//sequence").get("duration") == "2s"


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
    assert result.structured_content["scene_count"] == 2
    artifacts = result.structured_content["artifacts"]
    assert [artifact["scene"] for artifact in artifacts] == ["intro", "greeting"]
    for payload, destination in zip(artifacts, (intro, greeting), strict=True):
        _assert_receipt(payload, destination)


@pytest.mark.asyncio
async def test_puppet_multi_scene_canonicalizes_accepted_duration(
    tmp_path: Path,
) -> None:
    images = _write_rig_images(tmp_path)
    destination = tmp_path / "Contract_intro.fcpxml"

    result = await server.mcp.call_tool(
        "puppet_multi_scene",
        {
            "rigs_json": _rig_json(images),
            "scenes_json": json.dumps(
                [
                    {
                        "name": "intro",
                        "duration": " 2s ",
                        "preset": "idle",
                    }
                ]
            ),
            "project_name": "Contract",
            "output_path": str(tmp_path),
        },
    )

    assert result.content[0].text == (
        "{\n"
        '  "scenes_created": 1,\n'
        '  "files": [\n'
        "    {\n"
        '      "scene": "intro",\n'
        f'      "file": "{destination}",\n'
        '      "preset": "idle"\n'
        "    }\n"
        "  ],\n"
        '  "status": "multi_scene_built"\n'
        "}"
    )
    assert result.structured_content["artifacts"][0]["duration"] == "2s"
    assert ET.parse(destination).find(".//sequence").get("duration") == "2s"


@pytest.mark.asyncio
async def test_multi_scene_rejects_duplicate_normalized_destinations_prewrite(
    tmp_path: Path,
) -> None:
    images = _write_rig_images(tmp_path)
    scenes = json.dumps(
        [
            {"name": 42, "duration": "2s", "preset": "idle"},
            {"name": "42", "duration": "2s", "preset": "idle"},
        ]
    )

    with pytest.raises(ToolError, match="duplicate"):
        await server.mcp.call_tool(
            "puppet_multi_scene",
            {
                "rigs_json": _rig_json(images),
                "scenes_json": scenes,
                "project_name": "Contract",
                "output_path": str(tmp_path),
            },
        )

    assert list(tmp_path.glob("Contract_*.fcpxml")) == []


def _filesystem_aliases(
    directory: Path,
    first_name: str,
    second_name: str,
) -> bool:
    probe = directory / ".test-filesystem-aliases"
    probe.mkdir()
    try:
        with (probe / first_name).open("xb"):
            pass
        try:
            with (probe / second_name).open("xb"):
                pass
        except FileExistsError:
            return True
        return False
    finally:
        for child in probe.iterdir():
            child.unlink()
        probe.rmdir()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scene_names",
    (
        pytest.param(("Same", "same"), id="case-alias"),
        pytest.param(("caf\u00e9", "cafe\u0301"), id="unicode-alias"),
    ),
)
async def test_multi_scene_uses_destination_filesystem_alias_semantics(
    scene_names: tuple[str, str],
    tmp_path: Path,
) -> None:
    images = _write_rig_images(tmp_path)
    basenames = tuple(
        f"Contract_{scene_name}.fcpxml"
        for scene_name in scene_names
    )
    aliases = _filesystem_aliases(tmp_path, *basenames)
    arguments = {
        "rigs_json": _rig_json(images),
        "scenes_json": json.dumps(
            [
                {"name": scene_name, "duration": "1s", "preset": "idle"}
                for scene_name in scene_names
            ]
        ),
        "project_name": "Contract",
        "output_path": str(tmp_path),
    }

    if aliases:
        with pytest.raises(ToolError, match="duplicate"):
            await server.mcp.call_tool("puppet_multi_scene", arguments)
        assert list(tmp_path.glob("Contract_*.fcpxml")) == []
    else:
        result = await server.mcp.call_tool(
            "puppet_multi_scene",
            arguments,
        )
        assert result.structured_content["scene_count"] == 2
        assert all((tmp_path / basename).is_file() for basename in basenames)

    assert list(tmp_path.glob(".puppet-scene-probe-*")) == []
    assert list(tmp_path.glob("*.bak.*")) == []


@pytest.mark.asyncio
async def test_multi_scene_preflights_every_candidate_before_first_commit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    images = _write_rig_images(tmp_path)
    original_build = server.PuppetSceneBuilder.build
    build_count = 0

    class MalformedGenerator:
        def to_string(self) -> str:
            return "<not-fcpxml>"

    def second_candidate_is_malformed(
        builder: server.PuppetSceneBuilder,
        *args: object,
        **kwargs: object,
    ) -> object:
        nonlocal build_count
        build_count += 1
        if build_count == 2:
            return MalformedGenerator()
        return original_build(builder, *args, **kwargs)

    monkeypatch.setattr(
        server.PuppetSceneBuilder,
        "build",
        second_candidate_is_malformed,
    )

    with pytest.raises(ToolError, match="validation_failed"):
        await server.mcp.call_tool(
            "puppet_multi_scene",
            {
                "rigs_json": _rig_json(images),
                "scenes_json": json.dumps(
                    [
                        {"name": "first", "duration": "2s", "preset": "idle"},
                        {"name": "second", "duration": "2s", "preset": "idle"},
                    ]
                ),
                "project_name": "Contract",
                "output_path": str(tmp_path),
            },
        )

    assert list(tmp_path.glob("Contract_*.fcpxml")) == []


@pytest.mark.asyncio
async def test_multi_scene_rejects_preset_without_compatible_part_prewrite(
    tmp_path: Path,
) -> None:
    body = tmp_path / "body.png"
    body.write_bytes(b"body")
    rig = json.dumps(
        {
            "name": "BodyOnly",
            "parts": [{"name": "body", "image": str(body)}],
        }
    )

    with pytest.raises(ToolError, match="target_not_found"):
        await server.mcp.call_tool(
            "puppet_multi_scene",
            {
                "rigs_json": rig,
                "scenes_json": json.dumps(
                    [{"name": "wave", "duration": "2s", "preset": "wave"}]
                ),
                "project_name": "Contract",
                "output_path": str(tmp_path),
            },
        )

    assert list(tmp_path.glob("Contract_*.fcpxml")) == []


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
async def test_numeric_multi_scene_name_preserves_legacy_compatibility(
    tmp_path: Path,
) -> None:
    images = _write_rig_images(tmp_path)
    destination = tmp_path / "Contract_42.fcpxml"

    result = await server.mcp.call_tool(
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

    assert result.content[0].text == (
        "{\n"
        '  "scenes_created": 1,\n'
        '  "files": [\n'
        "    {\n"
        '      "scene": 42,\n'
        f'      "file": "{destination}",\n'
        '      "preset": "idle"\n'
        "    }\n"
        "  ],\n"
        '  "status": "multi_scene_built"\n'
        "}"
    )
    assert result.structured_content["artifacts"][0]["scene"] == "42"
    _assert_receipt(result.structured_content["artifacts"][0], destination)


@pytest.mark.asyncio
@pytest.mark.parametrize("unsafe_name", (["nested"], {"nested": "name"}))
async def test_structured_multi_scene_name_remains_invalid(
    unsafe_name: object,
    tmp_path: Path,
) -> None:
    images = _write_rig_images(tmp_path)

    with pytest.raises(ToolError, match="invalid_arguments"):
        await server.mcp.call_tool(
            "puppet_multi_scene",
            {
                "rigs_json": _rig_json(images),
                "scenes_json": json.dumps(
                    [
                        {
                            "name": unsafe_name,
                            "duration": "2s",
                            "preset": "idle",
                        }
                    ]
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


def _artifact_payload(
    path: str = "/tmp/destination.fcpxml",
    *,
    media_type: str = "application/vnd.apple.fcpxml+xml",
    sha256: str = "2" * 64,
) -> dict[str, object]:
    return {
        "path": path,
        "media_type": media_type,
        "sha256": sha256,
        "size_bytes": 1,
    }


def _receipt_payload(
    destination: str = "/tmp/destination.fcpxml",
    *,
    output_sha256: str = "2" * 64,
    elapsed_ms: int = 1,
) -> dict[str, object]:
    return {
        "transaction_id": "task-9-contract",
        "source": None,
        "destination": destination,
        "backup_path": None,
        "input_sha256": None,
        "prior_sha256": None,
        "output_sha256": output_sha256,
        "validation_warnings": [],
        "elapsed_ms": elapsed_ms,
        "disposition": "committed",
    }


def _rig_payload() -> dict[str, object]:
    return {
        "name": "hero",
        "position": [0.0, 0.0],
        "parts": [
            {
                "name": "head",
                "image": "/tmp/head.png",
                "position": [0.0, 0.0],
                "scale": 1.0,
                "rotation": 0.0,
                "anchor": [0.0, 0.0],
                "z_order": 1,
                "width": 1,
                "height": 1,
            }
        ],
    }


def _rotation_animation_payload() -> dict[str, object]:
    return {
        "rig_name": "hero",
        "part_name": "head",
        "property_name": "rotation",
        "keyframes": [
            {"time": "0s", "value": 0.0, "interp": "linear"},
            {"time": "1s", "value": 10.0, "interp": "smooth2"},
        ],
    }


def test_media_artifact_models_bind_operation_to_artifact_type() -> None:
    models = importlib.import_module("fcp_mcp.result_models.media")
    source = _artifact_payload(
        "/tmp/source.mov",
        media_type="video/quicktime",
        sha256="1" * 64,
    )

    with pytest.raises(ValidationError):
        models.MediaArtifactResult(
            operation="extract_thumbnail",
            source=source,
            artifact=_artifact_payload(
                "/tmp/not-an-image.flac",
                media_type="audio/flac",
            ),
        )
    with pytest.raises(ValidationError):
        models.MediaArtifactListResult(
            operation="extract_thumbnails",
            source=source,
            requested_count=1,
            artifacts=[
                _artifact_payload(
                    "/tmp/not-an-image.wav",
                    media_type="audio/wav",
                )
            ],
        )
    for unsupported_image_type in ("image/gif", "image/tiff"):
        with pytest.raises(ValidationError):
            models.MediaArtifactResult(
                operation="extract_thumbnail",
                source=source,
                artifact=_artifact_payload(
                    "/tmp/unsupported-image",
                    media_type=unsupported_image_type,
                ),
            )
        with pytest.raises(ValidationError):
            models.MediaArtifactListResult(
                operation="extract_thumbnails",
                source=source,
                requested_count=1,
                artifacts=[
                    _artifact_payload(
                        "/tmp/unsupported-image",
                        media_type=unsupported_image_type,
                    )
                ],
            )
    for unsupported_audio_type in ("audio/midi", "audio/ogg"):
        with pytest.raises(ValidationError):
            models.MediaArtifactResult(
                operation="extract_audio",
                source=source,
                artifact=_artifact_payload(
                    "/tmp/unsupported-audio",
                    media_type=unsupported_audio_type,
                ),
            )


def test_puppet_build_model_accepts_operation_and_binds_invariants() -> None:
    models = importlib.import_module("fcp_mcp.result_models.puppet")
    payload = {
        "operation": "animate",
        "project": "Contract",
        "rigs": [_rig_payload()],
        "animations": [_rotation_animation_payload()],
        "duration": "2s",
        "destination": _artifact_payload(),
        "receipt": _receipt_payload(),
    }

    result = models.PuppetBuildResult(**payload)
    assert result.operation == "animate"

    contradictions = (
        {**payload, "operation": "build_scene"},
        {**payload, "animations": []},
        {**payload, "duration": "not-a-time"},
        {
            **payload,
            "animations": [
                {
                    **_rotation_animation_payload(),
                    "keyframes": [
                        {"time": "0s", "value": [0.0, 1.0], "interp": "linear"}
                    ],
                }
            ],
        },
        {
            **payload,
            "animations": [
                {
                    **_rotation_animation_payload(),
                    "property_name": "scale",
                    "keyframes": [
                        {"time": "0s", "value": 1.0, "interp": "linear"}
                    ],
                }
            ],
        },
        {
            **payload,
            "animations": [
                {
                    **_rotation_animation_payload(),
                    "keyframes": [
                        {"time": "-1s", "value": 0.0, "interp": "linear"}
                    ],
                }
            ],
        },
        {
            **payload,
            "receipt": _receipt_payload("/tmp/other.fcpxml"),
        },
        {
            **payload,
            "receipt": _receipt_payload(output_sha256="3" * 64),
        },
        {
            **payload,
            "receipt": _receipt_payload(elapsed_ms=-1),
        },
    )
    for contradiction in contradictions:
        with pytest.raises(ValidationError):
            models.PuppetBuildResult(**contradiction)


def test_puppet_multi_scene_model_binds_operation_and_artifacts() -> None:
    models = importlib.import_module("fcp_mcp.result_models.puppet")
    artifact = {
        "operation": "multi_scene",
        "scene": "intro",
        "preset": "idle",
        "project": "Contract_intro",
        "rigs": [_rig_payload()],
        "animations": [_rotation_animation_payload()],
        "duration": "2s",
        "destination": _artifact_payload(),
        "receipt": _receipt_payload(),
    }
    result = models.PuppetMultiSceneResult(
        scene_count=1,
        artifacts=[artifact],
    )
    assert result.artifacts[0].operation == "multi_scene"

    for contradiction in (
        {**artifact, "operation": "animate"},
        {**artifact, "animations": []},
        {
            **artifact,
            "destination": _artifact_payload("/tmp/other.fcpxml"),
        },
    ):
        with pytest.raises(ValidationError):
            models.PuppetMultiSceneResult(
                scene_count=1,
                artifacts=[contradiction],
            )

    with pytest.raises(ValidationError):
        models.PuppetMultiSceneResult(scene_count=0, artifacts=[])


@pytest.mark.parametrize(
    ("rigs", "animation"),
    (
        pytest.param(
            [_rig_payload()],
            {**_rotation_animation_payload(), "rig_name": "ghost"},
            id="missing-rig",
        ),
        pytest.param(
            [_rig_payload()],
            {**_rotation_animation_payload(), "part_name": "body"},
            id="missing-part",
        ),
        pytest.param(
            [_rig_payload(), _rig_payload()],
            _rotation_animation_payload(),
            id="duplicate-rig-identity",
        ),
        pytest.param(
            [
                {
                    **_rig_payload(),
                    "parts": [
                        _rig_payload()["parts"][0],
                        _rig_payload()["parts"][0],
                    ],
                }
            ],
            _rotation_animation_payload(),
            id="duplicate-part-identity",
        ),
    ),
)
def test_puppet_write_models_bind_animations_to_unambiguous_rig_parts(
    rigs: list[dict[str, object]],
    animation: dict[str, object],
) -> None:
    models = importlib.import_module("fcp_mcp.result_models.puppet")
    build_payload = {
        "operation": "animate",
        "project": "Contract",
        "rigs": rigs,
        "animations": [animation],
        "duration": "2s",
        "destination": _artifact_payload(),
        "receipt": _receipt_payload(),
    }
    with pytest.raises(ValidationError):
        models.PuppetBuildResult(**build_payload)

    scene_payload = {
        "operation": "multi_scene",
        "scene": "intro",
        "preset": "idle",
        "project": "Contract_intro",
        "rigs": rigs,
        "animations": [animation],
        "duration": "2s",
        "destination": _artifact_payload(),
        "receipt": _receipt_payload(),
    }
    with pytest.raises(ValidationError):
        models.PuppetMultiSceneResult(
            scene_count=1,
            artifacts=[scene_payload],
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
