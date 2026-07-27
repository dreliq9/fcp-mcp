"""Verify an installed fcp-mcp command through a real MCP stdio session.

Run after installing the package:

    python examples/quickstart.py

The check initializes the server, validates the 89-tool/5-prompt
catalog, and calls the structured ``fcp_doctor`` tool. It performs no
project or media writes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.wheel_smoke import check_version, inspect_server


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Initialize and inspect an installed fcp-mcp server.",
    )
    parser.add_argument(
        "--command",
        default="fcp-mcp",
        help="Path to the installed fcp-mcp console script",
    )
    return parser.parse_args()


def main() -> int:
    options = _parse_args()
    try:
        version = check_version(options.command)
        report = asyncio.run(
            inspect_server(
                options.command,
                cwd=ROOT,
                env=dict(os.environ),
            )
        )
        report["version_command"] = version
        report["check"] = "quickstart"
    except Exception as error:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error": f"{type(error).__name__}: {error}",
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1

    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
