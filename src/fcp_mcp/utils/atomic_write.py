from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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


@dataclass(frozen=True)
class AtomicBundleItem:
    destination: str | Path
    payload: bytes
    validate: Callable[[Path], None] | None = None


@dataclass(frozen=True)
class AtomicBundleItemReceipt:
    destination: Path
    backup_path: Path | None
    prior_sha256: str | None
    output_sha256: str
    output_size_bytes: int


@dataclass(frozen=True)
class AtomicBundleReceipt:
    transaction_id: str
    items: tuple[AtomicBundleItemReceipt, ...]
    elapsed_ms: int
    replayed: bool


@dataclass(frozen=True)
class _JournalItem:
    destination: Path
    stage: Path
    backup: Path | None
    prior_exists: bool
    prior_sha256: str | None
    output_sha256: str
    output_size_bytes: int


@dataclass(frozen=True)
class _BundleJournal:
    key: str
    transaction_id: str
    state: str
    items: tuple[_JournalItem, ...]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _regular_sha256(path: Path, *, code: ErrorCode) -> str:
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(descriptor)
        entry = path.lstat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (entry.st_dev, entry.st_ino)
        ):
            raise FCPMCPError(code, "Bundle artifact is not a stable regular file")
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(descriptor)
        current = path.lstat()
        if (
            (opened.st_dev, opened.st_ino) != (after.st_dev, after.st_ino)
            or opened.st_size != after.st_size
            or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
        ):
            raise FCPMCPError(code, "Bundle artifact changed while it was read")
        return digest.hexdigest()
    except FCPMCPError:
        raise
    except OSError as error:
        raise FCPMCPError(code, "Bundle artifact could not be verified") from error
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _sync_directory(path: Path) -> None:
    descriptor = None
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
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
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
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


def _bundle_error(code: ErrorCode, message: str, **details: Any) -> FCPMCPError:
    return FCPMCPError(code, message, details or None)


def _canonical_transaction_id(transaction_id: str | None) -> str:
    if transaction_id is None:
        return str(uuid.uuid4())
    try:
        parsed = uuid.UUID(transaction_id)
    except (AttributeError, TypeError, ValueError) as error:
        raise _bundle_error(
            ErrorCode.INVALID_ARGUMENTS,
            "Bundle transaction_id must be a UUID",
        ) from error
    canonical = str(parsed)
    if canonical != transaction_id.lower():
        raise _bundle_error(
            ErrorCode.INVALID_ARGUMENTS,
            "Bundle transaction_id must use canonical UUID form",
        )
    return canonical


def _normalize_bundle_items(
    items: Sequence[AtomicBundleItem],
) -> tuple[Path, tuple[AtomicBundleItem, ...]]:
    if len(items) != 2:
        raise _bundle_error(
            ErrorCode.INVALID_ARGUMENTS,
            "An atomic bundle requires exactly one artifact pair",
        )
    normalized: list[AtomicBundleItem] = []
    parent: Path | None = None
    destinations: set[Path] = set()
    for item in items:
        if not isinstance(item, AtomicBundleItem) or not isinstance(item.payload, bytes):
            raise _bundle_error(
                ErrorCode.INVALID_ARGUMENTS,
                "Bundle items require byte payloads",
            )
        requested = Path(item.destination).expanduser()
        absolute = requested if requested.is_absolute() else Path.cwd() / requested
        try:
            resolved_parent = absolute.parent.resolve(strict=True)
        except OSError as error:
            raise _bundle_error(
                ErrorCode.INVALID_PATH,
                "Bundle parent directory does not exist",
            ) from error
        if not resolved_parent.is_dir():
            raise _bundle_error(
                ErrorCode.INVALID_PATH,
                "Bundle parent is not a directory",
            )
        destination = resolved_parent / absolute.name
        if parent is None:
            parent = resolved_parent
        elif resolved_parent != parent:
            raise _bundle_error(
                ErrorCode.INVALID_PATH,
                "All bundle artifacts must use the same existing parent directory",
            )
        if destination in destinations:
            raise _bundle_error(
                ErrorCode.INVALID_PATH,
                "Bundle destination paths collide",
            )
        destinations.add(destination)
        try:
            entry = destination.lstat()
        except FileNotFoundError:
            pass
        except OSError as error:
            raise _bundle_error(
                ErrorCode.INVALID_PATH,
                "Bundle destination could not be inspected",
            ) from error
        else:
            if not stat.S_ISREG(entry.st_mode) or entry.st_nlink != 1:
                raise _bundle_error(
                    ErrorCode.INVALID_PATH,
                    "Bundle destinations must be non-symlink regular files",
                )
        normalized.append(AtomicBundleItem(destination, item.payload, item.validate))
    assert parent is not None
    primary = Path(normalized[0].destination)
    sidecar = Path(normalized[1].destination)
    expected_sidecar = primary.with_name(primary.stem + ".sources.json")
    if primary.suffix.lower() != ".fcpxml" or sidecar != expected_sidecar:
        raise _bundle_error(
            ErrorCode.INVALID_PATH,
            "Atomic bundles are limited to a canonical FCPXML and sources pair",
        )
    return parent, tuple(normalized)


