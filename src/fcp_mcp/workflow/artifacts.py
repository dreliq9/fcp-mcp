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
import threading
from contextlib import ExitStack, contextmanager
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


_ARTIFACT_NAMES = {
    ArtifactKind.CANDIDATE: "candidate.fcpxml",
    ArtifactKind.DIFF: "diff.json",
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
        "artifact kind must be candidate or diff",
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


def _is_reparse_stat(result: object) -> bool:
    """Return whether a stat-like result carries the Windows reparse bit."""
    attributes = getattr(result, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)


def _windows_attributes_mark_reparse(path: Path) -> bool:
    """Use the Python-3.10-compatible Win32 boundary when stat lacks evidence."""
    if os.name != "nt":
        return False
    try:
        import ctypes

        get_attributes = ctypes.windll.kernel32.GetFileAttributesW
        get_attributes.argtypes = [ctypes.c_wchar_p]
        get_attributes.restype = ctypes.c_uint32
        attributes = int(get_attributes(str(path)))
    except (AttributeError, OSError):
        return False
    invalid_attributes = 0xFFFFFFFF
    return attributes != invalid_attributes and bool(
        attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _acquire_windows_directory_handles(path: Path) -> list[int]:
    """Hold every directory component open without delete sharing on Windows."""
    if os.name != "nt":
        return []
    try:
        import ctypes
        from ctypes import wintypes

        class _ByHandleFileInformation(ctypes.Structure):
            _fields_ = [
                ("dwFileAttributes", wintypes.DWORD),
                ("ftCreationTime", wintypes.FILETIME),
                ("ftLastAccessTime", wintypes.FILETIME),
                ("ftLastWriteTime", wintypes.FILETIME),
                ("dwVolumeSerialNumber", wintypes.DWORD),
                ("nFileSizeHigh", wintypes.DWORD),
                ("nFileSizeLow", wintypes.DWORD),
                ("nNumberOfLinks", wintypes.DWORD),
                ("nFileIndexHigh", wintypes.DWORD),
                ("nFileIndexLow", wintypes.DWORD),
            ]

        kernel32 = ctypes.windll.kernel32
        create_file = kernel32.CreateFileW
        create_file.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        create_file.restype = wintypes.HANDLE
        get_information = kernel32.GetFileInformationByHandle
        get_information.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_ByHandleFileInformation),
        ]
        get_information.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL
    except (AttributeError, OSError) as error:
        raise _coded(
            ErrorCode.ARTIFACT_CORRUPT,
            "safe Windows directory-handle validation is unavailable",
            error,
        )

    file_read_attributes = 0x0080
    share_read_write = 0x00000001 | 0x00000002
    open_existing = 3
    backup_semantics = 0x02000000
    open_reparse_point = 0x00200000
    directory_attribute = 0x00000010
    reparse_attribute = 0x00000400
    invalid_handle = ctypes.c_void_p(-1).value
    handles: list[int] = []
    current = Path(path.anchor)
    components: list[Path] = []
    for component in path.parts[1:]:
        current /= component
        components.append(current)
    if not components:
        components.append(current)
    try:
        for component_path in components:
            handle = create_file(
                str(component_path),
                file_read_attributes,
                share_read_write,
                None,
                open_existing,
                backup_semantics | open_reparse_point,
                None,
            )
            handle_value = int(handle) if handle is not None else 0
            if handle_value in {0, invalid_handle}:
                raise OSError(ctypes.get_last_error(), "CreateFileW failed")
            information = _ByHandleFileInformation()
            if not get_information(handle, ctypes.byref(information)):
                close_handle(handle)
                raise OSError(ctypes.get_last_error(), "GetFileInformationByHandle failed")
            if not information.dwFileAttributes & directory_attribute:
                close_handle(handle)
                raise OSError(errno.ENOTDIR, "Windows path component is not a directory")
            if information.dwFileAttributes & reparse_attribute:
                close_handle(handle)
                raise OSError(errno.ELOOP, "Windows path component is a reparse point")
            handles.append(handle_value)
    except OSError as error:
        for handle_value in reversed(handles):
            close_handle(handle_value)
        raise _coded(
            ErrorCode.ARTIFACT_CORRUPT,
            "cannot hold a safe Windows artifact directory chain",
            error,
        )
    return handles


