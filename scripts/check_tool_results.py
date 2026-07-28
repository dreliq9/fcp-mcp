"""Reject legacy scalar output schemas from the public MCP tool catalog."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError


class _InvalidSchema(ValueError):
    pass


def _resolve_local_ref(
    root: Mapping[str, object],
    reference: object,
) -> Mapping[str, object]:
    if not isinstance(reference, str):
        raise _InvalidSchema("ref must be a string")
    if not reference.startswith("#/"):
        raise _InvalidSchema("external refs are unsupported")
    current: object = root
    for encoded_part in reference[2:].split("/"):
        part = encoded_part.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, Mapping) or part not in current:
            raise _InvalidSchema(f"unresolved local ref {reference}")
        current = current[part]
    if not isinstance(current, Mapping):
        raise _InvalidSchema(f"local ref does not resolve to a schema {reference}")
    return current


def _root_schemas(
    root: Mapping[str, object],
    schema: Mapping[str, object],
    *,
    seen_refs: frozenset[str] = frozenset(),
) -> list[Mapping[str, object]]:
    roots: list[Mapping[str, object]] = []
    reference = schema.get("$ref")
    if reference is not None:
        if not isinstance(reference, str):
            raise _InvalidSchema("ref must be a string")
        if reference in seen_refs:
            raise _InvalidSchema(f"cyclic local ref {reference}")
        resolved = _resolve_local_ref(root, reference)
        roots.extend(
            _root_schemas(
                root,
                resolved,
                seen_refs=seen_refs | {reference},
            )
        )

    composed = False
    for keyword in ("allOf", "anyOf", "oneOf"):
        if keyword not in schema:
            continue
        composed = True
        branches = schema[keyword]
        if (
            not isinstance(branches, list)
            or not branches
            or any(not isinstance(branch, Mapping) for branch in branches)
        ):
            raise _InvalidSchema(f"{keyword} must be a nonempty schema list")
        for branch in branches:
            roots.extend(
                _root_schemas(
                    root,
                    branch,
                    seen_refs=seen_refs,
                )
            )

    if schema.get("type") == "object" or isinstance(
        schema.get("properties"),
        Mapping,
    ):
        roots.append(schema)
    elif reference is None and not composed:
        raise _InvalidSchema("root does not describe an object")
    return roots


def _schema_failures(name: str, schema: object) -> list[str]:
    if schema is None or schema == {}:
        return [f"{name}: missing output schema"]
    if not isinstance(schema, Mapping):
        return [f"{name}: invalid output schema"]
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError:
        return [f"{name}: invalid output schema"]
    try:
        roots = _root_schemas(schema, schema)
    except _InvalidSchema as error:
        return [f"{name}: invalid output schema: {error}"]
    problems = []
    if any(
        set(root.get("properties", {})) == {"result"}
        for root in roots
    ):
        problems.append(f"{name}: legacy scalar schema")
    if schema.get("title") == "LegacyTextResult" or any(
        root.get("title") == "LegacyTextResult"
        for root in roots
    ):
        problems.append(f"{name}: legacy result model")
    return problems


async def failures(server, *, expected_count: int = 93) -> list[str]:
    """Return deterministic legacy-result violations from a server catalog."""
    problems = []
    tools = await server.list_tools()
    if len(tools) != expected_count:
        problems.append(
            f"catalog: expected {expected_count} tools, got {len(tools)}"
        )
    for tool in tools:
        problems.extend(
            _schema_failures(
                tool.name,
                getattr(tool, "output_schema", None),
            )
        )
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
