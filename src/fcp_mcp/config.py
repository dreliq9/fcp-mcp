from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from fcp_mcp.contracts import ErrorCode, FCPMCPError

TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
FALSE_VALUES = frozenset({"", "0", "false", "no", "off"})


def _boolean(env: Mapping[str, str], name: str, default: bool) -> bool:
    if name not in env:
        return default
    value = env[name].strip().lower()
    if value in TRUE_VALUES:
        return True
    if value in FALSE_VALUES:
        return False
    raise FCPMCPError(
        ErrorCode.INVALID_CONFIGURATION,
        f"{name} must be one of 1/0, true/false, yes/no, or on/off",
    )


@dataclass(frozen=True)
class RuntimeConfig:
    output_dir: Path
    allowed_roots: tuple[Path, ...]
    live_control_enabled: bool
    log_format: str

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        home: Path | None = None,
    ) -> RuntimeConfig:
        values = dict(os.environ if env is None else env)
        home_dir = (home or Path.home()).expanduser().resolve()
        output_raw = (
            values.get("FCP_MCP_OUTPUT_DIR")
            or values.get("FCP_PROJECTS_DIR")
            or str(home_dir / "Movies")
        )
        output_dir = Path(output_raw).expanduser().resolve()
        roots_raw = values.get("FCP_MCP_ALLOWED_ROOTS", "")
        roots = tuple(
            Path(item.strip()).expanduser().resolve()
            for item in roots_raw.split(os.pathsep)
            if item.strip()
        ) or (output_dir,)
        log_format = values.get("FCP_MCP_LOG_FORMAT", "text").strip().lower()
        if log_format not in {"text", "json"}:
            raise FCPMCPError(
                ErrorCode.INVALID_CONFIGURATION,
                "FCP_MCP_LOG_FORMAT must be text or json",
            )
        return cls(
            output_dir=output_dir,
            allowed_roots=roots,
            live_control_enabled=_boolean(
                values,
                "FCP_MCP_ENABLE_LIVE_CONTROL",
                False,
            ),
            log_format=log_format,
        )
