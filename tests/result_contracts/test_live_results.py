import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import ValidationError

import fcp_mcp.automation.osascript as automation
import fcp_mcp.result_models.live as live_models
import fcp_mcp.utils.paths as utility_paths
from fcp_mcp import server
from fcp_mcp.config import RuntimeConfig
from fcp_mcp.contracts import FCPMCPError
from fcp_mcp.mcp_boundary import build_mcp_server
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
    "fcp_final_cut_12": "FCPFinalCut12Result",
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
        (server.fcp_final_cut_12, ("detect_edits",)),
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


def test_final_cut_12_browser_search_verifies_typed_criteria(
    monkeypatch,
    live_config,
):
    raw = json.dumps(
        {
            "query": "cloud outage",
            "scope": "transcript",
            "transcriptMatch": "is_related_to",
            "observedQuery": "cloud outage",
            "criteriaApplied": True,
            "resultsObserved": False,
        },
        separators=(",", ":"),
    )
    calls = []

    def fake(program, args=(), **kwargs):
        calls.append((program, list(args), kwargs))
        return raw

    monkeypatch.setattr(automation, "run_osascript", fake)

    outcome = server.fcp_final_cut_12(
        "set_browser_search",
        "cloud outage",
        "transcript",
        "is_related_to",
    )

    assert str(outcome) == raw
    assert calls[0][0] is automation.FCP_BROWSER_SEARCH
    assert calls[0][1] == [
        "cloud outage",
        "transcript",
        "is_related_to",
    ]
    assert outcome.structured.request.model_dump() == {
        "feature_action": "set_browser_search",
        "query": "cloud outage",
        "search_scope": "transcript",
        "transcript_match": "is_related_to",
    }
    assert outcome.structured.completion == "criteria_applied"
    assert outcome.structured.observed_query == "cloud outage"
    assert outcome.structured.criteria_applied is True
    assert outcome.structured.results_observed is False
    assert outcome.structured.requires_user_interaction is False


@pytest.mark.parametrize(
    (
        "action",
        "expected_program",
        "expected_args",
        "completion",
        "interactive",
    ),
    [
        (
            "detect_edits",
            automation.FCP_KEYBOARD_SHORTCUT,
            ["shift+e", "e", "shift"],
            "command_sent",
            False,
        ),
        (
            "add_auto_mask",
            automation.FCP_KEYBOARD_SHORTCUT,
            ["control+cmd+k", "k", "control", "command"],
            "interactive_mode_started",
            True,
        ),
        (
            "duplicate_captions_to_subtitles",
            automation.FCP_MENU_COMMAND,
            [
                '["Edit","Closed Captions","Duplicate Captions to Subtitles"]',
                "Edit > Closed Captions > Duplicate Captions to Subtitles",
                "Edit",
                "Closed Captions",
                "Duplicate Captions to Subtitles",
            ],
            "command_sent",
            False,
        ),
        (
            "send_frame_to_pixelmator_pro",
            automation.FCP_SHARE,
            ["Send Frame to Pixelmator Pro"],
            "interactive_mode_started",
            True,
        ),
    ],
)
def test_final_cut_12_actions_map_to_stable_automation(
    monkeypatch,
    live_config,
    action,
    expected_program,
    expected_args,
    completion,
    interactive,
):
    calls = []

    def fake(program, args=(), **kwargs):
        calls.append((program, list(args), kwargs))
        return f"sent:{action}"

    monkeypatch.setattr(automation, "run_osascript", fake)

    outcome = server.fcp_final_cut_12(action)

    assert calls[0][0] is expected_program
    assert calls[0][1] == expected_args
    assert outcome.structured.request.feature_action == action
    assert outcome.structured.completion == completion
    assert outcome.structured.requires_user_interaction is interactive
    assert bool(outcome.structured.next_step) is interactive


@pytest.mark.parametrize("query", ["", "   ", "line\nbreak", "x" * 501])
def test_final_cut_12_rejects_invalid_browser_queries(query):
    with pytest.raises(FCPMCPError, match="invalid_arguments"):
        server.fcp_final_cut_12("set_browser_search", query)


