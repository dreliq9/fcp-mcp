"""Wire-level contracts for every static MCP capability profile."""

from __future__ import annotations

import os
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, TypeVar

import pytest
from mcp import Client, StdioServerParameters
from mcp.client.stdio import stdio_client

PageT = TypeVar("PageT")

EXPECTED = {
    "inspect": {
        "tool_count": 30,
        "prompts": {"qc-check", "youtube-chapters"},
        "resource_count": 0,
    },
    "workflow": {
        "tool_count": 34,
        "prompts": {"qc-check", "cleanup", "youtube-chapters"},
        "resource_count": 3,
    },
    "edit": {
        "tool_count": 75,
        "prompts": {
            "qc-check",
            "rough-cut",
            "cleanup",
            "youtube-chapters",
            "beat-sync",
        },
        "resource_count": 3,
    },
    "full": {
        "tool_count": 95,
        "prompts": {
            "qc-check",
            "rough-cut",
            "cleanup",
            "youtube-chapters",
            "beat-sync",
        },
        "resource_count": 3,
    },
}


async def _collect(
    fetch: Callable[[str | None], Awaitable[PageT]],
    field: str,
) -> list[Any]:
    items: list[Any] = []
    cursor: str | None = None
    while True:
        page = await fetch(cursor)
        items.extend(getattr(page, field))
        cursor = page.next_cursor
        if cursor is None:
            return items


def _parameters(tmp_path: Path, profile: str) -> StdioServerParameters:
    environment = {
        **os.environ,
        "FCP_MCP_PROFILE": profile,
        "FCP_MCP_OUTPUT_DIR": str(tmp_path),
        "FCP_MCP_ALLOWED_ROOTS": str(tmp_path),
        "FCP_MCP_STATE_DIR": str(tmp_path / "state"),
        "FCP_MCP_ENABLE_LIVE_CONTROL": "0",
    }
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "fcp_mcp"],
        env=environment,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", tuple(EXPECTED))
async def test_stdio_profiles_expose_exact_static_catalogs(
    tmp_path: Path,
    profile: str,
):
    expected = EXPECTED[profile]
    async with Client(
        stdio_client(_parameters(tmp_path, profile)),
        mode="auto",
        raise_exceptions=True,
    ) as client:
        tools = await _collect(
            lambda cursor: client.list_tools(cursor=cursor),
            "tools",
        )
        prompts = await _collect(
            lambda cursor: client.list_prompts(cursor=cursor),
            "prompts",
        )
        templates = await _collect(
            lambda cursor: client.list_resource_templates(cursor=cursor),
            "resource_templates",
        )
        instructions = client.instructions

    assert len(tools) == expected["tool_count"]
    assert {prompt.name for prompt in prompts} == expected["prompts"]
    assert len(templates) == expected["resource_count"]
    assert instructions is not None
    assert f"profile={profile}" in instructions
    assert (
        f"catalog={len(tools)} tools/{len(prompts)} prompts/{len(templates)} resources"
        in instructions
    )
    assert "approval=client" in instructions
    assert "live_control=disabled" in instructions
    assert not (tmp_path / "state").exists()


@pytest.mark.asyncio
async def test_full_profile_exposes_but_gates_live_actions(tmp_path: Path):
    async with Client(
        stdio_client(_parameters(tmp_path, "full")),
        mode="auto",
        raise_exceptions=True,
    ) as client:
        tools = await client.list_tools()
        result = await client.call_tool("fcp_is_running")

    assert "fcp_is_running" in {tool.name for tool in tools.tools}
    assert result.is_error is True
    assert "live_control_disabled" in result.content[0].text
