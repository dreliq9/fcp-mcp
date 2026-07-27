import json
import os
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

import fcp_mcp.automation.osascript as automation
import fcp_mcp.utils.paths as utility_paths
from fcp_mcp import server
from fcp_mcp.config import RuntimeConfig
from fcp_mcp.contracts import FCPMCPError
from fcp_mcp.media import ffprobe
from fcp_mcp.result_models.live import (
    CompressorSubmissionRequest,
    FCPMenuCommandRequest,
    FCPMenuCommandResult,
    FCPNavigateRequest,
    FCPNavigateResult,
    FCPPlaybackRequest,
    FCPPlaybackResult,
    VerificationStatus,
)
from fcp_mcp.security.paths import PathPolicy

EXPECTED_LIVE_RESULT_MODELS = {
    "fcp_is_running": "FCPRunningResult",
    "fcp_get_libraries": "FCPCollectionResult",
    "fcp_get_events": "FCPCollectionResult",
    "fcp_get_projects": "FCPCollectionResult",
    "fcp_get_timeline_info": "FCPTimelineInfoResult",
    "fcp_get_app_state": "FCPAppStateResult",
    "fcp_open_library": "FCPOpenLibraryResult",
    "fcp_import_xml": "FCPImportXMLResult",
    "fcp_export_xml": "FCPExportXMLResult",
    "fcp_playback": "FCPPlaybackResult",
    "fcp_navigate": "FCPNavigateResult",
    "fcp_select_tool": "FCPSelectToolResult",
    "fcp_undo": "FCPUndoResult",
    "fcp_redo": "FCPRedoResult",
    "fcp_menu_command": "FCPMenuCommandResult",
    "fcp_keyboard_shortcut": "FCPKeyboardShortcutResult",
    "fcp_share": "FCPShareResult",
    "compressor_encode": "CompressorSubmissionResult",
    "compressor_list_settings": "CompressorSettingsResult",
}


def test_all_live_tools_declare_their_domain_result_models():
    actual = {
        name: server.TOOLS.definitions[name].result_model.__name__
        for name in EXPECTED_LIVE_RESULT_MODELS
    }

    assert actual == EXPECTED_LIVE_RESULT_MODELS


@pytest.fixture
def live_config(monkeypatch, tmp_path: Path):
    config = RuntimeConfig.from_env(
        {
            "FCP_MCP_ALLOWED_ROOTS": str(tmp_path),
            "FCP_MCP_ENABLE_LIVE_CONTROL": "1",
            "FCP_MCP_OUTPUT_DIR": str(tmp_path),
        },
        home=tmp_path,
    )
    monkeypatch.setattr(server, "CONFIG", config)
    monkeypatch.setattr(server, "PATHS", PathPolicy(config))
    return config


@pytest.mark.parametrize(
    ("handler_name", "arguments", "raw"),
    [
        (
            "fcp_get_libraries",
            (),
            '[{"name":"Library","id":"lib-1","file":"/tmp/Library.fcpbundle"}]',
        ),
        (
            "fcp_get_events",
            ("Library",),
            '[{"library":"Library","name":"Event","id":"event-1"}]',
        ),
        (
            "fcp_get_projects",
            ("Event",),
            (
                '[{"library":"Library","event":"Event","name":"Project",'
                '"id":"project-1","duration":"600/30s"}]'
            ),
        ),
        (
            "fcp_get_timeline_info",
            (),
            (
                '{"library":"Library","event":"Event","project":"Project",'
                '"duration":{"value":600,"timescale":30},'
                '"frameDuration":{"value":1,"timescale":30},"tcFormat":"NDF"}'
            ),
        ),
        (
            "fcp_get_app_state",
            (),
            (
                '{"name":"Final Cut Pro","version":"11.1","frontmost":true,'
                '"libraryCount":2}'
            ),
        ),
    ],
)
def test_live_read_text_is_exact_and_structured_evidence_is_typed(
    monkeypatch,
    handler_name,
    arguments,
    raw,
):
    monkeypatch.setattr(automation, "run_osascript", lambda *_args, **_kwargs: raw)

    outcome = getattr(server, handler_name)(*arguments)

    assert isinstance(outcome, str)
    assert str(outcome) == raw
    assert outcome.text == raw
    assert outcome.structured.schema_version == "1"


