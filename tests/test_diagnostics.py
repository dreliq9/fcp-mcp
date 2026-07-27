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

    async def catalog():
        return 89, 5

    report = await collect_doctor(config, catalog_provider=catalog)
    assert report.schema_version == "1"
    assert report.package_version == "0.2.1"
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


@pytest.mark.asyncio
async def test_missing_output_directory_blocks_doctor(tmp_path: Path):
    missing = tmp_path / "missing"
    config = RuntimeConfig.from_env(
        {"FCP_MCP_OUTPUT_DIR": str(missing)},
        home=tmp_path,
    )

    async def catalog():
        return 89, 5

    report = await collect_doctor(config, catalog_provider=catalog)
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

    async def catalog():
        return 88, 5

    report = await collect_doctor(config, catalog_provider=catalog)
    catalog_check = next(check for check in report.checks if check.id == "mcp_catalog")
    assert report.status == "blocked"
    assert catalog_check.status == "fail"
    assert report.tool_count == 88


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


def test_accessibility_probe_is_skipped_off_macos(monkeypatch):
    monkeypatch.setattr(diagnostics.platform, "system", lambda: "Linux")
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

    report = await collect_doctor(config, catalog_provider=broken_catalog)
    catalog_check = next(check for check in report.checks if check.id == "mcp_catalog")

    assert report.status == "blocked"
    assert catalog_check.status == "fail"
    assert catalog_check.details["error"] == "catalog exploded"
