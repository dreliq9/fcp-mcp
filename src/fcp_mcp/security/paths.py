from __future__ import annotations

from collections.abc import Collection
from pathlib import Path

from fcp_mcp.config import RuntimeConfig
from fcp_mcp.contracts import ErrorCode, FCPMCPError


class PathPolicy:
    def __init__(self, config: RuntimeConfig) -> None:
        self.config = config

    @staticmethod
    def _contained(path: Path, roots: tuple[Path, ...]) -> bool:
        return any(path == root or path.is_relative_to(root) for root in roots)

    def resolve_input(
        self,
        raw: str,
        *,
        kind: str = "file",
        suffixes: Collection[str] = (),
    ) -> Path:
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = self.config.output_dir / candidate
        resolved = candidate.resolve()
        if not self._contained(resolved, self.config.allowed_roots):
            raise FCPMCPError(
                ErrorCode.PATH_OUTSIDE_SCOPE,
                f"Input path is outside FCP_MCP_ALLOWED_ROOTS: {resolved}",
            )
        if not resolved.exists():
            raise FCPMCPError(
                ErrorCode.SOURCE_NOT_FOUND,
                f"Input does not exist: {resolved}",
            )
        if kind == "file" and not resolved.is_file():
            raise FCPMCPError(ErrorCode.INVALID_PATH, f"Expected a file: {resolved}")
        if kind == "dir" and not resolved.is_dir():
            raise FCPMCPError(ErrorCode.INVALID_PATH, f"Expected a directory: {resolved}")
        if suffixes and resolved.suffix.lower() not in {suffix.lower() for suffix in suffixes}:
            raise FCPMCPError(
                ErrorCode.INVALID_PATH,
                f"Expected one of {sorted(suffixes)}: {resolved}",
            )
        return resolved

    def resolve_output(
        self,
        raw: str | None,
        *,
        input_path: Path | None = None,
        default_name: str | None = None,
        suffixes: Collection[str] = (),
    ) -> Path:
        if raw:
            candidate = Path(raw).expanduser()
            if not candidate.is_absolute():
                candidate = self.config.output_dir / candidate
        elif input_path is not None:
            candidate = input_path.with_name(
                f"{input_path.stem}_modified{input_path.suffix}"
            )
        elif default_name:
            candidate = self.config.output_dir / default_name
        else:
            raise FCPMCPError(ErrorCode.INVALID_ARGUMENTS, "Output path is required")
        resolved = candidate.resolve()
        write_roots = (self.config.output_dir,)
        if input_path is not None:
            write_roots += (input_path.resolve().parent,)
        if not self._contained(resolved, write_roots):
            raise FCPMCPError(
                ErrorCode.PATH_OUTSIDE_SCOPE,
                f"Output path is outside the permitted write locations: {resolved}",
            )
        if input_path is not None and resolved == input_path.resolve():
            raise FCPMCPError(
                ErrorCode.SAME_FILE_FORBIDDEN,
                "Input and output must be different files",
            )
        if suffixes and resolved.suffix.lower() not in {suffix.lower() for suffix in suffixes}:
            raise FCPMCPError(
                ErrorCode.INVALID_PATH,
                f"Expected one of {sorted(suffixes)}: {resolved}",
            )
        return resolved
