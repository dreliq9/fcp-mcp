from __future__ import annotations

import ctypes
import os
import subprocess
import tempfile
from collections.abc import Awaitable, Callable, Collection
from pathlib import Path

from fcp_mcp.config import RuntimeConfig
from fcp_mcp.contracts import DoctorCheck, DoctorReport, FCPMCPError
from fcp_mcp.media.ffprobe import _find_ffmpeg, _find_ffprobe, _run_checked
from fcp_mcp.utils.paths import compressor_binary
from fcp_mcp.version import distribution_version, package_version

CatalogProvider = Callable[
    [],
    Awaitable[tuple[Collection[str], Collection[str]]],
]


def _configuration_check(config: RuntimeConfig) -> DoctorCheck:
    return DoctorCheck(
        id="configuration",
        status="pass",
        summary="Runtime configuration parsed",
        details={
            "output_dir": str(config.output_dir),
            "allowed_roots": [str(root) for root in config.allowed_roots],
            "live_control_enabled": config.live_control_enabled,
            "log_format": config.log_format,
        },
    )


def _output_check(config: RuntimeConfig) -> DoctorCheck:
    directory = config.output_dir
    if not directory.is_dir():
        return DoctorCheck(
            id="output_writable",
            status="fail",
            summary=f"Output directory does not exist: {directory}",
            remediation="Create the directory or set FCP_MCP_OUTPUT_DIR to an existing directory",
        )

    probe_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=".fcp-mcp-doctor-",
            dir=directory,
            delete=False,
        ) as probe:
            probe_path = Path(probe.name)
            probe.write(b"fcp-mcp doctor\n")
            probe.flush()
            os.fsync(probe.fileno())
        probe_path.unlink()
        probe_path = None
    except OSError as error:
        return DoctorCheck(
            id="output_writable",
            status="fail",
            summary=f"Output directory is not writable: {directory}",
            details={"error": str(error)},
            remediation="Grant write access or select another FCP_MCP_OUTPUT_DIR",
        )
    finally:
        if probe_path is not None:
            try:
                probe_path.unlink(missing_ok=True)
            except OSError:
                pass

    return DoctorCheck(
        id="output_writable",
        status="pass",
        summary="Output directory passed a private write/delete probe",
        details={"output_dir": str(directory)},
    )


