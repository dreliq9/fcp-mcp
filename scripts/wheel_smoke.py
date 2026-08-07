"""Exercise an installed fcp-mcp executable through its public interfaces."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, TypeVar
from uuid import uuid4

from mcp import Client, StdioServerParameters
from mcp.client.stdio import stdio_client

EXPECTED_VERSION = "0.3.0"
PROFILE_COUNTS = {
    "inspect": (30, 2, 0),
    "workflow": (34, 3, 3),
    "edit": (75, 5, 3),
    "full": (94, 5, 3),
}
SOURCE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<fcpxml version="1.11"><resources>
<format id="r1" frameDuration="1/30s"/>
<asset id="r2" name="Clip" duration="3s" hasVideo="1" hasAudio="1"
       audioSources="1" audioChannels="2">
<media-rep kind="original-media" src="file:///tmp/fcp-mcp-wheel-smoke-placeholder.mov"/>
</asset>
</resources><event name="Event"><project name="Project">
<sequence format="r1" duration="3s"><spine>
<asset-clip ref="r2" name="Clip" offset="0s" start="0s" duration="3s"/>
</spine></sequence></project></event></fcpxml>
"""
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
    profile: str = "full",
) -> dict[str, Any]:
    """Initialize stdio, inspect one exact profile, and call doctor."""
    expected_tool_count, expected_prompt_count, expected_resource_count = (
        PROFILE_COUNTS[profile]
    )
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
        resource_templates = await _collect_pages(
            lambda cursor: client.list_resource_templates(cursor=cursor),
            "resource_templates",
        )
        doctor = await client.call_tool("fcp_doctor")
        server_info = client.server_info
        protocol_version = client.protocol_version

    if server_info.name != "fcp-mcp":
        raise SmokeFailure(
            f"server name mismatch: expected fcp-mcp, got {server_info.name}"
        )
    if len(tools) != expected_tool_count:
        raise SmokeFailure(
            f"tool count mismatch: expected {expected_tool_count}, got {len(tools)}"
        )
    if len(prompts) != expected_prompt_count:
        raise SmokeFailure(
            f"prompt count mismatch: expected {expected_prompt_count}, got {len(prompts)}"
        )
    if len(resource_templates) != expected_resource_count:
        raise SmokeFailure(
            "resource template count mismatch: "
            f"expected {expected_resource_count}, got {len(resource_templates)}"
        )
    if doctor.is_error:
        raise SmokeFailure("fcp_doctor returned an MCP tool error")
    if not isinstance(doctor.structured_content, dict):
        raise SmokeFailure("fcp_doctor returned no structured content")

    doctor_report = doctor.structured_content
    expected_doctor_values = {
        "package_version": EXPECTED_VERSION,
        "wire_server_version": EXPECTED_VERSION,
        "server_name": "fcp-mcp",
        "tool_count": expected_tool_count,
        "prompt_count": expected_prompt_count,
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
        "profile": profile,
        "tool_count": len(tools),
        "prompt_count": len(prompts),
        "resource_template_count": len(resource_templates),
        "doctor_status": doctor_report["status"],
        "doctor_is_error": bool(doctor.is_error),
    }


def _text_content(result: Any) -> str:
    if not result.content or not isinstance(result.content[0].text, str):
        raise SmokeFailure("tool result did not contain leading text content")
    return result.content[0].text


def _assert_workflow_prepare(
    result: Any,
    *,
    destination: Path,
) -> dict[str, Any]:
    if result.is_error:
        raise SmokeFailure("fcpxml_workflow_prepare returned an MCP tool error")
    if not isinstance(result.structured_content, dict):
        raise SmokeFailure("fcpxml_workflow_prepare returned no structured content")
    payload = result.structured_content
    if payload.get("state") != "awaiting_approval":
        raise SmokeFailure(f"prepare state mismatch: {payload.get('state')!r}")
    if payload.get("destination_path") != str(destination.resolve()):
        raise SmokeFailure("prepare destination mismatch")
    candidate_sha256 = payload.get("candidate_sha256")
    if not isinstance(candidate_sha256, str) or len(candidate_sha256) != 64:
        raise SmokeFailure("prepare returned no candidate SHA-256")
    if destination.exists():
        raise SmokeFailure("prepare unexpectedly wrote the public destination")
    if not result.content or not isinstance(result.content[0].text, str):
        raise SmokeFailure("prepare returned no text content")
    return payload