def test_browser_search_program_never_interpolates_the_query(
    monkeypatch,
    live_config,
):
    hostile = 'x"; Application("Terminal").doScript("touch /tmp/pwned")'
    raw = json.dumps(
        {
            "query": hostile,
            "scope": "visual",
            "transcriptMatch": None,
            "observedQuery": hostile,
            "criteriaApplied": True,
            "resultsObserved": False,
        }
    )
    calls = []

    def fake(program, args=(), **kwargs):
        calls.append((program, list(args)))
        return raw

    monkeypatch.setattr(automation, "run_osascript", fake)

    server.fcp_final_cut_12("set_browser_search", hostile, "visual")

    assert hostile not in calls[0][0].source
    assert calls[0][1] == [hostile, "visual", "includes"]


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
    enabled_mcp = build_mcp_server(
        replace(server.CONFIG, live_control_enabled=True),
        server.TOOLS,
        server.PROMPTS,
        server.RESOURCES,
    )

    result = await enabled_mcp.call_tool("fcp_get_app_state", {})

    assert result.content[0].text == raw
    assert result.structured_content == {
        "schema_version": "1",
        "name": "Final Cut Pro",
        "version": "11.1",
        "frontmost": True,
        "library_count": 2,
    }


@pytest.mark.asyncio
async def test_registered_output_schema_titles_match_all_20_models():
    tools = {tool.name: tool for tool in await server.mcp.list_tools()}

    assert {
        name: tools[name].output_schema["title"]
        for name in EXPECTED_LIVE_RESULT_MODELS
    } == EXPECTED_LIVE_RESULT_MODELS


def test_final_cut_12_result_rejects_fabricated_completion_evidence():
    request = live_models.FCPFinalCut12Request(
        feature_action="set_browser_search",
        query="dialogue",
        search_scope="transcript",
        transcript_match="includes",
    )

    with pytest.raises(ValidationError):
        live_models.FCPFinalCut12Result(
            request=request,
            raw_response="{}",
            warnings=["No independent result observation was made."],
            completion="command_sent",
            requires_user_interaction=False,
            next_step=None,
            observed_query=None,
            criteria_applied=False,
        )


ACTION_MODEL_CASES = [
    (
        live_models.FCPOpenLibraryResult,
        live_models.FCPOpenLibraryRequest(library_path="/tmp/Library.fcpbundle"),
    ),
    (
        live_models.FCPImportXMLResult,
        live_models.FCPImportXMLRequest(fcpxml_path="/tmp/import.fcpxml"),
    ),
    (live_models.FCPExportXMLResult, live_models.EmptyActionRequest()),
    (
        live_models.FCPPlaybackResult,
        live_models.FCPPlaybackRequest(playback_action="play", key="l"),
    ),
    (
        live_models.FCPNavigateResult,
        live_models.FCPNavigateRequest(
            timecode="00:01:30:00",
            normalized_timecode="00013000",
        ),
    ),
    (
        live_models.FCPSelectToolResult,
        live_models.FCPSelectToolRequest(tool="blade", key="b"),
    ),
    (live_models.FCPUndoResult, live_models.EmptyActionRequest()),
    (live_models.FCPRedoResult, live_models.EmptyActionRequest()),
    (
        live_models.FCPMenuCommandResult,
        live_models.FCPMenuCommandRequest(
            menu_path="File > Export XML...",
            components=["File", "Export XML..."],
        ),
    ),
    (
        live_models.FCPKeyboardShortcutResult,
        live_models.FCPKeyboardShortcutRequest(
            keys="cmd+shift+e",
            key="e",
            modifiers=["command", "shift"],
        ),
    ),
    (
        live_models.FCPShareResult,
        live_models.FCPShareRequest(destination="Apple Devices 4K"),
    ),
]


@pytest.mark.parametrize(("result_model", "action_request"), ACTION_MODEL_CASES)
@pytest.mark.parametrize(
    ("status", "observed_outcome"),
    [
        (VerificationStatus.VERIFIED, "fabricated observation"),
        (VerificationStatus.FAILED, None),
    ],
)
def test_fcp_action_contracts_reject_non_unverified_statuses(
    result_model,
    action_request,
    status,
    observed_outcome,
):
    with pytest.raises(ValidationError):
        result_model(
            request=action_request,
            raw_response="command returned",
            verification_status=status,
            observed_outcome=observed_outcome,
            warnings=["No independent observation was made."],
        )


@pytest.mark.asyncio
async def test_fcp_action_output_schemas_admit_only_unverified_evidence():
    tools = {tool.name: tool for tool in await server.mcp.list_tools()}
    action_names = [
        name
        for name in EXPECTED_LIVE_RESULT_MODELS
        if name.startswith("fcp_")
        and name
        not in {
            "fcp_is_running",
            "fcp_get_libraries",
            "fcp_get_events",
            "fcp_get_projects",
            "fcp_get_timeline_info",
            "fcp_get_app_state",
        }
    ]

    for name in action_names:
        properties = tools[name].output_schema["properties"]
        assert properties["verification_status"] == {
            "const": "unverified",
            "default": "unverified",
            "title": "Verification Status",
            "type": "string",
        }
        assert properties["observed_outcome"]["type"] == "null"
        assert properties["warnings"]["minItems"] == 1
        serialized = json.dumps(tools[name].output_schema, sort_keys=True)
        assert '"verified"' not in serialized
        assert '"failed"' not in serialized


