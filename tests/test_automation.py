from pathlib import Path
from subprocess import CompletedProcess, TimeoutExpired

import pytest

from fcp_mcp.automation.osascript import OsaProgram, parse_shortcut, run_osascript
from fcp_mcp.config import RuntimeConfig
from fcp_mcp.contracts import FCPMCPError

HOSTILE = 'x" & do shell script "touch /tmp/pwned" & "'


def config(tmp_path: Path, enabled: bool) -> RuntimeConfig:
    return RuntimeConfig.from_env(
        {
            "FCP_MCP_OUTPUT_DIR": str(tmp_path),
            "FCP_MCP_ENABLE_LIVE_CONTROL": "1" if enabled else "0",
        },
        home=tmp_path,
    )


def test_dynamic_value_is_argv_not_script_source(tmp_path: Path):
    captured = {}

    def runner(command, **kwargs):
        captured["command"] = command
        return CompletedProcess(command, 0, "ok\n", "")

    program = OsaProgram(
        language="AppleScript",
        source="on run argv\nreturn item 1 of argv\nend run",
    )
    assert run_osascript(
        program,
        [HOSTILE],
        config=config(tmp_path, True),
        runner=runner,
    ) == "ok"
    source_index = captured["command"].index("-e") + 1
    assert HOSTILE not in captured["command"][source_index]
    assert captured["command"][-1] == HOSTILE


def test_live_control_disabled_runs_nothing(tmp_path: Path):
    called = False

    def runner(command, **kwargs):
        nonlocal called
        called = True
        return CompletedProcess(command, 0, "", "")

    with pytest.raises(FCPMCPError, match="live_control_disabled"):
        run_osascript(
            OsaProgram("AppleScript", "return true"),
            config=config(tmp_path, False),
            runner=runner,
        )
    assert called is False


def test_nonzero_osascript_is_command_failure(tmp_path: Path):
    def runner(command, **kwargs):
        return CompletedProcess(command, 1, "", "Execution failed")

    with pytest.raises(FCPMCPError, match="command_failed"):
        run_osascript(
            OsaProgram("AppleScript", "return true"),
            config=config(tmp_path, True),
            runner=runner,
        )


def test_accessibility_denial_is_permission_failure(tmp_path: Path):
    def runner(command, **kwargs):
        return CompletedProcess(command, 1, "", "Not authorized to send Apple events")

    with pytest.raises(FCPMCPError, match="permission_denied"):
        run_osascript(
            OsaProgram("AppleScript", "return true"),
            config=config(tmp_path, True),
            runner=runner,
        )


def test_timeout_is_command_failure(tmp_path: Path):
    def runner(command, **kwargs):
        raise TimeoutExpired(command, kwargs["timeout"])

    with pytest.raises(FCPMCPError, match="command_failed"):
        run_osascript(
            OsaProgram("JavaScript", "function run(argv) { return argv[0]; }"),
            ["value"],
            timeout=0.01,
            config=config(tmp_path, True),
            runner=runner,
        )


def test_missing_osascript_is_dependency_failure(tmp_path: Path):
    def runner(command, **kwargs):
        raise FileNotFoundError("osascript")

    with pytest.raises(FCPMCPError, match="dependency_missing"):
        run_osascript(
            OsaProgram("AppleScript", "return true"),
            config=config(tmp_path, True),
            runner=runner,
        )


def test_launch_permission_error_is_permission_failure(tmp_path: Path):
    def runner(command, **kwargs):
        raise PermissionError("denied")

    with pytest.raises(FCPMCPError, match="permission_denied"):
        run_osascript(
            OsaProgram("AppleScript", "return true"),
            config=config(tmp_path, True),
            runner=runner,
        )


def test_other_launch_error_is_command_failure(tmp_path: Path):
    def runner(command, **kwargs):
        raise OSError("launch failed")

    with pytest.raises(FCPMCPError, match="command_failed"):
        run_osascript(
            OsaProgram("AppleScript", "return true"),
            config=config(tmp_path, True),
            runner=runner,
        )


def test_unsupported_osa_language_is_rejected():
    with pytest.raises(FCPMCPError, match="invalid_arguments"):
        OsaProgram("Ruby", "return true")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("cmd+shift+e", ("e", ("command", "shift"))),
        ("control+left", ("left", ("control",))),
        ("alt+space", ("space", ("option",))),
    ],
)
def test_shortcut_parser_returns_canonical_tokens(raw, expected):
    assert parse_shortcut(raw) == expected


@pytest.mark.parametrize("raw", ["", "cmd", "cmd+shift+e+x", "cmd+'", "bogus+e"])
def test_invalid_shortcuts_are_rejected(raw):
    with pytest.raises(FCPMCPError, match="invalid_arguments"):
        parse_shortcut(raw)
