import asyncio
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from fcp_mcp.server import mcp


def test_tool_result_gate_script_checks_the_full_catalog():
    environment = os.environ.copy()
    environment["FCP_MCP_PROFILE"] = "full"

    result = subprocess.run(
        [sys.executable, "scripts/check_tool_results.py"],
        cwd=Path(__file__).parents[1],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "93 tools checked; 0 legacy result schemas\n"


def test_full_catalog_has_no_legacy_scalar_result_schema():
    from scripts.check_tool_results import failures

    tools = asyncio.run(mcp.list_tools())

    assert len(tools) == 93
    assert asyncio.run(failures(mcp)) == []


def test_gate_reports_both_legacy_schema_shapes():
    from scripts.check_tool_results import failures

    @dataclass
    class Tool:
        name: str
        output_schema: dict

    class FakeServer:
        async def list_tools(self):
            return [
                Tool(
                    "by_properties",
                    {
                        "title": "SomethingElse",
                        "type": "object",
                        "properties": {"result": {"type": "string"}},
                    },
                ),
                Tool(
                    "by_title",
                    {
                        "title": "LegacyTextResult",
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                    },
                ),
            ]

    assert asyncio.run(failures(FakeServer(), expected_count=2)) == [
        "by_properties: legacy scalar schema",
        "by_title: legacy result model",
    ]


def _typed_schema():
    return {
        "title": "TypedResult",
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
    }


def test_gate_rejects_catalog_count_other_than_93():
    from scripts.check_tool_results import failures

    @dataclass
    class Tool:
        name: str
        output_schema: dict

    class FakeServer:
        async def list_tools(self):
            return [
                Tool(f"tool_{index}", _typed_schema())
                for index in range(92)
            ]

    assert asyncio.run(failures(FakeServer())) == [
        "catalog: expected 93 tools, got 92"
    ]


def test_gate_rejects_missing_and_invalid_output_schemas():
    from scripts.check_tool_results import failures

    @dataclass
    class Tool:
        name: str
        output_schema: object

    class FakeServer:
        async def list_tools(self):
            return [
                Tool("missing", None),
                Tool("invalid", []),
                Tool(
                    "invalid_properties",
                    {"type": "object", "properties": []},
                ),
            ]

    assert asyncio.run(failures(FakeServer(), expected_count=3)) == [
        "missing: missing output schema",
        "invalid: invalid output schema",
        "invalid_properties: invalid output schema",
    ]


def test_gate_resolves_local_refs_and_root_compositions():
    from scripts.check_tool_results import failures

    @dataclass
    class Tool:
        name: str
        output_schema: dict

    legacy_definition = {
        "type": "object",
        "properties": {"result": {"type": "string"}},
        "required": ["result"],
    }

    class FakeServer:
        async def list_tools(self):
            return [
                Tool(
                    "local_ref",
                    {
                        "$ref": "#/$defs/Legacy",
                        "$defs": {"Legacy": legacy_definition},
                    },
                ),
                Tool(
                    "composed_ref",
                    {
                        "allOf": [{"$ref": "#/$defs/Legacy"}],
                        "$defs": {"Legacy": legacy_definition},
                    },
                ),
                Tool(
                    "outer_legacy_title",
                    {
                        "title": "LegacyTextResult",
                        "$ref": "#/$defs/Typed",
                        "$defs": {"Typed": _typed_schema()},
                    },
                ),
                Tool(
                    "nested_domain_record",
                    {
                        "type": "object",
                        "properties": {
                            "records": {
                                "type": "array",
                                "items": legacy_definition,
                            }
                        },
                    },
                ),
            ]

    assert asyncio.run(failures(FakeServer(), expected_count=4)) == [
        "local_ref: legacy scalar schema",
        "composed_ref: legacy scalar schema",
        "outer_legacy_title: legacy result model",
    ]


def test_gate_rejects_unresolved_and_external_refs_without_fetching():
    from scripts.check_tool_results import failures

    @dataclass
    class Tool:
        name: str
        output_schema: dict

    class FakeServer:
        async def list_tools(self):
            return [
                Tool("unresolved", {"$ref": "#/$defs/Missing"}),
                Tool("external", {"$ref": "https://example.com/schema.json"}),
                Tool(
                    "cyclic",
                    {
                        "$ref": "#/$defs/Cycle",
                        "$defs": {
                            "Cycle": {
                                "allOf": [{"$ref": "#/$defs/Cycle"}]
                            }
                        },
                    },
                ),
            ]

    assert asyncio.run(failures(FakeServer(), expected_count=3)) == [
        "unresolved: invalid output schema: unresolved local ref #/$defs/Missing",
        "external: invalid output schema: external refs are unsupported",
        "cyclic: invalid output schema: cyclic local ref #/$defs/Cycle",
    ]


def test_script_main_returns_nonzero_for_catalog_or_schema_failure(
    monkeypatch,
    capsys,
):
    import scripts.check_tool_results as gate

    async def fake_check():
        return 92, ["catalog: expected 93 tools, got 92"]

    monkeypatch.setattr(gate, "_check", fake_check)

    assert gate.main() == 1
    assert capsys.readouterr().out == "catalog: expected 93 tools, got 92\n"
