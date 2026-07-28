import subprocess
from pathlib import Path

import pytest

from fcp_mcp import diagnostics
from fcp_mcp.config import RuntimeConfig
from fcp_mcp.contracts import DoctorCheck, ErrorCode, FCPMCPError
from fcp_mcp.diagnostics import collect_doctor


@pytest.mark.asyncio
async def test_doctor_has_stable_schema_and_catalog(tmp_path: Path):
    config = RuntimeConfig.from_env(
        {"FCP_MCP_OUTPUT_DIR": str(tmp_path)},
        home=tmp_path,
    )
    tool_names = {"second", "first"} | {f"tool-{i}" for i in range(87)}
    prompt_names = {f"prompt-{i}" for i in range(5)}

    async def catalog():
        return tool_names, prompt_names

    report = await collect_doctor(
        config,
        catalog_provider=catalog,
        expected_tool_names=tool_names,
        expected_prompt_names=prompt_names,
    )
    assert report.schema_version == "1"
    assert report.package_version == "0.3.0"
    assert report.server_name == "fcp-mcp"
    assert report.tool_count == 89
    assert report.prompt_count == 5
    assert report.status in {"ready", "degraded"}
    assert {check.id for check in report.checks} >= {
        "configuration",
        "output_writable",
        "mcp_catalog",
        "ffmpeg",
        "ffprobe",
        "final_cut_pro",
        "live_control",
        "compressor",
    }
    assert list(tmp_path.glob(".fcp-mcp-doctor-*")) == []
    catalog_check = next(check for check in report.checks if check.id == "mcp_catalog")
    assert catalog_check.details["profile"] == "workflow"
    assert catalog_check.details["tool_names"] == sorted(
        ["first", "second", *[f"tool-{i}" for i in range(87)]]
    )
    assert catalog_check.summary == "MCP catalog contains 89 tools and 5 prompts"


@pytest.mark.asyncio
async def test_missing_output_directory_blocks_doctor(tmp_path: Path):
    missing = tmp_path / "missing"
    config = RuntimeConfig.from_env(
        {"FCP_MCP_OUTPUT_DIR": str(missing)},
        home=tmp_path,
    )
    tool_names = {f"tool-{i}" for i in range(89)}
    prompt_names = {f"prompt-{i}" for i in range(5)}

    async def catalog():
        return tool_names, prompt_names

    report = await collect_doctor(
        config,
        catalog_provider=catalog,
        expected_tool_names=tool_names,
        expected_prompt_names=prompt_names,
    )
    output_check = next(check for check in report.checks if check.id == "output_writable")
    assert report.status == "blocked"
    assert output_check.status == "fail"
    assert missing.exists() is False


@pytest.mark.asyncio
async def test_catalog_mismatch_blocks_doctor(tmp_path: Path):
    config = RuntimeConfig.from_env(
        {"FCP_MCP_OUTPUT_DIR": str(tmp_path)},
        home=tmp_path,
    )
    prompt_names = {f"prompt-{i}" for i in range(5)}

    async def catalog():
        return {f"tool-{i}" for i in range(88)}, prompt_names

    report = await collect_doctor(
        config,
        catalog_provider=catalog,
        expected_tool_names={f"tool-{i}" for i in range(89)},
        expected_prompt_names=prompt_names,
    )
    catalog_check = next(check for check in report.checks if check.id == "mcp_catalog")
    assert report.status == "blocked"
    assert catalog_check.status == "fail"
    assert report.tool_count == 88


@pytest.mark.asyncio
async def test_same_sized_substituted_catalog_is_invalid(tmp_path: Path):
    config = RuntimeConfig.from_env(
        {"FCP_MCP_OUTPUT_DIR": str(tmp_path)},
        home=tmp_path,
    )

    async def catalog():
        return {"expected-a", "unexpected"}, {"prompt-a"}

    report = await collect_doctor(
        config,
        catalog_provider=catalog,
        expected_tool_names={"expected-a", "expected-b"},
        expected_prompt_names={"prompt-a"},
    )
    catalog_check = next(check for check in report.checks if check.id == "mcp_catalog")

    assert report.status == "blocked"
    assert report.tool_count == 2
    assert catalog_check.status == "fail"
    assert catalog_check.details["tool_names"] == ["expected-a", "unexpected"]
    assert catalog_check.details["expected_tool_names"] == [
        "expected-a",
        "expected-b",
    ]
    assert catalog_check.details["missing_tool_names"] == ["expected-b"]
    assert catalog_check.details["unexpected_tool_names"] == ["unexpected"]


def test_output_probe_failure_is_reported_and_cleaned_up(
    tmp_path: Path,
    monkeypatch,
):
    config = RuntimeConfig.from_env(
        {"FCP_MCP_OUTPUT_DIR": str(tmp_path)},
        home=tmp_path,
    )

    def fail_fsync(_file_descriptor):
        raise OSError("sync denied")

    monkeypatch.setattr(diagnostics.os, "fsync", fail_fsync)
    check = diagnostics._output_check(config)

    assert check.status == "fail"
    assert check.details["error"] == "sync denied"
    assert list(tmp_path.glob(".fcp-mcp-doctor-*")) == []


