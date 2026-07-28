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
    "edit": (74, 5, 3),
    "full": (93, 5, 3),
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


def _resource_json(result: Any) -> dict[str, Any]:
    if len(result.contents) != 1:
        raise SmokeFailure("resource did not return exactly one content item")
    content = result.contents[0]
    text_value = getattr(content, "text", None)
    if not isinstance(text_value, str):
        raise SmokeFailure("resource did not return text JSON")
    payload = json.loads(text_value)
    if not isinstance(payload, dict):
        raise SmokeFailure("resource JSON was not an object")
    return payload


async def smoke_workflow(
    command: str,
    *,
    cwd: str | Path | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Exercise a real two-operation workflow through installed public APIs."""
    selected_env = dict(os.environ if env is None else env)
    output_dir = Path(selected_env["FCP_MCP_OUTPUT_DIR"]).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    nonce = uuid4().hex
    source = output_dir / f"wheel-smoke-source-{nonce}.fcpxml"
    destination = output_dir / f"wheel-smoke-destination-{nonce}.fcpxml"
    source.write_text(SOURCE_XML, encoding="utf-8")

    parameters = StdioServerParameters(command=command, env=selected_env, cwd=cwd)
    async with Client(
        stdio_client(parameters),
        mode="auto",
        raise_exceptions=True,
    ) as client:
        validation = await client.call_tool(
            "fcpxml_validate",
            {"path": str(source)},
        )
        if validation.is_error or not isinstance(validation.structured_content, dict):
            raise SmokeFailure("legacy validation did not return typed success")
        validation_payload = validation.structured_content
        if validation_payload.get("valid") is not True:
            raise SmokeFailure("workflow source failed legacy validation")
        if _text_content(validation) != validation_payload.get("summary"):
            raise SmokeFailure("legacy validation text no longer matches its exact summary")

        prepared = await client.call_tool(
            "fcpxml_workflow_prepare",
            {
                "schema_version": "1",
                "source_path": str(source),
                "destination_path": str(destination),
                "operations": [
                    {
                        "kind": "add_marker",
                        "clip_name": "Clip",
                        "start": "1/30s",
                        "value": "Wheel smoke",
                    },
                    {
                        "kind": "assign_role",
                        "clip_name": "Clip",
                        "role": "Dialogue",
                    },
                ],
                "idempotency_key": f"installed-wheel-smoke-{nonce}",
            },
        )
        if prepared.is_error or not isinstance(prepared.structured_content, dict):
            raise SmokeFailure("workflow prepare did not return typed success")
        preview = prepared.structured_content
        if preview.get("state") != "awaiting_approval":
            raise SmokeFailure(f"unexpected prepared state: {preview.get('state')!r}")
        operation_receipts = preview.get("operation_receipts")
        if not isinstance(operation_receipts, list) or [
            (
                receipt.get("operation_id"),
                receipt.get("kind"),
                receipt.get("disposition"),
            )
            for receipt in operation_receipts
            if isinstance(receipt, dict)
        ] != [
            ("op-001", "add_marker", "succeeded"),
            ("op-002", "assign_role", "succeeded"),
        ]:
            raise SmokeFailure("prepare did not prove both requested operations")
        prepare_left_destination_untouched = not destination.exists()
        if not prepare_left_destination_untouched:
            raise SmokeFailure("prepare mutated the public destination")

        run_id = preview["run_id"]
        candidate_sha256 = preview["candidate_sha256"]
        resource_uris = (
            f"fcp-workflow://runs/{run_id}",
            f"fcp-workflow://runs/{run_id}/events",
            f"fcp-workflow://runs/{run_id}/diff",
        )
        resource_payloads = [
            _resource_json(await client.read_resource(uri)) for uri in resource_uris
        ]
        run_resource, events_resource, diff_resource = resource_payloads
        if (
            run_resource.get("run_id") != run_id
            or run_resource.get("state") != "awaiting_approval"
            or run_resource.get("candidate_sha256") != candidate_sha256
            or run_resource.get("diff_sha256") != preview.get("diff_sha256")
        ):
            raise SmokeFailure("workflow run resource is not bound to the preview")
        events = events_resource.get("events")
        if (
            events_resource.get("run_id") != run_id
            or events_resource.get("truncated") is not False
            or not isinstance(events, list)
            or not events
            or events[-1].get("sequence") != run_resource.get("revision")
            or events[-1].get("event_type") != "awaiting_approval"
        ):
            raise SmokeFailure("workflow event resource is not a complete preview chain")
        canonical_diff = json.dumps(
            diff_resource,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        semantic_diff = diff_resource.get("semantic_diff")
        if (
            diff_resource.get("run_id") != run_id
            or hashlib.sha256(canonical_diff).hexdigest() != preview.get("diff_sha256")
            or not isinstance(semantic_diff, dict)
            or semantic_diff.get("change_count", 0) < 2
            or diff_resource.get("operation_receipts") != operation_receipts
        ):
            raise SmokeFailure("workflow diff resource is not bound to both operations")

        committed = await client.call_tool(
            "fcpxml_workflow_commit",
            {
                "run_id": run_id,
                "expected_candidate_sha256": candidate_sha256,
            },
        )
        if committed.is_error or not isinstance(committed.structured_content, dict):
            raise SmokeFailure("workflow commit did not return typed success")
        receipt = committed.structured_content
        pre_reconcile = _resource_json(
            await client.read_resource(resource_uris[0], cache_mode="refresh")
        )
        if (
            pre_reconcile.get("state") != "committed"
            or pre_reconcile.get("recovery") is not None
        ):
            raise SmokeFailure("committed run resource is not terminal and unambiguous")

    destination_sha256 = hashlib.sha256(destination.read_bytes()).hexdigest()
    if destination_sha256 != candidate_sha256:
        raise SmokeFailure("committed destination does not match reviewed candidate")
    if receipt.get("output_sha256") != candidate_sha256:
        raise SmokeFailure("commit receipt does not match reviewed candidate")

    reconcile_process = await asyncio.create_subprocess_exec(
        command,
        "workflow",
        "reconcile",
        run_id,
        "--json",
        cwd=cwd,
        env=selected_env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(reconcile_process.communicate(), timeout=30)
    if reconcile_process.returncode != 0:
        raise SmokeFailure(
            "installed reconcile failed: "
            f"{stderr.decode().strip() or stdout.decode().strip()}"
        )
    reconcile_payload = json.loads(stdout)
    for field in ("state", "revision", "updated_at", "recovery"):
        if reconcile_payload.get(field) != pre_reconcile.get(field):
            raise SmokeFailure(
                f"reconcile changed completed-run field {field}: "
                f"{pre_reconcile.get(field)!r} -> {reconcile_payload.get(field)!r}"
            )

    doctor_process = await asyncio.create_subprocess_exec(
        command,
        "doctor",
        "--json",
        cwd=cwd,
        env=selected_env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    doctor_stdout, doctor_stderr = await asyncio.wait_for(
        doctor_process.communicate(),
        timeout=30,
    )
    if doctor_process.returncode not in {0, 1}:
        raise SmokeFailure(
            "installed doctor failed: "
            f"{doctor_stderr.decode().strip() or doctor_stdout.decode().strip()}"
        )
    doctor_payload = json.loads(doctor_stdout)
    checks = doctor_payload.get("checks")
    if not isinstance(checks, list):
        raise SmokeFailure("post-commit doctor omitted checks")
    ledger_check = next(
        (
            check
            for check in checks
            if isinstance(check, dict) and check.get("id") == "workflow_ledger"
        ),
        None,
    )
    if ledger_check is None or ledger_check.get("status") != "pass":
        raise SmokeFailure(
            "post-commit workflow ledger check did not pass: "
            f"{ledger_check!r}"
        )
    ledger_details = ledger_check.get("details")
    if (
        not isinstance(ledger_details, dict)
        or ledger_details.get("integrity_valid") is not True
        or ledger_details.get("checked_runs", 0) < 1
        or ledger_details.get("incomplete_count") != 0
        or ledger_details.get("recovery_required_count") != 0
    ):
        raise SmokeFailure("post-commit workflow ledger diagnostics are incomplete")

    return {
        "state": reconcile_payload["state"],
        "run_id": run_id,
        "candidate_sha256": candidate_sha256,
        "destination_sha256": destination_sha256,
        "prepare_left_destination_untouched": prepare_left_destination_untouched,
        "candidate_matches_destination": destination_sha256 == candidate_sha256,
        "resource_count": len(resource_payloads),
        "ledger_integrity_valid": ledger_details["integrity_valid"],
        "ledger_checked_runs": ledger_details["checked_runs"],
        "reconcile_state": reconcile_payload["state"],
        "reconcile_recovery": reconcile_payload.get("recovery"),
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
        base_env = dict(os.environ)
        default_env = dict(base_env)
        default_env.pop("FCP_MCP_PROFILE", None)
        default_report = asyncio.run(
            inspect_server(
                options.command,
                cwd=Path.cwd(),
                env=default_env,
                profile="workflow",
            )
        )
        profiles = {"workflow": default_report}
        for profile in ("inspect", "workflow", "edit", "full"):
            profile_env = {**base_env, "FCP_MCP_PROFILE": profile}
            profiles[profile] = asyncio.run(
                inspect_server(
                    options.command,
                    cwd=Path.cwd(),
                    env=profile_env,
                    profile=profile,
                )
            )
        workflow_env = {**base_env, "FCP_MCP_PROFILE": "workflow"}
        workflow_report = asyncio.run(
            smoke_workflow(
                options.command,
                cwd=Path.cwd(),
                env=workflow_env,
            )
        )
        report = {
            **default_report,
            "default_profile": "workflow",
            "profiles": profiles,
            "workflow": workflow_report,
        }
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