def _bundle_paths(
    parent: Path,
    items: Sequence[AtomicBundleItem],
    transaction_id: str,
) -> tuple[str, Path, Path, Path, Path, tuple[Path, ...], tuple[Path, ...]]:
    key_material = "\0".join(sorted(Path(item.destination).name for item in items))
    key = hashlib.sha256(key_material.encode("utf-8")).hexdigest()[:24]
    lock = parent / f".fcp-mcp.bundle.{key}.lock"
    journal = parent / f".fcp-mcp.bundle.{key}.journal.json"
    journal_temp = parent / f".fcp-mcp.bundle.{key}.journal.tmp"
    receipt = parent / f".fcp-mcp.bundle.{key}.{transaction_id}.receipt.json"
    stages = tuple(
        parent / f".{Path(item.destination).name}.{transaction_id}.stage" for item in items
    )
    backups = tuple(
        parent / f"{Path(item.destination).name}.bak.{transaction_id}" for item in items
    )
    destinations = {Path(item.destination) for item in items}
    internals = {lock, journal, journal_temp, receipt, *stages, *backups}
    if len(internals) != 4 + len(stages) + len(backups) or destinations & internals:
        raise _bundle_error(
            ErrorCode.INVALID_PATH,
            "Bundle destination collides with transaction evidence",
        )
    return key, lock, journal, journal_temp, receipt, stages, backups


def _bundle_receipt_path(parent: Path, key: str, transaction_id: str) -> Path:
    return parent / f".fcp-mcp.bundle.{key}.{transaction_id}.receipt.json"


def _find_unjournaled_bundle_evidence(
    parent: Path,
    items: Sequence[AtomicBundleItem],
    journal_temp: Path,
) -> Path | None:
    stage_markers = tuple(
        (f".{Path(item.destination).name}.", ".stage") for item in items
    )
    try:
        for artifact in parent.iterdir():
            if artifact == journal_temp or any(
                artifact.name.startswith(prefix) and artifact.name.endswith(suffix)
                for prefix, suffix in stage_markers
            ):
                return artifact
    except OSError as error:
        raise _bundle_error(
            ErrorCode.RECOVERY_REQUIRED,
            "Bundle transaction evidence could not be inspected",
        ) from error
    return None


@contextmanager
def _exclusive_bundle_lock(path: Path) -> Iterator[None]:
    try:
        import fcntl
    except ImportError as error:  # pragma: no cover - the package is macOS-only
        raise _bundle_error(
            ErrorCode.RECOVERY_REQUIRED,
            "Bundle locking is unavailable on this platform",
        ) from error
    descriptor = -1
    locked = False
    try:
        descriptor = os.open(
            path,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        opened = os.fstat(descriptor)
        entry = path.lstat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_uid != os.geteuid()
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (entry.st_dev, entry.st_ino)
        ):
            raise _bundle_error(
                ErrorCode.RECOVERY_REQUIRED,
                "Bundle lock identity is ambiguous",
            )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as error:
            raise _bundle_error(
                ErrorCode.RECOVERY_REQUIRED,
                "A conflicting bundle transaction is still active",
                lock_path=str(path),
            ) from error
        locked = True
        _sync_directory(path.parent)
        yield
    except FCPMCPError:
        raise
    except OSError as error:
        raise _bundle_error(
            ErrorCode.RECOVERY_REQUIRED,
            "Bundle lock operation failed",
        ) from error
    finally:
        if descriptor >= 0:
            if locked:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                except OSError:
                    pass
            try:
                os.close(descriptor)
            except OSError:
                pass


