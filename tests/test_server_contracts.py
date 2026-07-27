from pathlib import Path

import pytest

import fcp_mcp.automation.osascript as automation
from fcp_mcp import server
from fcp_mcp.config import RuntimeConfig
from fcp_mcp.contracts import FCPMCPError

HOSTILE = 'x" & do shell script "touch /tmp/pwned" & "'


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