def test_ledger_check_initializes_missing_database_in_existing_private_state(
    tmp_path: Path,
):
    state_dir = tmp_path / "state"
    config = RuntimeConfig.from_env(
        {
            "FCP_MCP_OUTPUT_DIR": str(tmp_path),
            "FCP_MCP_STATE_DIR": str(state_dir),
        },
        home=tmp_path,
    )
    state_dir.mkdir(mode=0o700)

    check = diagnostics.collect_ledger_check(config)

    assert check.id == "workflow_ledger"
    assert check.status == "pass"
    assert check.details == {
        "approval_mode": "client",
        "state_dir": str(state_dir),
        "initialized_missing_ledger": True,
        "integrity_valid": True,
        "checked_migrations": 1,
        "checked_runs": 0,
        "checked_events": 0,
        "incomplete_count": 0,
        "recovery_required_count": 0,
        "counts_truncated": False,
    }
    assert state_dir.is_dir()


def test_process_probe_timeout_is_not_running(monkeypatch):
    def time_out(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("pgrep", 2)

    monkeypatch.setattr(diagnostics.subprocess, "run", time_out)
    assert diagnostics._process_running("Final Cut Pro") is False


def test_missing_final_cut_pro_is_a_warning(monkeypatch):
    monkeypatch.setattr(Path, "is_dir", lambda _path: False)
    check, installed, running = diagnostics._fcp_check()

    assert (installed, running) == (False, False)
    assert check.status == "warn"
    assert check.id == "final_cut_pro"


@pytest.mark.parametrize(
    ("trusted", "expected_status"),
    [
        (True, "pass"),
        (False, "warn"),
        (None, "warn"),
    ],
)
def test_enabled_live_control_reports_accessibility_state(
    tmp_path: Path,
    monkeypatch,
    trusted,
    expected_status,
):
    config = RuntimeConfig.from_env(
        {
            "FCP_MCP_OUTPUT_DIR": str(tmp_path),
            "FCP_MCP_ENABLE_LIVE_CONTROL": "1",
        },
        home=tmp_path,
    )
    monkeypatch.setattr(diagnostics, "_accessibility_trusted", lambda: trusted)

    checks = diagnostics._live_checks(
        config,
        fcp_installed=True,
        fcp_running=True,
    )

    assert checks[0].status == "pass"
    assert checks[1].status == expected_status


def test_accessibility_probe_reports_unavailable_framework(monkeypatch):
    def unavailable(_framework):
        raise OSError("framework unavailable")

    monkeypatch.setattr(diagnostics.ctypes, "CDLL", unavailable)
    assert diagnostics._accessibility_trusted() is None


def test_unavailable_binary_is_a_warning(monkeypatch):
    def missing():
        raise FCPMCPError(
            ErrorCode.DEPENDENCY_MISSING,
            "ffmpeg was not found",
        )

    monkeypatch.setattr(diagnostics, "_find_ffmpeg", missing)
    check = diagnostics._binary_check("ffmpeg")

    assert check.status == "warn"
    assert "dependency_missing" in check.details["error"]


def test_installed_compressor_is_reported(tmp_path: Path, monkeypatch):
    executable = tmp_path / "Compressor"
    executable.touch()
    monkeypatch.setattr(diagnostics, "compressor_binary", lambda: executable)

    check = diagnostics._compressor_check()

    assert check.status == "pass"
    assert check.details["executable"] == str(executable)


def test_all_non_warning_checks_are_ready():
    checks = [
        DoctorCheck(id="configuration", status="pass", summary="ok"),
        DoctorCheck(id="optional", status="skip", summary="not requested"),
    ]
    assert diagnostics._status(checks) == "ready"


@pytest.mark.asyncio
async def test_catalog_provider_failure_blocks_doctor(tmp_path: Path):
    config = RuntimeConfig.from_env(
        {"FCP_MCP_OUTPUT_DIR": str(tmp_path)},
        home=tmp_path,
    )

    async def broken_catalog():
        raise RuntimeError("catalog exploded")

    report = await collect_doctor(
        config,
        catalog_provider=broken_catalog,
        expected_tool_names={f"tool-{i}" for i in range(89)},
        expected_prompt_names={f"prompt-{i}" for i in range(5)},
    )
    catalog_check = next(check for check in report.checks if check.id == "mcp_catalog")

    assert report.status == "blocked"
    assert catalog_check.status == "fail"
    assert catalog_check.details["error"] == "catalog exploded"
    assert catalog_check.details["profile"] == "workflow"
    assert catalog_check.details["tool_names"] == []
    assert catalog_check.details["expected_tool_names"] == sorted(
        f"tool-{index}" for index in range(89)
    )