def _write_exclusive(path: Path, payload: bytes) -> None:
    descriptor = -1
    created_identity: tuple[int, int] | None = None
    complete = False
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        opened = os.fstat(descriptor)
        created_identity = (opened.st_dev, opened.st_ino)
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_uid != os.geteuid()
            or opened.st_nlink != 1
        ):
            raise _bundle_error(
                ErrorCode.RECOVERY_REQUIRED,
                "Bundle evidence creation invariants failed",
            )
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short bundle write")
            view = view[written:]
        os.fsync(descriptor)
        entry = path.lstat()
        if (opened.st_dev, opened.st_ino) != (entry.st_dev, entry.st_ino):
            raise _bundle_error(
                ErrorCode.RECOVERY_REQUIRED,
                "Bundle evidence identity changed while it was written",
            )
        complete = True
    except FileExistsError:
        raise _bundle_error(
            ErrorCode.RECOVERY_REQUIRED,
            "Unexpected bundle transaction evidence already exists",
            evidence_path=str(path),
        ) from None
    except FCPMCPError:
        raise
    except OSError as error:
        raise _bundle_error(
            ErrorCode.TRANSACTION_FAILED,
            "Bundle evidence could not be written durably",
        ) from error
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if not complete and created_identity is not None:
            try:
                entry = path.lstat()
                if created_identity == (entry.st_dev, entry.st_ino):
                    path.unlink()
            except OSError:
                pass


