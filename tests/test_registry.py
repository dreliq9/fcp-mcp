from __future__ import annotations

import subprocess
import sys

import pytest

from fcp_mcp.profiles import Profile, ToolClass
from fcp_mcp.registry import PromptRegistry, ToolRegistry
from fcp_mcp.result_models.common import LegacyTextResult
from fcp_mcp.tool_metadata import OFFLINE_READ


def test_registry_filters_in_registration_order():
    registry = ToolRegistry()

    @registry.tool(tool_class=ToolClass.INSPECT)
    def inspect_one() -> str:
        return "ok"

    @registry.tool(tool_class=ToolClass.LIVE_WRITE)
    def live_one() -> str:
        return "changed"

    assert [item.name for item in registry.for_profile(Profile.INSPECT)] == [
        "inspect_one"
    ]
    assert [item.name for item in registry.for_profile(Profile.FULL)] == [
        "inspect_one",
        "live_one",
    ]
    assert registry.definitions["inspect_one"].result_model is LegacyTextResult


def test_registry_module_does_not_import_mcp():
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import fcp_mcp.registry; "
                "print(any(name == 'mcp' or name.startswith('mcp.') "
                "for name in sys.modules))"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert completed.stdout.strip() == "False"


def test_tool_registry_rejects_duplicate_names():
    registry = ToolRegistry()

    @registry.tool(tool_class=ToolClass.INSPECT, name="same")
    def first() -> str:
        return "first"

    with pytest.raises(ValueError, match="Duplicate tool name: same"):

        @registry.tool(tool_class=ToolClass.INSPECT, name="same")
        def second() -> str:
            return "second"


def test_tool_definition_preserves_sdk_neutral_metadata():
    registry = ToolRegistry()

    @registry.tool(
        tool_class=ToolClass.INSPECT,
        safety_hints=OFFLINE_READ,
        description="Read one thing.",
    )
    def read_one(value: int) -> str:
        return str(value)

    definition = registry.definitions["read_one"]
    assert definition.handler is read_one
    assert definition.description == "Read one thing."
    assert definition.safety_hints == OFFLINE_READ
    assert definition.registration_order == 0


def test_prompt_registry_rejects_duplicates_and_freezes_exact_dependencies():
    registry = PromptRegistry()
    supplied_dependencies = {"first", "second"}

    @registry.prompt(
        name="flow",
        description="Run two tools.",
        dependencies=supplied_dependencies,
    )
    def flow() -> str:
        return "flow"

    supplied_dependencies.add("late")
    definition = registry.definitions["flow"]
    assert definition.dependencies == frozenset({"first", "second"})
    with pytest.raises(AttributeError):
        definition.dependencies.add("third")  # type: ignore[attr-defined]

    with pytest.raises(ValueError, match="Duplicate prompt name: flow"):

        @registry.prompt(name="flow", dependencies=set())
        def duplicate() -> str:
            return "duplicate"
