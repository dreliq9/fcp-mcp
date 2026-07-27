from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
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
    if os.name != "posix":
        return
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY)
        os.fsync(descriptor)
    except OSError:
        return
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _backup_name(destination: Path, transaction_id: str) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return destination.with_name(f"{destination.name}.bak.{timestamp}.{transaction_id}")


def atomic_replace_bytes(
    destination: str | Path,
    payload: bytes,
    *,
    validate: Callable[[Path], None] | None = None,
    event_format: str = "text",
    transaction_id: str | None = None,
) -> AtomicWriteReceipt:
    started = time.perf_counter()
    destination_path = Path(destination).expanduser().resolve()
    transaction_id = transaction_id or str(uuid.uuid4())
    parent = destination_path.parent

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
            prior_sha256 = _sha256(destination_path)
            backup_path = _backup_name(destination_path, transaction_id)
            shutil.copy2(destination_path, backup_path)
            with backup_path.open("rb") as backup:
                os.fsync(backup.fileno())

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
