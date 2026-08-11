import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from fcp_mcp import cli
from fcp_mcp.cli import main
from fcp_mcp.config import RuntimeConfig
from fcp_mcp.contracts import ErrorCode, FCPMCPError
from fcp_mcp.workflow.models import AddMarkerOperation, WorkflowPrepareRequestV1
from fcp_mcp.workflow.surface import WorkflowRuntime

SOURCE_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<fcpxml version="1.11"><resources><format id="r1" frameDuration="1/30s"/>
<asset id="r2" name="Clip" duration="3s"/></resources><event name="Event">
<project name="Project"><sequence format="r1" duration="3s"><spine>
<asset-clip ref="r2" name="Clip" offset="0s" start="0s" duration="3s"/>
</spine></sequence></project></event></fcpxml>"""


def test_version_prints_package_version(capsys):
    assert main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == "fcp-mcp 0.3.0"


@pytest.mark.parametrize(
    "arguments",
    [[], ["serve"], ["doctor"], ["doctor", "--json"]],
)
def test_non_macos_operations_fail_before_config_or_server(
    arguments,
    monkeypatch,
    capsys,
):
    def forbidden(*args, **kwargs):
        raise AssertionError("unsupported startup crossed a side-effect boundary")

    monkeypatch.setattr("fcp_mcp.platform_support.sys.platform", "linux")
    monkeypatch.setattr("fcp_mcp.cli.serve", forbidden)
    monkeypatch.setattr("fcp_mcp.cli.RuntimeConfig.from_env", forbidden)

    assert main(arguments) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "unsupported_platform: fcp-mcp requires macOS\n"


def test_non_macos_help_and_version_remain_available(monkeypatch, capsys):
    monkeypatch.setattr("fcp_mcp.platform_support.sys.platform", "linux")

    assert main(["--version"]) == 0
    with pytest.raises(SystemExit) as help_exit:
        main(["--help"])
    assert help_exit.value.code == 0


def test_doctor_json_is_machine_readable(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("FCP_MCP_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setenv("FCP_MCP_STATE_DIR", str(tmp_path / "state"))
    code = main(["doctor", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code in {0, 1, 2}
    assert payload["schema_version"] == "1"
    assert payload["package_version"] == "0.3.0"
    assert payload["tool_count"] == 95
    assert payload["prompt_count"] == 5
    ledger = next(
        check for check in payload["checks"] if check["id"] == "workflow_ledger"
    )
    assert ledger["status"] == "pass"
    assert ledger["details"]["approval_mode"] == "client"
    assert ledger["details"]["state_dir"] == str(tmp_path / "state")


def test_doctor_text_lists_status_and_checks(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("FCP_MCP_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setenv("FCP_MCP_STATE_DIR", str(tmp_path / "state"))
    assert main(["doctor"]) in {0, 1, 2}
    output = capsys.readouterr().out
    assert "fcp-mcp doctor:" in output
    assert "mcp_catalog" in output
    assert "workflow_ledger" in output


def test_doctor_uses_profile_registry_as_catalog_expectation(tmp_path):
    environment = os.environ.copy()
    environment.update(
        {
            "FCP_MCP_OUTPUT_DIR": str(tmp_path),
            "FCP_MCP_PROFILE": "inspect",
            "FCP_MCP_STATE_DIR": str(tmp_path / "state"),
        }
    )
    completed = subprocess.run(
        [sys.executable, "-m", "fcp_mcp", "doctor", "--json"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    payload = json.loads(completed.stdout)
    catalog = next(
        check for check in payload["checks"] if check["id"] == "mcp_catalog"
    )
    expected_tools = {
        "fcp_doctor",
        "fcp_discover_effects",
        "fcp_list_motion_templates",
        "fcp_list_share_destinations",
        "fcpxml_analyze_pacing",
        "fcpxml_check_audio_levels",
        "fcpxml_check_duration",
        "fcpxml_check_frame_rates",
        "fcpxml_check_media_links",
        "fcpxml_check_safe_zones",
        "fcpxml_detect_duplicates",
        "fcpxml_detect_flash_frames",
        "fcpxml_detect_gaps",
        "fcpxml_diff",
        "fcpxml_list_clips",
        "fcpxml_list_effects",
        "fcpxml_list_markers",
        "fcpxml_list_roles",
        "fcpxml_list_templates",
        "fcpxml_parse",
        "fcpxml_qc_report",
        "fcpxml_timeline_stats",
        "fcpxml_validate",
        "media_detect_beats",
        "media_detect_silence",
        "media_info",
        "media_list_streams",
        "media_loudness",
        "media_scene_detect",
        "puppet_list_presets",
    }
    assert payload["tool_count"] == 30
    assert payload["prompt_count"] == 2
    assert catalog["status"] == "pass"
    assert catalog["details"]["profile"] == "inspect"
    assert catalog["details"]["tool_names"] == sorted(expected_tools)


def test_doctor_rejects_same_sized_catalog_substitution(
    tmp_path,
    monkeypatch,
):
    from fcp_mcp import server

    monkeypatch.setenv("FCP_MCP_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setenv("FCP_MCP_STATE_DIR", str(tmp_path / "state"))
    registered = asyncio.run(server.mcp.list_tools())
    observed_names = [tool.name for tool in registered]
    removed = observed_names[0]
    observed_names[0] = "unexpected-substitute"

    async def substituted_tools():
        return [SimpleNamespace(name=name) for name in observed_names]

    monkeypatch.setattr(server.mcp, "list_tools", substituted_tools)
    report = asyncio.run(cli._doctor())
    catalog = next(check for check in report.checks if check.id == "mcp_catalog")

    assert report.tool_count == len(registered)
    assert catalog.status == "fail"
    assert catalog.details["tool_names"] == sorted(observed_names)
    assert catalog.details["missing_tool_names"] == [removed]
    assert catalog.details["unexpected_tool_names"] == [
        "unexpected-substitute"
    ]


def test_no_arguments_starts_stdio(monkeypatch):
    called = []
    monkeypatch.setattr("fcp_mcp.cli.serve", lambda: called.append(True))
    assert main([]) == 0
    assert called == [True]


def test_serve_command_starts_stdio(monkeypatch):
    called = []
    monkeypatch.setattr("fcp_mcp.cli.serve", lambda: called.append(True))
    assert main(["serve"]) == 0
    assert called == [True]


def test_serve_profile_override_does_not_mutate_environment(monkeypatch):
    selected = []
    monkeypatch.setenv("FCP_MCP_PROFILE", "inspect")
    monkeypatch.setattr(
        "fcp_mcp.cli.serve",
        lambda profile_override=None: selected.append(profile_override),
    )

    assert main(["serve", "--profile", "edit"]) == 0

    assert selected == ["edit"]
    assert os.environ["FCP_MCP_PROFILE"] == "inspect"


def test_reconcile_command_uses_real_single_run_recovery_path(
    tmp_path: Path,
    monkeypatch,
    capsys,
):
    state_dir = tmp_path / "state"
    environment = {
        "FCP_MCP_OUTPUT_DIR": str(tmp_path),
        "FCP_MCP_ALLOWED_ROOTS": str(tmp_path),
        "FCP_MCP_STATE_DIR": str(state_dir),
        "FCP_MCP_PROFILE": "workflow",
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    config = RuntimeConfig.from_env(environment, home=tmp_path)
    runtime = WorkflowRuntime(config)
    source = tmp_path / "source.fcpxml"
    source.write_bytes(SOURCE_XML)
    preview = runtime.engine.prepare(
        WorkflowPrepareRequestV1(
            source_path=str(source),
            destination_path=str(tmp_path / "destination.fcpxml"),
            operations=(
                AddMarkerOperation(
                    kind="add_marker",
                    clip_name="Clip",
                    start="1/30s",
                    value="Chapter",
                ),
            ),
        )
    )

    assert main(["workflow", "reconcile", preview.run_id, "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["run_id"] == preview.run_id
    assert payload["state"] == "awaiting_approval"
    assert payload["recovery"] is None


def test_invalid_reconcile_id_fails_before_ledger_initialization(
    tmp_path: Path,
    monkeypatch,
    capsys,
):
    state_dir = tmp_path / "state"
    monkeypatch.setenv("FCP_MCP_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setenv("FCP_MCP_STATE_DIR", str(state_dir))

    assert main(["workflow", "reconcile", "NOT-A-RUN-ID"]) == 2

    assert "invalid_arguments" in capsys.readouterr().err
    assert not state_dir.exists()


@pytest.mark.parametrize(
    ("code", "expected_exit"),
    [
        (ErrorCode.INVALID_ARGUMENTS, 2),
        (ErrorCode.WORKFLOW_STATE_CONFLICT, 2),
        (ErrorCode.RECOVERY_REQUIRED, 1),
        (ErrorCode.ARTIFACT_CORRUPT, 3),
        (ErrorCode.LEDGER_UNAVAILABLE, 3),
    ],
)
def test_reconcile_errors_have_stable_exit_classes(
    code: ErrorCode,
    expected_exit: int,
):
    assert cli._workflow_error_exit(FCPMCPError(code, "bounded")) == expected_exit


def test_invalid_configuration_returns_blocked_doctor_json(
    capsys,
    monkeypatch,
):
    monkeypatch.setenv("FCP_MCP_LOG_FORMAT", "xml")

    assert main(["doctor", "--json"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "blocked"
    assert payload["checks"][0]["id"] == "configuration"
    assert payload["checks"][0]["status"] == "fail"


def test_serve_delegates_to_server_main(monkeypatch):
    called = []
    monkeypatch.setattr(
        "fcp_mcp.server.main",
        lambda profile_override=None: called.append(profile_override),
    )

    cli.serve("edit")

    assert called == ["edit"]


def test_server_main_builds_profile_override_without_mutating_environment(
    monkeypatch,
):
    from fcp_mcp import server
    from fcp_mcp.mcp_boundary import FCPFastMCP

    started = []
    monkeypatch.setenv("FCP_MCP_PROFILE", "inspect")
    monkeypatch.setattr(FCPFastMCP, "run", lambda self: started.append(self))

    server.main("edit")

    assert os.environ["FCP_MCP_PROFILE"] == "inspect"
    assert len(started) == 1
    assert "profile=edit" in started[0].instructions
    assert len(asyncio.run(started[0].list_tools())) == 75


def test_invalid_option_uses_argparse_error(capsys):
    with pytest.raises(SystemExit, match="2"):
        main(["--version=false"])
    assert "ignored explicit argument" in capsys.readouterr().err
