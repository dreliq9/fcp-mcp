from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fcp_mcp.server import mcp

OUTPUT = Path("tests/contracts/v0_2_1_catalog.json")


async def snapshot() -> dict[str, object]:
    tools = {}
    for tool in await mcp.list_tools():
        tools[tool.name] = {
            "input_schema": tool.input_schema,
            "output_schema": tool.output_schema,
            "annotations": tool.annotations.model_dump(
                by_alias=True, exclude_none=True
            ),
        }
    prompts = sorted(prompt.name for prompt in await mcp.list_prompts())
    return {"tools": dict(sorted(tools.items())), "prompts": prompts}


def main() -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        json.dumps(asyncio.run(snapshot()), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
