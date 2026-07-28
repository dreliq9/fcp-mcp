from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any, TextIO

from fcp_mcp.version import package_version


def _single_line(value: Any) -> str:
    return str(value).replace("\n", "\\n")


def emit_event(
    event: Mapping[str, Any],
    *,
    format: str,
    stream: TextIO | None = None,
) -> None:
    target = stream if stream is not None else sys.stderr
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "package_version": package_version(),
        **event,
    }
    if format == "json":
        line = json.dumps(record, default=str, separators=(",", ":"), sort_keys=True)
    elif format == "text":
        name = _single_line(record.pop("event", "event"))
        fields = " ".join(
            f"{key}={_single_line(value)}"
            for key, value in sorted(record.items())
            if value is not None
        )
        line = f"{name} {fields}".rstrip()
    else:
        raise ValueError(f"Unsupported event format: {format}")
    target.write(f"{line}\n")
    target.flush()
