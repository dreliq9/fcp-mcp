"""Exercise an installed fcp-mcp executable through its public interfaces."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, TypeVar

from mcp import Client, StdioServerParameters
from mcp.client.stdio import stdio_client

EXPECTED_VERSION = "0.2.1"
EXPECTED_TOOL_COUNT = 93
EXPECTED_PROMPT_COUNT = 5
VERSION_PATTERN = re.compile(r"^fcp-mcp (?P<version>\d+\.\d+\.\d+)$")
Page = TypeVar("Page")


class SmokeFailure(RuntimeError):
    """The installed artifact did not satisfy its public release contract."""


def check_version(command: str) -> str:
    """Run the installed console script's version path."""
    result = subprocess.run(
        [command, "--version"],
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )
    if result.returncode != 0:
        raise SmokeFailure(
            f"{command} --version exited {result.returncode}: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    match = VERSION_PATTERN.fullmatch(result.stdout.strip())
    if match is None:
        raise SmokeFailure(f"unexpected --version output: {result.stdout.strip()!r}")
    version = match.group("version")
    if version != EXPECTED_VERSION:
        raise SmokeFailure(
            f"package version mismatch: expected {EXPECTED_VERSION}, got {version}"
        )
    return version


async def _collect_pages(
    fetch: Callable[[str | None], Awaitable[Page]],
    field: str,
) -> list[Any]:
    items: list[Any] = []
    cursor: str | None = None
    seen: set[str] = set()
    while True:
        page = await fetch(cursor)
        items.extend(getattr(page, field))
        cursor = page.next_cursor
        if cursor is None:
            return items
        if cursor in seen:
            raise SmokeFailure(f"repeated {field} cursor: {cursor}")
        seen.add(cursor)


async def inspect_server(
    command: str,
    *,
    cwd: str | Path | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Initialize stdio, inspect the full catalog, and call doctor."""
    parameters = StdioServerParameters(
        command=command,
        env=env,
        cwd=cwd,
    )
    async with Client(
        stdio_client(parameters),
        mode="auto",
        raise_exceptions=True,
    ) as client:
        tools = await _collect_pages(
            lambda cursor: client.list_tools(cursor=cursor),
            "tools",
        )
        prompts = await _collect_pages(
            lambda cursor: client.list_prompts(cursor=cursor),
            "prompts",
        )
        doctor = await client.call_tool("fcp_doctor")
        server_info = client.server_info
        protocol_version = client.protocol_version

    if server_info.name != "fcp-mcp":
        raise SmokeFailure(
            f"server name mismatch: expected fcp-mcp, got {server_info.name}"
        )
    if len(tools) != EXPECTED_TOOL_COUNT:
        raise SmokeFailure(
            f"tool count mismatch: expected {EXPECTED_TOOL_COUNT}, got {len(tools)}"
        )
    if len(prompts) != EXPECTED_PROMPT_COUNT:
        raise SmokeFailure(
            f"prompt count mismatch: expected {EXPECTED_PROMPT_COUNT}, got {len(prompts)}"
        )
    if doctor.is_error:
        raise SmokeFailure("fcp_doctor returned an MCP tool error")
    if not isinstance(doctor.structured_content, dict):
        raise SmokeFailure("fcp_doctor returned no structured content")

    doctor_report = doctor.structured_content
    expected_doctor_values = {
        "package_version": EXPECTED_VERSION,
        "server_name": "fcp-mcp",
        "tool_count": EXPECTED_TOOL_COUNT,
        "prompt_count": EXPECTED_PROMPT_COUNT,
    }
    for field, expected in expected_doctor_values.items():
        actual = doctor_report.get(field)
        if actual != expected:
            raise SmokeFailure(
                f"doctor {field} mismatch: expected {expected!r}, got {actual!r}"
            )
    if doctor_report.get("status") not in {"ready", "degraded"}:
        raise SmokeFailure(
            f"doctor reported blocked runtime: {doctor_report.get('status')!r}"
        )
    if doctor_report.get("wire_server_version") != server_info.version:
        raise SmokeFailure("doctor and client disagree on the wire server version")

    return {
        "status": "passed",
        "package_version": doctor_report["package_version"],
        "mcp_sdk_version": doctor_report["mcp_sdk_version"],
        "wire_server_version": server_info.version,
        "protocol_version": str(protocol_version),
        "server_name": server_info.name,
        "tool_count": len(tools),
        "prompt_count": len(prompts),
        "doctor_status": doctor_report["status"],
        "doctor_is_error": bool(doctor.is_error),
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Smoke-test an installed fcp-mcp wheel through stdio.",
    )
    parser.add_argument(
        "--command",
        default="fcp-mcp",
        help="Path to the installed fcp-mcp console script",
    )
    return parser.parse_args()


def main() -> int:
    options = _parse_args()
    try:
        version = check_version(options.command)
        report = asyncio.run(
            inspect_server(
                options.command,
                cwd=Path.cwd(),
                env=dict(os.environ),
            )
        )
        report["version_command"] = version
    except Exception as error:  # noqa: BLE001 - CLI must serialize any smoke failure
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error": f"{type(error).__name__}: {error}",
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1

    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
