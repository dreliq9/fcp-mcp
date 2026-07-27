from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ErrorCode(str, Enum):
    INVALID_ARGUMENTS = "invalid_arguments"
    INVALID_CONFIGURATION = "invalid_configuration"
    INVALID_PATH = "invalid_path"
    PATH_OUTSIDE_SCOPE = "path_outside_scope"
    SOURCE_NOT_FOUND = "source_not_found"
    TARGET_NOT_FOUND = "target_not_found"
    SAME_FILE_FORBIDDEN = "same_file_forbidden"
    LIVE_CONTROL_DISABLED = "live_control_disabled"
    PERMISSION_DENIED = "permission_denied"
    DEPENDENCY_MISSING = "dependency_missing"
    COMMAND_FAILED = "command_failed"
    OUTPUT_MISSING = "output_missing"
    VALIDATION_FAILED = "validation_failed"
    TRANSACTION_FAILED = "transaction_failed"
    UNSUPPORTED_CONTRACT = "unsupported_contract"
    INTERNAL_ERROR = "internal_error"


class FCPMCPError(RuntimeError):
    def __init__(
        self,
        code: ErrorCode,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.details = details or {}
        super().__init__(f"{code.value}: {message}")


class DoctorCheck(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    status: Literal["pass", "warn", "fail", "skip"]
    summary: str
    details: dict[str, Any] = Field(default_factory=dict)
    remediation: str | None = None


class DoctorReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: Literal["1"] = "1"
    status: Literal["ready", "degraded", "blocked"]
    package_version: str
    mcp_sdk_version: str
    wire_server_version: str
    protocol_target: Literal["2025-11-25"] = "2025-11-25"
    server_name: Literal["fcp-mcp"] = "fcp-mcp"
    tool_count: int
    prompt_count: int
    checks: list[DoctorCheck]