def _journal_payload(journal: _BundleJournal) -> bytes:
    body = {
        "schema": "fcp-mcp.atomic-bundle/v1",
        "bundle_key": journal.key,
        "transaction_id": journal.transaction_id,
        "state": journal.state,
        "items": [
            {
                "destination_name": item.destination.name,
                "stage_name": item.stage.name,
                "backup_name": item.backup.name if item.backup else None,
                "prior_exists": item.prior_exists,
                "prior_sha256": item.prior_sha256,
                "output_sha256": item.output_sha256,
                "output_size_bytes": item.output_size_bytes,
            }
            for item in journal.items
        ],
    }
    canonical = json.dumps(
        body,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    body["journal_sha256"] = hashlib.sha256(canonical).hexdigest()
    return (
        json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
    ).encode("utf-8")


def _valid_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _load_bundle_journal(
    path: Path,
    *,
    key: str,
    destinations: Sequence[Path],
) -> _BundleJournal:
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(descriptor)
        entry = path.lstat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_uid != os.geteuid()
            or opened.st_nlink != 1
            or opened.st_size > 65536
            or (opened.st_dev, opened.st_ino) != (entry.st_dev, entry.st_ino)
        ):
            raise ValueError("journal is not a bounded regular file")
        chunks: list[bytes] = []
        read_size = 0
        while read_size <= 65536:
            chunk = os.read(descriptor, 65537 - read_size)
            if not chunk:
                break
            chunks.append(chunk)
            read_size += len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        current = path.lstat()
        if (
            read_size > 65536
            or opened.st_size != read_size
            or opened.st_size != after.st_size
            or (opened.st_dev, opened.st_ino) != (after.st_dev, after.st_ino)
            or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
        ):
            raise ValueError("journal identity or size changed")
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise TypeError("journal root is not an object")
        journal_sha256 = data.pop("journal_sha256", None)
        canonical = json.dumps(
            data,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        if (
            not _valid_sha256(journal_sha256)
            or hashlib.sha256(canonical).hexdigest() != journal_sha256
        ):
            raise ValueError("journal integrity mismatch")
        transaction_id = _canonical_transaction_id(data["transaction_id"])
        if (
            data.get("schema") != "fcp-mcp.atomic-bundle/v1"
            or data.get("bundle_key") != key
            or data.get("state") not in {"prepared", "committed", "rolled_back"}
            or not isinstance(data.get("items"), list)
            or len(data["items"]) != len(destinations)
        ):
            raise ValueError("journal contract mismatch")
        destinations_by_name = {destination.name: destination for destination in destinations}
        if len(destinations_by_name) != len(destinations):
            raise ValueError("destination names collide")
        parsed_items: list[_JournalItem] = []
        seen_destinations: set[str] = set()
        for raw_item in data["items"]:
            if not isinstance(raw_item, dict):
                raise TypeError("journal item is not an object")
            destination_name = raw_item.get("destination_name")
            if (
                not isinstance(destination_name, str)
                or destination_name in seen_destinations
                or destination_name not in destinations_by_name
            ):
                raise ValueError("journal destination identity mismatch")
            seen_destinations.add(destination_name)
            destination = destinations_by_name[destination_name]
            expected_stage = f".{destination.name}.{transaction_id}.stage"
            prior_exists = raw_item.get("prior_exists")
            prior_sha256 = raw_item.get("prior_sha256")
            backup_name = raw_item.get("backup_name")
            if (
                raw_item.get("stage_name") != expected_stage
                or type(prior_exists) is not bool
                or not _valid_sha256(raw_item.get("output_sha256"))
                or type(raw_item.get("output_size_bytes")) is not int
                or raw_item["output_size_bytes"] < 0
            ):
                raise ValueError("journal item identity mismatch")
            if prior_exists:
                expected_backup = f"{destination.name}.bak.{transaction_id}"
                if not _valid_sha256(prior_sha256) or backup_name != expected_backup:
                    raise ValueError("journal backup identity mismatch")
                backup = path.parent / backup_name
            else:
                if prior_sha256 is not None or backup_name is not None:
                    raise ValueError("journal records an impossible absent prior state")
                backup = None
            parsed_items.append(
                _JournalItem(
                    destination=destination,
                    stage=path.parent / expected_stage,
                    backup=backup,
                    prior_exists=prior_exists,
                    prior_sha256=prior_sha256,
                    output_sha256=raw_item["output_sha256"],
                    output_size_bytes=raw_item["output_size_bytes"],
                )
            )
        return _BundleJournal(
            key=key,
            transaction_id=transaction_id,
            state=data["state"],
            items=tuple(parsed_items),
        )
    except (
        FCPMCPError,
        KeyError,
        OSError,
        TypeError,
        UnicodeDecodeError,
        ValueError,
        json.JSONDecodeError,
    ) as error:
        raise _bundle_error(
            ErrorCode.RECOVERY_REQUIRED,
            "Bundle recovery journal is malformed or ambiguous",
            journal_path=str(path),
        ) from error
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _replace_bundle_journal(
    journal_path: Path,
    journal_temp: Path,
    journal: _BundleJournal,
) -> None:
    _write_exclusive(journal_temp, _journal_payload(journal))
    os.replace(journal_temp, journal_path)
    _sync_directory(journal_path.parent)


def _path_hash_or_absent(path: Path) -> str | None:
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    return _regular_sha256(path, code=ErrorCode.RECOVERY_REQUIRED)


def _classify_destination(item: _JournalItem) -> str:
    observed = _path_hash_or_absent(item.destination)
    matches_output = observed == item.output_sha256
    matches_prior = (item.prior_exists and observed == item.prior_sha256) or (
        not item.prior_exists and observed is None
    )
    if matches_output and matches_prior:
        return "both"
    if matches_output:
        return "output"
    if matches_prior:
        return "prior"
    return "unknown"


def _is_prior_state(state: str) -> bool:
    return state in {"prior", "both"}


def _is_output_state(state: str) -> bool:
    return state in {"output", "both"}


def _unlink_verified(path: Path, expected_sha256: str) -> None:
    try:
        path.lstat()
    except FileNotFoundError:
        return
    if _regular_sha256(path, code=ErrorCode.RECOVERY_REQUIRED) != expected_sha256:
        raise _bundle_error(
            ErrorCode.RECOVERY_REQUIRED,
            "Bundle cleanup artifact hash is ambiguous",
            evidence_path=str(path),
        )
    path.unlink()


def _cleanup_journal_temp(journal_temp: Path) -> None:
    try:
        entry = journal_temp.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(entry.st_mode) or entry.st_nlink != 1:
        raise _bundle_error(
            ErrorCode.RECOVERY_REQUIRED,
            "Bundle journal staging evidence is ambiguous",
        )
    journal_temp.unlink()


def _cleanup_bundle_evidence(
    journal: _BundleJournal,
    journal_path: Path,
    journal_temp: Path,
    *,
    cleanup_backups: bool,
) -> None:
    for item in journal.items:
        _unlink_verified(item.stage, item.output_sha256)
        if cleanup_backups and item.backup is not None:
            _unlink_verified(item.backup, item.prior_sha256 or "")
    _cleanup_journal_temp(journal_temp)
    journal_path.unlink(missing_ok=True)
    _sync_directory(journal_path.parent)


def _receipt_from_journal(
    journal: _BundleJournal,
    *,
    elapsed_ms: int,
    replayed: bool,
) -> AtomicBundleReceipt:
    return AtomicBundleReceipt(
        transaction_id=journal.transaction_id,
        items=tuple(
            AtomicBundleItemReceipt(
                destination=item.destination,
                backup_path=item.backup,
                prior_sha256=item.prior_sha256,
                output_sha256=item.output_sha256,
                output_size_bytes=item.output_size_bytes,
            )
            for item in journal.items
        ),
        elapsed_ms=elapsed_ms,
        replayed=replayed,
    )


def _journal_matches_request(
    journal: _BundleJournal,
    transaction_id: str,
    items: Sequence[AtomicBundleItem],
) -> bool:
    requested_hashes = {
        Path(item.destination): hashlib.sha256(item.payload).hexdigest() for item in items
    }
    return journal.transaction_id == transaction_id and all(
        requested_hashes.get(journal_item.destination) == journal_item.output_sha256
        for journal_item in journal.items
    )


def _ensure_terminal_receipt(
    journal: _BundleJournal,
    receipt_path: Path,
) -> bool:
    destinations = tuple(item.destination for item in journal.items)
    try:
        receipt_path.lstat()
    except FileNotFoundError:
        _write_exclusive(receipt_path, _journal_payload(journal))
        _sync_directory(receipt_path.parent)
        return True
    terminal = _load_bundle_journal(
        receipt_path,
        key=journal.key,
        destinations=destinations,
    )
    if terminal != journal or terminal.state != "committed":
        raise _bundle_error(
            ErrorCode.RECOVERY_REQUIRED,
            "Terminal bundle receipt does not match committed evidence",
            receipt_path=str(receipt_path),
        )
    return False


def _load_replay_receipt(
    receipt_path: Path,
    *,
    key: str,
    transaction_id: str,
    items: Sequence[AtomicBundleItem],
    elapsed_ms: int,
) -> AtomicBundleReceipt:
    destinations = tuple(Path(item.destination) for item in items)
    terminal = _load_bundle_journal(receipt_path, key=key, destinations=destinations)
    if terminal.state != "committed" or not _journal_matches_request(
        terminal,
        transaction_id,
        items,
    ):
        raise _bundle_error(
            ErrorCode.RECOVERY_REQUIRED,
            "Terminal bundle receipt does not prove the requested replay",
            receipt_path=str(receipt_path),
        )
    if not all(_is_output_state(_classify_destination(item)) for item in terminal.items):
        raise _bundle_error(
            ErrorCode.RECOVERY_REQUIRED,
            "Terminal bundle receipt does not match destination evidence",
            receipt_path=str(receipt_path),
        )
    for item in terminal.items:
        if item.destination.stat().st_size != item.output_size_bytes:
            raise _bundle_error(
                ErrorCode.RECOVERY_REQUIRED,
                "Terminal bundle output size does not match receipt evidence",
            )
        if item.prior_exists:
            assert item.backup is not None and item.prior_sha256 is not None
            if (
                _regular_sha256(item.backup, code=ErrorCode.RECOVERY_REQUIRED)
                != item.prior_sha256
            ):
                raise _bundle_error(
                    ErrorCode.RECOVERY_REQUIRED,
                    "Terminal bundle backup does not match receipt evidence",
                    backup_path=str(item.backup),
                )
    for item in items:
        if item.validate is not None:
            item.validate(Path(item.destination))
    return _receipt_from_journal(terminal, elapsed_ms=elapsed_ms, replayed=True)


def _rollback_bundle(
    journal: _BundleJournal,
    journal_path: Path,
    journal_temp: Path,
) -> None:
    try:
        journal_path.lstat()
    except FileNotFoundError:
        _write_exclusive(journal_path, _journal_payload(journal))
        _sync_directory(journal_path.parent)
    except OSError as error:
        raise _bundle_error(
            ErrorCode.RECOVERY_REQUIRED,
            "Bundle rollback journal could not be inspected",
        ) from error
    states = tuple(_classify_destination(item) for item in journal.items)
    if "unknown" in states:
        raise _bundle_error(
            ErrorCode.RECOVERY_REQUIRED,
            "Bundle destination state is ambiguous; recovery evidence was preserved",
        )
    for item, state in zip(journal.items, states):
        if state != "output" or not item.prior_exists:
            continue
        assert item.backup is not None and item.prior_sha256 is not None
        if (
            _regular_sha256(
                item.backup,
                code=ErrorCode.RECOVERY_REQUIRED,
            )
            != item.prior_sha256
        ):
            raise _bundle_error(
                ErrorCode.RECOVERY_REQUIRED,
                "A required bundle rollback backup is missing or invalid",
                backup_path=str(item.backup),
            )
    for item, state in zip(journal.items, states):
        if state != "output":
            continue
        if item.prior_exists:
            assert item.backup is not None
            os.replace(item.backup, item.destination)
        else:
            item.destination.unlink()
        _sync_directory(item.destination.parent)
    for item in journal.items:
        observed = _classify_destination(item)
        if not _is_prior_state(observed):
            raise _bundle_error(
                ErrorCode.RECOVERY_REQUIRED,
                "Bundle rollback could not be proven",
            )
    _cleanup_journal_temp(journal_temp)
    rolled_back = _BundleJournal(
        key=journal.key,
        transaction_id=journal.transaction_id,
        state="rolled_back",
        items=journal.items,
    )
    _replace_bundle_journal(journal_path, journal_temp, rolled_back)
    _cleanup_bundle_evidence(
        rolled_back,
        journal_path,
        journal_temp,
        cleanup_backups=True,
    )


def _recover_bundle(
    journal: _BundleJournal,
    journal_path: Path,
    journal_temp: Path,
    *,
    transaction_id: str,
    requested_items: Sequence[AtomicBundleItem],
    elapsed_ms: int,
) -> AtomicBundleReceipt | None:
    states = tuple(_classify_destination(item) for item in journal.items)
    if "unknown" in states:
        raise _bundle_error(
            ErrorCode.RECOVERY_REQUIRED,
            "Bundle destination state is ambiguous; recovery evidence was preserved",
            journal_path=str(journal_path),
        )
    if journal.state == "rolled_back":
        if not all(_is_prior_state(state) for state in states):
            raise _bundle_error(
                ErrorCode.RECOVERY_REQUIRED,
                "Rolled-back bundle journal does not match destination evidence",
            )
        _rollback_bundle(journal, journal_path, journal_temp)
        return None
    if journal.state == "committed" or all(_is_output_state(state) for state in states):
        if not all(_is_output_state(state) for state in states):
            raise _bundle_error(
                ErrorCode.RECOVERY_REQUIRED,
                "Terminal bundle journal does not match destination evidence",
            )
        matches_request = _journal_matches_request(
            journal,
            transaction_id,
            requested_items,
        )
        if matches_request:
            try:
                for requested in requested_items:
                    if requested.validate is not None:
                        requested.validate(Path(requested.destination))
            except Exception as error:
                if journal.state == "committed":
                    raise _bundle_error(
                        ErrorCode.RECOVERY_REQUIRED,
                        "Terminal bundle failed committed-pair validation",
                    ) from error
                try:
                    _rollback_bundle(journal, journal_path, journal_temp)
                except Exception as rollback_error:
                    raise _bundle_error(
                        ErrorCode.RECOVERY_REQUIRED,
                        "Recovered bundle failed validation and exact rollback could not be proven",
                    ) from rollback_error
                if isinstance(error, FCPMCPError):
                    raise
                raise _bundle_error(
                    ErrorCode.TRANSACTION_FAILED,
                    "Recovered bundle failed committed-pair validation",
                ) from error
        committed = _BundleJournal(
            key=journal.key,
            transaction_id=journal.transaction_id,
            state="committed",
            items=journal.items,
        )
        if journal.state != "committed":
            _cleanup_journal_temp(journal_temp)
            _replace_bundle_journal(journal_path, journal_temp, committed)
        receipt_path = _bundle_receipt_path(
            journal_path.parent,
            journal.key,
            journal.transaction_id,
        )
        _ensure_terminal_receipt(committed, receipt_path)
        _cleanup_bundle_evidence(
            committed,
            journal_path,
            journal_temp,
            cleanup_backups=False,
        )
        if matches_request:
            return _receipt_from_journal(
                committed,
                elapsed_ms=elapsed_ms,
                replayed=True,
            )
        return None
    _rollback_bundle(journal, journal_path, journal_temp)
    return None


def _same_bundle_already_committed(
    items: Sequence[AtomicBundleItem],
) -> bool:
    for item in items:
        try:
            observed = _regular_sha256(
                Path(item.destination),
                code=ErrorCode.INVALID_PATH,
            )
        except FCPMCPError:
            return False
        if observed != hashlib.sha256(item.payload).hexdigest():
            return False
    for item in items:
        if item.validate is not None:
            item.validate(Path(item.destination))
    return True


def _bundle_destination_snapshot(
    items: Sequence[AtomicBundleItem],
) -> tuple[str | None, ...]:
    snapshot: list[str | None] = []
    for item in items:
        destination = Path(item.destination)
        try:
            destination.lstat()
        except FileNotFoundError:
            snapshot.append(None)
        except OSError as error:
            raise _bundle_error(
                ErrorCode.INVALID_PATH,
                "Bundle destination could not be inspected",
            ) from error
        else:
            snapshot.append(
                _regular_sha256(destination, code=ErrorCode.INVALID_PATH)
            )
    return tuple(snapshot)


def atomic_replace_bundle(
    items: Sequence[AtomicBundleItem],
    *,
    event_format: str = "text",
    transaction_id: str | None = None,
    expected_prior_sha256: tuple[str | None, str | None] | None = None,
) -> AtomicBundleReceipt:
    """Commit a same-directory artifact bundle with durable rollback recovery.

    POSIX replacement is atomic for each destination, not for the bundle. This
    helper coordinates the replacements with fsynced stages, backups, a durable
    journal, and an exclusive pair-scoped lock so handled failures are all-or-old
    and an interrupted attempt can be reconciled before a conflicting commit.

    Rollback preserves each destination's exact prior bytes and prior existence.
    It does not preserve timestamps, mode bits, ACLs, flags, or extended
    attributes; backup and receipt evidence is deliberately created mode 0600.
    """
    started = time.perf_counter()
    attempt = _canonical_transaction_id(transaction_id)
    parent, normalized_items = _normalize_bundle_items(tuple(items))
    key, lock, journal_path, journal_temp, receipt_path, stages, backups = _bundle_paths(
        parent,
        normalized_items,
        attempt,
    )
    with _exclusive_bundle_lock(lock):
        _, normalized_items = _normalize_bundle_items(normalized_items)
        if (
            expected_prior_sha256 is not None
            and _bundle_destination_snapshot(normalized_items) != expected_prior_sha256
        ):
            raise _bundle_error(
                ErrorCode.IDEMPOTENCY_CONFLICT,
                "Bundle destinations changed after the request snapshot",
            )
        try:
            journal_path.lstat()
        except FileNotFoundError:
            pass
        except OSError as error:
            raise _bundle_error(
                ErrorCode.RECOVERY_REQUIRED,
                "Bundle journal could not be inspected",
            ) from error
        else:
            hot = _load_bundle_journal(
                journal_path,
                key=key,
                destinations=tuple(Path(item.destination) for item in normalized_items),
            )
            replay = _recover_bundle(
                hot,
                journal_path,
                journal_temp,
                transaction_id=attempt,
                requested_items=normalized_items,
                elapsed_ms=round((time.perf_counter() - started) * 1000),
            )
            if replay is not None:
                return replay

        unjournaled = _find_unjournaled_bundle_evidence(
            parent,
            normalized_items,
            journal_temp,
        )
        if unjournaled is not None:
            raise _bundle_error(
                ErrorCode.RECOVERY_REQUIRED,
                "Unjournaled bundle evidence requires recovery",
                evidence_path=str(unjournaled),
            )

        if _same_bundle_already_committed(normalized_items):
            return _load_replay_receipt(
                receipt_path,
                key=key,
                transaction_id=attempt,
                items=normalized_items,
                elapsed_ms=round((time.perf_counter() - started) * 1000),
            )
        if receipt_path.exists() or receipt_path.is_symlink():
            raise _bundle_error(
                ErrorCode.IDEMPOTENCY_CONFLICT,
                "Terminal receipt identity is already bound to different output bytes",
                receipt_path=str(receipt_path),
            )

        created_stages: list[Path] = []
        created_backups: list[Path] = []
        active_journal: _BundleJournal | None = None
        journal_published = False
        receipt_published = False
        try:
            for item, stage in zip(normalized_items, stages):
                _write_exclusive(stage, item.payload)
                created_stages.append(stage)
                if (
                    _regular_sha256(stage, code=ErrorCode.TRANSACTION_FAILED)
                    != hashlib.sha256(item.payload).hexdigest()
                ):
                    raise _bundle_error(
                        ErrorCode.TRANSACTION_FAILED,
                        "Staged bundle payload hash changed",
                    )
                if item.validate is not None:
                    item.validate(stage)
            _sync_directory(parent)

            journal_items = []
            for item, stage, backup in zip(normalized_items, stages, backups):
                destination = Path(item.destination)
                try:
                    destination.lstat()
                except FileNotFoundError:
                    prior_exists = False
                    prior_sha256 = None
                    backup_path = None
                else:
                    prior_exists = True
                    if backup.exists() or backup.is_symlink():
                        raise _bundle_error(
                            ErrorCode.RECOVERY_REQUIRED,
                            "Preassigned bundle backup path is occupied",
                            backup_path=str(backup),
                        )
                    prior_sha256 = _copy_backup_exclusive(destination, backup)
                    created_backups.append(backup)
                    backup_path = backup
                journal_items.append(
                    _JournalItem(
                        destination=destination,
                        stage=stage,
                        backup=backup_path,
                        prior_exists=prior_exists,
                        prior_sha256=prior_sha256,
                        output_sha256=hashlib.sha256(item.payload).hexdigest(),
                        output_size_bytes=len(item.payload),
                    )
                )
            _sync_directory(parent)
            active_journal = _BundleJournal(key, attempt, "prepared", tuple(journal_items))
            _write_exclusive(journal_path, _journal_payload(active_journal))
            journal_published = True
            _sync_directory(parent)

            if any(
                not _is_prior_state(_classify_destination(item))
                for item in active_journal.items
            ):
                raise _bundle_error(
                    ErrorCode.RECOVERY_REQUIRED,
                    "Bundle destinations changed before replacement",
                )
            for item in active_journal.items:
                if item.prior_exists and item.prior_sha256 == item.output_sha256:
                    continue
                os.replace(item.stage, item.destination)
                _sync_directory(parent)
            for item, requested in zip(active_journal.items, normalized_items):
                if (
                    _regular_sha256(item.destination, code=ErrorCode.RECOVERY_REQUIRED)
                    != item.output_sha256
                ):
                    raise _bundle_error(
                        ErrorCode.RECOVERY_REQUIRED,
                        "Committed bundle payload hash does not match its stage",
                    )
                if requested.validate is not None:
                    requested.validate(item.destination)

            committed = _BundleJournal(key, attempt, "committed", active_journal.items)
            _replace_bundle_journal(journal_path, journal_temp, committed)
            _write_exclusive(receipt_path, _journal_payload(committed))
            receipt_published = True
            _sync_directory(parent)
            _cleanup_bundle_evidence(
                committed,
                journal_path,
                journal_temp,
                cleanup_backups=False,
            )
            receipt = _receipt_from_journal(
                committed,
                elapsed_ms=round((time.perf_counter() - started) * 1000),
                replayed=False,
            )
            emit_event(
                {
                    "event": "atomic_bundle_write",
                    "transaction_id": receipt.transaction_id,
                    "output_paths": [str(item.destination) for item in receipt.items],
                    "output_sha256": [item.output_sha256 for item in receipt.items],
                    "elapsed_ms": receipt.elapsed_ms,
                    "disposition": "committed",
                },
                format=event_format,
            )
            return receipt
        except Exception as error:
            if active_journal is not None and journal_published:
                try:
                    _rollback_bundle(active_journal, journal_path, journal_temp)
                    if receipt_published:
                        terminal = _BundleJournal(
                            active_journal.key,
                            active_journal.transaction_id,
                            "committed",
                            active_journal.items,
                        )
                        _unlink_verified(
                            receipt_path,
                            hashlib.sha256(_journal_payload(terminal)).hexdigest(),
                        )
                        _sync_directory(parent)
                except Exception as rollback_error:
                    raise _bundle_error(
                        ErrorCode.RECOVERY_REQUIRED,
                        "Bundle commit failed and exact rollback could not be proven",
                        journal_path=str(journal_path),
                    ) from rollback_error
            else:
                cleanup_error: OSError | FCPMCPError | None = None
                for path in (*created_stages, *created_backups):
                    try:
                        path.unlink(missing_ok=True)
                    except (OSError, FCPMCPError) as caught:
                        cleanup_error = caught
                try:
                    _sync_directory(parent)
                except OSError as caught:
                    cleanup_error = caught
                if cleanup_error is not None:
                    raise _bundle_error(
                        ErrorCode.RECOVERY_REQUIRED,
                        "Bundle candidate cleanup could not be proven",
                    ) from cleanup_error
            emit_event(
                {
                    "event": "atomic_bundle_write",
                    "transaction_id": attempt,
                    "output_paths": [str(item.destination) for item in normalized_items],
                    "elapsed_ms": round((time.perf_counter() - started) * 1000),
                    "disposition": "rolled_back" if journal_published else "failed",
                    "error_code": (
                        error.code.value
                        if isinstance(error, FCPMCPError)
                        else ErrorCode.TRANSACTION_FAILED.value
                    ),
                },
                format=event_format,
            )
            if isinstance(error, FCPMCPError):
                raise
            raise _bundle_error(
                ErrorCode.TRANSACTION_FAILED,
                "Atomic bundle commit failed",
            ) from error


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
        Path(backup_path).expanduser().absolute() if backup_path is not None else None
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