def _release_windows_handles(handles: list[int]) -> None:
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes

        close_handle = ctypes.windll.kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL
        for handle in reversed(handles):
            close_handle(handle)
    except (AttributeError, OSError) as error:
        raise _coded(
            ErrorCode.ARTIFACT_CORRUPT,
            "cannot release Windows artifact directory handles",
            error,
        )


@contextmanager
def _windows_directory_guard(path: Path):
    """Prevent rename/reparse swaps while fallback pathname I/O is active."""
    handles = _acquire_windows_directory_handles(path)
    try:
        yield
    finally:
        _release_windows_handles(handles)


@contextmanager
def _windows_run_mutex(root: Path, run_id: str):
    """Serialize a run's aggregate check and commit across Windows processes."""
    if os.name != "nt":
        yield
        return
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        create_mutex = kernel32.CreateMutexW
        create_mutex.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        create_mutex.restype = wintypes.HANDLE
        wait_for_single_object = kernel32.WaitForSingleObject
        wait_for_single_object.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        wait_for_single_object.restype = wintypes.DWORD
        release_mutex = kernel32.ReleaseMutex
        release_mutex.argtypes = [wintypes.HANDLE]
        release_mutex.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL
        mutex_name = "Local\\fcp-mcp-artifact-" + hashlib.sha256(
            f"{root}\0{run_id}".encode()
        ).hexdigest()
        handle = create_mutex(None, False, mutex_name)
        if not handle:
            raise OSError(ctypes.get_last_error(), "CreateMutexW failed")
        wait_result = wait_for_single_object(handle, 0xFFFFFFFF)
        if wait_result not in {0x00000000, 0x00000080}:
            close_handle(handle)
            raise OSError(ctypes.get_last_error(), "WaitForSingleObject failed")
    except (AttributeError, OSError) as error:
        raise _coded(
            ErrorCode.TRANSACTION_FAILED,
            "safe Windows artifact serialization is unavailable",
            error,
        )
    try:
        yield
    finally:
        release_mutex(handle)
        close_handle(handle)


def _fallback_open_file(
    path: Path,
    *,
    write: bool,
    exclusive: bool = False,
    failure_code: ErrorCode = ErrorCode.ARTIFACT_CORRUPT,
) -> int:
    """Open a final fallback component itself, never its reparse target."""
    if os.name != "nt":
        flags = _file_open_flags(write=write) | getattr(os, "O_BINARY", 0)
        if exclusive:
            flags |= os.O_CREAT | os.O_EXCL
        try:
            return os.open(path, flags, _FILE_MODE)
        except OSError as error:
            raise _coded(failure_code, "fallback artifact file open failed", error)
    try:
        import ctypes
        import msvcrt
        from ctypes import wintypes

        class _ByHandleFileInformation(ctypes.Structure):
            _fields_ = [
                ("dwFileAttributes", wintypes.DWORD),
                ("ftCreationTime", wintypes.FILETIME),
                ("ftLastAccessTime", wintypes.FILETIME),
                ("ftLastWriteTime", wintypes.FILETIME),
                ("dwVolumeSerialNumber", wintypes.DWORD),
                ("nFileSizeHigh", wintypes.DWORD),
                ("nFileSizeLow", wintypes.DWORD),
                ("nNumberOfLinks", wintypes.DWORD),
                ("nFileIndexHigh", wintypes.DWORD),
                ("nFileIndexLow", wintypes.DWORD),
            ]

        kernel32 = ctypes.windll.kernel32
        create_file = kernel32.CreateFileW
        create_file.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        create_file.restype = wintypes.HANDLE
        get_attributes = kernel32.GetFileInformationByHandle
        get_attributes.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_ByHandleFileInformation),
        ]
        get_attributes.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL

        desired_access = 0x40000000 if write else 0x80000000
        share_mode = 0 if write else 0x00000001
        creation = 1 if exclusive else 3
        attributes = 0x00000080 | 0x00200000
        handle = create_file(
            str(path),
            desired_access,
            share_mode,
            None,
            creation,
            attributes,
            None,
        )
        invalid_handle = ctypes.c_void_p(-1).value
        handle_value = int(handle) if handle is not None else 0
        if handle_value in {0, invalid_handle}:
            raise OSError(ctypes.get_last_error(), "CreateFileW failed")
        information = _ByHandleFileInformation()
        if not get_attributes(handle, ctypes.byref(information)):
            close_handle(handle)
            raise OSError(ctypes.get_last_error(), "GetFileInformationByHandle failed")
        if information.dwFileAttributes & (0x00000010 | 0x00000400):
            close_handle(handle)
            raise _coded(
                ErrorCode.ARTIFACT_CORRUPT,
                "artifact file is a directory or reparse point",
            )
        flags = (os.O_WRONLY if write else os.O_RDONLY) | getattr(os, "O_BINARY", 0)
        try:
            return msvcrt.open_osfhandle(handle_value, flags)
        except OSError:
            close_handle(handle)
            raise
    except FCPMCPError:
        raise
    except (AttributeError, OSError) as error:
        raise _coded(
            failure_code,
            "safe Windows artifact file open is unavailable",
            error,
        )