def _process_running(name: str) -> bool:
    try:
        result = subprocess.run(
            ["pgrep", "-x", name],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (FileNotFoundError, PermissionError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _fcp_check() -> tuple[DoctorCheck, bool, bool]:
    application = Path("/Applications/Final Cut Pro.app")
    installed = application.is_dir()
    running = _process_running("Final Cut Pro") if installed else False
    if not installed:
        return (
            DoctorCheck(
                id="final_cut_pro",
                status="warn",
                summary="Final Cut Pro is not installed in /Applications",
                details={"path": str(application), "running": False},
                remediation="Install Final Cut Pro to use live-control tools",
            ),
            installed,
            running,
        )
    return (
        DoctorCheck(
            id="final_cut_pro",
            status="pass",
            summary=(
                "Final Cut Pro is installed and running"
                if running
                else "Final Cut Pro is installed but not running"
            ),
            details={"path": str(application), "running": running},
        ),
        installed,
        running,
    )


def _accessibility_trusted() -> bool | None:
    framework = (
        "/System/Library/Frameworks/ApplicationServices.framework/"
        "ApplicationServices"
    )
    try:
        application_services = ctypes.CDLL(framework)
        application_services.AXIsProcessTrusted.restype = ctypes.c_bool
        return bool(application_services.AXIsProcessTrusted())
    except (AttributeError, OSError):
        return None


def _live_checks(
    config: RuntimeConfig,
    *,
    fcp_installed: bool,
    fcp_running: bool,
) -> list[DoctorCheck]:
    if not config.live_control_enabled:
        return [
            DoctorCheck(
                id="live_control",
                status="skip",
                summary="Live control is disabled by default",
                details={"enabled": False},
                remediation="Set FCP_MCP_ENABLE_LIVE_CONTROL=1 only when live actions are intended",
            ),
            DoctorCheck(
                id="accessibility",
                status="skip",
                summary="Accessibility trust was not probed while live control is disabled",
            ),
        ]

    live_status = "pass" if fcp_installed and fcp_running else "warn"
    live_summary = (
        "Live control is enabled and Final Cut Pro is running"
        if live_status == "pass"
        else "Live control is enabled but Final Cut Pro is unavailable or not running"
    )
    trusted = _accessibility_trusted()
    if trusted is True:
        accessibility = DoctorCheck(
            id="accessibility",
            status="pass",
            summary="This process has macOS Accessibility trust",
        )
    elif trusted is False:
        accessibility = DoctorCheck(
            id="accessibility",
            status="warn",
            summary="This process lacks macOS Accessibility trust",
            remediation=(
                "Enable the invoking terminal or agent in System Settings > "
                "Privacy & Security > Accessibility"
            ),
        )
    else:
        accessibility = DoctorCheck(
            id="accessibility",
            status="warn",
            summary="Accessibility trust could not be determined non-interactively",
            remediation="Verify Accessibility permission before using live-control tools",
        )
    return [
        DoctorCheck(
            id="live_control",
            status=live_status,
            summary=live_summary,
            details={"enabled": True},
        ),
        accessibility,
    ]


def _binary_check(name: str) -> DoctorCheck:
    finder = _find_ffmpeg if name == "ffmpeg" else _find_ffprobe
    try:
        executable = finder()
        result = _run_checked([executable, "-version"], timeout=5)
    except FCPMCPError as error:
        return DoctorCheck(
            id=name,
            status="warn",
            summary=f"{name} is unavailable or unusable",
            details={"error": str(error)},
            remediation="Install or repair FFmpeg, for example: brew install ffmpeg",
        )
    first_line = (result.stdout or "").splitlines()
    return DoctorCheck(
        id=name,
        status="pass",
        summary=f"{name} passed its version probe",
        details={
            "executable": executable,
            "version": first_line[0] if first_line else "unknown",
        },
    )


def _compressor_check() -> DoctorCheck:
    executable = compressor_binary()
    if executable.is_file():
        return DoctorCheck(
            id="compressor",
            status="pass",
            summary="Compressor CLI is installed",
            details={"executable": str(executable)},
        )
    return DoctorCheck(
        id="compressor",
        status="warn",
        summary="Compressor CLI is not installed",
        details={"expected_executable": str(executable)},
        remediation="Install Apple Compressor to use compressor_encode",
    )


def _status(checks: list[DoctorCheck]) -> str:
    required = {"configuration", "output_writable", "mcp_catalog"}
    if any(check.id in required and check.status == "fail" for check in checks):
        return "blocked"
    if any(check.status in {"warn", "fail"} for check in checks):
        return "degraded"
    return "ready"


def configuration_failure_report(error: FCPMCPError) -> DoctorReport:
    sdk_version = distribution_version("mcp")
    return DoctorReport(
        status="blocked",
        package_version=package_version(),
        mcp_sdk_version=sdk_version,
        wire_server_version=sdk_version,
        tool_count=0,
        prompt_count=0,
        checks=[
            DoctorCheck(
                id="configuration",
                status="fail",
                summary="Runtime configuration is invalid",
                details={"error": str(error)},
                remediation="Correct the reported FCP_MCP environment setting",
            )
        ],
    )


async def collect_doctor(
    config: RuntimeConfig,
    catalog_provider: CatalogProvider,
    *,
    expected_tool_names: Collection[str] | None = None,
    expected_prompt_names: Collection[str] | None = None,
) -> DoctorReport:
    checks = [_configuration_check(config), _output_check(config)]
    tool_count = 0
    prompt_count = 0
    tool_names: list[str] = []
    prompt_names: list[str] = []
    expected_tools = sorted(expected_tool_names or ())
    expected_prompts = sorted(expected_prompt_names or ())
    try:
        observed_tools, observed_prompts = await catalog_provider()
        tool_names = sorted(observed_tools)
        prompt_names = sorted(observed_prompts)
        tool_count = len(tool_names)
        prompt_count = len(prompt_names)
    except Exception as error:  # noqa: BLE001 - diagnostics must report provider failures
        checks.append(
            DoctorCheck(
                id="mcp_catalog",
                status="fail",
                summary="MCP catalog could not be built",
                details={
                    "error": str(error),
                    "profile": config.profile.value,
                    "tool_names": tool_names,
                    "expected_tool_names": expected_tools,
                    "prompt_names": prompt_names,
                    "expected_prompt_names": expected_prompts,
                },
            )
        )
    else:
        expected_tools = (
            expected_tools
            if expected_tool_names is not None
            else tool_names
        )
        expected_prompts = (
            expected_prompts
            if expected_prompt_names is not None
            else prompt_names
        )
        missing_tools = sorted(set(expected_tools) - set(tool_names))
        unexpected_tools = sorted(set(tool_names) - set(expected_tools))
        missing_prompts = sorted(set(expected_prompts) - set(prompt_names))
        unexpected_prompts = sorted(set(prompt_names) - set(expected_prompts))
        catalog_valid = (
            tool_names == expected_tools
            and prompt_names == expected_prompts
        )
        checks.append(
            DoctorCheck(
                id="mcp_catalog",
                status="pass" if catalog_valid else "fail",
                summary=(
                    f"MCP catalog contains {tool_count} tools and "
                    f"{prompt_count} prompts"
                    if catalog_valid
                    else f"Unexpected MCP catalog: {tool_count} tools, {prompt_count} prompts"
                ),
                details={
                    "tool_count": tool_count,
                    "prompt_count": prompt_count,
                    "profile": config.profile.value,
                    "tool_names": tool_names,
                    "expected_tool_names": expected_tools,
                    "missing_tool_names": missing_tools,
                    "unexpected_tool_names": unexpected_tools,
                    "prompt_names": prompt_names,
                    "expected_prompt_names": expected_prompts,
                    "missing_prompt_names": missing_prompts,
                    "unexpected_prompt_names": unexpected_prompts,
                },
                remediation=(
                    None
                    if catalog_valid
                    else "Reinstall a verified fcp-mcp 0.2.1 artifact"
                ),
            )
        )

    fcp_check, installed, running = _fcp_check()
    checks.append(fcp_check)
    checks.extend(
        _live_checks(
            config,
            fcp_installed=installed,
            fcp_running=running,
        )
    )
    checks.extend([_binary_check("ffmpeg"), _binary_check("ffprobe")])
    checks.append(_compressor_check())
    sdk_version = distribution_version("mcp")
    return DoctorReport(
        status=_status(checks),
        package_version=package_version(),
        mcp_sdk_version=sdk_version,
        wire_server_version=sdk_version,
        tool_count=tool_count,
        prompt_count=prompt_count,
        checks=checks,
    )
