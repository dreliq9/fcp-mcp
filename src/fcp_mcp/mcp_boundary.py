"""MCP SDK adapter for SDK-neutral tool and prompt registries."""

from __future__ import annotations

import functools
import inspect
import json
import logging
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, ValidationError

from .config import RuntimeConfig
from .contracts import ErrorCode, FCPMCPError
from .registry import PromptRegistry, ToolDefinition, ToolRegistry
from .result_models.common import LegacyTextResult, ToolOutcome
from .tool_metadata import SafetyHints

logger = logging.getLogger(__name__)


class FCPFastMCP(FastMCP):
    """FastMCP boundary that preserves domain codes and sanitizes defects."""

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> Any:
        try:
            return await super().call_tool(name, arguments)
        except ToolError as error:
            cause = error.__cause__
            if isinstance(cause, FCPMCPError):
                raise
            if isinstance(cause, ValidationError):
                raise ToolError(
                    f"Error executing tool {name}: "
                    f"{ErrorCode.INVALID_ARGUMENTS.value}: "
                    "Tool arguments did not match the schema"
                ) from error
            logger.exception(
                "Unexpected failure while executing tool %s",
                name,
            )
            raise ToolError(
                f"Error executing tool {name}: "
                f"{ErrorCode.INTERNAL_ERROR.value}: Unexpected internal failure"
            ) from error


def _tool_annotations(hints: SafetyHints) -> ToolAnnotations:
    return ToolAnnotations(
        readOnlyHint=hints.read_only,
        destructiveHint=hints.destructive,
        idempotentHint=hints.idempotent,
        openWorldHint=hints.open_world,
    )


def _text_for_model(model: BaseModel) -> str:
    return model.model_dump_json(indent=2)


def coerce_outcome(
    returned: Any,
    result_model: type[BaseModel],
) -> ToolOutcome[Any]:
    if isinstance(returned, ToolOutcome):
        structured = result_model.model_validate(returned.structured)
        return ToolOutcome(text=returned.text, structured=structured)
    if result_model is LegacyTextResult and isinstance(returned, str):
        return ToolOutcome(
            text=returned,
            structured=LegacyTextResult(result=returned),
        )
    structured = result_model.model_validate(returned)
    text = (
        _text_for_model(returned)
        if isinstance(returned, BaseModel)
        else json.dumps(structured.model_dump(mode="json"), indent=2)
    )
    return ToolOutcome(text=text, structured=structured)


def _invoker(definition: ToolDefinition):
    @functools.wraps(definition.handler)
    async def invoke(**arguments):
        returned = definition.handler(**arguments)
        if inspect.isawaitable(returned):
            returned = await returned
        outcome = coerce_outcome(returned, definition.result_model)
        return CallToolResult(
            content=[TextContent(type="text", text=outcome.text)],
            structuredContent=outcome.structured.model_dump(mode="json"),
        )

    signature = inspect.signature(definition.handler)
    invoke.__signature__ = signature.replace(  # type: ignore[attr-defined]
        return_annotation=Annotated[CallToolResult, definition.result_model]
    )
    return invoke


def _legacy_output_schema(name: str) -> dict[str, Any]:
    return {
        "properties": {
            "result": {
                "title": "Result",
                "type": "string",
            },
        },
        "required": ["result"],
        "title": f"{name}Output",
        "type": "object",
    }


def build_mcp_server(
    config: RuntimeConfig,
    tools: ToolRegistry,
    prompts: PromptRegistry,
) -> FCPFastMCP:
    server = FCPFastMCP(
        "fcp-mcp",
        instructions=(
            "fcp-mcp v0.2.1 — FCPXML engine + live FCP control + media analysis "
            "+ puppet animation. 89 tools across 12 categories and 5 prompts."
        ),
    )
    visible_tools = tools.for_profile(config.profile)
    for definition in visible_tools:
        invoke = _invoker(definition)
        server.tool(
            name=definition.name,
            description=definition.description,
            annotations=_tool_annotations(definition.safety_hints),
        )(invoke)
        if definition.result_model is LegacyTextResult:
            registered = server._tool_manager.get_tool(definition.name)
            registered.fn_metadata.output_schema = _legacy_output_schema(
                definition.name
            )

    visible_names = {definition.name for definition in visible_tools}
    for definition in prompts.for_tools(visible_names):
        server.prompt(
            name=definition.name,
            description=definition.description,
        )(definition.handler)
    return server


__all__ = ["FCPFastMCP", "build_mcp_server", "coerce_outcome"]