@pytest.mark.parametrize(
    ("keys", "key", "modifiers"),
    [
        ("cmd+shift+e", "x", ["command", "shift"]),
        ("cmd+shift+e", "e", ["shift", "command"]),
        ("alt+space", "space", ["alt"]),
        ("control+left", "left", []),
    ],
)
def test_keyboard_shortcut_request_rejects_cross_field_mismatches(
    keys,
    key,
    modifiers,
):
    with pytest.raises(ValidationError):
        live_models.FCPKeyboardShortcutRequest(
            keys=keys,
            key=key,
            modifiers=modifiers,
        )


@pytest.mark.parametrize(
    ("keys", "key", "modifiers"),
    [
        ("shift+cmd+E", "e", ["shift", "command"]),
        ("alt+space", "space", ["option"]),
        ("ctrl+left", "left", ["control"]),
    ],
)
def test_keyboard_shortcut_request_accepts_canonical_aliases_and_order(
    keys,
    key,
    modifiers,
):
    request = live_models.FCPKeyboardShortcutRequest(
        keys=keys,
        key=key,
        modifiers=modifiers,
    )

    assert request.key == key
    assert request.modifiers == modifiers


@pytest.mark.parametrize(
    "raw_constant",
    ["NaN", "Infinity", "-Infinity", "1e999"],
)
def test_timeline_handler_rejects_non_finite_json_numbers(
    monkeypatch,
    raw_constant,
):
    raw = (
        '{"library":"Library","event":"Event","project":"Project",'
        f'"duration":{{"value":{raw_constant},"timescale":30}},'
        '"frameDuration":{"value":1,"timescale":30},"tcFormat":"NDF"}'
    )
    monkeypatch.setattr(automation, "run_osascript", lambda *_args, **_kwargs: raw)

    with pytest.raises(FCPMCPError, match="command_failed"):
        server.fcp_get_timeline_info()


@pytest.mark.parametrize("timescale", [0, -1, 29.97])
def test_timeline_handler_rejects_nonpositive_or_fractional_timescale(
    monkeypatch,
    timescale,
):
    raw = (
        '{"library":"Library","event":"Event","project":"Project",'
        f'"duration":{{"value":0,"timescale":{timescale}}},'
        '"frameDuration":{"value":1,"timescale":30},"tcFormat":"NDF"}'
    )
    monkeypatch.setattr(automation, "run_osascript", lambda *_args, **_kwargs: raw)

    with pytest.raises(FCPMCPError, match="command_failed"):
        server.fcp_get_timeline_info()


@pytest.mark.parametrize(
    ("duration_value", "frame_value"),
    [(-1, 1), (0, 0)],
)
def test_timeline_handler_rejects_negative_duration_or_zero_frame_duration(
    monkeypatch,
    duration_value,
    frame_value,
):
    raw = (
        '{"library":"Library","event":"Event","project":"Project",'
        f'"duration":{{"value":{duration_value},"timescale":30}},'
        f'"frameDuration":{{"value":{frame_value},"timescale":30}},'
        '"tcFormat":"NDF"}'
    )
    monkeypatch.setattr(automation, "run_osascript", lambda *_args, **_kwargs: raw)

    with pytest.raises(FCPMCPError, match="command_failed"):
        server.fcp_get_timeline_info()


def test_timeline_numeric_boundary_values_are_valid(monkeypatch):
    raw = (
        '{"library":"Library","event":"Event","project":"Project",'
        '"duration":{"value":0,"timescale":1},'
        '"frameDuration":{"value":1,"timescale":1},"tcFormat":null}'
    )
    monkeypatch.setattr(automation, "run_osascript", lambda *_args, **_kwargs: raw)

    structured = server.fcp_get_timeline_info().structured

    assert structured.duration == live_models.FCPTimeObservation(
        value=0,
        timescale=1,
    )
    assert structured.frame_duration == live_models.FCPTimeObservation(
        value=1,
        timescale=1,
    )


@pytest.mark.parametrize("library_count", [-1, "NaN"])
def test_app_state_rejects_negative_or_nonstandard_library_count(
    monkeypatch,
    library_count,
):
    encoded_count = (
        library_count if isinstance(library_count, str) else str(library_count)
    )
    raw = (
        '{"name":"Final Cut Pro","version":"11.1","frontmost":true,'
        f'"libraryCount":{encoded_count}}}'
    )
    monkeypatch.setattr(automation, "run_osascript", lambda *_args, **_kwargs: raw)

    with pytest.raises(FCPMCPError, match="command_failed"):
        server.fcp_get_app_state()


