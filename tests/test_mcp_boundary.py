from __future__ import annotations

import asyncio
import inspect
import threading
from typing import Annotated, get_args, get_origin

import pytest
from mcp import Client
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import CallToolResult
from pydantic import BaseModel, ConfigDict

from fcp_mcp.config import RuntimeConfig
from fcp_mcp.contracts import ErrorCode, FCPMCPError
from fcp_mcp.mcp_boundary import build_mcp_server
from fcp_mcp.profiles import Profile, ToolClass
from fcp_mcp.registry import PromptRegistry, ResourceRegistry, ToolRegistry
from fcp_mcp.result_models.common import ToolOutcome
from fcp_mcp.tool_metadata import OFFLINE_READ
from fcp_mcp.version import package_version


class CountResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: str = "1"
    count: int


def _config(tmp_path, profile: str = "inspect") -> RuntimeConfig:
    return RuntimeConfig.from_env(
        {
            "FCP_MCP_OUTPUT_DIR": str(tmp_path),
            "FCP_MCP_PROFILE": profile,
        },
        home=tmp_path,
    )


def test_boundary_preserves_text_and_validates_structured_content(tmp_path):
    tools = ToolRegistry()

    @tools.tool(tool_class=ToolClass.INSPECT, result_model=CountResult)
    def count() -> ToolOutcome[CountResult]:
        return ToolOutcome(text="count=2", structured=CountResult(count=2))

    server = build_mcp_server(_config(tmp_path), tools, PromptRegistry())
    result = asyncio.run(server.call_tool("count", {}))
    assert isinstance(result, CallToolResult)
    assert result.content[0].text == "count=2"
    assert result.structured_content == {"schema_version": "1", "count": 2}
    schema = asyncio.run(server.list_tools())[0].output_schema
    assert schema["properties"]["count"]["type"] == "integer"


def test_boundary_sanitizes_structured_result_validation_defects(
    tmp_path,
    caplog,
):
    tools = ToolRegistry()

    @tools.tool(tool_class=ToolClass.INSPECT, result_model=CountResult)
    def invalid_count() -> ToolOutcome[CountResult]:
        return ToolOutcome(  # type: ignore[arg-type]
            text="not a count",
            structured={"schema_version": "1", "count": "wrong"},
        )

    server = build_mcp_server(_config(tmp_path), tools, PromptRegistry())
    with pytest.raises(ToolError) as caught:
        asyncio.run(server.call_tool("invalid_count", {}))

    assert "internal_error: Unexpected internal failure" in str(caught.value)
    assert "wrong" not in str(caught.value)
    assert "valid integer" not in str(caught.value)
    assert "validation error" in caplog.text.lower()


def test_boundary_coerces_legacy_text_without_changing_text(tmp_path):
    tools = ToolRegistry()

    @tools.tool(tool_class=ToolClass.INSPECT)
    def legacy() -> str:
        return "legacy text"

    server = build_mcp_server(_config(tmp_path), tools, PromptRegistry())
    result = asyncio.run(server.call_tool("legacy", {}))
    assert result.content[0].text == "legacy text"
    assert result.structured_content == {
        "schema_version": "1",
        "result": "legacy text",
    }
    assert asyncio.run(server.list_tools())[0].output_schema == {
        "properties": {
            "result": {"title": "Result", "type": "string"},
        },
        "required": ["result"],
        "title": "legacyOutput",
        "type": "object",
    }


def test_boundary_invokes_sync_and_async_handlers(tmp_path):
    tools = ToolRegistry()

    @tools.tool(tool_class=ToolClass.INSPECT, result_model=CountResult)
    def synchronous(value: int) -> ToolOutcome[CountResult]:
        return ToolOutcome(
            text=f"sync={value}",
            structured=CountResult(count=value),
        )

    @tools.tool(tool_class=ToolClass.INSPECT, result_model=CountResult)
    async def asynchronous(value: int) -> ToolOutcome[CountResult]:
        await asyncio.sleep(0)
        return ToolOutcome(
            text=f"async={value}",
            structured=CountResult(count=value),
        )

    server = build_mcp_server(_config(tmp_path), tools, PromptRegistry())
    sync_result = asyncio.run(server.call_tool("synchronous", {"value": 3}))
    async_result = asyncio.run(server.call_tool("asynchronous", {"value": 4}))
    assert sync_result.content[0].text == "sync=3"
    assert async_result.content[0].text == "async=4"


def test_boundary_copies_inputs_and_replaces_only_return_annotation(tmp_path):
    tools = ToolRegistry()

    @tools.tool(tool_class=ToolClass.INSPECT, result_model=CountResult)
    def count(value: int, label: str = "items") -> str:
        return f"{label}={value}"

    server = build_mcp_server(_config(tmp_path), tools, PromptRegistry())
    registered = server._tool_manager.get_tool("count")
    signature = inspect.signature(registered.fn)
    assert list(signature.parameters) == ["value", "label"]
    assert signature.parameters["value"].annotation == "int"
    assert signature.parameters["label"].default == "items"
    assert get_origin(signature.return_annotation) is Annotated
    assert get_args(signature.return_annotation) == (CallToolResult, CountResult)


