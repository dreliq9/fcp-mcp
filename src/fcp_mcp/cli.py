from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence

from fcp_mcp.config import RuntimeConfig
from fcp_mcp.contracts import DoctorReport, FCPMCPError
from fcp_mcp.diagnostics import collect_doctor, configuration_failure_report
from fcp_mcp.version import package_version


def serve() -> None:
    from fcp_mcp.server import main as server_main

    server_main()


async def _doctor() -> DoctorReport:
    try:
        config = RuntimeConfig.from_env()
    except FCPMCPError as error:
        return configuration_failure_report(error)

    from fcp_mcp.server import mcp

    async def catalog() -> tuple[int, int]:
        return len(await mcp.list_tools()), len(await mcp.list_prompts())

    return await collect_doctor(config, catalog_provider=catalog)


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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fcp-mcp")
    parser.add_argument("--version", action="store_true")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("serve")
    doctor = commands.add_parser("doctor")
    doctor.add_argument("--json", action="store_true", dest="as_json")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        serve()
        return 0

    options = _parser().parse_args(arguments)
    if options.version:
        print(f"fcp-mcp {package_version()}")
        return 0
    if options.command == "serve":
        serve()
        return 0
    if options.command == "doctor":
        report = asyncio.run(_doctor())
        print(
            report.model_dump_json(indent=2)
            if options.as_json
            else _render_doctor(report)
        )
        return {"ready": 0, "degraded": 1, "blocked": 2}[report.status]

    _parser().print_help()
    return 2
