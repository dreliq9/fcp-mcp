import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.fastmcp.exceptions import ToolError

import fcp_mcp.automation.osascript as automation
from fcp_mcp import server
from fcp_mcp.config import RuntimeConfig
from fcp_mcp.contracts import FCPMCPError
from fcp_mcp.result_models.common import LegacyTextResult, ToolOutcome
from fcp_mcp.security.paths import PathPolicy
from fcp_mcp.version import distribution_version

HOSTILE = 'x" & do shell script "touch /tmp/pwned" & "'


@pytest.fixture
def scoped_server_paths(
    monkeypatch,
    sample_fcpxml_path: Path,
    tmp_path: Path,
):
    config = RuntimeConfig.from_env(
        {
            "FCP_MCP_OUTPUT_DIR": str(tmp_path),
            "FCP_MCP_ALLOWED_ROOTS": os.pathsep.join(
                [str(sample_fcpxml_path.parent), str(tmp_path)]
            ),
        },
        home=tmp_path,
    )
    monkeypatch.setattr(server, "CONFIG", config)
    monkeypatch.setattr(server, "PATHS", PathPolicy(config), raising=False)
    return config


@pytest.fixture
def automation_calls(monkeypatch, tmp_path: Path):
    config = RuntimeConfig.from_env(
        {
            "FCP_MCP_OUTPUT_DIR": str(tmp_path),
            "FCP_MCP_ENABLE_LIVE_CONTROL": "1",
        },
        home=tmp_path,
    )
    calls = []

    def fake(program, args=(), **kwargs):
        calls.append((program, tuple(args), kwargs))
        return "ok"

    monkeypatch.setattr(server, "CONFIG", config)
    monkeypatch.setattr(automation, "run_osascript", fake)
    return calls


@pytest.mark.parametrize(
    ("handler", "value"),
    [
        (server.fcp_get_events, HOSTILE),
        (server.fcp_get_projects, HOSTILE),
        (server.fcp_navigate, HOSTILE),
        (server.fcp_menu_command, f"File > {HOSTILE}"),
        (server.fcp_share, HOSTILE),
    ],
)
def test_live_handler_keeps_hostile_value_out_of_program_source(
    handler,
    value,
    automation_calls,
):
    assert handler(value) == "ok"
    program, args, _ = automation_calls[-1]
    assert HOSTILE not in program.source
    assert HOSTILE in " ".join(args)


def test_keyboard_shortcut_passes_only_canonical_tokens(automation_calls):
    assert server.fcp_keyboard_shortcut("cmd+shift+e") == "ok"
    program, args, _ = automation_calls[-1]
    assert "cmd+shift+e" not in program.source
    assert args == ("cmd+shift+e", "e", "command", "shift")


def test_hostile_keyboard_shortcut_runs_nothing(automation_calls):
    with pytest.raises(FCPMCPError, match="invalid_arguments"):
        server.fcp_keyboard_shortcut("cmd+'")
    assert automation_calls == []


