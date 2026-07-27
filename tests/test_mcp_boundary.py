from __future__ import annotations

import asyncio
import inspect
from typing import Annotated, get_args, get_origin

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import CallToolResult
from pydantic import BaseModel, ConfigDict

from fcp_mcp.config import RuntimeConfig
from fcp_mcp.contracts import ErrorCode, FCPMCPError
from fcp_mcp.mcp_boundary import build_mcp_server
from fcp_mcp.profiles import ToolClass
from fcp_mcp.registry import PromptRegistry, ToolRegistry
from fcp_mcp.result_models.common import ToolOutcome
from fcp_mcp.tool_metadata import OFFLINE_READ


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
    assert result.structuredContent == {"schema_version": "1", "count": 2}
    schema = asyncio.run(server.list_tools())[0].outputSchema
    assert schema["properties"]["count"]["type"] == "integer"


def test_boundary_rejects_structured_content_that_misses_result_schema(tmp_path):
    tools = ToolRegistry()

    @tools.tool(tool_class=ToolClass.INSPECT, result_model=CountResult)
    def invalid_count() -> ToolOutcome[CountResult]:
        return ToolOutcome(  # type: ignore[arg-type]
            text="not a count",
            structured={"schema_version": "1", "count": "wrong"},
        )

    server = build_mcp_server(_config(tmp_path), tools, PromptRegistry())
    with pytest.raises(ToolError, match="invalid_arguments"):
        asyncio.run(server.call_tool("invalid_count", {}))


def test_boundary_coerces_legacy_text_without_changing_text(tmp_path):
    tools = ToolRegistry()

    @tools.tool(tool_class=ToolClass.INSPECT)
    def legacy() -> str:
        return "legacy text"

    server = build_mcp_server(_config(tmp_path), tools, PromptRegistry())
    result = asyncio.run(server.call_tool("legacy", {}))
    assert result.content[0].text == "legacy text"
    assert result.structuredContent == {
        "schema_version": "1",
        "result": "legacy text",
    }
    assert asyncio.run(server.list_tools())[0].outputSchema == {
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