def test_app_state_accepts_zero_library_count(monkeypatch):
    raw = (
        '{"name":"Final Cut Pro","version":"11.1","frontmost":true,'
        '"libraryCount":0}'
    )
    monkeypatch.setattr(automation, "run_osascript", lambda *_args, **_kwargs: raw)

    assert server.fcp_get_app_state().structured.library_count == 0


@pytest.mark.parametrize(
    ("handler_name", "arguments", "raw"),
    [
        (
            "fcp_get_libraries",
            (),
            '[{"kind":"library","name":"Library","id":"lib-1","file":"/tmp/L"}]',
        ),
        (
            "fcp_get_libraries",
            (),
            '[{"name":"Library","id":"lib-1"}]',
        ),
        (
            "fcp_get_events",
            ("Library",),
            (
                '[{"library":"Library","name":"Event","id":"event-1",'
                '"unexpected":true}]'
            ),
        ),
        (
            "fcp_get_projects",
            ("Event",),
            (
                '[{"library":"Library","event":"Event","name":"Project",'
                '"id":"project-1"}]'
            ),
        ),
    ],
)
def test_collection_handler_rejects_nonexact_raw_record_keys(
    monkeypatch,
    handler_name,
    arguments,
    raw,
):
    monkeypatch.setattr(automation, "run_osascript", lambda *_args, **_kwargs: raw)

    with pytest.raises(FCPMCPError, match="command_failed"):
        getattr(server, handler_name)(*arguments)


@pytest.mark.parametrize(
    ("handler_name", "filter_value", "raw"),
    [
        (
            "fcp_get_events",
            "Requested Library",
            '[{"library":"Other Library","name":"Event","id":"event-1"}]',
        ),
        (
            "fcp_get_projects",
            "Requested Event",
            (
                '[{"library":"Library","event":"Other Event","name":"Project",'
                '"id":"project-1","duration":"1s"}]'
            ),
        ),
    ],
)
def test_collection_handler_rejects_parent_filter_mismatch(
    monkeypatch,
    handler_name,
    filter_value,
    raw,
):
    monkeypatch.setattr(automation, "run_osascript", lambda *_args, **_kwargs: raw)

    with pytest.raises(FCPMCPError, match="command_failed"):
        getattr(server, handler_name)(filter_value)


def test_collection_model_rejects_parent_filter_mismatch():
    with pytest.raises(ValidationError):
        live_models.FCPCollectionResult(
            collection_type="events",
            parent_filter=live_models.CollectionParentFilter(
                field="library_name",
                value="Requested Library",
            ),
            names=["Event"],
            records=[
                live_models.EventRecord(
                    library="Other Library",
                    name="Event",
                    id="event-1",
                )
            ],
        )
    with pytest.raises(ValidationError):
        live_models.FCPCollectionResult(
            collection_type="projects",
            parent_filter=live_models.CollectionParentFilter(
                field="event_name",
                value="Requested Event",
            ),
            names=["Project"],
            records=[
                live_models.ProjectRecord(
                    library="Library",
                    event="Other Event",
                    name="Project",
                    id="project-1",
                    duration="1s",
                )
            ],
        )


@pytest.mark.parametrize(
    ("handler_name", "filter_value", "raw", "expected_filter"),
    [
        ("fcp_get_events", "", "[]", None),
        (
            "fcp_get_events",
            "Library",
            '[{"library":"Library","name":"Event","id":"event-1"}]',
            {"field": "library_name", "value": "Library"},
        ),
        ("fcp_get_projects", "Event", "[]", {"field": "event_name", "value": "Event"}),
        (
            "fcp_get_projects",
            "",
            (
                '[{"library":"Library","event":"Any Event","name":"Project",'
                '"id":"project-1","duration":"1s"}]'
            ),
            None,
        ),
    ],
)
def test_collection_handler_accepts_truthful_filtered_and_unfiltered_results(
    monkeypatch,
    handler_name,
    filter_value,
    raw,
    expected_filter,
):
    monkeypatch.setattr(automation, "run_osascript", lambda *_args, **_kwargs: raw)

    structured = getattr(server, handler_name)(filter_value).structured

    dumped_filter = (
        structured.parent_filter.model_dump()
        if structured.parent_filter is not None
        else None
    )
    assert dumped_filter == expected_filter


