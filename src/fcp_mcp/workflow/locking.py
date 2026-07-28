"""Destination-scoped macOS commit locks with conservative owner assessment."""

from __future__ import annotations

import hashlib
import os
import socket
import stat
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from typing_extensions import Self

from fcp_mcp.contracts import ErrorCode, FCPMCPError
from fcp_mcp.workflow.artifacts import StatePaths
from fcp_mcp.workflow.models import canonical_json

_FILE_MODE = 0o600
_MAX_LOCK_BYTES = 4096


class OwnerLiveness(str, Enum):
    ALIVE = "alive"
    DEAD = "dead"
    UNKNOWN = "unknown"


def _coded(code: ErrorCode, message: str) -> FCPMCPError:
    return FCPMCPError(code, message)


def assess_owner_liveness(pid: int, host: str) -> OwnerLiveness:
    """Assess one same-host PID without ever treating ambiguity as dead."""
    if (
        type(pid) is not int
        or pid <= 0
        or not isinstance(host, str)
        or not host
        or host != socket.gethostname()
    ):
        return OwnerLiveness.UNKNOWN
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return OwnerLiveness.DEAD
    except (PermissionError, OSError):
        return OwnerLiveness.UNKNOWN
    return OwnerLiveness.ALIVE


class DestinationLock:
    """One atomic exclusive lock entry bound to a resolved destination."""

    def __init__(
        self,
        paths: StatePaths,
        destination: str | Path,
        run_id: str,
        *,
        clock: Callable[[], datetime] | None = None,
        pid: int | None = None,
        host: str | None = None,
    ) -> None:
        self.paths = paths
        self.destination = Path(destination).expanduser().resolve()
        self.destination_sha256 = hashlib.sha256(
            str(self.destination).encode("utf-8")
        ).hexdigest()
        self.path = paths.locks / f"{self.destination_sha256}.lock"
        self.run_id = run_id
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.pid = os.getpid() if pid is None else pid
        self.host = socket.gethostname() if host is None else host
        self._identity: tuple[int, int] | None = None

    def _payload(self) -> bytes:
        instant = self.clock()
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise _coded(ErrorCode.INVALID_ARGUMENTS, "lock clock must be timezone-aware")
        created_at = instant.astimezone(timezone.utc).isoformat(
            timespec="microseconds"
        ).replace("+00:00", "Z")
        body = canonical_json(
            {
                "created_at": created_at,
                "destination_sha256": self.destination_sha256,
                "host": self.host,
                "pid": self.pid,
                "run_id": self.run_id,
                "schema_version": "1",
            }
        )
        if len(body) + 1 > _MAX_LOCK_BYTES:
            raise _coded(ErrorCode.INVALID_ARGUMENTS, "lock evidence is too large")
        return body + b"\n"

    def acquire(self) -> DestinationLock:
        if self._identity is not None:
            raise _coded(ErrorCode.WORKFLOW_STATE_CONFLICT, "lock is already acquired")
        directory = -1
        descriptor = -1
        name = self.path.name
        try:
            directory = os.open(
                self.paths.locks,
                os.O_RDONLY
                | os.O_DIRECTORY
                | os.O_NOFOLLOW
                | getattr(os, "O_CLOEXEC", 0),
            )
            directory_stat = os.fstat(directory)
            canonical_stat = self.paths.locks.lstat()
            if (
                not stat.S_ISDIR(directory_stat.st_mode)
                or (directory_stat.st_dev, directory_stat.st_ino)
                != (canonical_stat.st_dev, canonical_stat.st_ino)
                or stat.S_IMODE(directory_stat.st_mode) != 0o700
            ):
                raise _coded(
                    ErrorCode.RECOVERY_REQUIRED,
                    "destination lock directory identity changed",
                )
            try:
                descriptor = os.open(
                    name,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | os.O_NOFOLLOW
                    | getattr(os, "O_CLOEXEC", 0),
                    _FILE_MODE,
                    dir_fd=directory,
                )
            except FileExistsError:
                raise _coded(
                    ErrorCode.RECOVERY_REQUIRED,
                    "destination lock is already held or ambiguous",
                ) from None
            os.fchmod(descriptor, _FILE_MODE)
            payload = self._payload()
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("short lock write")
                view = view[written:]
            os.fsync(descriptor)
            result = os.fstat(descriptor)
            entry = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if (
                not stat.S_ISREG(result.st_mode)
                or stat.S_IMODE(result.st_mode) != _FILE_MODE
                or result.st_uid != os.geteuid()
                or result.st_nlink != 1
                or (result.st_dev, result.st_ino) != (entry.st_dev, entry.st_ino)
            ):
                raise _coded(
                    ErrorCode.RECOVERY_REQUIRED,
                    "destination lock creation invariants failed",
                )
            os.fsync(directory)
            self._identity = (result.st_dev, result.st_ino)
            return self
        except FCPMCPError:
            raise
        except OSError as error:
            raise _coded(
                ErrorCode.RECOVERY_REQUIRED,
                "destination lock operation failed",
            ) from error
        finally:
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            if directory >= 0:
                try:
                    os.close(directory)
                except OSError:
                    pass

    def release(self) -> None:
        identity = self._identity
        if identity is None:
            return
        directory = -1
        try:
            directory = os.open(
                self.paths.locks,
                os.O_RDONLY
                | os.O_DIRECTORY
                | os.O_NOFOLLOW
                | getattr(os, "O_CLOEXEC", 0),
            )
            entry = os.stat(
                self.path.name,
                dir_fd=directory,
                follow_symlinks=False,
            )
            if (entry.st_dev, entry.st_ino) != identity:
                raise _coded(
                    ErrorCode.RECOVERY_REQUIRED,
                    "destination lock identity changed during release",
                )
            os.unlink(self.path.name, dir_fd=directory)
            os.fsync(directory)
            self._identity = None
        except FileNotFoundError:
            raise _coded(
                ErrorCode.RECOVERY_REQUIRED,
                "destination lock disappeared before release",
            ) from None
        except FCPMCPError:
            raise
        except OSError as error:
            raise _coded(
                ErrorCode.RECOVERY_REQUIRED,
                "destination lock release failed",
            ) from error
        finally:
            primary = sys.exc_info()[1]
            if directory >= 0:
                try:
                    os.close(directory)
                except OSError:
                    cleanup = _coded(
                        ErrorCode.RECOVERY_REQUIRED,
                        "destination lock cleanup failed",
                    )
                    if primary is None:
                        raise cleanup from None
                    if primary.__context__ is None:
                        primary.__context__ = cleanup

    def __enter__(self) -> Self:
        return self.acquire()

    def __exit__(self, *_: object) -> None:
        self.release()


__all__ = ["DestinationLock", "OwnerLiveness", "assess_owner_liveness"]
