from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path

import platformdirs

from fcp_mcp.contracts import ErrorCode, FCPMCPError
from fcp_mcp.profiles import ApprovalMode, Profile

TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
FALSE_VALUES = frozenset({"", "0", "false", "no", "off"})


def _lexical_absolute_path(value: str | os.PathLike[str]) -> Path:
    """Normalize an absolute path without following symlinks or creating it."""
    return Path(os.path.abspath(os.path.expanduser(os.fspath(value))))


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


def _bounded_integer(
    env: Mapping[str, str],
    name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    if name not in env:
        return default
    try:
        value = int(env[name].strip())
    except ValueError as error:
        raise FCPMCPError(
            ErrorCode.INVALID_CONFIGURATION,
            f"{name} must be an integer between {minimum} and {maximum}",
        ) from error
    if not minimum <= value <= maximum:
        raise FCPMCPError(
            ErrorCode.INVALID_CONFIGURATION,
            f"{name} must be between {minimum} and {maximum}",
        )
    return value


def _enum_value(
    enum_type: type[Profile | ApprovalMode],
    value: str,
    name: str,
) -> Profile | ApprovalMode:
    try:
        return enum_type(value.strip().lower())
    except ValueError as error:
        raise FCPMCPError(
            ErrorCode.INVALID_CONFIGURATION,
            f"{name} has an unsupported value",
        ) from error


@dataclass(frozen=True)
class RuntimeConfig:
    output_dir: Path
    allowed_roots: tuple[Path, ...]
    live_control_enabled: bool
    log_format: str
    profile: Profile
    workflow_approval: ApprovalMode
    state_dir: Path
    approval_ttl_seconds: int
    max_operations: int
    max_source_bytes: int
    max_artifact_bytes: int
    max_diff_bytes: int

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        home: Path | None = None,
        profile_override: str | None = None,
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
        profile_value = (
            profile_override
            if profile_override is not None
            else values.get("FCP_MCP_PROFILE", Profile.WORKFLOW.value)
        )
        profile = _enum_value(Profile, profile_value, "FCP_MCP_PROFILE")
        workflow_approval = _enum_value(
            ApprovalMode,
            values.get("FCP_MCP_WORKFLOW_APPROVAL", ApprovalMode.CLIENT.value),
            "FCP_MCP_WORKFLOW_APPROVAL",
        )
        state_raw = values.get("FCP_MCP_STATE_DIR")
        state_dir = (
            _lexical_absolute_path(state_raw)
            if state_raw is not None
            else _lexical_absolute_path(
                platformdirs.user_state_path(
                    "fcp-mcp",
                    appauthor=False,
                    ensure_exists=False,
                )
            )
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
            profile=profile,
            workflow_approval=workflow_approval,
            state_dir=state_dir,
            approval_ttl_seconds=_bounded_integer(
                values,
                "FCP_MCP_WORKFLOW_APPROVAL_TTL_SECONDS",
                86400,
                60,
                604800,
            ),
            max_operations=_bounded_integer(
                values,
                "FCP_MCP_WORKFLOW_MAX_OPERATIONS",
                100,
                1,
                1000,
            ),
            max_source_bytes=_bounded_integer(
                values,
                "FCP_MCP_WORKFLOW_MAX_SOURCE_BYTES",
                134217728,
                1048576,
                1073741824,
            ),
            max_artifact_bytes=_bounded_integer(
                values,
                "FCP_MCP_WORKFLOW_MAX_ARTIFACT_BYTES",
                268435456,
                1048576,
                2147483648,
            ),
            max_diff_bytes=_bounded_integer(
                values,
                "FCP_MCP_WORKFLOW_MAX_DIFF_BYTES",
                204800,
                1024,
                10485760,
            ),
        )

    def with_profile(self, profile: Profile) -> RuntimeConfig:
        return replace(self, profile=profile)