@pytest.mark.parametrize(
    "stdout",
    [
        "diagnostic\nJob ID: JOB-123\n",
        "Job ID: JOB-123\ntrailing diagnostic\n",
        "Job ID: JOB-123\nJob ID: JOB-456\n",
        "Job Identifier: JOB-123\nJob Identifier: JOB-123\n",
        "Job Identifier: JOB-123\n",
        "job id: JOB-123\n",
    ],
)
def test_compressor_handler_requires_one_entire_stdout_identifier_shape(
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

    assert server.compressor_encode(str(source)).structured.job_identifier is None


def _valid_submission_payload():
    request = live_models.CompressorSubmissionRequest(
        source_path="/tmp/source.mov",
        setting_path="/tmp/setting.cmprstng",
        output_directory="/tmp/output",
        batch_name="MCP Encode",
    )
    command = live_models.CompressorCommandResult(
        argv=[
            "/Applications/Compressor",
            "-batchName",
            "MCP Encode",
            "-jobpath",
            "/tmp/source.mov",
            "-settingpath",
            "/tmp/setting.cmprstng",
            "-locationpath",
            "/tmp/output",
        ],
        returncode=0,
        stdout="Job ID: JOB-123\n",
        stderr="",
    )
    return {
        "request": request,
        "command": command,
        "verification_status": VerificationStatus.UNVERIFIED,
        "observed_outcome": None,
        "job_identifier": "JOB-123",
        "warnings": ["Durable Compressor state was not observed."],
    }


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("job_identifier", "FABRICATED"),
        ("job_identifier", None),
        (
            "command",
            live_models.CompressorCommandResult(
                argv=["/Applications/Compressor", "--unknown", "value"],
                returncode=0,
                stdout="Job ID: JOB-123\n",
                stderr="",
            ),
        ),
        (
            "command",
            live_models.CompressorCommandResult(
                argv=[
                    "/Applications/Compressor",
                    "-batchName",
                    "MCP Encode",
                    "-batchName",
                    "Duplicate",
                    "-jobpath",
                    "/tmp/source.mov",
                ],
                returncode=0,
                stdout="Job ID: JOB-123\n",
                stderr="",
            ),
        ),
        (
            "command",
            live_models.CompressorCommandResult(
                argv=[
                    "/Applications/Compressor",
                    "-batchName",
                    "Wrong Batch",
                    "-jobpath",
                    "/tmp/source.mov",
                    "-settingpath",
                    "/tmp/setting.cmprstng",
                    "-locationpath",
                    "/tmp/output",
                ],
                returncode=0,
                stdout="Job ID: JOB-123\n",
                stderr="",
            ),
        ),
        (
            "command",
            live_models.CompressorCommandResult(
                argv=[
                    "/Applications/Compressor",
                    "-batchName",
                    "MCP Encode",
                    "-jobpath",
                    "/tmp/source.mov",
                    "-settingpath",
                    "/tmp/setting.cmprstng",
                    "-locationpath",
                    "/tmp/output",
                ],
                returncode=1,
                stdout="Job ID: JOB-123\n",
                stderr="failed",
            ),
        ),
        (
            "command",
            live_models.CompressorCommandResult(
                argv=[
                    "/Applications/Compressor",
                    "-batchName",
                    "MCP Encode",
                    "-jobpath",
                    "/tmp/source.mov",
                    "-settingpath",
                    "/tmp/setting.cmprstng",
                    "-locationpath",
                    "/tmp/output",
                ],
                returncode=0,
                stdout="Job ID: JOB-123\nJob ID: JOB-456\n",
                stderr="",
            ),
        ),
    ],
)
def test_compressor_submission_model_rejects_unbound_evidence(
    field,
    replacement,
):
    payload = _valid_submission_payload()
    payload[field] = replacement

    with pytest.raises(ValidationError):
        live_models.CompressorSubmissionResult(**payload)


def test_compressor_setting_symlink_preserves_legacy_text_spelling(
    monkeypatch,
    tmp_path: Path,
):
    settings = tmp_path / "Settings"
    settings.mkdir()
    target = tmp_path / "Resolved.cmprstng"
    target.write_text("preset", encoding="utf-8")
    link = settings / "Alias.cmprstng"
    link.symlink_to(target)
    monkeypatch.setattr(utility_paths, "compressor_settings_dir", lambda: settings)
    monkeypatch.setattr(
        utility_paths,
        "compressor_binary",
        lambda: tmp_path / "Missing Compressor",
    )

    outcome = server.compressor_list_settings()

    assert json.loads(str(outcome))["custom_presets"] == [str(link)]
    assert outcome.structured.custom_settings[0].path == str(target.resolve())
