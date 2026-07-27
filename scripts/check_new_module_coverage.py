"""Enforce focused coverage floors for the v0.2.1 trust-boundary modules."""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

MINIMUM_COVERAGE = 90.0
REQUIRED_PATHS = (
    "src/fcp_mcp/version.py",
    "src/fcp_mcp/contracts.py",
    "src/fcp_mcp/config.py",
    "src/fcp_mcp/security/paths.py",
    "src/fcp_mcp/observability.py",
    "src/fcp_mcp/utils/atomic_write.py",
    "src/fcp_mcp/fcpxml/transaction.py",
    "src/fcp_mcp/automation/osascript.py",
    "src/fcp_mcp/diagnostics.py",
    "src/fcp_mcp/cli.py",
    "src/fcp_mcp/tool_metadata.py",
)


def _normalize(path: str) -> str:
    return path.replace("\\", "/").lstrip("./")


def _percent_for(
    files: Mapping[str, Any],
    required_path: str,
) -> float | None:
    required = _normalize(required_path)
    matches = [
        payload
        for raw_path, payload in files.items()
        if (
            _normalize(raw_path) == required
            or _normalize(raw_path).endswith(f"/{required}")
        )
    ]
    if len(matches) != 1:
        return None
    try:
        return float(matches[0]["summary"]["percent_covered"])
    except (KeyError, TypeError, ValueError):
        return None


def coverage_failures(
    report: Mapping[str, Any],
    *,
    required_paths: Iterable[str] = REQUIRED_PATHS,
    minimum: float = MINIMUM_COVERAGE,
) -> dict[str, float | None]:
    """Return missing or under-covered required files in declared order."""
    files = report.get("files", {})
    if not isinstance(files, Mapping):
        files = {}

    failures: dict[str, float | None] = {}
    for required_path in required_paths:
        percent = _percent_for(files, required_path)
        if percent is None or percent < minimum:
            failures[required_path] = percent
    return failures


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Require at least 90% coverage on v0.2.1 trust modules.",
    )
    parser.add_argument("report", type=Path, help="pytest-cov JSON report")
    return parser.parse_args()


def main() -> int:
    options = _parse_args()
    try:
        report = json.loads(options.report.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        print(f"{options.report}: {error}")
        return 1

    failures = coverage_failures(report)
    if failures:
        for path, percent in failures.items():
            detail = "missing from coverage report" if percent is None else f"{percent:.2f}%"
            print(f"{path}: {detail} (required: {MINIMUM_COVERAGE:.2f}%)")
        return 1

    print(
        f"{len(REQUIRED_PATHS)} trust modules meet "
        f"the {MINIMUM_COVERAGE:.0f}% coverage floor."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
