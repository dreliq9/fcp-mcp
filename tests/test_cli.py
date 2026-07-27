import json

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