def _assert_workflow_commit(
    result: Any,
    *,
    destination: Path,
    candidate_sha256: str,
) -> dict[str, Any]:
    if result.is_error:
        raise SmokeFailure("fcpxml_workflow_commit returned an MCP tool error")
    if not isinstance(result.structured_content, dict):
        raise SmokeFailure("fcpxml_workflow_commit returned no structured content")
    payload = result.structured_content
    if payload.get("state") != "committed":
        raise SmokeFailure(f"commit state mismatch: {payload.get('state')!r}")
    if payload.get("destination_path") != str(destination.resolve()):
        raise SmokeFailure("commit destination mismatch")
    if not destination.exists():
        raise SmokeFailure("commit did not write the destination")
    actual_sha256 = hashlib.sha256(destination.read_bytes()).hexdigest()
    if actual_sha256 != candidate_sha256:
        raise SmokeFailure("commit destination hash differs from approved candidate")
    receipt = payload.get("receipt")
    if not isinstance(receipt, dict) or receipt.get("disposition") != "committed":
        raise SmokeFailure("commit returned no committed transaction receipt")
    return payload


async def exercise_workflow(
    command: str,
    *,
    cwd: str | Path | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Exercise a prepare -> status -> commit run through stdio."""
    root = Path(cwd or Path.cwd())
    workspace = root / f".wheel-smoke-{uuid4()}"
    workspace.mkdir(parents=True, exist_ok=False)
    source = workspace / "source.fcpxml"
    destination = workspace / "destination.fcpxml"
    source.write_text(SOURCE_XML, encoding="utf-8")
    environment = dict(env or {})
    environment["FCP_MCP_OUTPUT_DIR"] = str(workspace)
    environment["FCP_MCP_ALLOWED_ROOTS"] = str(workspace)
    environment["FCP_MCP_STATE_DIR"] = str(workspace / "state")
    environment["FCP_MCP_PROFILE"] = "workflow"

    try:
        parameters = StdioServerParameters(
            command=command,
            env=environment,
            cwd=cwd,
        )
        async with Client(
            stdio_client(parameters),
            mode="auto",
            raise_exceptions=True,
        ) as client:
            prepare = await client.call_tool(
                "fcpxml_workflow_prepare",
                {
                    "request": {
                        "schema_version": "1",
                        "source_path": str(source),
                        "destination_path": str(destination),
                        "operations": [
                            {
                                "kind": "add_marker",
                                "clip_name": "Clip",
                                "start": "1/30s",
                                "value": "Chapter",
                            }
                        ],
                    }
                },
            )
            prepare_payload = _assert_workflow_prepare(
                prepare,
                destination=destination,
            )
            run_id = prepare_payload["run_id"]
            candidate_sha256 = prepare_payload["candidate_sha256"]
            status = await client.call_tool(
                "fcpxml_workflow_status",
                {"run_id": run_id},
            )
            if status.is_error or not isinstance(status.structured_content, dict):
                raise SmokeFailure("workflow status failed")
            if status.structured_content.get("state") != "awaiting_approval":
                raise SmokeFailure("workflow status lost awaiting_approval state")
            commit = await client.call_tool(
                "fcpxml_workflow_commit",
                {
                    "run_id": run_id,
                    "expected_candidate_sha256": candidate_sha256,
                },
            )
            commit_payload = _assert_workflow_commit(
                commit,
                destination=destination,
                candidate_sha256=candidate_sha256,
            )
        return {
            "status": "passed",
            "run_id": run_id,
            "candidate_sha256": candidate_sha256,
            "commit_state": commit_payload["state"],
            "destination_sha256": hashlib.sha256(
                destination.read_bytes()
            ).hexdigest(),
        }
    finally:
        if workspace.exists():
            for path in sorted(workspace.rglob("*"), reverse=True):
                if path.is_file() or path.is_symlink():
                    path.unlink(missing_ok=True)
                elif path.is_dir():
                    path.rmdir()
            workspace.rmdir()


def _environment(output_dir: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment["FCP_MCP_OUTPUT_DIR"] = str(output_dir)
    environment["FCP_MCP_ALLOWED_ROOTS"] = str(output_dir)
    environment["FCP_MCP_STATE_DIR"] = str(output_dir / "state")
    environment["FCP_MCP_ENABLE_LIVE_CONTROL"] = "0"
    return environment


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--command", default="fcp-mcp")
    parser.add_argument("--cwd", default=None)
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--profile", choices=tuple(PROFILE_COUNTS), default="full")
    parser.add_argument("--exercise-workflow", action="store_true")
    args = parser.parse_args()

    output_dir = Path(args.output_dir or Path.cwd())
    output_dir.mkdir(parents=True, exist_ok=True)
    environment = _environment(output_dir)
    environment["FCP_MCP_PROFILE"] = args.profile

    try:
        report = asyncio.run(
            inspect_server(
                args.command,
                cwd=args.cwd,
                env=environment,
                profile=args.profile,
            )
        )
        report["script_version"] = check_version(args.command)
        if args.exercise_workflow:
            report["workflow"] = asyncio.run(
                exercise_workflow(
                    args.command,
                    cwd=args.cwd,
                    env=environment,
                )
            )
    except (SmokeFailure, OSError, subprocess.SubprocessError) as error:
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