@pytest.mark.parametrize(
    ("handler_name", "arguments", "raw"),
    [
        ("fcp_get_libraries", (), "{"),
        ("fcp_get_libraries", (), "{}"),
        ("fcp_get_libraries", (), '[{"name":"Library","id":1,"file":"/tmp/L"}]'),
        ("fcp_get_events", ("Library",), "null"),
        (
            "fcp_get_events",
            ("Library",),
            '[{"library":"Library","name":null,"id":"event-1"}]',
        ),
        ("fcp_get_projects", ("Event",), '"not-a-list"'),
        (
            "fcp_get_projects",
            ("Event",),
            (
                '[{"library":"Library","event":"Event","name":"Project",'
                '"id":"project-1","duration":30}]'
            ),
        ),
        ("fcp_get_timeline_info", (), "not-json"),
        (
            "fcp_get_timeline_info",
            (),
            (
                '{"library":"Library","event":"Event","project":"Project",'
                '"duration":{"value":"600","timescale":30},'
                '"frameDuration":{"value":1,"timescale":30},"tcFormat":"NDF"}'
            ),
        ),
        ("fcp_get_app_state", (), "[]"),
        (
            "fcp_get_app_state",
            (),
            (
                '{"name":"Final Cut Pro","version":11.1,"frontmost":true,'
                '"libraryCount":2}'
            ),
        ),
        (
            "fcp_get_app_state",
            (),
            (
                '{"name":"Final Cut Pro","version":"11.1","frontmost":"true",'
                '"libraryCount":2}'
            ),
        ),
    ],
)
def test_malformed_live_read_is_a_coded_command_failure(
    monkeypatch,
    handler_name,
    arguments,
    raw,
):
    monkeypatch.setattr(automation, "run_osascript", lambda *_args, **_kwargs: raw)

    with pytest.raises(FCPMCPError, match="command_failed"):
        getattr(server, handler_name)(*arguments)


def test_timeline_does_not_infer_unobserved_playhead_or_range(monkeypatch):
    raw = (
        '{"library":"Library","event":"Event","project":"Project",'
        '"duration":{"value":600,"timescale":30},'
        '"frameDuration":{"value":1,"timescale":30},"tcFormat":"NDF"}'
    )
    monkeypatch.setattr(automation, "run_osascript", lambda *_args, **_kwargs: raw)

    structured = server.fcp_get_timeline_info().structured

    assert structured.playhead is None
    assert structured.selection_range is None
    assert structured.warnings == [
        "The current JXA response does not observe the playhead position.",
        "The current JXA response does not observe a timeline selection or range.",
    ]


def test_timeline_error_payload_remains_target_not_found(monkeypatch):
    monkeypatch.setattr(
        automation,
        "run_osascript",
        lambda *_args, **_kwargs: '{"error":"No projects"}',
    )

    with pytest.raises(FCPMCPError, match="target_not_found"):
        server.fcp_get_timeline_info()


@pytest.mark.parametrize("state", ["true", "false"])
def test_running_probe_retains_exact_json_text_and_process_observation(
    monkeypatch,
    state,
):
    monkeypatch.setattr(
        server.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            ["osascript"],
            0,
            f"{state}\n",
            "",
        ),
    )

    outcome = server.fcp_is_running()

    assert str(outcome) == json.dumps({"running": state == "true"})
    assert outcome.structured.running is (state == "true")
    assert outcome.structured.observation_method == "system_events_process_list"


@pytest.mark.parametrize("stdout", ["1", "yes", "", "null"])
def test_running_probe_rejects_non_boolean_observation(monkeypatch, stdout):
    monkeypatch.setattr(
        server.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            ["osascript"],
            0,
            stdout,
            "",
        ),
    )

    with pytest.raises(FCPMCPError, match="command_failed"):
        server.fcp_is_running()