def test_boundary_filters_tools_and_prompts_by_exact_dependencies(tmp_path):
    tools = ToolRegistry()
    prompts = PromptRegistry()

    @tools.tool(tool_class=ToolClass.INSPECT)
    def inspect_one() -> str:
        return "inspect"

    @tools.tool(tool_class=ToolClass.LIVE_WRITE)
    def live_one() -> str:
        return "live"

    @prompts.prompt(name="inspect-flow", dependencies={"inspect_one"})
    def inspect_flow() -> str:
        return "inspect flow"

    @prompts.prompt(name="live-flow", dependencies={"inspect_one", "live_one"})
    def live_flow() -> str:
        return "live flow"

    inspect_server = build_mcp_server(_config(tmp_path), tools, prompts)
    assert [tool.name for tool in asyncio.run(inspect_server.list_tools())] == [
        "inspect_one"
    ]
    assert [prompt.name for prompt in asyncio.run(inspect_server.list_prompts())] == [
        "inspect-flow"
    ]

    full_server = build_mcp_server(_config(tmp_path, "full"), tools, prompts)
    assert [tool.name for tool in asyncio.run(full_server.list_tools())] == [
        "inspect_one",
        "live_one",
    ]
    assert [prompt.name for prompt in asyncio.run(full_server.list_prompts())] == [
        "inspect-flow",
        "live-flow",
    ]


def test_boundary_translates_safety_hints(tmp_path):
    tools = ToolRegistry()

    @tools.tool(
        tool_class=ToolClass.INSPECT,
        safety_hints=OFFLINE_READ,
    )
    def inspect_one() -> str:
        return "inspect"

    server = build_mcp_server(_config(tmp_path), tools, PromptRegistry())
    annotations = asyncio.run(server.list_tools())[0].annotations
    assert annotations.model_dump(by_alias=True, exclude_none=True) == {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "expected_protocol"),
    [
        ("auto", "2026-07-28"),
        ("legacy", "2025-11-25"),
    ],
)
async def test_public_client_preserves_identity_and_dual_channel_across_eras(
    tmp_path,
    mode,
    expected_protocol,
):
    tools = ToolRegistry()

    @tools.tool(tool_class=ToolClass.INSPECT, result_model=CountResult)
    def count() -> ToolOutcome[CountResult]:
        return ToolOutcome(text="count=2", structured=CountResult(count=2))

    server = build_mcp_server(_config(tmp_path), tools, PromptRegistry())
    async with Client(server, mode=mode, raise_exceptions=True) as client:
        result = await client.call_tool("count", {})

        assert str(client.protocol_version) == expected_protocol
        assert client.server_info.name == "fcp-mcp"
        assert client.server_info.version == package_version()
        assert result.content[0].text == "count=2"
        assert result.structured_content == {"schema_version": "1", "count": 2}
        assert result.is_error is False


@pytest.mark.asyncio
async def test_python_attributes_are_snake_case_and_wire_json_is_camel_case(tmp_path):
    tools = ToolRegistry()

    @tools.tool(tool_class=ToolClass.INSPECT, result_model=CountResult)
    def count() -> ToolOutcome[CountResult]:
        return ToolOutcome(text="count=2", structured=CountResult(count=2))

    server = build_mcp_server(_config(tmp_path), tools, PromptRegistry())
    async with Client(server, raise_exceptions=True) as client:
        listed = await client.list_tools()
        result = await client.call_tool("count", {})

    assert listed.tools[0].output_schema["properties"]["count"]["type"] == "integer"
    assert result.structured_content["count"] == 2
    assert result.model_dump(by_alias=True, mode="json")["structuredContent"][
        "count"
    ] == 2


@pytest.mark.asyncio
async def test_sync_resource_handlers_retain_v2_worker_thread_execution(tmp_path):
    resources = ResourceRegistry()
    handler_threads: list[int] = []

    @resources.resource(
        "probe://thread/{name}",
        name="thread-probe",
        mime_type="text/plain",
        profiles={Profile.WORKFLOW},
    )
    def thread_probe(name: str) -> str:
        handler_threads.append(threading.get_ident())
        return name

    server = build_mcp_server(
        _config(tmp_path, "workflow"),
        ToolRegistry(),
        PromptRegistry(),
        resources,
    )
    event_loop_thread = threading.get_ident()
    async with Client(server, raise_exceptions=True) as client:
        result = await client.read_resource("probe://thread/worker")

    assert result.contents[0].text == "worker"
    assert handler_threads
    assert handler_threads[0] != event_loop_thread


@pytest.mark.asyncio
async def test_boundary_preserves_coded_domain_failures(tmp_path):
    tools = ToolRegistry()

    @tools.tool(tool_class=ToolClass.INSPECT)
    def missing() -> str:
        raise FCPMCPError(ErrorCode.TARGET_NOT_FOUND, "clip missing")

    server = build_mcp_server(_config(tmp_path), tools, PromptRegistry())
    with pytest.raises(ToolError, match="target_not_found"):
        await server.call_tool("missing", {})


@pytest.mark.asyncio
async def test_boundary_codes_schema_validation_as_invalid_arguments(tmp_path):
    tools = ToolRegistry()

    @tools.tool(tool_class=ToolClass.INSPECT)
    def requires_count(count: int) -> str:
        return str(count)

    server = build_mcp_server(_config(tmp_path), tools, PromptRegistry())
    with pytest.raises(ToolError, match="invalid_arguments"):
        await server.call_tool("requires_count", {"count": "not-an-integer"})


@pytest.mark.asyncio
async def test_boundary_sanitizes_unexpected_failures(tmp_path, caplog):
    tools = ToolRegistry()

    @tools.tool(tool_class=ToolClass.INSPECT)
    def explode() -> str:
        raise RuntimeError("private implementation detail")

    server = build_mcp_server(_config(tmp_path), tools, PromptRegistry())
    with pytest.raises(ToolError) as caught:
        await server.call_tool("explode", {})

    assert "internal_error: Unexpected internal failure" in str(caught.value)
    assert "private implementation detail" not in str(caught.value)
    assert "private implementation detail" in caplog.text
