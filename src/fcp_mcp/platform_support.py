from __future__ import annotations

import sys

from fcp_mcp.contracts import ErrorCode, FCPMCPError


def require_macos(platform: str | None = None) -> None:
    observed = sys.platform if platform is None else platform
    if observed != "darwin":
        raise FCPMCPError(
            ErrorCode.UNSUPPORTED_PLATFORM,
            "fcp-mcp requires macOS",
        )