def test_compressor_settings_separates_resolved_paths_from_raw_cli_lines(
    monkeypatch,
    tmp_path: Path,
):
    settings = tmp_path / "Settings"
    custom = settings / "Nested" / "Web.cmprstng"
    custom.parent.mkdir(parents=True)
    custom.write_text("preset", encoding="utf-8")
    compressor = tmp_path / "Compressor"
    compressor.write_text("binary", encoding="utf-8")
    monkeypatch.setattr(utility_paths, "compressor_settings_dir", lambda: settings)
    monkeypatch.setattr(utility_paths, "compressor_binary", lambda: compressor)
    monkeypatch.setattr(
        ffprobe,
        "_run_checked",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [str(compressor), "-info"],
            0,
            "Built-in HD\nRaw: CLI information\n",
            "diagnostic warning\n",
        ),
    )

    outcome = server.compressor_list_settings()

    assert json.loads(str(outcome)) == {
        "custom_presets": [str(custom)],
        "cli_info": ["Built-in HD", "Raw: CLI information"],
    }
    assert [item.path for item in outcome.structured.custom_settings] == [
        str(custom.resolve())
    ]
    assert [item.raw_line for item in outcome.structured.cli_information] == [
        "Built-in HD",
        "Raw: CLI information",
    ]
    assert outcome.structured.warnings == [
        "Custom setting paths were discovered, but preset contents were not validated.",
        "Compressor -info output is retained as raw CLI information.",
    ]


@pytest.mark.parametrize(
    ("handler", "arguments"),
    [
        (server.fcp_open_library, ("Library.fcpbundle",)),
        (server.fcp_import_xml, ("import.fcpxml",)),
        (server.fcp_export_xml, ()),
        (server.fcp_playback, ("play",)),
        (server.fcp_navigate, ("00:01:30:00",)),
        (server.fcp_select_tool, ("blade",)),
        (server.fcp_undo, ()),
        (server.fcp_redo, ()),
        (server.fcp_menu_command, ("File > Export XML...",)),
        (server.fcp_keyboard_shortcut, ("cmd+shift+e",)),
        (server.fcp_share, ("Apple Devices 4K",)),
        (server.compressor_encode, ("source.mov",)),
    ],
)
def test_disabled_live_control_executes_no_external_action(
    monkeypatch,
    tmp_path: Path,
    handler,
    arguments,
):
    config = RuntimeConfig.from_env(
        {"FCP_MCP_OUTPUT_DIR": str(tmp_path)},
        home=tmp_path,
    )
    external_calls = []
    monkeypatch.setattr(server, "CONFIG", config)
    monkeypatch.setattr(
        server.subprocess,
        "run",
        lambda *_args, **_kwargs: external_calls.append("subprocess"),
    )

    with pytest.raises(FCPMCPError, match="live_control_disabled"):
        handler(*arguments)

    assert external_calls == []


@pytest.mark.parametrize(
    ("handler_name", "arguments", "raw", "expected_request"),
    [
        ("fcp_export_xml", (), "Export XML dialog opened", {}),
        (
            "fcp_playback",
            ("play",),
            "Playback: play",
            {"playback_action": "play", "key": "l"},
        ),
        (
            "fcp_navigate",
            ("00:01:30:00",),
            "Navigated to 00:01:30:00",
            {"timecode": "00:01:30:00", "normalized_timecode": "00013000"},
        ),
        (
            "fcp_select_tool",
            ("blade",),
            "Tool: blade",
            {"tool": "blade", "key": "b"},
        ),
        ("fcp_undo", (), "Undo performed", {}),
        ("fcp_redo", (), "Redo performed", {}),
        (
            "fcp_menu_command",
            ("File > Export XML...",),
            "Menu command executed: File > Export XML...",
            {
                "menu_path": "File > Export XML...",
                "components": ["File", "Export XML..."],
            },
        ),
        (
            "fcp_keyboard_shortcut",
            ("cmd+shift+e",),
            "Shortcut sent: cmd+shift+e",
            {
                "keys": "cmd+shift+e",
                "key": "e",
                "modifiers": ["command", "shift"],
            },
        ),
        (
            "fcp_share",
            ("Apple Devices 4K",),
            "Share dialog opened: Apple Devices 4K",
            {"destination": "Apple Devices 4K"},
        ),
    ],
)
def test_osascript_actions_preserve_text_and_remain_unverified(
    monkeypatch,
    live_config,
    handler_name,
    arguments,
    raw,
    expected_request,
):
    calls = []

    def fake(program, args=(), **kwargs):
        calls.append((program, list(args), kwargs))
        return raw

    monkeypatch.setattr(automation, "run_osascript", fake)

    outcome = getattr(server, handler_name)(*arguments)

    assert str(outcome) == raw
    assert outcome.structured.request.model_dump() == expected_request
    assert outcome.structured.raw_response == raw
    assert outcome.structured.verification_status is VerificationStatus.UNVERIFIED
    assert outcome.structured.observed_outcome is None
    assert outcome.structured.warnings == [
        "The automation command returned, but Final Cut Pro state was not independently observed."
    ]
    assert len(calls) == 1


