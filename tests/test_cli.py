import json
import os
import subprocess
import sys

import pytest

from fcp_mcp import cli
from fcp_mcp.cli import main


def test_version_prints_package_version(capsys):
    assert main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == "fcp-mcp 0.2.1"


def test_doctor_json_is_machine_readable(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("FCP_MCP_OUTPUT_DIR", str(tmp_path))
    code = main(["doctor", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code in {0, 1, 2}
    assert payload["schema_version"] == "1"
    assert payload["package_version"] == "0.2.1"
    assert payload["tool_count"] == 89
    assert payload["prompt_count"] == 5


def test_doctor_text_lists_status_and_checks(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("FCP_MCP_OUTPUT_DIR", str(tmp_path))
    assert main(["doctor"]) in {0, 1, 2}
    output = capsys.readouterr().out
    assert "fcp-mcp doctor:" in output
    assert "mcp_catalog" in output


def test_doctor_uses_profile_registry_as_catalog_expectation(tmp_path):
    environment = os.environ.copy()
    environment.update(
        {
            "FCP_MCP_OUTPUT_DIR": str(tmp_path),
            "FCP_MCP_PROFILE": "inspect",
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
    monkeypatch.setattr("fcp_mcp.server.main", lambda: called.append(True))

    cli.serve()

    assert called == [True]


def test_invalid_option_uses_argparse_error(capsys):
    with pytest.raises(SystemExit, match="2"):
        main(["--version=false"])
    assert "ignored explicit argument" in capsys.readouterr().err
