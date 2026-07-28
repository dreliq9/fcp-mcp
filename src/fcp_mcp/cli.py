from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence

from fcp_mcp.config import RuntimeConfig
from fcp_mcp.contracts import DoctorReport, ErrorCode, FCPMCPError
from fcp_mcp.diagnostics import (
    collect_doctor,
    collect_ledger_check,
    configuration_failure_report,
)
from fcp_mcp.platform_support import require_macos
from fcp_mcp.profiles import Profile
from fcp_mcp.version import package_version


def serve(profile_override: str | None = None) -> None:
    from fcp_mcp.server import main as server_main

    server_main(profile_override=profile_override)


async def _doctor() -> DoctorReport:
    require_macos()
    try:
        config = RuntimeConfig.from_env()
    except FCPMCPError as error:
        return configuration_failure_report(error)

    from fcp_mcp.server import catalog_expectations, mcp

    async def catalog() -> tuple[tuple[str, ...], tuple[str, ...]]:
        tools = await mcp.list_tools()
        prompts = await mcp.list_prompts()
        return (
            tuple(tool.name for tool in tools),
            tuple(prompt.name for prompt in prompts),
        )

    expected_tool_names, expected_prompt_names = catalog_expectations(
        config.profile
    )
    return await collect_doctor(
        config,
        catalog_provider=catalog,
        expected_tool_names=expected_tool_names,
        expected_prompt_names=expected_prompt_names,
        ledger_check=collect_ledger_check(config),
    )


def _render_doctor(report: DoctorReport) -> str:
    lines = [
        f"fcp-mcp doctor: {report.status}",
        (
            f"package={report.package_version} mcp-sdk={report.mcp_sdk_version} "
            f"wire={report.wire_server_version} "
            f"catalog={report.tool_count} tools/{report.prompt_count} prompts"
        ),
    ]
    for check in report.checks:
        lines.append(f"{check.status.upper():5} {check.id:20} {check.summary}")
        if check.remediation:
            lines.append(f"      fix: {check.remediation}")
    return "\n".join(lines)


def _reconcile(config: RuntimeConfig, run_id: str):
    from fcp_mcp.workflow.surface import WorkflowRuntime, _canonical_run_id

    selected = _canonical_run_id(run_id)
    runtime = WorkflowRuntime(config)
    runtime.engine.reconcile(selected)
    return runtime.status(selected)


def _render_reconcile(status) -> str:
    recovery = (
        status.recovery.recommended_branch.value
        if status.recovery is not None
        else "none"
    )
    return (
        f"Workflow {status.run_id}: state={status.state.value} "
        f"recovery={recovery}"
    )


def _workflow_error_exit(error: FCPMCPError) -> int:
    if error.code in {
        ErrorCode.INVALID_ARGUMENTS,
        ErrorCode.INVALID_CONFIGURATION,
        ErrorCode.WORKFLOW_STATE_CONFLICT,
    }:
        return 2
    if error.code in {
        ErrorCode.ARTIFACT_CORRUPT,
        ErrorCode.LEDGER_UNAVAILABLE,
        ErrorCode.INTERNAL_ERROR,
    }:
        return 3
    return 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fcp-mcp")
    parser.add_argument("--version", action="store_true")
    commands = parser.add_subparsers(dest="command")
    serve_parser = commands.add_parser("serve")
    serve_parser.add_argument(
        "--profile",
        choices=[profile.value for profile in Profile],
    )
    doctor = commands.add_parser("doctor")
    doctor.add_argument("--json", action="store_true", dest="as_json")
    workflow = commands.add_parser("workflow")
    workflow_commands = workflow.add_subparsers(
        dest="workflow_command",
        required=True,
    )
    reconcile = workflow_commands.add_parser("reconcile")
    reconcile.add_argument("run_id")
    reconcile.add_argument("--json", action="store_true", dest="as_json")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    options = _parser().parse_args(arguments)
    if options.version:
        print(f"fcp-mcp {package_version()}")
        return 0

    try:
        require_macos()
    except FCPMCPError as error:
        if error.code is not ErrorCode.UNSUPPORTED_PLATFORM:
            raise
        print(str(error), file=sys.stderr)
        return 2

    if options.command is None:
        serve()
        return 0
    if options.command == "serve":
        if options.profile is None:
            serve()
        else:
            serve(options.profile)
        return 0
    if options.command == "doctor":
        report = asyncio.run(_doctor())
        print(
            report.model_dump_json(indent=2)
            if options.as_json
            else _render_doctor(report)
        )
        return {"ready": 0, "degraded": 1, "blocked": 2}[report.status]
    if options.command == "workflow" and options.workflow_command == "reconcile":
        try:
            config = RuntimeConfig.from_env()
            status = _reconcile(config, options.run_id)
        except FCPMCPError as error:
            print(str(error), file=sys.stderr)
            return _workflow_error_exit(error)
        print(
            status.model_dump_json(indent=2)
            if options.as_json
            else _render_reconcile(status)
        )
        if status.state.value == "recovery_required" or status.recovery is not None:
            return 1
        return 0

    _parser().print_help()
    return 2