def test_hostile_menu_value_remains_argv_and_typed_request(
    monkeypatch,
    live_config,
):
    hostile = 'x" & do shell script "touch /tmp/pwned" & "'
    calls = []

    def fake(program, args=(), **kwargs):
        calls.append((program, list(args)))
        return "ok"

    monkeypatch.setattr(automation, "run_osascript", fake)

    outcome = server.fcp_menu_command(f"File > {hostile}")

    program, argv = calls[0]
    assert hostile not in program.source
    assert hostile in argv
    assert outcome.structured.request.components == ["File", hostile]


def test_open_and_import_preserve_resolved_request_and_unverified_status(
    monkeypatch,
    live_config,
    tmp_path: Path,
):
    library = tmp_path / "Library.fcpbundle"
    library.mkdir()
    fcpxml = tmp_path / "import.fcpxml"
    fcpxml.write_text("<fcpxml/>", encoding="utf-8")
    commands = []

    def runner(command, **kwargs):
        commands.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(server.subprocess, "run", runner)

    opened = server.fcp_open_library(str(library))
    imported = server.fcp_import_xml(str(fcpxml))

    assert commands == [
        (["open", str(library.resolve())], {"check": True, "timeout": 10}),
        (
            ["open", "-a", "Final Cut Pro", str(fcpxml.resolve())],
            {"check": True, "timeout": 10},
        ),
    ]
    assert str(opened) == f"Opening library: {library.resolve()}"
    assert opened.structured.request.library_path == str(library.resolve())
    assert opened.structured.verification_status is VerificationStatus.UNVERIFIED
    assert opened.structured.observed_outcome is None
    assert str(imported) == f"Importing FCPXML: {fcpxml.resolve()}"
    assert imported.structured.request.fcpxml_path == str(fcpxml.resolve())
    assert imported.structured.verification_status is VerificationStatus.UNVERIFIED
    assert imported.structured.observed_outcome is None


def test_compressor_submission_keeps_exact_argv_and_command_channels(
    monkeypatch,
    live_config,
    tmp_path: Path,
):
    compressor = tmp_path / "Compressor"
    compressor.write_text("binary", encoding="utf-8")
    source = tmp_path / 'source $(touch "pwned").mov'
    source.write_bytes(b"media")
    setting = tmp_path / 'setting "hostile".cmprstng'
    setting.write_text("preset", encoding="utf-8")
    destination = tmp_path / "output"
    destination.mkdir()
    captured = {}
    monkeypatch.setattr(utility_paths, "compressor_binary", lambda: compressor)

    def run_checked(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(
            command,
            0,
            "Job ID: JOB-123_abc\n",
            "Compressor diagnostic\n",
        )

    monkeypatch.setattr(ffprobe, "_run_checked", run_checked)

    outcome = server.compressor_encode(
        str(source),
        str(setting),
        str(destination),
        'Batch "$(hostile)"',
    )

    expected_argv = [
        str(compressor),
        "-batchName",
        'Batch "$(hostile)"',
        "-jobpath",
        str(source.resolve()),
        "-settingpath",
        str(setting.resolve()),
        "-locationpath",
        str(destination.resolve()),
    ]
    assert captured == {"command": expected_argv, "kwargs": {"timeout": 30}}
    assert str(outcome) == "Compressor encode started: Job ID: JOB-123_abc\n"
    assert outcome.structured.request == CompressorSubmissionRequest(
        source_path=str(source.resolve()),
        setting_path=str(setting.resolve()),
        output_directory=str(destination.resolve()),
        batch_name='Batch "$(hostile)"',
    )
    assert outcome.structured.command.argv == expected_argv
    assert outcome.structured.command.returncode == 0
    assert outcome.structured.command.stdout == "Job ID: JOB-123_abc\n"
    assert outcome.structured.command.stderr == "Compressor diagnostic\n"
    assert outcome.structured.job_identifier == "JOB-123_abc"
    assert outcome.structured.verification_status is VerificationStatus.UNVERIFIED
    assert outcome.structured.observed_outcome is None


@pytest.mark.parametrize(
    "stdout",
    [
        "JOB-123",
        "Submission succeeded",
        "job id: has spaces",
        "Job: ../not-an-identifier",
    ],
)
def test_compressor_does_not_invent_identifier_from_arbitrary_output(
    monkeypatch,
    live_config,
    tmp_path: Path,
    stdout,
):
    compressor = tmp_path / "Compressor"
    compressor.write_text("binary", encoding="utf-8")
    source = tmp_path / "source.mov"
    source.write_bytes(b"media")
    monkeypatch.setattr(utility_paths, "compressor_binary", lambda: compressor)
    monkeypatch.setattr(
        ffprobe,
        "_run_checked",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command,
            0,
            stdout,
            "",
        ),
    )

    outcome = server.compressor_encode(str(source))

    assert outcome.structured.job_identifier is None


