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
    assert result.stdout == "89 tools checked; 0 legacy result schemas\n"


def test_full_catalog_has_no_legacy_scalar_result_schema():
    from scripts.check_tool_results import failures

    tools = asyncio.run(mcp.list_tools())

    assert len(tools) == 89
    assert asyncio.run(failures(mcp)) == []


def test_gate_reports_both_legacy_schema_shapes():
    from scripts.check_tool_results import failures

    @dataclass
    class Tool:
        name: str
        outputSchema: dict

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

    assert asyncio.run(failures(FakeServer())) == [
        "by_properties: legacy scalar schema",
        "by_title: legacy result model",
    ]
