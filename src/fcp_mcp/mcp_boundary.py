"""MCP SDK adapter for SDK-neutral tool and prompt registries."""

from __future__ import annotations

import functools
import inspect
import json
import logging
from typing import Annotated, Any, get_type_hints

from mcp import MCPError
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import INTERNAL_ERROR, INVALID_PARAMS, CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, ValidationError

from .config import RuntimeConfig
from .contracts import ErrorCode, FCPMCPError
from .profiles import ToolClass
from .registry import PromptRegistry, ResourceRegistry, ToolDefinition, ToolRegistry
from .result_models.common import LegacyTextResult, ToolOutcome
from .tool_metadata import SafetyHints
from .version import package_version

logger = logging.getLogger(__name__)


class _ResultValidationError(RuntimeError):
    """Marks invalid handler output as an internal implementation defect."""


class FCPFastMCP(MCPServer):
    """MCPServer boundary that preserves domain codes and sanitizes defects."""

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        context: Context[Any, Any] | None = None,
    ) -> Any:
        try:
            return await super().call_tool(name, arguments, context)
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
        read_only_hint=hints.read_only,
        destructive_hint=hints.destructive,
        idempotent_hint=hints.idempotent,
        open_world_hint=hints.open_world,
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


def _invoker(definition: ToolDefinition, config: RuntimeConfig):
    @functools.wraps(definition.handler)
    async def invoke(**arguments):
        if (
            definition.tool_class in {ToolClass.LIVE_READ, ToolClass.LIVE_WRITE}
            and not config.live_control_enabled
        ):
            raise FCPMCPError(
                ErrorCode.LIVE_CONTROL_DISABLED,
                "Set FCP_MCP_ENABLE_LIVE_CONTROL=1 to use live FCP or Compressor tools",
            )
        returned = definition.handler(**arguments)
        if inspect.isawaitable(returned):
            returned = await returned
        try:
            outcome = coerce_outcome(returned, definition.result_model)
            return CallToolResult(
                content=[TextContent(type="text", text=outcome.text)],
                structured_content=outcome.structured.model_dump(mode="json"),
            )
        except ValidationError as error:
            raise _ResultValidationError(
                "Tool result did not match its declared schema"
            ) from error

    signature = inspect.signature(definition.handler)
    try:
        resolved_hints = get_type_hints(definition.handler)
    except (NameError, TypeError):
        resolved_hints = {}
    simple_forward_annotations = {"str", "int", "float", "bool", "bytes", "Any"}
    parameters = [
        parameter.replace(
            annotation=(
                resolved_hints.get(name, parameter.annotation)
                if parameter.annotation not in simple_forward_annotations
                else parameter.annotation
            )
        )
        for name, parameter in signature.parameters.items()
    ]
    invoke.__signature__ = signature.replace(  # type: ignore[attr-defined]
        parameters=parameters,
        return_annotation=Annotated[CallToolResult, definition.result_model]
    )
    return invoke


def _raise_resource_error(error: FCPMCPError) -> None:
    code = (
        INVALID_PARAMS
        if error.code
        in {
            ErrorCode.INVALID_ARGUMENTS,
            ErrorCode.TARGET_NOT_FOUND,
        }
        else INTERNAL_ERROR
    )
    raise MCPError(
        code=code,
        message=str(error),
        data=error.details or None,
    ) from error


def _resource_invoker(handler):
    """Keep coded failures intact without changing v2's threading behavior."""

    if inspect.iscoroutinefunction(handler):

        @functools.wraps(handler)
        async def async_invoke(**arguments):
            try:
                return await handler(**arguments)
            except FCPMCPError as error:
                _raise_resource_error(error)

        return async_invoke

    @functools.wraps(handler)
    def sync_invoke(**arguments):
        try:
            return handler(**arguments)
        except FCPMCPError as error:
            _raise_resource_error(error)

    return sync_invoke


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


def _json_output_validator(result_model: type[BaseModel]):
    """Validate JSON-compatible structured content against strict result models."""

    class JSONOutputValidator:
        @classmethod
        def model_validate(cls, value: Any) -> BaseModel:
            return result_model.model_validate_json(
                json.dumps(value, separators=(",", ":"), ensure_ascii=False)
            )

    return JSONOutputValidator


def build_mcp_server(
    config: RuntimeConfig,
    tools: ToolRegistry,
    prompts: PromptRegistry,
    resources: ResourceRegistry | None = None,
) -> FCPFastMCP:
    visible_tools = tools.for_profile(config.profile)
    visible_names = {definition.name for definition in visible_tools}
    visible_prompts = prompts.for_tools(visible_names)
    visible_resources = resources.for_profile(config.profile) if resources else ()
    live_control = "enabled" if config.live_control_enabled else "disabled"

    server = FCPFastMCP(
        "fcp-mcp",
        version=package_version(),
        instructions=(
            f"fcp-mcp {package_version()}; profile={config.profile.value}; "
            f"catalog={len(visible_tools)} tools/{len(visible_prompts)} prompts/"
            f"{len(visible_resources)} resources; "
            f"approval={config.workflow_approval.value}; live_control={live_control}. "
            "Transactional FCPXML workflows require evidence review and explicit "
            "approval before commit."
        ),
    )
    for definition in visible_tools:
        invoke = _invoker(definition, config)
        server.tool(
            name=definition.name,
            description=definition.description,
            annotations=_tool_annotations(definition.safety_hints),
        )(invoke)
        registered = server._tool_manager.get_tool(definition.name)
        registered.fn_metadata.output_model = _json_output_validator(
            definition.result_model
        )
        if definition.result_model is LegacyTextResult:
            registered.fn_metadata.output_schema = _legacy_output_schema(
                definition.name
            )

    for definition in visible_prompts:
        if not definition.dependencies <= visible_names:
            raise AssertionError(
                f"Prompt {definition.name!r} depends on tools outside profile "
                f"{config.profile.value!r}"
            )
        server.prompt(
            name=definition.name,
            description=definition.description,
        )(definition.handler)
    for definition in visible_resources:
        server.resource(
            definition.uri_template,
            name=definition.name,
            description=definition.description,
            mime_type=definition.mime_type,
        )(_resource_invoker(definition.handler))
    return server


__all__ = ["FCPFastMCP", "build_mcp_server", "coerce_outcome"]