def test_live_action_models_reject_cross_tool_and_contradictory_requests():
    with pytest.raises(ValidationError):
        FCPNavigateResult(
            action="fcp_playback",
            request=FCPNavigateRequest(
                timecode="00:01:30:00",
                normalized_timecode="00013000",
            ),
            raw_response="ok",
            verification_status="unverified",
            observed_outcome=None,
            warnings=["not observed"],
        )
    with pytest.raises(ValidationError):
        FCPPlaybackResult(
            action="fcp_playback",
            request=FCPPlaybackRequest(playback_action="play", key="k"),
            raw_response="ok",
            verification_status="unverified",
            observed_outcome=None,
            warnings=["not observed"],
        )
    with pytest.raises(ValidationError):
        FCPMenuCommandResult(
            action="fcp_menu_command",
            request=FCPMenuCommandRequest(
                menu_path="File > Export XML...",
                components=["Edit", "Undo"],
            ),
            raw_response="ok",
            verification_status="unverified",
            observed_outcome=None,
            warnings=["not observed"],
        )


def test_live_models_are_strict_frozen_and_forbid_extra_fields():
    result = FCPPlaybackResult(
        action="fcp_playback",
        request=FCPPlaybackRequest(playback_action="play", key="l"),
        raw_response="Playback: play",
        verification_status=VerificationStatus.UNVERIFIED,
        observed_outcome=None,
        warnings=["not observed"],
    )
    with pytest.raises(ValidationError):
        FCPPlaybackRequest.model_validate(
            {"playback_action": "play", "key": "l", "extra": True}
        )
    with pytest.raises(ValidationError):
        FCPPlaybackRequest.model_validate(
            {"playback_action": "play", "key": 1}
        )
    with pytest.raises(ValidationError):
        result.raw_response = "changed"


@pytest.mark.asyncio
async def test_registered_mcp_boundary_preserves_text_and_structured_schema(
    monkeypatch,
):
    raw = (
        '{"name":"Final Cut Pro","version":"11.1","frontmost":true,'
        '"libraryCount":2}'
    )
    monkeypatch.setattr(automation, "run_osascript", lambda *_args, **_kwargs: raw)

    result = await server.mcp.call_tool("fcp_get_app_state", {})

    assert result.content[0].text == raw
    assert result.structuredContent == {
        "schema_version": "1",
        "name": "Final Cut Pro",
        "version": "11.1",
        "frontmost": True,
        "library_count": 2,
    }


@pytest.mark.asyncio
async def test_registered_output_schema_titles_match_all_19_models():
    tools = {tool.name: tool for tool in await server.mcp.list_tools()}

    assert {
        name: tools[name].outputSchema["title"]
        for name in EXPECTED_LIVE_RESULT_MODELS
    } == EXPECTED_LIVE_RESULT_MODELS
