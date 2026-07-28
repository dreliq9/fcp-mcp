"""Private, contained workflow artifact storage.

Artifact metadata is evidence, not a path capability: every filesystem access
reconstructs a fixed path from a canonical run UUID and closed artifact kind.
"""

from __future__ import annotations

import errno
import hashlib
import os
import re
import secrets
import stat
import sys
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from fcp_mcp.config import RuntimeConfig
from fcp_mcp.contracts import ErrorCode, FCPMCPError

_UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_LOCK_KEY_PATTERN = _SHA256_PATTERN
_DIRECTORY_MODE = 0o700
_FILE_MODE = 0o600
_READ_CHUNK_BYTES = 64 * 1024

class _RunLockEntry:
    __slots__ = ("lock", "users")

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.users = 0


_RUN_LOCKS: dict[tuple[str, str], _RunLockEntry] = {}
_RUN_LOCKS_GUARD = threading.Lock()


class ArtifactKind(str, Enum):
    """Closed set of private workflow artifact bodies."""

    CANDIDATE = "candidate"
    DIFF = "diff"
    FAILURE_EVIDENCE = "failure_evidence"


_ARTIFACT_NAMES = {
    ArtifactKind.CANDIDATE: "candidate.fcpxml",
    ArtifactKind.DIFF: "diff.json",
    ArtifactKind.FAILURE_EVIDENCE: "failure.json",
}


def _coded(code: ErrorCode, message: str, cause: BaseException | None = None) -> FCPMCPError:
    error = FCPMCPError(code, message)
    if cause is not None:
        error.__cause__ = cause
    return error


def _canonical_run_id(value: object) -> str:
    if not isinstance(value, str) or not _UUID_PATTERN.fullmatch(value):
        raise _coded(
            ErrorCode.INVALID_ARGUMENTS,
            "run_id must use canonical lowercase UUID spelling",
        )
    try:
        parsed = UUID(value)
    except ValueError as error:
        raise _coded(ErrorCode.INVALID_ARGUMENTS, "run_id is not a UUID", error)
    if parsed.int == 0 or str(parsed) != value:
        raise _coded(
            ErrorCode.INVALID_ARGUMENTS,
            "run_id must be a non-nil canonical lowercase UUID",
        )
    return value


def _artifact_kind(value: object) -> ArtifactKind:
    if isinstance(value, ArtifactKind):
        return value
    if isinstance(value, str):
        try:
            return ArtifactKind(value)
        except ValueError:
            pass
    raise _coded(
        ErrorCode.INVALID_ARGUMENTS,
        "artifact kind must be candidate, diff, or failure_evidence",
    )


def _canonical_lock_key(value: object) -> str:
    if not isinstance(value, str) or not _LOCK_KEY_PATTERN.fullmatch(value):
        raise _coded(
            ErrorCode.INVALID_ARGUMENTS,
            "lock key must be exactly 64 lowercase hexadecimal characters",
        )
    return value


def _relative_artifact_path(run_id: str, kind: ArtifactKind) -> str:
    return PurePosixPath("artifacts", run_id, _ARTIFACT_NAMES[kind]).as_posix()


@contextmanager
def _run_thread_guard(root: Path, run_id: str):
    key = (os.fspath(root), run_id)
    with _RUN_LOCKS_GUARD:
        entry = _RUN_LOCKS.setdefault(key, _RunLockEntry())
        entry.users += 1
    try:
        with entry.lock:
            yield
    finally:
        with _RUN_LOCKS_GUARD:
            entry.users -= 1
            if entry.users == 0 and _RUN_LOCKS.get(key) is entry:
                del _RUN_LOCKS[key]


def _attach_cleanup_errors(
    primary_error: BaseException | None,
    failures: list[str],
) -> None:
    if not failures:
        return
    if isinstance(primary_error, FCPMCPError):
        existing = primary_error.details.setdefault("cleanup_errors", [])
        if isinstance(existing, list):
            existing.extend(failures)
        return
    if primary_error is not None:
        primary_error._fcp_cleanup_errors = tuple(failures)
        return
    raise _coded(
        ErrorCode.TRANSACTION_FAILED,
        "resource cleanup failed",
        OSError("; ".join(failures)),
    )


def _as_coded(
    error: BaseException,
    *,
    code: ErrorCode,
    message: str,
) -> FCPMCPError:
    if isinstance(error, FCPMCPError):
        return error
    return _coded(code, message, error)


def _cleanup_failure(description: str, error: OSError) -> str:
    detail = type(error).__name__
    if isinstance(error.errno, int):
        detail = f"{detail} errno {error.errno}"
    return f"{description}: {detail}"[:240]


def _close_descriptor(
    fd: int,
    *,
    description: str,
    failures: list[str],
) -> None:
    try:
        os.close(fd)
    except OSError as error:
        failures.append(_cleanup_failure(description, error))


