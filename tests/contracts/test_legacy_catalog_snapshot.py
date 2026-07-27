import asyncio
import json
from pathlib import Path

import tomllib

from fcp_mcp.server import mcp


def test_phase_1_dependencies_are_bounded():
    project = tomllib.loads(Path("pyproject.toml").read_text())
    assert "platformdirs>=4.11,<5" in project["project"]["dependencies"]
    assert "hypothesis>=6.161,<7" in project["project"]["optional-dependencies"]["dev"]


def test_v021_snapshot_matches_names_inputs_and_annotations():
    snapshot = json.loads(
        Path("tests/contracts/v0_2_1_catalog.json").read_text(encoding="utf-8")
    )
    tools = {tool.name: tool for tool in asyncio.run(mcp.list_tools())}
    assert len(snapshot["tools"]) == len(tools) == 89
    assert snapshot["prompts"] == sorted(
        prompt.name for prompt in asyncio.run(mcp.list_prompts())
    )
    for name, frozen in snapshot["tools"].items():
        tool = tools[name]
        assert frozen["output_schema"]
        assert frozen["input_schema"] == tool.inputSchema
        assert frozen["annotations"] == tool.annotations.model_dump(
            by_alias=True, exclude_none=True
        )