def _is_link_or_reparse(path: Path, result: os.stat_result) -> bool:
    return (
        stat.S_ISLNK(result.st_mode)
        or _is_reparse_stat(result)
        or _windows_attributes_mark_reparse(path)
    )


def _require_exact_mode(
    result: os.stat_result,
    expected: int,
    *,
    code: ErrorCode,
    description: str,
) -> None:
    if os.name == "posix" and stat.S_IMODE(result.st_mode) != expected:
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
    if _is_link_or_reparse(path, result):
        raise _coded(code, f"{description} must not be a symlink or reparse point")
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
    if not stat.S_ISDIR(result.st_mode) or _is_reparse_stat(result):
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
    except FileNotFoundError:
        if not create:
            raise
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
    except BaseException:
        os.close(fd)
        raise
    if created and os.name == "posix":
        try:
            os.fsync(parent_fd)
        except OSError as error:
            os.close(fd)
            raise _coded(code, f"cannot durably create {description}", error)
    return fd, created


def _open_absolute_directory_chain(
    path: Path,
    *,
    create: bool,
    code: ErrorCode,
    description: str,
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
    try:
        for index, component in enumerate(parts[1:]):
            is_leaf = index == len(parts[1:]) - 1
            next_fd, _ = _open_directory_at(
                current_fd,
                component,
                create=create,
                require_private_mode=is_leaf,
                code=code,
                description=description if is_leaf else "state parent directory",
            )
            os.close(current_fd)
            current_fd = next_fd
        _validate_directory_fd(
            current_fd,
            code=code,
            description=description,
            require_private_mode=True,
        )
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


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
    if stat.S_ISLNK(result.st_mode) or _is_reparse_stat(result):
        raise _coded(code, f"{description} must not be a symlink or reparse point")
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


def _validate_fallback_chain(path: Path, *, code: ErrorCode, description: str) -> None:
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            result = current.lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise _coded(code, f"cannot inspect {description}", error)
        if _is_link_or_reparse(current, result):
            raise _coded(code, f"{description} must not cross a symlink or reparse point")


def _initialize_fallback_state(root: Path) -> None:
    current = Path(root.anchor)
    with ExitStack() as handles:
        handles.enter_context(_windows_directory_guard(current))
        for component in root.parts[1:]:
            current /= component
            _validate_fallback_chain(
                current,
                code=ErrorCode.INVALID_CONFIGURATION,
                description="workflow state root",
            )
            try:
                current.mkdir(mode=_DIRECTORY_MODE, exist_ok=True)
            except OSError as error:
                raise _coded(
                    ErrorCode.INVALID_CONFIGURATION,
                    "cannot create workflow state root",
                    error,
                )
            handles.enter_context(_windows_directory_guard(current))
        _validate_existing_path(
            root,
            expected="directory",
            code=ErrorCode.INVALID_CONFIGURATION,
            description="workflow state root",
            require_private_mode=False,
        )
        _validate_existing_path(
            root / "runs.sqlite3",
            expected="file",
            code=ErrorCode.INVALID_CONFIGURATION,
            description="workflow database",
            require_private_mode=False,
        )
        for name in ("artifacts", "locks"):
            child = root / name
            _validate_existing_path(
                child,
                expected="directory",
                code=ErrorCode.INVALID_CONFIGURATION,
                description=f"workflow {name} directory",
                require_private_mode=False,
            )
            try:
                child.mkdir(mode=_DIRECTORY_MODE, exist_ok=True)
            except OSError as error:
                raise _coded(
                    ErrorCode.INVALID_CONFIGURATION,
                    f"cannot create workflow {name} directory",
                    error,
                )
            handles.enter_context(_windows_directory_guard(child))


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
        if os.name == "posix":
            _initialize_posix_state(root)
        else:
            _initialize_fallback_state(root)
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
            require_private_mode=os.name == "posix",
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
            require_private_mode=os.name == "posix",
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
            require_private_mode=os.name == "posix",
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
        except FileNotFoundError as error:
            for fd in (
                locals().get("artifacts_fd"),
                locals().get("root_fd"),
            ):
                if isinstance(fd, int):
                    os.close(fd)
            raise _coded(ErrorCode.ARTIFACT_CORRUPT, "artifact path is missing", error)
        except BaseException:
            for fd in (
                locals().get("artifacts_fd"),
                locals().get("root_fd"),
            ):
                if isinstance(fd, int):
                    os.close(fd)
            raise

    @staticmethod
    def _close_posix_run(root_fd: int, artifacts_fd: int, run_fd: int) -> None:
        os.close(run_fd)
        os.close(artifacts_fd)
        os.close(root_fd)

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
        with (
            _run_thread_guard(self.paths.root, canonical),
            _windows_run_mutex(self.paths.root, canonical),
        ):
            if os.name == "posix":
                return self._write_posix(canonical, closed_kind, committed_bytes)
            return self._write_fallback(canonical, closed_kind, committed_bytes)

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
        replaced = False
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
            temporary_name = f".{target_name}.{secrets.token_hex(16)}.tmp"
            flags = (
                _file_open_flags(write=True)
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_BINARY", 0)
            )
            temporary_fd = os.open(temporary_name, flags, _FILE_MODE, dir_fd=run_fd)
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
                or _is_reparse_stat(result)
                or result.st_size != len(payload)
                or stat.S_IMODE(result.st_mode) != _FILE_MODE
            ):
                raise OSError(errno.EIO, "temporary artifact validation failed")
            os.close(temporary_fd)
            temporary_fd = None
            os.replace(
                temporary_name,
                target_name,
                src_dir_fd=run_fd,
                dst_dir_fd=run_fd,
            )
            replaced = True
            os.fsync(run_fd)
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
            if temporary_fd is not None:
                try:
                    os.close(temporary_fd)
                except OSError:
                    pass
            if temporary_name is not None:
                cleanup_error: OSError | None = None
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
                if cleanup_error is not None:
                    if process_locked:
                        fcntl.flock(run_fd, fcntl.LOCK_UN)
                    self._close_posix_run(root_fd, artifacts_fd, run_fd)
                    raise _coded(
                        ErrorCode.TRANSACTION_FAILED,
                        "failed to clean up private artifact temporary file",
                        cleanup_error,
                    )
            if process_locked:
                fcntl.flock(run_fd, fcntl.LOCK_UN)
            self._close_posix_run(root_fd, artifacts_fd, run_fd)
        return self._metadata(run_id, kind, payload)

    def _fallback_existing_sizes(
        self,
        run_directory: Path,
    ) -> dict[ArtifactKind, int]:
        sizes: dict[ArtifactKind, int] = {}
        for kind, name in _ARTIFACT_NAMES.items():
            path = run_directory / name
            _validate_fallback_chain(
                path,
                code=ErrorCode.ARTIFACT_CORRUPT,
                description=f"{kind.value} artifact",
            )
            _validate_existing_path(
                path,
                expected="file",
                code=ErrorCode.ARTIFACT_CORRUPT,
                description=f"{kind.value} artifact",
                require_private_mode=False,
            )
            try:
                result = path.lstat()
            except FileNotFoundError:
                continue
            if _is_link_or_reparse(path, result) or not stat.S_ISREG(result.st_mode):
                raise _coded(
                    ErrorCode.ARTIFACT_CORRUPT,
                    f"{kind.value} artifact is not a safe regular file",
                )
            sizes[kind] = result.st_size
        return sizes

    def _write_fallback(
        self,
        run_id: str,
        kind: ArtifactKind,
        payload: bytes,
    ) -> ArtifactMetadataV1:
        run_directory = self.paths.root / "artifacts" / run_id
        with _windows_directory_guard(run_directory.parent):
            _validate_fallback_chain(
                run_directory,
                code=ErrorCode.ARTIFACT_CORRUPT,
                description="artifact run directory",
            )
            try:
                run_directory.mkdir(mode=_DIRECTORY_MODE, exist_ok=True)
            except OSError as error:
                raise _coded(
                    ErrorCode.TRANSACTION_FAILED,
                    "cannot create artifact run directory",
                    error,
                )
            with _windows_directory_guard(run_directory):
                return self._write_guarded_fallback(run_id, kind, payload, run_directory)

    def _write_guarded_fallback(
        self,
        run_id: str,
        kind: ArtifactKind,
        payload: bytes,
        run_directory: Path,
    ) -> ArtifactMetadataV1:
        _validate_fallback_chain(
            run_directory,
            code=ErrorCode.ARTIFACT_CORRUPT,
            description="artifact run directory",
        )
        _validate_existing_path(
            run_directory,
            expected="directory",
            code=ErrorCode.ARTIFACT_CORRUPT,
            description="artifact run directory",
            require_private_mode=False,
        )
        self._enforce_aggregate(self._fallback_existing_sizes(run_directory), kind, len(payload))
        target = run_directory / _ARTIFACT_NAMES[kind]
        temporary = run_directory / f".{target.name}.{secrets.token_hex(16)}.tmp"
        fd: int | None = None
        replaced = False
        try:
            _validate_fallback_chain(
                temporary,
                code=ErrorCode.ARTIFACT_CORRUPT,
                description="temporary artifact",
            )
            fd = _fallback_open_file(
                temporary,
                write=True,
                exclusive=True,
                failure_code=ErrorCode.TRANSACTION_FAILED,
            )
            view = memoryview(payload)
            written = 0
            while written < len(view):
                count = os.write(fd, view[written:])
                if count <= 0:
                    raise OSError(errno.EIO, "artifact write made no progress")
                written += count
            os.fsync(fd)
            result = os.fstat(fd)
            if not stat.S_ISREG(result.st_mode) or _is_reparse_stat(result):
                raise OSError(errno.EIO, "temporary artifact validation failed")
            if result.st_size != len(payload):
                raise OSError(errno.EIO, "temporary artifact size mismatch")
            os.close(fd)
            fd = None
            _validate_fallback_chain(
                run_directory,
                code=ErrorCode.ARTIFACT_CORRUPT,
                description="artifact run directory",
            )
            _validate_fallback_chain(
                target,
                code=ErrorCode.ARTIFACT_CORRUPT,
                description=f"{kind.value} artifact target",
            )
            _validate_existing_path(
                target,
                expected="file",
                code=ErrorCode.ARTIFACT_CORRUPT,
                description=f"{kind.value} artifact target",
                require_private_mode=False,
            )
            _validate_existing_path(
                temporary,
                expected="file",
                code=ErrorCode.ARTIFACT_CORRUPT,
                description="temporary artifact",
                require_private_mode=False,
            )
            os.replace(temporary, target)
            replaced = True
            _validate_fallback_chain(
                target,
                code=ErrorCode.ARTIFACT_CORRUPT,
                description=f"{kind.value} artifact target",
            )
            _validate_existing_path(
                target,
                expected="file",
                code=ErrorCode.ARTIFACT_CORRUPT,
                description=f"{kind.value} artifact target",
                require_private_mode=False,
            )
        except OSError as error:
            action = "commit" if replaced else "write"
            raise _coded(ErrorCode.TRANSACTION_FAILED, f"failed to {action} private artifact", error)
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            cleanup_error: OSError | None = None
            for _ in range(2):
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    cleanup_error = None
                    break
                except OSError as error:
                    cleanup_error = error
                else:
                    cleanup_error = None
                    break
            if cleanup_error is not None:
                raise _coded(
                    ErrorCode.TRANSACTION_FAILED,
                    "failed to clean up private artifact temporary file",
                    cleanup_error,
                )
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
        if os.name == "posix":
            return self._read_posix(metadata, run_id, kind)
        return self._read_fallback(metadata, run_id, kind)

    def _read_fd(self, fd: int, metadata: ArtifactMetadataV1) -> bytes:
        result = os.fstat(fd)
        if not stat.S_ISREG(result.st_mode) or _is_reparse_stat(result):
            raise _coded(ErrorCode.ARTIFACT_CORRUPT, "recorded artifact is not a regular file")
        if os.name == "posix" and stat.S_IMODE(result.st_mode) != _FILE_MODE:
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
        try:
            root_fd, artifacts_fd, run_fd = self._open_posix_run(run_id, create=False)
            artifact_fd = os.open(
                _ARTIFACT_NAMES[kind],
                _file_open_flags() | getattr(os, "O_BINARY", 0),
                dir_fd=run_fd,
            )
            return self._read_fd(artifact_fd, metadata)
        except FCPMCPError as error:
            if error.code is ErrorCode.ARTIFACT_CORRUPT:
                raise
            raise _coded(ErrorCode.ARTIFACT_CORRUPT, "recorded artifact cannot be read", error)
        except OSError as error:
            raise _coded(ErrorCode.ARTIFACT_CORRUPT, "recorded artifact cannot be read", error)
        finally:
            if artifact_fd is not None:
                os.close(artifact_fd)
            if root_fd is not None and artifacts_fd is not None and run_fd is not None:
                self._close_posix_run(root_fd, artifacts_fd, run_fd)

    def _read_fallback(
        self,
        metadata: ArtifactMetadataV1,
        run_id: str,
        kind: ArtifactKind,
    ) -> bytes:
        target = self.paths.root / _relative_artifact_path(run_id, kind)
        run_directory = target.parent
        with _windows_directory_guard(run_directory):
            _validate_fallback_chain(
                run_directory,
                code=ErrorCode.ARTIFACT_CORRUPT,
                description="artifact run directory",
            )
            _validate_fallback_chain(
                target,
                code=ErrorCode.ARTIFACT_CORRUPT,
                description="recorded artifact",
            )
            _validate_existing_path(
                target,
                expected="file",
                code=ErrorCode.ARTIFACT_CORRUPT,
                description="recorded artifact",
                require_private_mode=False,
            )
            fd: int | None = None
            try:
                fd = _fallback_open_file(target, write=False)
                opened_result = os.fstat(fd)
                payload = self._read_fd(fd, metadata)
                _validate_fallback_chain(
                    target,
                    code=ErrorCode.ARTIFACT_CORRUPT,
                    description="recorded artifact",
                )
                current_result = target.lstat()
                if (
                    _is_link_or_reparse(target, current_result)
                    or not stat.S_ISREG(current_result.st_mode)
                    or not os.path.samestat(opened_result, current_result)
                ):
                    raise _coded(
                        ErrorCode.ARTIFACT_CORRUPT,
                        "recorded artifact changed during read",
                    )
                return payload
            except FCPMCPError:
                raise
            except OSError as error:
                raise _coded(
                    ErrorCode.ARTIFACT_CORRUPT,
                    "recorded artifact cannot be read",
                    error,
                )
            finally:
                if fd is not None:
                    os.close(fd)

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
