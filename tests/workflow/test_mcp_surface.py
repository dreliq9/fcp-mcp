from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from fcp_mcp.config import RuntimeConfig
from fcp_mcp.mcp_boundary import build_mcp_server
from fcp_mcp.registry import PromptRegistry, ResourceRegistry, ToolRegistry
from fcp_mcp.workflow.models import (
    WorkflowCancelResultV1,
    WorkflowCommitReceiptV1,
    WorkflowPreviewV1,
    WorkflowStatusV1,
)
from fcp_mcp.workflow.surface import WorkflowRuntime, register_workflow_surface

SOURCE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<fcpxml version="1.11">
  <resources>
    <format id="r1" frameDuration="1001/30000s" width="1920" height="1080"/>
    <asset id="r2" name="Clip" duration="3003/1000s" hasVideo="1" hasAudio="1"/>
  </resources>
  <event name="Event">
    <project name="Project">
      <sequence format="r1" duration="3003/1000s">
        <spine>
          <asset-clip ref="r2" name="Clip" offset="0s" start="0s"
                      duration="3003/1000s"/>
        </spine>
      </sequence>
    </project>
  </event>
</fcpxml>
"""

EXPECTED_ANNOTATIONS = {
    "fcpxml_workflow_prepare": {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
    "fcpxml_workflow_status": {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
    "fcpxml_workflow_commit": {
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": False,
    },
    "fcpxml_workflow_cancel": {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
}

EXPECTED_RESOURCE_TEMPLATES = {
    "fcp-workflow://runs/{run_id}",
    "fcp-workflow://runs/{run_id}/events",
    "fcp-workflow://runs/{run_id}/diff",
}


def _config(
    tmp_path: Path,
    approval_mode: str = "client",
) -> RuntimeConfig:
    return RuntimeConfig.from_env(
        {
            "FCP_MCP_OUTPUT_DIR": str(tmp_path),
            "FCP_MCP_ALLOWED_ROOTS": str(tmp_path),
            "FCP_MCP_STATE_DIR": str(tmp_path / "state"),
            "FCP_MCP_PROFILE": "workflow",
            "FCP_MCP_WORKFLOW_APPROVAL": approval_mode,
            "FCP_MCP_WORKFLOW_MAX_SOURCE_BYTES": str(1024 * 1024),
            "FCP_MCP_WORKFLOW_MAX_ARTIFACT_BYTES": str(4 * 1024 * 1024),
            "FCP_MCP_WORKFLOW_MAX_DIFF_BYTES": str(200 * 1024),
            "FCP_MCP_WORKFLOW_APPROVAL_TTL_SECONDS": "600",
        },
        home=tmp_path,
    )


def _server(tmp_path: Path, approval_mode: str = "client"):
    config = _config(tmp_path, approval_mode)
    tools = ToolRegistry()
    resources = ResourceRegistry()
    register_workflow_surface(config, tools, resources)
    return (
        build_mcp_server(config, tools, PromptRegistry(), resources),
        config,
        resources,
    )


def _prepare_arguments(source: Path, destination: Path, key: str) -> dict[str, object]:
    return {
        "schema_version": "1",
        "source_path": str(source),
        "destination_path": str(destination),
        "operations": [
            {
                "kind": "add_marker",
                "clip_name": "Clip",
                "start": "1001/30000s",
                "duration": "1001/30000s",
                "value": "Chapter",
                "note": "surface test",
            }
        ],
        "idempotency_key": key,
    }


def test_surface_registers_exact_tools_annotations_and_resources(tmp_path: Path):
    server, _, resources = _server(tmp_path)

    tools = asyncio.run(server.list_tools())
    assert {item.name for item in tools} == set(EXPECTED_ANNOTATIONS)
    for item in tools:
        assert item.annotations is not None
        assert item.annotations.model_dump(by_alias=True, exclude_none=True) == (
            EXPECTED_ANNOTATIONS[item.name]
        )
        assert item.outputSchema is not None

    templates = asyncio.run(server.list_resource_templates())
    assert {str(item.uriTemplate) for item in templates} == EXPECTED_RESOURCE_TEMPLATES
    assert set(resources.definitions) == EXPECTED_RESOURCE_TEMPLATES
    assert all(definition.read_only for definition in resources.definitions.values())
    diff = next(item for item in templates if str(item.uriTemplate).endswith("/diff"))
    assert diff.mimeType == "application/json"
    assert not any("candidate" in str(item.uriTemplate) for item in templates)


def test_invalid_resource_run_id_fails_before_lazy_ledger_creation(tmp_path: Path):
    server, config, _ = _server(tmp_path)
    assert not config.state_dir.exists()

    with pytest.raises(ValueError, match="canonical lowercase UUID"):
        asyncio.run(
            server.read_resource(
                "fcp-workflow://runs/123E4567-E89B-42D3-A456-426614174000"
            )
        )

    assert not config.state_dir.exists()


def test_workflow_tools_and_resources_round_trip_without_resource_mutation(
    tmp_path: Path,
):
    server, _, _ = _server(tmp_path)
    source = tmp_path / "source.fcpxml"
    destination = tmp_path / "destination.fcpxml"
    source.write_text(SOURCE_XML)

    prepared = asyncio.run(
        server.call_tool(
            "fcpxml_workflow_prepare",
            _prepare_arguments(source, destination, "surface-prepare"),
        )
    )
    preview = WorkflowPreviewV1.model_validate_json(
        json.dumps(prepared.structuredContent)
    )
    assert preview.state.value == "awaiting_approval"
    assert preview.run_id in prepared.content[0].text
    assert len(prepared.content[0].text) <= 4096
    assert not destination.exists()

    status_result = asyncio.run(
        server.call_tool("fcpxml_workflow_status", {"run_id": preview.run_id})
    )
    status = WorkflowStatusV1.model_validate_json(
        json.dumps(status_result.structuredContent)
    )
    revision = status.revision
    assert status.state.value == "awaiting_approval"
    assert len(status_result.content[0].text) <= 4096

    run_resource = next(
        iter(
            asyncio.run(
                server.read_resource(f"fcp-workflow://runs/{preview.run_id}")
            )
        )
    )
    run_payload = WorkflowStatusV1.model_validate_json(run_resource.content)
    assert run_payload.revision == revision

    events_resource = next(
        iter(
            asyncio.run(
                server.read_resource(
                    f"fcp-workflow://runs/{preview.run_id}/events"
                )
            )
        )
    )
    events_payload = json.loads(events_resource.content)
    assert events_payload["run_id"] == preview.run_id
    assert events_payload["events"][-1]["sequence"] == revision

    diff_resource = next(
        iter(
            asyncio.run(
                server.read_resource(f"fcp-workflow://runs/{preview.run_id}/diff")
            )
        )
    )
    diff_payload = json.loads(diff_resource.content)
    assert diff_resource.mime_type == "application/json"
    assert diff_payload["run_id"] == preview.run_id

    status_after_resources = asyncio.run(
        server.call_tool("fcpxml_workflow_status", {"run_id": preview.run_id})
    )
    assert status_after_resources.structuredContent["revision"] == revision

    committed = asyncio.run(
        server.call_tool(
            "fcpxml_workflow_commit",
            {
                "run_id": preview.run_id,
                "expected_candidate_sha256": preview.candidate_sha256,
            },
        )
    )
    receipt = WorkflowCommitReceiptV1.model_validate_json(
        json.dumps(committed.structuredContent)
    )
    assert receipt.run_id == preview.run_id
    assert receipt.output_sha256 == preview.candidate_sha256
    assert destination.exists()
    assert len(committed.content[0].text) <= 4096

    cancelled_destination = tmp_path / "cancelled.fcpxml"
    cancel_preview_result = asyncio.run(
        server.call_tool(
            "fcpxml_workflow_prepare",
            _prepare_arguments(source, cancelled_destination, "surface-cancel"),
        )
    )
    cancel_run_id = cancel_preview_result.structuredContent["run_id"]
    cancelled = asyncio.run(
        server.call_tool(
            "fcpxml_workflow_cancel",
            {"run_id": cancel_run_id, "reason": "No longer needed"},
        )
    )
    cancel_result = WorkflowCancelResultV1.model_validate_json(
        json.dumps(cancelled.structuredContent)
    )
    assert cancel_result.state.value == "cancelled"
    assert cancel_result.reason == "No longer needed"
    assert len(cancelled.content[0].text) <= 4096
    assert not cancelled_destination.exists()


def test_cli_mode_rejects_client_hash_but_consumes_existing_durable_approval(
    tmp_path: Path,
):
    server, config, _ = _server(tmp_path, "cli")
    source = tmp_path / "source.fcpxml"
    destination = tmp_path / "destination.fcpxml"
    source.write_text(SOURCE_XML)
    prepared = asyncio.run(
        server.call_tool(
            "fcpxml_workflow_prepare",
            _prepare_arguments(source, destination, "cli-mode"),
        )
    )
    run_id = prepared.structuredContent["run_id"]
    candidate_sha256 = prepared.structuredContent["candidate_sha256"]
    runtime = WorkflowRuntime(config)
    runtime.engine.approve_cli(
        run_id,
        input_stream=io.StringIO(),
        output_stream=io.StringIO(),
        yes=True,
        expect_candidate_sha256=candidate_sha256,
    )

    with pytest.raises(ToolError, match="workflow_state_conflict"):
        asyncio.run(
            server.call_tool(
                "fcpxml_workflow_commit",
                {
                    "run_id": run_id,
                    "expected_candidate_sha256": candidate_sha256,
                },
            )
        )

    assert runtime.status(run_id).state.value == "approved"
    assert not destination.exists()

    committed = asyncio.run(
        server.call_tool("fcpxml_workflow_commit", {"run_id": run_id})
    )
    assert committed.structuredContent["approval_source"] == "cli"
    assert destination.exists()


@pytest.mark.parametrize(
    ("commit_arguments", "error_code"),
    [
        ({}, "approval_required"),
        ({"expected_candidate_sha256": "0" * 64}, "workflow_stale"),
    ],
)
def test_client_commit_rejects_missing_or_stale_candidate_hash_without_mutation(
    tmp_path: Path,
    commit_arguments: dict[str, str],
    error_code: str,
):
    server, _, _ = _server(tmp_path)
    source = tmp_path / "source.fcpxml"
    destination = tmp_path / "destination.fcpxml"
    source.write_text(SOURCE_XML)
    prepared = asyncio.run(
        server.call_tool(
            "fcpxml_workflow_prepare",
            _prepare_arguments(source, destination, f"reject-{error_code}"),
        )
    )
    run_id = prepared.structuredContent["run_id"]

    with pytest.raises(ToolError, match=error_code):
        asyncio.run(
            server.call_tool(
                "fcpxml_workflow_commit",
                {"run_id": run_id, **commit_arguments},
            )
        )

    status = asyncio.run(
        server.call_tool("fcpxml_workflow_status", {"run_id": run_id})
    )
    assert status.structuredContent["state"] == "awaiting_approval"
    assert not destination.exists()