def test_disabled_open_library_runs_no_external_command(monkeypatch, tmp_path: Path):
    config = RuntimeConfig.from_env(
        {"FCP_MCP_OUTPUT_DIR": str(tmp_path)},
        home=tmp_path,
    )
    called = False

    def runner(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(server, "CONFIG", config)
    monkeypatch.setattr(server.subprocess, "run", runner)

    with pytest.raises(FCPMCPError, match="live_control_disabled"):
        server.fcp_open_library(str(tmp_path / "Library.fcpbundle"))
    assert called is False


def test_add_marker_success_text_stays_compatible(
    sample_fcpxml_path: Path,
    tmp_path: Path,
    scoped_server_paths,
):
    output = tmp_path / "marked.fcpxml"
    result = server.fcpxml_add_marker(
        str(sample_fcpxml_path),
        "Interview_A",
        "0s",
        "Marker",
        output_path=str(output),
    )

    assert result.text == f"Marker added. Saved to: {output}"


def test_tool_outcome_equality_preserves_structured_payload_identity():
    left = ToolOutcome(
        text="same text",
        structured=LegacyTextResult(result="left"),
    )
    right = ToolOutcome(
        text="same text",
        structured=LegacyTextResult(result="right"),
    )

    assert str(left) == "same text"
    assert left != "same text"
    assert "same text" != left
    assert left != right


def test_handler_rejects_same_file_output(
    sample_fcpxml_path: Path,
    tmp_path: Path,
    scoped_server_paths,
):
    source = tmp_path / "same-file.fcpxml"
    source.write_bytes(sample_fcpxml_path.read_bytes())

    with pytest.raises(FCPMCPError, match="same_file_forbidden"):
        server.fcpxml_add_marker(
            str(source),
            "Interview_A",
            "0s",
            "Marker",
            output_path=str(source),
        )


def test_generator_rejects_embedded_media_outside_read_roots(
    tmp_path: Path,
    scoped_server_paths,
):
    outside = tmp_path.parent / f"{tmp_path.name}-outside.mov"
    outside.write_bytes(b"media")
    try:
        clips = json.dumps(
            [{"src": str(outside), "duration": "1s"}]
        )
        output = tmp_path / "timeline.fcpxml"

        with pytest.raises(FCPMCPError, match="path_outside_scope"):
            server.fcpxml_create_timeline(
                clips,
                output_path=str(output),
            )

        assert output.exists() is False
    finally:
        outside.unlink(missing_ok=True)


def test_puppet_rig_rejects_embedded_image_outside_read_roots(
    tmp_path: Path,
    scoped_server_paths,
):
    outside = tmp_path.parent / f"{tmp_path.name}-outside.png"
    outside.write_bytes(b"image")
    try:
        rig = json.dumps(
            {
                "name": "unsafe",
                "parts": [
                    {
                        "name": "head",
                        "image": str(outside),
                    }
                ],
            }
        )

        with pytest.raises(FCPMCPError, match="path_outside_scope"):
            server.puppet_create_rig(rig)
    finally:
        outside.unlink(missing_ok=True)


def test_media_link_check_rejects_embedded_paths_outside_read_roots(
    sample_fcpxml_path: Path,
    scoped_server_paths,
):
    with pytest.raises(FCPMCPError, match="path_outside_scope"):
        server.fcpxml_check_media_links(str(sample_fcpxml_path))


@pytest.mark.parametrize(
    ("handler", "filename", "content_marker"),
    [
        (server.fcpxml_export_fcp7, "timeline.xml", "<xmeml"),
        (server.fcpxml_export_edl, "timeline.edl", "TITLE:"),
    ],
)
def test_generic_exports_atomically_backup_existing_destination(
    handler,
    filename,
    content_marker,
    sample_fcpxml_path: Path,
    tmp_path: Path,
    scoped_server_paths,
):
    output = tmp_path / filename
    output.write_text("BEFORE", encoding="utf-8")

    handler(str(sample_fcpxml_path), output_path=str(output))

    assert content_marker in output.read_text(encoding="utf-8")
    backups = list(tmp_path.glob(f"{filename}.bak.*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "BEFORE"


def test_missing_clip_is_a_domain_error(
    sample_fcpxml_path: Path,
    scoped_server_paths,
):
    with pytest.raises(FCPMCPError, match="target_not_found"):
        server.fcpxml_add_marker(
            str(sample_fcpxml_path),
            "missing",
            "0s",
            "Marker",
        )


def test_apply_template_fails_honestly(
    sample_fcpxml_path: Path,
    tmp_path: Path,
    scoped_server_paths,
):
    with pytest.raises(FCPMCPError, match="unsupported_contract"):
        server.fcpxml_apply_template(
            str(sample_fcpxml_path),
            '[{"src":"replacement.mov","duration":"1s"}]',
            output_path=str(tmp_path / "unused.fcpxml"),
        )


@pytest.mark.asyncio
async def test_missing_clip_is_wire_error(tmp_path: Path, sample_fcpxml_path: Path):
    source = tmp_path / "source.fcpxml"
    source.write_bytes(sample_fcpxml_path.read_bytes())
    environment = os.environ.copy()
    environment.update(
        {
            "FCP_MCP_OUTPUT_DIR": str(tmp_path),
            "FCP_MCP_ALLOWED_ROOTS": str(tmp_path),
            "FCP_MCP_PROFILE": "full",
        }
    )
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "fcp_mcp"],
        env=environment,
    )

    async with (
        stdio_client(parameters) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        result = await session.call_tool(
            "fcpxml_add_marker",
            {
                "path": str(source),
                "clip_name": "missing",
                "start": "0s",
                "value": "Marker",
            },
        )

    assert result.isError is True
    assert "target_not_found" in result.content[0].text
    assert (tmp_path / "source_modified.fcpxml").exists() is False


def test_malformed_json_is_invalid_arguments(
    sample_fcpxml_path: Path,
    scoped_server_paths,
):
    with pytest.raises(FCPMCPError, match="invalid_arguments"):
        server.fcpxml_batch_add_markers(
            str(sample_fcpxml_path),
            "{",
        )


def test_invalid_speed_is_invalid_arguments(
    sample_fcpxml_path: Path,
    scoped_server_paths,
):
    with pytest.raises(FCPMCPError, match="invalid_arguments"):
        server.fcpxml_change_speed(
            str(sample_fcpxml_path),
            "Interview_A",
            0,
        )


def test_empty_srt_is_invalid_arguments(
    sample_fcpxml_path: Path,
    tmp_path: Path,
    scoped_server_paths,
):
    srt = tmp_path / "empty.srt"
    srt.write_text("", encoding="utf-8")

    with pytest.raises(FCPMCPError, match="invalid_arguments"):
        server.fcpxml_import_srt(
            str(sample_fcpxml_path),
            str(srt),
        )


def test_unknown_puppet_preset_is_invalid_arguments(
    tmp_path: Path,
    scoped_server_paths,
):
    image = tmp_path / "head.png"
    image.write_bytes(b"image")
    rig = json.dumps(
        {
            "name": "puppet",
            "parts": [{"name": "head", "image": str(image)}],
        }
    )

    with pytest.raises(FCPMCPError, match="invalid_arguments"):
        server.puppet_preset_motion(rig, preset="teleport")


def test_fcp_running_probe_failure_is_a_command_error(monkeypatch):
    def time_out(*_args, **_kwargs):
        raise TimeoutError("probe stalled")

    monkeypatch.setattr(server.subprocess, "run", time_out)

    with pytest.raises(FCPMCPError, match="command_failed"):
        server.fcp_is_running()


@pytest.mark.asyncio
async def test_tool_boundary_sanitizes_unexpected_failures(caplog):
    boundary = server.FCPFastMCP("boundary-test")

    @boundary.tool()
    def explode() -> str:
        raise RuntimeError("private implementation detail")

    with pytest.raises(ToolError) as caught:
        await boundary.call_tool("explode", {})

    assert "internal_error: Unexpected internal failure" in str(caught.value)
    assert "private implementation detail" not in str(caught.value)
    assert "private implementation detail" in caplog.text


@pytest.mark.asyncio
async def test_tool_boundary_preserves_coded_domain_failures():
    boundary = server.FCPFastMCP("boundary-test")

    @boundary.tool()
    def missing() -> str:
        raise FCPMCPError(
            server.ErrorCode.TARGET_NOT_FOUND,
            "clip missing",
        )

    with pytest.raises(ToolError, match="target_not_found"):
        await boundary.call_tool("missing", {})


@pytest.mark.asyncio
async def test_tool_boundary_codes_schema_validation_as_invalid_arguments():
    boundary = server.FCPFastMCP("boundary-test")

    @boundary.tool()
    def requires_count(count: int) -> str:
        return str(count)

    with pytest.raises(ToolError, match="invalid_arguments"):
        await boundary.call_tool("requires_count", {"count": "not-an-integer"})


def test_fcp_running_probe_success_remains_json(monkeypatch):
    monkeypatch.setattr(
        server.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=["osascript"],
            returncode=0,
            stdout="true\n",
            stderr="",
        ),
    )

    assert json.loads(server.fcp_is_running()) == {"running": True}


def test_puppet_animation_success_stays_compatible(
    tmp_path: Path,
    scoped_server_paths,
):
    image = tmp_path / "head.png"
    image.write_bytes(b"image")
    rig = json.dumps(
        {
            "name": "puppet",
            "parts": [{"name": "head", "image": str(image)}],
        }
    )
    animations = json.dumps(
        [
            {
                "part": "head",
                "property": "rotation",
                "keyframes": [
                    {"time": "0s", "value": 0},
                    {"time": "1s", "value": 10},
                ],
            }
        ]
    )
    output = tmp_path / "animated.fcpxml"

    result = json.loads(
        server.puppet_animate(
            rig,
            animations,
            duration="2s",
            output_path=str(output),
        )
    )

    assert result["status"] == "animated_scene_built"
    assert result["animations"] == 1
    assert output.is_file()


@pytest.mark.asyncio
async def test_missing_media_is_wire_error_and_creates_no_output(tmp_path: Path):
    output = tmp_path / "thumbnail.jpg"
    environment = os.environ.copy()
    environment.update(
        {
            "FCP_MCP_OUTPUT_DIR": str(tmp_path),
            "FCP_MCP_ALLOWED_ROOTS": str(tmp_path),
            "FCP_MCP_PROFILE": "full",
        }
    )
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "fcp_mcp"],
        env=environment,
    )

    async with (
        stdio_client(parameters) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        result = await session.call_tool(
            "media_extract_thumbnail",
            {
                "path": str(tmp_path / "missing.mov"),
                "output_path": str(output),
            },
        )

    assert result.isError is True
    assert output.exists() is False


@pytest.mark.asyncio
async def test_wire_identity_and_doctor_versions_are_truthful(tmp_path: Path):
    environment = os.environ.copy()
    environment["FCP_MCP_OUTPUT_DIR"] = str(tmp_path)
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "fcp_mcp"],
        env=environment,
    )

    async with (
        stdio_client(parameters) as (read, write),
        ClientSession(read, write) as session,
    ):
        initialized = await session.initialize()
        result = await session.call_tool("fcp_doctor")

    assert initialized.serverInfo.name == "fcp-mcp"
    assert initialized.serverInfo.version == distribution_version("mcp")
    assert result.isError is False
    assert result.structuredContent["package_version"] == "0.2.1"
    assert result.structuredContent["mcp_sdk_version"] == distribution_version("mcp")
    assert result.structuredContent["wire_server_version"] == distribution_version("mcp")