def _read_opened_payload(fd: int, expected_size: int) -> bytes:
    os.lseek(fd, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    total = 0
    while total <= expected_size:
        remaining = expected_size + 1 - total
        if remaining <= 0:
            break
        chunk = os.read(fd, min(_READ_CHUNK_BYTES, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


def _require_exact_mode(
    result: os.stat_result,
    expected: int,
    *,
    code: ErrorCode,
    description: str,
) -> None:
    if stat.S_IMODE(result.st_mode) != expected:
        raise _coded(code, f"{description} does not have private mode {expected:04o}")


def _validate_existing_path(
    path: Path,
    *,
    expected: Literal["directory", "file"],
    code: ErrorCode,
    description: str,
    require_private_mode: bool = True,
) -> None:
    try:
        result = path.lstat()
    except FileNotFoundError:
        return
    except OSError as error:
        raise _coded(code, f"cannot inspect {description}", error)
    if stat.S_ISLNK(result.st_mode):
        raise _coded(code, f"{description} must not be a symlink")
    expected_type = stat.S_ISDIR if expected == "directory" else stat.S_ISREG
    if not expected_type(result.st_mode):
        raise _coded(code, f"{description} has the wrong filesystem type")
    if require_private_mode:
        _require_exact_mode(
            result,
            _DIRECTORY_MODE if expected == "directory" else _FILE_MODE,
            code=code,
            description=description,
        )


def _directory_open_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def _file_open_flags(*, write: bool = False) -> int:
    access = os.O_WRONLY if write else os.O_RDONLY
    return access | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)


def _validate_directory_fd(
    fd: int,
    *,
    code: ErrorCode,
    description: str,
    require_private_mode: bool,
) -> None:
    result = os.fstat(fd)
    if not stat.S_ISDIR(result.st_mode):
        raise _coded(code, f"{description} is not a safe directory")
    if require_private_mode:
        _require_exact_mode(
            result,
            _DIRECTORY_MODE,
            code=code,
            description=description,
        )


def _open_directory_at(
    parent_fd: int,
    name: str,
    *,
    create: bool,
    require_private_mode: bool,
    code: ErrorCode,
    description: str,
) -> tuple[int, bool]:
    flags = _directory_open_flags()
    created = False
    try:
        fd = os.open(name, flags, dir_fd=parent_fd)
    except FileNotFoundError as error:
        if not create:
            raise _coded(code, f"{description} is missing", error)
        try:
            os.mkdir(name, _DIRECTORY_MODE, dir_fd=parent_fd)
            created = True
        except FileExistsError:
            pass
        except OSError as error:
            raise _coded(code, f"cannot create {description}", error)
        try:
            fd = os.open(name, flags, dir_fd=parent_fd)
        except OSError as error:
            raise _coded(code, f"cannot securely open {description}", error)
    except OSError as error:
        raise _coded(code, f"cannot securely open {description}", error)
    try:
        if created:
            os.fchmod(fd, _DIRECTORY_MODE)
        _validate_directory_fd(
            fd,
            code=code,
            description=description,
            require_private_mode=require_private_mode or created,
        )
    except BaseException as error:  # noqa: BLE001 - close the acquired descriptor
        primary_error = _as_coded(
            error,
            code=code,
            message=f"cannot validate {description}",
        )
        cleanup_failures: list[str] = []
        _close_descriptor(
            fd,
            description="close opened directory",
            failures=cleanup_failures,
        )
        _attach_cleanup_errors(primary_error, cleanup_failures)
        raise primary_error
    if created:
        try:
            os.fsync(parent_fd)
        except OSError as error:
            primary_error = _coded(
                code,
                f"cannot durably create {description}",
                error,
            )
            cleanup_failures = []
            _close_descriptor(
                fd,
                description="close created directory",
                failures=cleanup_failures,
            )
            _attach_cleanup_errors(primary_error, cleanup_failures)
            raise primary_error
    return fd, created


def _open_absolute_directory_chain(
    path: Path,
    *,
    create: bool,
    code: ErrorCode,
    description: str,
    require_private_mode: bool = True,
) -> int:
    if not path.is_absolute():
        raise _coded(code, f"{description} must be absolute")
    parts = path.parts
    if not parts:
        raise _coded(code, f"{description} is empty")
    try:
        current_fd = os.open(parts[0], _directory_open_flags())
    except OSError as error:
        raise _coded(code, f"cannot open filesystem root for {description}", error)
    for index, component in enumerate(parts[1:]):
        is_leaf = index == len(parts[1:]) - 1
        try:
            next_fd, _ = _open_directory_at(
                current_fd,
                component,
                create=create,
                require_private_mode=is_leaf and require_private_mode,
                code=code,
                description=description if is_leaf else "state parent directory",
            )
        except BaseException as error:  # noqa: BLE001 - close the retained parent
            primary_error = _as_coded(
                error,
                code=code,
                message=f"cannot open {description}",
            )
            cleanup_failures: list[str] = []
            _close_descriptor(
                current_fd,
                description="close directory chain",
                failures=cleanup_failures,
            )
            _attach_cleanup_errors(primary_error, cleanup_failures)
            raise primary_error
        try:
            os.close(current_fd)
        except OSError as error:
            primary_error = _coded(
                code,
                f"cannot release parent while opening {description}",
                error,
            )
            cleanup_failures = [
                _cleanup_failure("close parent directory", error)
            ]
            _close_descriptor(
                next_fd,
                description="close acquired child directory",
                failures=cleanup_failures,
            )
            _attach_cleanup_errors(primary_error, cleanup_failures)
            raise primary_error
        current_fd = next_fd
    try:
        _validate_directory_fd(
            current_fd,
            code=code,
            description=description,
            require_private_mode=require_private_mode,
        )
    except BaseException as error:  # noqa: BLE001 - close the retained leaf
        primary_error = _as_coded(
            error,
            code=code,
            message=f"cannot validate {description}",
        )
        cleanup_failures = []
        _close_descriptor(
            current_fd,
            description="close invalid directory",
            failures=cleanup_failures,
        )
        _attach_cleanup_errors(primary_error, cleanup_failures)
        raise primary_error
    return current_fd


def _open_absolute_directory_lease(
    path: Path,
    *,
    code: ErrorCode,
    description: str,
) -> tuple[list[int], list[tuple[int, str, int, str, bool]]]:
    if not path.is_absolute():
        raise _coded(code, f"{description} must be absolute")
    parts = path.parts
    if not parts:
        raise _coded(code, f"{description} is empty")
    try:
        root_fd = os.open(parts[0], _directory_open_flags())
    except OSError as error:
        raise _coded(code, f"cannot open filesystem root for {description}", error)
    descriptors = [root_fd]
    edges: list[tuple[int, str, int, str, bool]] = []
    primary_error: FCPMCPError | None = None
    try:
        _validate_directory_fd(
            root_fd,
            code=code,
            description="filesystem root",
            require_private_mode=False,
        )
        for index, component in enumerate(parts[1:]):
            is_leaf = index == len(parts[1:]) - 1
            child_description = (
                description if is_leaf else "canonical ancestor directory"
            )
            child_fd, _ = _open_directory_at(
                descriptors[-1],
                component,
                create=False,
                require_private_mode=is_leaf,
                code=code,
                description=child_description,
            )
            edges.append(
                (
                    descriptors[-1],
                    component,
                    child_fd,
                    child_description,
                    is_leaf,
                )
            )
            descriptors.append(child_fd)
        return descriptors, edges
    except BaseException as error:  # noqa: BLE001 - close the retained lease
        primary_error = _as_coded(
            error,
            code=code,
            message=f"cannot open {description}",
        )
        raise primary_error
    finally:
        if primary_error is not None:
            cleanup_failures: list[str] = []
            for fd in reversed(descriptors):
                _close_descriptor(
                    fd,
                    description="close canonical directory lease",
                    failures=cleanup_failures,
                )
            _attach_cleanup_errors(primary_error, cleanup_failures)


def _validate_entry_at(
    parent_fd: int,
    name: str,
    *,
    expected: Literal["directory", "file"],
    code: ErrorCode,
    description: str,
    require_private_mode: bool = True,
) -> os.stat_result | None:
    try:
        result = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise _coded(code, f"cannot inspect {description}", error)
    if stat.S_ISLNK(result.st_mode):
        raise _coded(code, f"{description} must not be a symlink")
    expected_type = stat.S_ISDIR if expected == "directory" else stat.S_ISREG
    if not expected_type(result.st_mode):
        raise _coded(code, f"{description} has the wrong filesystem type")
    if require_private_mode:
        _require_exact_mode(
            result,
            _DIRECTORY_MODE if expected == "directory" else _FILE_MODE,
            code=code,
            description=description,
        )
    return result


def _require_opened_directory_identity(
    parent_fd: int,
    name: str,
    opened_fd: int,
    *,
    description: str,
    require_private_mode: bool = True,
) -> None:
    try:
        opened_result = os.fstat(opened_fd)
    except OSError as error:
        raise _coded(
            ErrorCode.ARTIFACT_CORRUPT,
            f"cannot inspect opened {description}",
            error,
        )
    current_result = _validate_entry_at(
        parent_fd,
        name,
        expected="directory",
        code=ErrorCode.ARTIFACT_CORRUPT,
        description=description,
        require_private_mode=require_private_mode,
    )
    if current_result is None or not os.path.samestat(opened_result, current_result):
        raise _coded(
            ErrorCode.ARTIFACT_CORRUPT,
            f"{description} changed during artifact access",
        )


def _require_opened_file_identity(
    parent_fd: int,
    name: str,
    opened_fd: int,
    *,
    description: str,
) -> None:
    try:
        opened_result = os.fstat(opened_fd)
    except OSError as error:
        raise _coded(
            ErrorCode.ARTIFACT_CORRUPT,
            f"cannot inspect opened {description}",
            error,
        )
    current_result = _validate_entry_at(
        parent_fd,
        name,
        expected="file",
        code=ErrorCode.ARTIFACT_CORRUPT,
        description=description,
    )
    if current_result is None or not os.path.samestat(opened_result, current_result):
        raise _coded(
            ErrorCode.ARTIFACT_CORRUPT,
            f"{description} changed during artifact access",
        )


def _remove_owned_file_at(
    parent_fd: int,
    name: str,
    owned_fd: int,
) -> list[str]:
    failures: list[str] = []
    try:
        current_result = os.stat(
            name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        opened_result = os.fstat(owned_fd)
    except FileNotFoundError:
        return failures
    except OSError as error:
        failures.append(
            _cleanup_failure("inspect published artifact for cleanup", error)
        )
        return failures
    if (
        stat.S_ISLNK(current_result.st_mode)
        or not stat.S_ISREG(current_result.st_mode)
        or not os.path.samestat(opened_result, current_result)
    ):
        return failures
    try:
        os.unlink(name, dir_fd=parent_fd)
    except FileNotFoundError:
        return failures
    except OSError as error:
        failures.append(_cleanup_failure("remove published artifact", error))
        return failures
    try:
        os.fsync(parent_fd)
    except OSError as error:
        failures.append(_cleanup_failure("sync artifact cleanup", error))
    return failures


def _initialize_posix_state(root: Path) -> None:
    root_fd = _open_absolute_directory_chain(
        root,
        create=True,
        code=ErrorCode.INVALID_CONFIGURATION,
        description="workflow state root",
    )
    try:
        _validate_entry_at(
            root_fd,
            "runs.sqlite3",
            expected="file",
            code=ErrorCode.INVALID_CONFIGURATION,
            description="workflow database",
        )
        for name in ("artifacts", "locks"):
            child_fd, _ = _open_directory_at(
                root_fd,
                name,
                create=True,
                require_private_mode=True,
                code=ErrorCode.INVALID_CONFIGURATION,
                description=f"workflow {name} directory",
            )
            os.close(child_fd)
    finally:
        os.close(root_fd)


class StatePaths(BaseModel):
    """Immutable deterministic paths under the private workflow state root."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    root: Path
    database: Path
    artifacts: Path
    locks: Path

    @model_validator(mode="after")
    def _validate_fixed_children(self) -> StatePaths:
        if not self.root.is_absolute():
            raise ValueError("state root must be absolute")
        lexical_root = Path(os.path.abspath(os.fspath(self.root)))
        if self.root != lexical_root:
            raise ValueError("state root must be lexically normalized")
        expected = (
            self.root / "runs.sqlite3",
            self.root / "artifacts",
            self.root / "locks",
        )
        if (self.database, self.artifacts, self.locks) != expected:
            raise ValueError("state paths must be fixed children of root")
        return self

    @classmethod
    def from_config(cls, config: RuntimeConfig) -> StatePaths:
        root = config.state_dir
        if not root.is_absolute():
            raise _coded(ErrorCode.INVALID_CONFIGURATION, "workflow state root must be absolute")
        if root != Path(os.path.abspath(os.fspath(root))):
            raise _coded(
                ErrorCode.INVALID_CONFIGURATION,
                "workflow state root must be lexically normalized",
            )
        _initialize_posix_state(root)
        return cls(
            root=root,
            database=root / "runs.sqlite3",
            artifacts=root / "artifacts",
            locks=root / "locks",
        )

    def run_dir(self, run_id: str) -> Path:
        canonical = _canonical_run_id(run_id)
        path = self.artifacts / canonical
        _validate_existing_path(
            path,
            expected="directory",
            code=ErrorCode.ARTIFACT_CORRUPT,
            description="artifact run directory",
            require_private_mode=True,
        )
        return path

    def artifact_path(self, run_id: str, kind: ArtifactKind | str) -> Path:
        canonical = _canonical_run_id(run_id)
        closed_kind = _artifact_kind(kind)
        run_directory = self.run_dir(canonical)
        path = run_directory / _ARTIFACT_NAMES[closed_kind]
        _validate_existing_path(
            path,
            expected="file",
            code=ErrorCode.ARTIFACT_CORRUPT,
            description=f"{closed_kind.value} artifact",
            require_private_mode=True,
        )
        return path

    def lock_path(self, destination_sha256: str) -> Path:
        key = _canonical_lock_key(destination_sha256)
        path = self.locks / f"{key}.lock"
        _validate_existing_path(
            path,
            expected="file",
            code=ErrorCode.ARTIFACT_CORRUPT,
            description="workflow destination lock",
            require_private_mode=True,
        )
        return path


class ArtifactMetadataV1(BaseModel):
    """Immutable evidence for a committed private artifact body."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        strict=True,
        allow_inf_nan=False,
    )

    schema_version: Literal["1"] = "1"
    run_id: str
    kind: ArtifactKind
    relative_path: str = Field(min_length=1, max_length=255)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_size: int = Field(strict=True, ge=0)
    created_at: datetime

    @field_validator("run_id")
    @classmethod
    def _validate_run_id(cls, value: str) -> str:
        try:
            return _canonical_run_id(value)
        except FCPMCPError as error:
            raise ValueError(error.message) from error

    @field_validator("created_at")
    @classmethod
    def _validate_created_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
            raise ValueError("created_at must be UTC-aware")
        return value

    @model_validator(mode="after")
    def _validate_relative_path(self) -> ArtifactMetadataV1:
        expected = _relative_artifact_path(self.run_id, self.kind)
        if self.relative_path != expected:
            raise ValueError("relative_path must match the canonical run_id and artifact kind")
        return self


def enforce_source_size(byte_count: int, *, max_source_bytes: int) -> int:
    """Validate a source byte count without reading or parsing source content."""
    if (
        type(byte_count) is not int
        or type(max_source_bytes) is not int
        or byte_count < 0
        or max_source_bytes < 0
        or byte_count > max_source_bytes
    ):
        raise _coded(
            ErrorCode.INVALID_ARGUMENTS,
            "source exceeds the configured source byte limit",
        )
    return byte_count


def render_diff_summary(text: str, *, max_bytes: int) -> str:
    """Return a UTF-8-safe rendered prefix, independent of the full diff body."""
    if not isinstance(text, str) or type(max_bytes) is not int or max_bytes < 0:
        raise _coded(
            ErrorCode.INVALID_ARGUMENTS,
            "rendered diff summary requires text and a nonnegative byte limit",
        )
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


class ArtifactStore:
    """Atomic private storage for candidate and complete diff artifacts."""

    def __init__(self, paths: StatePaths, *, max_artifact_bytes: int) -> None:
        if not isinstance(paths, StatePaths):
            raise _coded(ErrorCode.INVALID_ARGUMENTS, "paths must be StatePaths")
        if type(max_artifact_bytes) is not int or max_artifact_bytes < 0:
            raise _coded(
                ErrorCode.INVALID_ARGUMENTS,
                "max_artifact_bytes must be a nonnegative integer",
            )
        self.paths = paths
        self.max_artifact_bytes = max_artifact_bytes

    @classmethod
    def from_config(cls, config: RuntimeConfig) -> ArtifactStore:
        return cls(
            StatePaths.from_config(config),
            max_artifact_bytes=config.max_artifact_bytes,
        )

    def _open_posix_run(self, run_id: str, *, create: bool) -> tuple[int, int, int]:
        try:
            root_fd = _open_absolute_directory_chain(
                self.paths.root,
                create=False,
                code=ErrorCode.ARTIFACT_CORRUPT,
                description="workflow state root",
            )
            artifacts_fd, _ = _open_directory_at(
                root_fd,
                "artifacts",
                create=False,
                require_private_mode=True,
                code=ErrorCode.ARTIFACT_CORRUPT,
                description="workflow artifacts directory",
            )
            run_fd, _ = _open_directory_at(
                artifacts_fd,
                run_id,
                create=create,
                require_private_mode=True,
                code=ErrorCode.ARTIFACT_CORRUPT,
                description="artifact run directory",
            )
            return root_fd, artifacts_fd, run_fd
        except BaseException as error:  # noqa: BLE001 - close all acquired descriptors
            primary_error = _as_coded(
                error,
                code=ErrorCode.ARTIFACT_CORRUPT,
                message="artifact path cannot be opened",
            )
            cleanup_failures: list[str] = []
            for description, fd in (
                ("artifacts", locals().get("artifacts_fd")),
                ("root", locals().get("root_fd")),
            ):
                if not isinstance(fd, int):
                    continue
                _close_descriptor(
                    fd,
                    description=f"close {description} directory",
                    failures=cleanup_failures,
                )
            _attach_cleanup_errors(primary_error, cleanup_failures)
            raise primary_error

    @staticmethod
    def _close_posix_run(root_fd: int, artifacts_fd: int, run_fd: int) -> None:
        failures: list[str] = []
        for description, fd in (
            ("run", run_fd),
            ("artifacts", artifacts_fd),
            ("root", root_fd),
        ):
            _close_descriptor(
                fd,
                description=f"close {description} directory",
                failures=failures,
            )
        _attach_cleanup_errors(None, failures)

    def _revalidate_posix_run(
        self,
        run_id: str,
        *,
        root_fd: int,
        artifacts_fd: int,
        run_fd: int,
        target_name: str,
        artifact_fd: int,
    ) -> None:
        lease_fds: list[int] = []
        lease_edges: list[tuple[int, str, int, str, bool]] = []
        current_root_fd: int | None = None
        current_artifacts_fd: int | None = None
        current_run_fd: int | None = None
        primary_error: FCPMCPError | None = None
        try:
            lease_fds, lease_edges = _open_absolute_directory_lease(
                self.paths.root,
                code=ErrorCode.ARTIFACT_CORRUPT,
                description="workflow state root",
            )
            current_root_fd = lease_fds[-1]
            current_artifacts_fd, _ = _open_directory_at(
                current_root_fd,
                "artifacts",
                create=False,
                require_private_mode=True,
                code=ErrorCode.ARTIFACT_CORRUPT,
                description="workflow artifacts directory",
            )
            lease_edges.append(
                (
                    current_root_fd,
                    "artifacts",
                    current_artifacts_fd,
                    "workflow artifacts directory",
                    True,
                )
            )
            lease_fds.append(current_artifacts_fd)
            current_run_fd, _ = _open_directory_at(
                current_artifacts_fd,
                run_id,
                create=False,
                require_private_mode=True,
                code=ErrorCode.ARTIFACT_CORRUPT,
                description="artifact run directory",
            )
            lease_edges.append(
                (
                    current_artifacts_fd,
                    run_id,
                    current_run_fd,
                    "artifact run directory",
                    True,
                )
            )
            lease_fds.append(current_run_fd)
            for description, retained_fd, current_fd in (
                ("workflow state root", root_fd, current_root_fd),
                ("workflow artifacts directory", artifacts_fd, current_artifacts_fd),
                ("artifact run directory", run_fd, current_run_fd),
            ):
                if not os.path.samestat(
                    os.fstat(retained_fd),
                    os.fstat(current_fd),
                ):
                    raise _coded(
                        ErrorCode.ARTIFACT_CORRUPT,
                        f"{description} changed during artifact access",
                    )
            for (
                parent_fd,
                name,
                child_fd,
                description,
                require_private_mode,
            ) in lease_edges:
                _require_opened_directory_identity(
                    parent_fd,
                    name,
                    child_fd,
                    description=description,
                    require_private_mode=require_private_mode,
                )
            _require_opened_file_identity(
                current_run_fd,
                target_name,
                artifact_fd,
                description="artifact target",
            )
            for (
                parent_fd,
                name,
                child_fd,
                description,
                require_private_mode,
            ) in reversed(lease_edges):
                _require_opened_directory_identity(
                    parent_fd,
                    name,
                    child_fd,
                    description=description,
                    require_private_mode=require_private_mode,
                )
            _require_opened_file_identity(
                current_run_fd,
                target_name,
                artifact_fd,
                description="artifact target",
            )
        except BaseException as error:  # noqa: BLE001 - close the fresh descriptor chain
            primary_error = _as_coded(
                error,
                code=ErrorCode.ARTIFACT_CORRUPT,
                message="canonical artifact directory chain cannot be verified",
            )
            raise primary_error
        finally:
            cleanup_failures: list[str] = []
            for fd in reversed(lease_fds):
                _close_descriptor(
                    fd,
                    description="close canonical directory lease",
                    failures=cleanup_failures,
                )
            _attach_cleanup_errors(primary_error, cleanup_failures)

    def _existing_artifact_sizes_posix(self, run_fd: int) -> dict[ArtifactKind, int]:
        sizes: dict[ArtifactKind, int] = {}
        for kind, name in _ARTIFACT_NAMES.items():
            result = _validate_entry_at(
                run_fd,
                name,
                expected="file",
                code=ErrorCode.ARTIFACT_CORRUPT,
                description=f"{kind.value} artifact",
            )
            if result is not None:
                sizes[kind] = result.st_size
        return sizes

    def _enforce_aggregate(
        self,
        sizes: dict[ArtifactKind, int],
        kind: ArtifactKind,
        payload_size: int,
    ) -> None:
        resulting_sizes = dict(sizes)
        resulting_sizes[kind] = payload_size
        if sum(resulting_sizes.values()) > self.max_artifact_bytes:
            raise _coded(
                ErrorCode.INVALID_ARGUMENTS,
                "aggregate private artifacts exceed the configured artifact byte limit",
            )

    def write(
        self,
        run_id: str,
        kind: ArtifactKind | str,
        payload: bytes | bytearray | memoryview,
    ) -> ArtifactMetadataV1:
        canonical = _canonical_run_id(run_id)
        closed_kind = _artifact_kind(kind)
        if not isinstance(payload, (bytes, bytearray, memoryview)):
            raise _coded(ErrorCode.INVALID_ARGUMENTS, "artifact payload must be bytes-like")
        committed_bytes = bytes(payload)
        if len(committed_bytes) > self.max_artifact_bytes:
            raise _coded(
                ErrorCode.INVALID_ARGUMENTS,
                "artifact exceeds the configured artifact byte limit",
            )
        with _run_thread_guard(self.paths.root, canonical):
            return self._write_posix(canonical, closed_kind, committed_bytes)

    def _write_posix(
        self,
        run_id: str,
        kind: ArtifactKind,
        payload: bytes,
    ) -> ArtifactMetadataV1:
        root_fd, artifacts_fd, run_fd = self._open_posix_run(run_id, create=True)
        target_name = _ARTIFACT_NAMES[kind]
        temporary_name: str | None = None
        temporary_fd: int | None = None
        temporary_owned = False
        replaced = False
        published_owned = False
        process_locked = False
        try:
            import fcntl

            fcntl.flock(run_fd, fcntl.LOCK_EX)
            process_locked = True
            self._enforce_aggregate(
                self._existing_artifact_sizes_posix(run_fd),
                kind,
                len(payload),
            )
            flags = (
                os.O_RDWR
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_BINARY", 0)
            )
            collision: OSError | None = None
            for _ in range(3):
                temporary_name = f".{target_name}.{secrets.token_hex(16)}.tmp"
                try:
                    temporary_fd = os.open(
                        temporary_name,
                        flags,
                        _FILE_MODE,
                        dir_fd=run_fd,
                    )
                except FileExistsError as error:
                    collision = error
                    continue
                temporary_owned = True
                break
            if temporary_fd is None:
                raise collision or OSError(errno.EEXIST, "cannot allocate artifact temp")
            os.fchmod(temporary_fd, _FILE_MODE)
            view = memoryview(payload)
            written = 0
            while written < len(view):
                count = os.write(temporary_fd, view[written:])
                if count <= 0:
                    raise OSError(errno.EIO, "artifact write made no progress")
                written += count
            os.fsync(temporary_fd)
            result = os.fstat(temporary_fd)
            if (
                not stat.S_ISREG(result.st_mode)
                or result.st_size != len(payload)
                or stat.S_IMODE(result.st_mode) != _FILE_MODE
            ):
                raise OSError(errno.EIO, "temporary artifact validation failed")
            os.replace(
                temporary_name,
                target_name,
                src_dir_fd=run_fd,
                dst_dir_fd=run_fd,
            )
            replaced = True
            published_owned = True
            temporary_owned = False
            if _read_opened_payload(temporary_fd, len(payload)) != payload:
                raise _coded(
                    ErrorCode.ARTIFACT_CORRUPT,
                    "committed artifact bytes changed during replacement",
                )
            os.fsync(run_fd)
            self._revalidate_posix_run(
                run_id,
                root_fd=root_fd,
                artifacts_fd=artifacts_fd,
                run_fd=run_fd,
                target_name=target_name,
                artifact_fd=temporary_fd,
            )
            published_owned = False
        except FCPMCPError:
            raise
        except OSError as error:
            action = "durably commit" if replaced else "write"
            raise _coded(
                ErrorCode.TRANSACTION_FAILED,
                f"failed to {action} private artifact",
                error,
            )
        finally:
            primary_error = sys.exc_info()[1]
            cleanup_failures: list[str] = []
            cleanup_error: OSError | None = None
            if (
                primary_error is not None
                and published_owned
                and temporary_fd is not None
            ):
                cleanup_failures.extend(
                    _remove_owned_file_at(
                        run_fd,
                        target_name,
                        temporary_fd,
                    )
                )
            if (
                temporary_owned
                and temporary_name is not None
                and temporary_fd is not None
            ):
                try:
                    current_result = os.stat(
                        temporary_name,
                        dir_fd=run_fd,
                        follow_symlinks=False,
                    )
                    opened_result = os.fstat(temporary_fd)
                except FileNotFoundError:
                    cleanup_error = None
                except OSError as error:
                    cleanup_error = error
                else:
                    if (
                        not stat.S_ISLNK(current_result.st_mode)
                        and os.path.samestat(opened_result, current_result)
                    ):
                        for _ in range(2):
                            try:
                                os.unlink(temporary_name, dir_fd=run_fd)
                            except FileNotFoundError:
                                cleanup_error = None
                                break
                            except OSError as error:
                                cleanup_error = error
                            else:
                                cleanup_error = None
                                break
            if temporary_fd is not None:
                _close_descriptor(
                    temporary_fd,
                    description="close temporary artifact",
                    failures=cleanup_failures,
                )
            if process_locked:
                try:
                    fcntl.flock(run_fd, fcntl.LOCK_UN)
                except OSError as error:
                    cleanup_failures.append(
                        _cleanup_failure("unlock artifact run", error)
                    )
            try:
                self._close_posix_run(root_fd, artifacts_fd, run_fd)
            except FCPMCPError as error:
                cleanup_failures.append(error.message)
            if cleanup_error is not None:
                cleanup_failures.append(
                    _cleanup_failure(
                        "remove private artifact temporary file",
                        cleanup_error,
                    )
                )
            _attach_cleanup_errors(primary_error, cleanup_failures)
        return self._metadata(run_id, kind, payload)

    @staticmethod
    def _metadata(
        run_id: str,
        kind: ArtifactKind,
        payload: bytes,
    ) -> ArtifactMetadataV1:
        return ArtifactMetadataV1(
            run_id=run_id,
            kind=kind,
            relative_path=_relative_artifact_path(run_id, kind),
            sha256=hashlib.sha256(payload).hexdigest(),
            byte_size=len(payload),
            created_at=datetime.now(timezone.utc),
        )

    def _validated_metadata(self, metadata: ArtifactMetadataV1) -> tuple[str, ArtifactKind]:
        if not isinstance(metadata, ArtifactMetadataV1):
            raise _coded(ErrorCode.ARTIFACT_CORRUPT, "artifact metadata has the wrong type")
        try:
            validated = ArtifactMetadataV1.model_validate(
                metadata.model_dump(mode="python", warnings=False)
            )
        except (ValidationError, TypeError, ValueError) as error:
            raise _coded(ErrorCode.ARTIFACT_CORRUPT, "artifact metadata is not canonical", error)
        run_id = validated.run_id
        kind = validated.kind
        expected = _relative_artifact_path(run_id, kind)
        if metadata.relative_path != expected:
            raise _coded(
                ErrorCode.ARTIFACT_CORRUPT,
                "artifact metadata relative path does not match its identity",
            )
        if (
            not _SHA256_PATTERN.fullmatch(metadata.sha256)
            or type(metadata.byte_size) is not int
            or metadata.byte_size < 0
            or metadata.byte_size > self.max_artifact_bytes
        ):
            raise _coded(ErrorCode.ARTIFACT_CORRUPT, "artifact metadata bounds are invalid")
        return run_id, kind

    def read(self, metadata: ArtifactMetadataV1) -> bytes:
        run_id, kind = self._validated_metadata(metadata)
        with _run_thread_guard(self.paths.root, run_id):
            return self._read_posix(metadata, run_id, kind)

    def _read_fd(self, fd: int, metadata: ArtifactMetadataV1) -> bytes:
        result = os.fstat(fd)
        if not stat.S_ISREG(result.st_mode):
            raise _coded(ErrorCode.ARTIFACT_CORRUPT, "recorded artifact is not a regular file")
        if stat.S_IMODE(result.st_mode) != _FILE_MODE:
            raise _coded(ErrorCode.ARTIFACT_CORRUPT, "recorded artifact mode is not private")
        if result.st_size != metadata.byte_size or result.st_size > self.max_artifact_bytes:
            raise _coded(ErrorCode.ARTIFACT_CORRUPT, "recorded artifact size does not match")
        chunks: list[bytes] = []
        total = 0
        while total <= self.max_artifact_bytes:
            remaining = metadata.byte_size + 1 - total
            if remaining <= 0:
                break
            chunk = os.read(
                fd,
                min(_READ_CHUNK_BYTES, remaining),
            )
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        payload = b"".join(chunks)
        if len(payload) != metadata.byte_size:
            raise _coded(ErrorCode.ARTIFACT_CORRUPT, "recorded artifact size changed during read")
        if hashlib.sha256(payload).hexdigest() != metadata.sha256:
            raise _coded(ErrorCode.ARTIFACT_CORRUPT, "recorded artifact hash does not match")
        return payload

    def _read_posix(
        self,
        metadata: ArtifactMetadataV1,
        run_id: str,
        kind: ArtifactKind,
    ) -> bytes:
        root_fd: int | None = None
        artifacts_fd: int | None = None
        run_fd: int | None = None
        artifact_fd: int | None = None
        process_locked = False
        try:
            import fcntl

            root_fd, artifacts_fd, run_fd = self._open_posix_run(run_id, create=False)
            fcntl.flock(run_fd, fcntl.LOCK_SH)
            process_locked = True
            artifact_fd = os.open(
                _ARTIFACT_NAMES[kind],
                _file_open_flags() | getattr(os, "O_BINARY", 0),
                dir_fd=run_fd,
            )
            payload = self._read_fd(artifact_fd, metadata)
            self._revalidate_posix_run(
                run_id,
                root_fd=root_fd,
                artifacts_fd=artifacts_fd,
                run_fd=run_fd,
                target_name=_ARTIFACT_NAMES[kind],
                artifact_fd=artifact_fd,
            )
            return payload
        except FCPMCPError as error:
            if error.code is ErrorCode.ARTIFACT_CORRUPT:
                raise
            raise _coded(ErrorCode.ARTIFACT_CORRUPT, "recorded artifact cannot be read", error)
        except OSError as error:
            raise _coded(ErrorCode.ARTIFACT_CORRUPT, "recorded artifact cannot be read", error)
        finally:
            primary_error = sys.exc_info()[1]
            cleanup_failures: list[str] = []
            if artifact_fd is not None:
                _close_descriptor(
                    artifact_fd,
                    description="close recorded artifact",
                    failures=cleanup_failures,
                )
            if process_locked and run_fd is not None:
                try:
                    fcntl.flock(run_fd, fcntl.LOCK_UN)
                except OSError as error:
                    cleanup_failures.append(
                        _cleanup_failure("unlock artifact run", error)
                    )
            if root_fd is not None and artifacts_fd is not None and run_fd is not None:
                try:
                    self._close_posix_run(root_fd, artifacts_fd, run_fd)
                except FCPMCPError as error:
                    cleanup_failures.append(error.message)
            _attach_cleanup_errors(primary_error, cleanup_failures)

    def verify(self, metadata: ArtifactMetadataV1) -> bool:
        self.read(metadata)
        return True


__all__ = [
    "ArtifactKind",
    "ArtifactMetadataV1",
    "ArtifactStore",
    "StatePaths",
    "enforce_source_size",
    "render_diff_summary",
]
