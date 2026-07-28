from __future__ import annotations

import hashlib
import os
import stat
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from fcp_mcp.contracts import ErrorCode, FCPMCPError
from fcp_mcp.observability import emit_event


@dataclass(frozen=True)
class AtomicWriteReceipt:
    transaction_id: str
    destination: Path
    backup_path: Path | None
    prior_sha256: str | None
    output_sha256: str
    elapsed_ms: int


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sync_directory(path: Path) -> None:
    descriptor = None
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        os.fsync(descriptor)
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _backup_name(destination: Path, transaction_id: str) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return destination.with_name(f"{destination.name}.bak.{timestamp}.{transaction_id}")


def _copy_backup_exclusive(source: Path, backup: Path) -> str:
    source_descriptor = -1
    backup_descriptor = -1
    backup_identity: tuple[int, int] | None = None
    complete = False
    try:
        source_descriptor = os.open(
            source,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(source_descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise FCPMCPError(
                ErrorCode.INVALID_PATH,
                "Output destination is not a regular file",
            )
        backup_descriptor = os.open(
            backup,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        created = os.fstat(backup_descriptor)
        backup_identity = (created.st_dev, created.st_ino)
        if (
            not stat.S_ISREG(created.st_mode)
            or stat.S_IMODE(created.st_mode) != 0o600
            or created.st_uid != os.geteuid()
            or created.st_nlink != 1
        ):
            raise FCPMCPError(
                ErrorCode.TRANSACTION_FAILED,
                "Backup creation invariants failed",
            )
        digest = hashlib.sha256()
        copied = 0
        while True:
            chunk = os.read(source_descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(backup_descriptor, view)
                if written <= 0:
                    raise OSError("short backup write")
                view = view[written:]
            copied += len(chunk)
        os.fsync(backup_descriptor)
        after = os.fstat(source_descriptor)
        current = source.lstat()
        entry = backup.lstat()
        if (
            (opened.st_dev, opened.st_ino) != (after.st_dev, after.st_ino)
            or opened.st_size != after.st_size
            or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
            or copied != opened.st_size
            or backup_identity != (entry.st_dev, entry.st_ino)
        ):
            raise FCPMCPError(
                ErrorCode.TRANSACTION_FAILED,
                "Backup source or destination changed during copy",
            )
        complete = True
        return digest.hexdigest()
    except FileExistsError:
        raise FCPMCPError(
            ErrorCode.INVALID_PATH,
            "Preassigned backup path is no longer available",
        ) from None
    except FCPMCPError:
        raise
    except OSError as error:
        raise FCPMCPError(
            ErrorCode.TRANSACTION_FAILED,
            "Backup creation failed",
        ) from error
    finally:
        for descriptor in (backup_descriptor, source_descriptor):
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        if not complete and backup_identity is not None:
            try:
                entry = backup.lstat()
                if backup_identity == (entry.st_dev, entry.st_ino):
                    backup.unlink()
            except OSError:
                pass


def atomic_replace_bytes(
    destination: str | Path,
    payload: bytes,
    *,
    validate: Callable[[Path], None] | None = None,
    event_format: str = "text",
    transaction_id: str | None = None,
    backup_path: str | Path | None = None,
) -> AtomicWriteReceipt:
    started = time.perf_counter()
    destination_path = Path(destination).expanduser().resolve()
    transaction_id = transaction_id or str(uuid.uuid4())
    parent = destination_path.parent
    requested_backup = (
        Path(backup_path).expanduser().absolute()
        if backup_path is not None
        else None
    )

    if not parent.is_dir():
        raise FCPMCPError(
            ErrorCode.INVALID_PATH,
            f"Output parent directory does not exist: {parent}",
        )
    if destination_path.exists() and not destination_path.is_file():
        raise FCPMCPError(
            ErrorCode.INVALID_PATH,
            f"Output destination is not a file: {destination_path}",
        )
    if requested_backup is not None:
        expected_backup = parent / f"{destination_path.name}.bak.{transaction_id}"
        if (
            requested_backup != expected_backup
            or requested_backup.parent != parent
            or requested_backup.exists()
            or requested_backup.is_symlink()
        ):
            raise FCPMCPError(
                ErrorCode.INVALID_PATH,
                "Preassigned backup path is invalid",
            )

    temporary_path: Path | None = None
    backup_path: Path | None = None
    prior_sha256: str | None = None
    replaced = False

    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{destination_path.name}.",
            suffix=".tmp",
            dir=parent,
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())

        if validate is not None:
            validate(temporary_path)

        if destination_path.exists():
            backup_path = requested_backup or _backup_name(
                destination_path,
                transaction_id,
            )
            prior_sha256 = _copy_backup_exclusive(destination_path, backup_path)
            _sync_directory(parent)

        os.replace(temporary_path, destination_path)
        replaced = True
        _sync_directory(parent)

        if validate is not None:
            validate(destination_path)

        output_sha256 = _sha256(destination_path)
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        emit_event(
            {
                "event": "atomic_write",
                "transaction_id": transaction_id,
                "output_path": str(destination_path),
                "prior_sha256": prior_sha256,
                "output_sha256": output_sha256,
                "backup_path": str(backup_path) if backup_path else None,
                "elapsed_ms": elapsed_ms,
                "disposition": "committed",
            },
            format=event_format,
        )
        return AtomicWriteReceipt(
            transaction_id=transaction_id,
            destination=destination_path,
            backup_path=backup_path,
            prior_sha256=prior_sha256,
            output_sha256=output_sha256,
            elapsed_ms=elapsed_ms,
        )
    except Exception as error:
        disposition = "failed"
        rollback_error: OSError | None = None
        if replaced:
            try:
                if backup_path is not None:
                    os.replace(backup_path, destination_path)
                else:
                    destination_path.unlink(missing_ok=True)
                _sync_directory(parent)
                disposition = "rolled_back"
            except OSError as caught:
                rollback_error = caught

        emit_event(
            {
                "event": "atomic_write",
                "transaction_id": transaction_id,
                "output_path": str(destination_path),
                "backup_path": str(backup_path) if backup_path else None,
                "elapsed_ms": round((time.perf_counter() - started) * 1000),
                "disposition": disposition,
                "error_code": (
                    error.code.value if isinstance(error, FCPMCPError) else "transaction_failed"
                ),
            },
            format=event_format,
        )

        if rollback_error is not None:
            raise FCPMCPError(
                ErrorCode.TRANSACTION_FAILED,
                f"Commit failed and rollback failed: {rollback_error}",
            ) from error
        if isinstance(error, FCPMCPError):
            raise
        raise FCPMCPError(
            ErrorCode.TRANSACTION_FAILED,
            f"Atomic output commit failed: {error}",
        ) from error
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
