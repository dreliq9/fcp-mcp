"""Validate documented MCP tool calls against the live public catalog."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from fcp_mcp.profiles import Profile

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PATHS = (ROOT / "WORKFLOWS.md", ROOT / "LLM_GUIDE.md")
TOOL_CALL_BLOCK = re.compile(
    r"^```tool-call[ \t]*\r?\n(.*?)^```[ \t]*$",
    re.DOTALL | re.MULTILINE,
)


class ContractBlockError(ValueError):
    """A tool-call fence could not be decoded as a JSON object."""

    def __init__(self, block_number: int, detail: str) -> None:
        self.block_number = block_number
        self.detail = detail
        super().__init__(f"block {block_number}: {detail}")


def extract_tool_call_blocks(text: str) -> list[tuple[int, dict[str, Any]]]:
    """Return numbered JSON values from every ``tool-call`` fenced block."""
    calls: list[tuple[int, dict[str, Any]]] = []
    for block_number, match in enumerate(TOOL_CALL_BLOCK.finditer(text), start=1):
        try:
            call = json.loads(match.group(1))
        except json.JSONDecodeError as error:
            raise ContractBlockError(
                block_number,
                f"invalid JSON at line {error.lineno}, column {error.colno}: {error.msg}",
            ) from error
        if not isinstance(call, dict):
            raise ContractBlockError(block_number, "top-level JSON value must be an object")
        calls.append((block_number, call))
    return calls


def _catalog_by_name(catalog: Mapping[str, Any] | Iterable[Any]) -> dict[str, Any]:
    if isinstance(catalog, Mapping):
        return dict(catalog)
    return {tool.name: tool for tool in catalog}


def _json_path(parts: Iterable[Any]) -> str:
    suffix = "".join(
        f"[{part}]" if isinstance(part, int) else f".{part}"
        for part in parts
    )
    return f"arguments{suffix}"


def validate_call(
    catalog: Mapping[str, Any] | Iterable[Any],
    call: Mapping[str, Any],
) -> list[str]:
    """Return deterministic validation errors for one documented tool call."""
    errors: list[str] = []
    name = call.get("name")
    arguments = call.get("arguments")

    if not isinstance(name, str):
        errors.append("name: must be a string")
    if not isinstance(arguments, dict):
        errors.append("arguments: must be an object")
    if errors:
        return errors

    tools = _catalog_by_name(catalog)
    tool = tools.get(name)
    if tool is None:
        return [f"name: unknown tool '{name}'"]

    schema = dict(tool.inputSchema)
    schema["additionalProperties"] = False
    validator = Draft202012Validator(schema)
    for error in sorted(
        validator.iter_errors(arguments),
        key=lambda item: (list(item.absolute_path), item.message),
    ):
        errors.append(f"{_json_path(error.absolute_path)}: {error.message}")
    return errors


async def _profile_catalog(profile: Profile) -> dict[str, Any]:
    from fcp_mcp.mcp_boundary import build_mcp_server
    from fcp_mcp.server import CONFIG, PROMPTS, RESOURCES, TOOLS

    server = build_mcp_server(
        CONFIG.with_profile(profile),
        TOOLS,
        PROMPTS,
        RESOURCES,
    )
    return {tool.name: tool for tool in await server.list_tools()}


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate ```tool-call fences against the live MCP catalog.",
    )
    parser.add_argument(
        "--profile",
        choices=[profile.value for profile in Profile],
        default=Profile.FULL.value,
        help="Validate documentation against this capability profile",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        default=list(DEFAULT_PATHS),
        help="Markdown files to validate (defaults to WORKFLOWS.md and LLM_GUIDE.md)",
    )
    return parser.parse_args(argv)


def main() -> int:
    args = _parse_args()
    catalog = asyncio.run(_profile_catalog(Profile(args.profile)))
    failures: list[str] = []
    checked = 0

    for path in args.paths:
        try:
            blocks = extract_tool_call_blocks(path.read_text(encoding="utf-8"))
        except (OSError, ContractBlockError) as error:
            failures.append(f"{path}: {error}")
            continue

        for block_number, call in blocks:
            checked += 1
            failures.extend(
                f"{path}: block {block_number}: {error}"
                for error in validate_call(catalog, call)
            )

    if checked == 0:
        failures.append("no tool-call blocks were checked")

    if failures:
        for failure in failures:
            print(failure)
        return 1

    print(f"Validated {checked} tool-call blocks across {len(args.paths)} file(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
