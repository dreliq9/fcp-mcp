"""SDK-neutral tool and prompt definitions."""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, TypeVar, get_type_hints

from pydantic import BaseModel

from .profiles import PROFILE_TOOL_CLASSES, Profile, ToolClass
from .result_models.common import LegacyTextResult
from .tool_metadata import SafetyHints

HandlerT = TypeVar("HandlerT", bound=Callable[..., Any])
ResultModel = type[BaseModel]

DEFAULT_SAFETY_HINTS = SafetyHints(
    read_only=False,
    destructive=False,
    idempotent=False,
    open_world=False,
)


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    handler: Callable[..., Any]
    tool_class: ToolClass
    safety_hints: SafetyHints
    result_model: ResultModel
    description: str
    registration_order: int


@dataclass(frozen=True)
class PromptDefinition:
    name: str
    handler: Callable[..., Any]
    description: str
    dependencies: frozenset[str]
    registration_order: int


def _inferred_result_model(handler: Callable[..., Any]) -> ResultModel:
    try:
        return_annotation = get_type_hints(handler).get(
            "return",
            inspect.Signature.empty,
        )
    except (NameError, TypeError):
        return_annotation = inspect.signature(handler).return_annotation
    if (
        isinstance(return_annotation, type)
        and issubclass(return_annotation, BaseModel)
    ):
        return return_annotation
    return LegacyTextResult


class ToolRegistry:
    def __init__(self) -> None:
        self._definitions: dict[str, ToolDefinition] = {}

    @property
    def definitions(self) -> Mapping[str, ToolDefinition]:
        return MappingProxyType(self._definitions)

    def tool(
        self,
        *,
        tool_class: ToolClass,
        safety_hints: SafetyHints = DEFAULT_SAFETY_HINTS,
        result_model: ResultModel | None = None,
        name: str | None = None,
        description: str | None = None,
    ) -> Callable[[HandlerT], HandlerT]:
        def decorator(handler: HandlerT) -> HandlerT:
            tool_name = name or handler.__name__
            if tool_name in self._definitions:
                raise ValueError(f"Duplicate tool name: {tool_name}")
            self._definitions[tool_name] = ToolDefinition(
                name=tool_name,
                handler=handler,
                tool_class=tool_class,
                safety_hints=safety_hints,
                result_model=result_model or _inferred_result_model(handler),
                description=description if description is not None else handler.__doc__ or "",
                registration_order=len(self._definitions),
            )
            return handler

        return decorator

    def for_profile(self, profile: Profile) -> tuple[ToolDefinition, ...]:
        classes = PROFILE_TOOL_CLASSES[profile]
        return tuple(
            definition
            for definition in self._definitions.values()
            if definition.tool_class in classes
        )


class PromptRegistry:
    def __init__(self) -> None:
        self._definitions: dict[str, PromptDefinition] = {}

    @property
    def definitions(self) -> Mapping[str, PromptDefinition]:
        return MappingProxyType(self._definitions)

    def prompt(
        self,
        *,
        name: str | None = None,
        description: str | None = None,
        dependencies: Iterable[str],
    ) -> Callable[[HandlerT], HandlerT]:
        def decorator(handler: HandlerT) -> HandlerT:
            prompt_name = name or handler.__name__
            if prompt_name in self._definitions:
                raise ValueError(f"Duplicate prompt name: {prompt_name}")
            self._definitions[prompt_name] = PromptDefinition(
                name=prompt_name,
                handler=handler,
                description=(
                    description if description is not None else handler.__doc__ or ""
                ),
                dependencies=frozenset(dependencies),
                registration_order=len(self._definitions),
            )
            return handler

        return decorator

    def for_tools(self, tool_names: Iterable[str]) -> tuple[PromptDefinition, ...]:
        visible = frozenset(tool_names)
        return tuple(
            definition
            for definition in self._definitions.values()
            if definition.dependencies <= visible
        )


__all__ = [
    "PromptDefinition",
    "PromptRegistry",
    "ToolDefinition",
    "ToolRegistry",
]
