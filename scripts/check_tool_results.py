"""Reject legacy scalar output schemas from the public MCP tool catalog."""

from __future__ import annotations

import asyncio


async def failures(server) -> list[str]:
    """Return deterministic legacy-result violations from a server catalog."""
    problems = []
    for tool in await server.list_tools():
        schema = tool.outputSchema or {}
        properties = schema.get("properties", {})
        if set(properties) == {"result"}:
            problems.append(f"{tool.name}: legacy scalar schema")
        if schema.get("title") == "LegacyTextResult":
            problems.append(f"{tool.name}: legacy result model")
    return problems


async def _check() -> tuple[int, list[str]]:
    from fcp_mcp.server import mcp

    tools = await mcp.list_tools()
    return len(tools), await failures(mcp)


def main() -> int:
    count, problems = asyncio.run(_check())
    if problems:
        for problem in problems:
            print(problem)
        return 1
    print(f"{count} tools checked; 0 legacy result schemas")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
