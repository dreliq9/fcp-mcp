"""Executable contracts for published MCP tool-call examples."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from fcp_mcp.server import mcp
from scripts.check_contracts import (
    ContractBlockError,
    extract_tool_call_blocks,
    validate_call,
)

ROOT = Path(__file__).resolve().parents[1]


def test_extract_tool_call_blocks_parses_and_numbers_calls():
    text = """\
Before.

```tool-call
{"name":"first","arguments":{"path":"one.fcpxml"}}
```

```python
print("ignored")
```

```tool-call
{"name":"second","arguments":{}}
```
"""

    assert extract_tool_call_blocks(text) == [
        (1, {"name": "first", "arguments": {"path": "one.fcpxml"}}),
        (2, {"name": "second", "arguments": {}}),
    ]


def test_extract_tool_call_blocks_rejects_invalid_json():
    with pytest.raises(ContractBlockError, match="block 1"):
        extract_tool_call_blocks("```tool-call\n{not json}\n```\n")


def test_validate_call_uses_strict_draft_2020_12_schema():
    catalog = {
        "inspect": SimpleNamespace(
            name="inspect",
            inputSchema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        )
    }

    assert validate_call(
        catalog,
        {"name": "inspect", "arguments": {"path": "show.fcpxml"}},
    ) == []
    assert validate_call(
        catalog,
        {"name": "inspect", "arguments": {}},
    ) == ["arguments: 'path' is a required property"]
    assert validate_call(
        catalog,
        {
            "name": "inspect",
            "arguments": {"path": "show.fcpxml", "target": "wrong"},
        },
    ) == ["arguments: Additional properties are not allowed ('target' was unexpected)"]


def test_validate_call_rejects_unknown_tool_and_shape():
    catalog: dict[str, object] = {}

    assert validate_call(catalog, {"name": "missing", "arguments": {}}) == [
        "name: unknown tool 'missing'"
    ]
    assert validate_call(catalog, {"name": 7, "arguments": []}) == [
        "name: must be a string",
        "arguments: must be an object",
    ]


def test_published_tool_calls_match_live_catalog():
    catalog = {tool.name: tool for tool in asyncio.run(mcp.list_tools())}
    counts: dict[str, int] = {}
    failures: list[str] = []

    for relative_path in ("WORKFLOWS.md", "LLM_GUIDE.md"):
        blocks = extract_tool_call_blocks((ROOT / relative_path).read_text())
        counts[relative_path] = len(blocks)
        for block_number, call in blocks:
            for error in validate_call(catalog, call):
                failures.append(f"{relative_path}: block {block_number}: {error}")

    assert counts["WORKFLOWS.md"] >= 25
    assert counts["LLM_GUIDE.md"] >= 10
    assert failures == []
