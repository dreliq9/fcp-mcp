"""Durable, append-only workflow ledger backed by private SQLite storage."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import stat
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import ValidationError

from fcp_mcp.contracts import ErrorCode, FCPMCPError
from fcp_mcp.profiles import ApprovalMode, Profile
from fcp_mcp.version import package_version as installed_package_version
from fcp_mcp.workflow.artifacts import (
    ArtifactKind,
    ArtifactMetadataV1,
    StatePaths,
)
from fcp_mcp.workflow.models import (
    ApprovalDecision,
    ApprovalSource,
    PriorDestinationState,
    WorkflowState,
    canonical_json,
    transition_allowed,
)

_DATABASE_NAME = "runs.sqlite3"
_DATABASE_MODE = 0o600
_BUSY_TIMEOUT_MS = 5000
_ZERO_HASH = "0" * 64
_MAX_EVENT_TYPE_CHARS = 128
_MAX_EVENT_PAYLOAD_BYTES = 1024 * 1024
_MAX_TEXT_CHARS = 4096
_MAX_METADATA_CHARS = 255
_MAX_IDEMPOTENCY_KEY_CHARS = 128
_MAX_READ_LIMIT = 1000
_MAX_INTEGRITY_FINDINGS = 100
_MAX_CREATE_RACE_RETRIES = 3
_SQLITE_MAX_INTEGER = 2**63 - 1
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_UTC_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{6})?Z$"
)


def _coded(
    code: ErrorCode,
    message: str,
    cause: BaseException | None = None,
) -> FCPMCPError:
    error = FCPMCPError(code, message)
    if cause is not None:
        error.__cause__ = cause
    return error


def _invalid(message: str, cause: BaseException | None = None) -> FCPMCPError:
    return _coded(ErrorCode.INVALID_ARGUMENTS, message, cause)


def _state_conflict(message: str) -> FCPMCPError:
    return _coded(ErrorCode.WORKFLOW_STATE_CONFLICT, message)


def _ledger_unavailable(cause: BaseException) -> FCPMCPError:
    return _coded(
        ErrorCode.LEDGER_UNAVAILABLE,
        "workflow ledger is unavailable",
        cause,
    )


class _MigrationError(RuntimeError):
    pass


_LEDGER_FAILURES = (
    sqlite3.Error,
    OSError,
    ValueError,
    TypeError,
    KeyError,
    IndexError,
    OverflowError,
    RuntimeError,
)


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]
    checksum: str


@dataclass(frozen=True)
class LedgerRunRecord:
    run_id: str
    graph_version: str
    package_version: str
    run_version: str
    state: WorkflowState
    revision: int
    profile: Profile
    approval_mode: ApprovalMode
    source_path: str
    destination_path: str
    source_sha256: str | None
    destination_sha256: str | None
    prior_destination_state: PriorDestinationState | None
    prior_destination_sha256: str | None
    plan_sha256: str | None
    candidate_sha256: str | None
    candidate_size_bytes: int | None
    diff_sha256: str | None
    diff_size_bytes: int | None
    receipt_sha256: str | None
    receipt_size_bytes: int | None
    created_at: str
    updated_at: str
    approved_at: str | None
    committed_at: str | None
    expires_at: str | None
    commit_attempt_id: str | None
    expected_backup_path: str | None
    backup_sha256: str | None
    approval_decision: ApprovalDecision | None
    approval_source: ApprovalSource | None
    approval_summary: str | None
    terminal_error_code: ErrorCode | None
    terminal_error_summary: str | None


@dataclass(frozen=True)
class EventRecord:
    run_id: str
    sequence: int
    event_type: str
    payload_text: str
    timestamp: str
    elapsed_ms: int | None
    previous_hash: str
    event_hash: str

    @property
    def payload(self) -> Mapping[str, object]:
        parsed = json.loads(self.payload_text)
        if not isinstance(parsed, dict):
            raise TypeError("stored event payload is not an object")
        return parsed


@dataclass(frozen=True)
class ArtifactRecord:
    run_id: str
    kind: ArtifactKind
    relative_path: str
    sha256: str
    byte_size: int
    created_at: str


@dataclass(frozen=True)
class ApprovalRecord:
    run_id: str
    decision: ApprovalDecision
    source: ApprovalSource
    operator: str | None
    host: str | None
    terminal_present: bool
    binding_sha256: str
    created_at: str
    expires_at: str | None


@dataclass(frozen=True)
class IdempotencyResult:
    existing: bool
    run: LedgerRunRecord


@dataclass(frozen=True)
class EventMutationResult:
    run: LedgerRunRecord
    event: EventRecord


@dataclass(frozen=True)
class ArtifactMutationResult:
    run: LedgerRunRecord
    artifact: ArtifactRecord
    event: EventRecord


@dataclass(frozen=True)
class DecisionMutationResult:
    run: LedgerRunRecord
    approval: ApprovalRecord
    event: EventRecord


@dataclass(frozen=True)
class IntegrityFinding:
    code: str
    summary: str
    run_id: str | None = None
    sequence: int | None = None


@dataclass(frozen=True)
class IntegrityResult:
    valid: bool
    checked_migrations: int
    checked_runs: int
    checked_events: int
    findings: tuple[IntegrityFinding, ...]


def migration_checksum(
    version: int,
    name: str,
    statements: Sequence[str],
) -> str:
    """Hash an unambiguous canonical representation of one migration."""
    if type(version) is not int or version < 1:
        raise ValueError("migration version must be a positive integer")
    if not isinstance(name, str) or not name:
        raise ValueError("migration name must be nonempty")
    if not isinstance(statements, (tuple, list)) or not statements:
        raise ValueError("migration statements must be a nonempty sequence")
    if any(not isinstance(statement, str) or not statement for statement in statements):
        raise ValueError("migration statements must be nonempty strings")
    body = canonical_json(
        {
            "version": version,
            "name": name,
            "statements": list(statements),
        }
    )
    return hashlib.sha256(body).hexdigest()


_MIGRATION_1_STATEMENTS = (
    """
    CREATE TABLE schema_migrations (
        version INTEGER PRIMARY KEY CHECK (version > 0),
        name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 255),
        checksum TEXT NOT NULL CHECK (
            length(checksum) = 64 AND checksum NOT GLOB '*[^0-9a-f]*'
        ),
        package_version TEXT NOT NULL CHECK (length(package_version) BETWEEN 1 AND 64),
        applied_at TEXT NOT NULL CHECK (length(applied_at) BETWEEN 20 AND 27)
    )
    """.strip(),
    """
    CREATE TABLE runs (
        run_id TEXT PRIMARY KEY CHECK (length(run_id) = 36),
        graph_version TEXT NOT NULL CHECK (length(graph_version) BETWEEN 1 AND 64),
        package_version TEXT NOT NULL CHECK (length(package_version) BETWEEN 1 AND 64),
        run_version TEXT NOT NULL CHECK (length(run_version) BETWEEN 1 AND 64),
        state TEXT NOT NULL CHECK (state IN (
            'preparing', 'awaiting_approval', 'approved', 'committing',
            'committed', 'failed', 'rejected', 'cancelled', 'expired',
            'stale', 'rolled_back', 'recovery_required'
        )),
        revision INTEGER NOT NULL CHECK (revision > 0),
        profile TEXT NOT NULL CHECK (profile IN ('inspect', 'workflow', 'edit', 'full')),
        approval_mode TEXT NOT NULL CHECK (approval_mode IN ('cli', 'client')),
        source_path TEXT NOT NULL CHECK (length(source_path) BETWEEN 1 AND 4096),
        destination_path TEXT NOT NULL CHECK (length(destination_path) BETWEEN 1 AND 4096),
        source_sha256 TEXT CHECK (
            source_sha256 IS NULL OR (
                length(source_sha256) = 64 AND source_sha256 NOT GLOB '*[^0-9a-f]*'
            )
        ),
        destination_sha256 TEXT CHECK (
            destination_sha256 IS NULL OR (
                length(destination_sha256) = 64
                AND destination_sha256 NOT GLOB '*[^0-9a-f]*'
            )
        ),
        prior_destination_state TEXT CHECK (
            prior_destination_state IS NULL OR prior_destination_state IN ('absent', 'present')
        ),
        prior_destination_sha256 TEXT CHECK (
            prior_destination_sha256 IS NULL OR (
                length(prior_destination_sha256) = 64
                AND prior_destination_sha256 NOT GLOB '*[^0-9a-f]*'
            )
        ),
        plan_sha256 TEXT CHECK (
            plan_sha256 IS NULL OR (
                length(plan_sha256) = 64 AND plan_sha256 NOT GLOB '*[^0-9a-f]*'
            )
        ),
        candidate_sha256 TEXT CHECK (
            candidate_sha256 IS NULL OR (
                length(candidate_sha256) = 64 AND candidate_sha256 NOT GLOB '*[^0-9a-f]*'
            )
        ),
        candidate_size_bytes INTEGER CHECK (
            candidate_size_bytes IS NULL OR candidate_size_bytes >= 0
        ),
        diff_sha256 TEXT CHECK (
            diff_sha256 IS NULL OR (
                length(diff_sha256) = 64 AND diff_sha256 NOT GLOB '*[^0-9a-f]*'
            )
        ),
        diff_size_bytes INTEGER CHECK (diff_size_bytes IS NULL OR diff_size_bytes >= 0),
        receipt_sha256 TEXT CHECK (
            receipt_sha256 IS NULL OR (
                length(receipt_sha256) = 64 AND receipt_sha256 NOT GLOB '*[^0-9a-f]*'
            )
        ),
        receipt_size_bytes INTEGER CHECK (
            receipt_size_bytes IS NULL OR receipt_size_bytes >= 0
        ),
        created_at TEXT NOT NULL CHECK (length(created_at) BETWEEN 20 AND 27),
        updated_at TEXT NOT NULL CHECK (length(updated_at) BETWEEN 20 AND 27),
        approved_at TEXT CHECK (approved_at IS NULL OR length(approved_at) BETWEEN 20 AND 27),
        committed_at TEXT CHECK (
            committed_at IS NULL OR length(committed_at) BETWEEN 20 AND 27
        ),
        expires_at TEXT CHECK (expires_at IS NULL OR length(expires_at) BETWEEN 20 AND 27),
        commit_attempt_id TEXT CHECK (
            commit_attempt_id IS NULL OR length(commit_attempt_id) BETWEEN 1 AND 255
        ),
        expected_backup_path TEXT CHECK (
            expected_backup_path IS NULL OR length(expected_backup_path) BETWEEN 1 AND 4096
        ),
        backup_sha256 TEXT CHECK (
            backup_sha256 IS NULL OR (
                length(backup_sha256) = 64 AND backup_sha256 NOT GLOB '*[^0-9a-f]*'
            )
        ),
        approval_decision TEXT CHECK (
            approval_decision IS NULL OR approval_decision IN ('approved', 'rejected')
        ),
        approval_source TEXT CHECK (
            approval_source IS NULL OR approval_source IN ('cli', 'client')
        ),
        approval_summary TEXT CHECK (
            approval_summary IS NULL OR length(approval_summary) BETWEEN 1 AND 4096
        ),
        terminal_error_code TEXT CHECK (
            terminal_error_code IS NULL OR length(terminal_error_code) BETWEEN 1 AND 64
        ),
        terminal_error_summary TEXT CHECK (
            terminal_error_summary IS NULL
            OR length(terminal_error_summary) BETWEEN 1 AND 4096
        )
    )
    """.strip(),
    """
    CREATE TABLE events (
        run_id TEXT NOT NULL REFERENCES runs(run_id),
        sequence INTEGER NOT NULL CHECK (sequence > 0),
        event_type TEXT NOT NULL CHECK (length(event_type) BETWEEN 1 AND 128),
        payload_text TEXT NOT NULL CHECK (length(payload_text) <= 1048576),
        timestamp TEXT NOT NULL CHECK (length(timestamp) BETWEEN 20 AND 27),
        elapsed_ms INTEGER CHECK (elapsed_ms IS NULL OR elapsed_ms >= 0),
        previous_hash TEXT NOT NULL CHECK (
            length(previous_hash) = 64 AND previous_hash NOT GLOB '*[^0-9a-f]*'
        ),
        event_hash TEXT NOT NULL CHECK (
            length(event_hash) = 64 AND event_hash NOT GLOB '*[^0-9a-f]*'
        ),
        PRIMARY KEY (run_id, sequence)
    )
    """.strip(),
    """
    CREATE TABLE artifacts (
        run_id TEXT NOT NULL REFERENCES runs(run_id),
        kind TEXT NOT NULL CHECK (kind IN ('candidate', 'diff')),
        relative_path TEXT NOT NULL UNIQUE CHECK (length(relative_path) BETWEEN 1 AND 255),
        sha256 TEXT NOT NULL CHECK (
            length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*'
        ),
        byte_size INTEGER NOT NULL CHECK (byte_size >= 0),
        created_at TEXT NOT NULL CHECK (length(created_at) BETWEEN 20 AND 27),
        PRIMARY KEY (run_id, kind)
    )
    """.strip(),
    """
    CREATE TABLE approvals (
        run_id TEXT PRIMARY KEY REFERENCES runs(run_id),
        decision TEXT NOT NULL CHECK (decision IN ('approved', 'rejected')),
        source TEXT NOT NULL CHECK (source IN ('cli', 'client')),
        operator TEXT CHECK (operator IS NULL OR length(operator) BETWEEN 1 AND 255),
        host TEXT CHECK (host IS NULL OR length(host) BETWEEN 1 AND 255),
        terminal_present INTEGER NOT NULL CHECK (terminal_present IN (0, 1)),
        binding_sha256 TEXT NOT NULL CHECK (
            length(binding_sha256) = 64 AND binding_sha256 NOT GLOB '*[^0-9a-f]*'
        ),
        created_at TEXT NOT NULL CHECK (length(created_at) BETWEEN 20 AND 27),
        expires_at TEXT CHECK (expires_at IS NULL OR length(expires_at) BETWEEN 20 AND 27)
    )
    """.strip(),
    """
    CREATE TABLE idempotency_keys (
        key TEXT PRIMARY KEY CHECK (length(key) BETWEEN 1 AND 128),
        request_sha256 TEXT NOT NULL CHECK (
            length(request_sha256) = 64 AND request_sha256 NOT GLOB '*[^0-9a-f]*'
        ),
        run_id TEXT NOT NULL UNIQUE REFERENCES runs(run_id)
    )
    """.strip(),
    "CREATE INDEX idx_runs_created ON runs(created_at DESC, run_id ASC)",
    "CREATE INDEX idx_runs_state_created ON runs(state, created_at DESC, run_id ASC)",
    """
    CREATE TRIGGER schema_migrations_no_update
    BEFORE UPDATE ON schema_migrations
    BEGIN SELECT RAISE(ABORT, 'schema_migrations is append-only'); END
    """.strip(),
    """
    CREATE TRIGGER schema_migrations_no_delete
    BEFORE DELETE ON schema_migrations
    BEGIN SELECT RAISE(ABORT, 'schema_migrations is append-only'); END
    """.strip(),
    """
    CREATE TRIGGER events_no_update
    BEFORE UPDATE ON events
    BEGIN SELECT RAISE(ABORT, 'events is append-only'); END
    """.strip(),
    """
    CREATE TRIGGER events_no_delete
    BEFORE DELETE ON events
    BEGIN SELECT RAISE(ABORT, 'events is append-only'); END
    """.strip(),
    """
    CREATE TRIGGER artifacts_no_update
    BEFORE UPDATE ON artifacts
    BEGIN SELECT RAISE(ABORT, 'artifacts is append-only'); END
    """.strip(),
    """
    CREATE TRIGGER artifacts_no_delete
    BEFORE DELETE ON artifacts
    BEGIN SELECT RAISE(ABORT, 'artifacts is append-only'); END
    """.strip(),
    """
    CREATE TRIGGER approvals_no_update
    BEFORE UPDATE ON approvals
    BEGIN SELECT RAISE(ABORT, 'approvals is append-only'); END
    """.strip(),
    """
    CREATE TRIGGER approvals_no_delete
    BEFORE DELETE ON approvals
    BEGIN SELECT RAISE(ABORT, 'approvals is append-only'); END
    """.strip(),
    """
    CREATE TRIGGER idempotency_keys_no_update
    BEFORE UPDATE ON idempotency_keys
    BEGIN SELECT RAISE(ABORT, 'idempotency_keys is append-only'); END
    """.strip(),
    """
    CREATE TRIGGER idempotency_keys_no_delete
    BEFORE DELETE ON idempotency_keys
    BEGIN SELECT RAISE(ABORT, 'idempotency_keys is append-only'); END
    """.strip(),
)

MIGRATIONS = (
    Migration(
        version=1,
        name="initial_workflow_ledger",
        statements=_MIGRATION_1_STATEMENTS,
        checksum=migration_checksum(
            1,
            "initial_workflow_ledger",
            _MIGRATION_1_STATEMENTS,
        ),
    ),
)

_REQUIRED_TABLES = frozenset(
    {
        "schema_migrations",
        "runs",
        "events",
        "artifacts",
        "approvals",
        "idempotency_keys",
    }
)
_REQUIRED_TRIGGERS = frozenset(
    {
        "schema_migrations_no_update",
        "schema_migrations_no_delete",
        "events_no_update",
        "events_no_delete",
        "artifacts_no_update",
        "artifacts_no_delete",
        "approvals_no_update",
        "approvals_no_delete",
        "idempotency_keys_no_update",
        "idempotency_keys_no_delete",
    }
)
_PROJECTION_PATCH_FIELDS = frozenset(
    {
        "source_sha256",
        "destination_sha256",
        "prior_destination_state",
        "prior_destination_sha256",
        "plan_sha256",
        "receipt_sha256",
        "receipt_size_bytes",
        "expires_at",
        "committed_at",
        "commit_attempt_id",
        "expected_backup_path",
        "backup_sha256",
        "approval_summary",
        "terminal_error_code",
        "terminal_error_summary",
    }
)


def _is_reparse_stat(result: os.stat_result) -> bool:
    attributes = getattr(result, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)


def _stat_identity(result: os.stat_result) -> tuple[int, int, int]:
    return (
        result.st_dev,
        result.st_ino,
        stat.S_IFMT(result.st_mode),
    )


def _rollback(connection: sqlite3.Connection, begun: bool) -> None:
    if not begun:
        return
    try:
        connection.execute("ROLLBACK")
    except sqlite3.Error:
        pass


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class WorkflowLedger:
    """Explicitly initialized SQLite projection and tamper-evident event chain."""

    def __init__(
        self,
        paths: StatePaths,
        *,
        clock: Callable[[], datetime] | None = None,
        package_version: str | None = None,
    ) -> None:
        if not isinstance(paths, StatePaths):
            raise _invalid("paths must be StatePaths")
        if clock is not None and not callable(clock):
            raise _invalid("clock must be callable")
        if package_version is not None:
            _bounded_text(
                package_version,
                field="package_version",
                maximum=64,
            )
        self.paths = paths
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._package_version = package_version

    def _current_package_version(self) -> str:
        value = self._package_version
        if value is None:
            value = installed_package_version()
        return _bounded_text(value, field="package_version", maximum=64)

    def _database_path(self) -> Path:
        expected = self.paths.root / _DATABASE_NAME
        if self.paths.database != expected:
            raise _ledger_unavailable(_MigrationError("database path contract changed"))
        return expected

    def _validate_root(self) -> None:
        try:
            result = self.paths.root.lstat()
        except OSError as error:
            raise _ledger_unavailable(error)
        if (
            stat.S_ISLNK(result.st_mode)
            or _is_reparse_stat(result)
            or not stat.S_ISDIR(result.st_mode)
        ):
            raise _ledger_unavailable(_MigrationError("unsafe state root"))

    def _secure_database_entry(self, *, create: bool = True) -> Path:
        self._validate_root()
        path = self._database_path()
        for attempt in range(_MAX_CREATE_RACE_RETRIES):
            try:
                result = path.lstat()
            except FileNotFoundError:
                if not create:
                    raise _ledger_unavailable(
                        FileNotFoundError("workflow database is missing")
                    )
                flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
                if hasattr(os, "O_CLOEXEC"):
                    flags |= os.O_CLOEXEC
                if hasattr(os, "O_NOFOLLOW"):
                    flags |= os.O_NOFOLLOW
                try:
                    descriptor = os.open(path, flags, _DATABASE_MODE)
                except FileExistsError as error:
                    if attempt + 1 == _MAX_CREATE_RACE_RETRIES:
                        raise _ledger_unavailable(error)
                    continue
                except OSError as error:
                    raise _ledger_unavailable(error)
                try:
                    if os.name == "posix":
                        os.fchmod(descriptor, _DATABASE_MODE)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                try:
                    _fsync_directory(self.paths.root)
                    result = path.lstat()
                except OSError as error:
                    raise _ledger_unavailable(error)
            except OSError as error:
                raise _ledger_unavailable(error)
            self._validate_database_stat(result)
            return path
        raise _ledger_unavailable(_MigrationError("database creation race did not settle"))

    def _validate_database_stat(self, result: os.stat_result) -> None:
        if (
            stat.S_ISLNK(result.st_mode)
            or _is_reparse_stat(result)
            or not stat.S_ISREG(result.st_mode)
        ):
            raise _ledger_unavailable(_MigrationError("unsafe workflow database entry"))
        if os.name == "posix" and stat.S_IMODE(result.st_mode) != _DATABASE_MODE:
            raise _ledger_unavailable(_MigrationError("unsafe workflow database mode"))

    def _validate_internal_database_entry(self, path: Path) -> os.stat_result:
        result = path.lstat()
        try:
            self._validate_database_stat(result)
        except FCPMCPError as error:
            cause = error.__cause__ or error
            raise OSError("unsafe internal database entry") from cause
        return result

    def _configure_connection(self, connection: sqlite3.Connection) -> None:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        observed = (
            connection.execute("PRAGMA journal_mode").fetchone()[0],
            connection.execute("PRAGMA synchronous").fetchone()[0],
            connection.execute("PRAGMA foreign_keys").fetchone()[0],
            connection.execute("PRAGMA busy_timeout").fetchone()[0],
        )
        if observed != ("delete", 2, 1, _BUSY_TIMEOUT_MS):
            raise _MigrationError("SQLite connection contract was not applied")

    def _connect_path(self, path: Path) -> sqlite3.Connection:
        before = self._validate_internal_database_entry(path)
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                f"{path.as_uri()}?mode=rw",
                timeout=_BUSY_TIMEOUT_MS / 1000,
                isolation_level=None,
                uri=True,
            )
            connection.row_factory = sqlite3.Row
            database_row = connection.execute("PRAGMA database_list").fetchone()
            if database_row is None or not database_row["file"]:
                raise _MigrationError("SQLite did not identify the opened database")
            after = self._validate_internal_database_entry(path)
            opened_path = Path(database_row["file"])
            opened = self._validate_internal_database_entry(opened_path)
            expected_identity = _stat_identity(before)
            if (
                _stat_identity(after) != expected_identity
                or _stat_identity(opened) != expected_identity
            ):
                raise _MigrationError("database entry changed while it was opened")
            self._configure_connection(connection)
            return connection
        except BaseException:
            if connection is not None:
                connection.close()
            raise

    def _connect(self) -> sqlite3.Connection:
        try:
            path = self._secure_database_entry()
            return self._connect_path(path)
        except FCPMCPError:
            raise
        except _LEDGER_FAILURES as error:
            raise _ledger_unavailable(error)

    def _connect_existing(self) -> sqlite3.Connection:
        try:
            path = self._secure_database_entry(create=False)
            return self._connect_path(path)
        except FCPMCPError:
            raise
        except _LEDGER_FAILURES as error:
            raise _ledger_unavailable(error)

    def _migration_rows(self, connection: sqlite3.Connection) -> tuple[sqlite3.Row, ...]:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'schema_migrations'"
        ).fetchone()
        if exists is None:
            return ()
        cursor = connection.execute(
            "SELECT version, name, checksum, package_version, applied_at "
            "FROM schema_migrations ORDER BY version"
        )
        return tuple(cursor.fetchmany(len(MIGRATIONS) + 2))

    def _validate_migrations(self, rows: Sequence[sqlite3.Row]) -> None:
        if len(rows) > len(MIGRATIONS):
            raise _MigrationError("unknown future migration")
        for position, row in enumerate(rows, start=1):
            if type(row["version"]) is not int or row["version"] != position:
                raise _MigrationError("migration version gap")
            if position > len(MIGRATIONS):
                raise _MigrationError("unknown future migration")
            expected = MIGRATIONS[position - 1]
            if (
                row["name"] != expected.name
                or row["checksum"] != expected.checksum
            ):
                raise _MigrationError("migration identity mismatch")

    def _meaningful_database(self, connection: sqlite3.Connection) -> bool:
        return (
            connection.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' LIMIT 1"
            ).fetchone()
            is not None
        )

    def _backup_database(
        self,
        migration_connection: sqlite3.Connection,
        *,
        from_version: int,
        to_version: int,
    ) -> Path:
        del migration_connection
        timestamp = _canonical_clock_timestamp(self._clock()).replace(":", "").replace("-", "")
        token = secrets.token_hex(8)
        stem = (
            f"{_DATABASE_NAME}.backup-v{from_version}-to-v{to_version}-"
            f"{timestamp}-{token}"
        )
        published = self.paths.root / stem
        temporary = self.paths.root / f".{stem}.tmp"
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(temporary, flags, _DATABASE_MODE)
        try:
            if os.name == "posix":
                os.fchmod(descriptor, _DATABASE_MODE)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

        source: sqlite3.Connection | None = None
        destination: sqlite3.Connection | None = None
        replaced = False
        try:
            source = self._connect_existing()
            destination = self._connect_path(temporary)
            source.backup(destination)
            destination.close()
            destination = None
            source.close()
            source = None
            _fsync_file(temporary)
            os.replace(temporary, published)
            replaced = True
            _fsync_directory(self.paths.root)
            self._validate_internal_database_entry(published)
            return published
        except BaseException:
            if destination is not None:
                destination.close()
            if source is not None:
                source.close()
            for candidate in (temporary, published if replaced else None):
                if candidate is not None:
                    try:
                        candidate.unlink(missing_ok=True)
                    except OSError:
                        pass
            raise

    def _execute_migration_statement(
        self,
        connection: sqlite3.Connection,
        statement: str,
    ) -> None:
        connection.execute(statement)

    def _validate_schema_objects(self, connection: sqlite3.Connection) -> None:
        cursor = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
        tables = {row["name"] for row in cursor.fetchmany(100)}
        missing = _REQUIRED_TABLES - tables
        if missing:
            raise _MigrationError("current migration is missing schema objects")
        trigger_cursor = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger'"
        )
        triggers = {row["name"] for row in trigger_cursor.fetchmany(100)}
        if _REQUIRED_TRIGGERS - triggers:
            raise _MigrationError("current migration is missing append-only guards")

    def initialize(self) -> None:
        """Configure the database and apply all pending migrations atomically."""
        connection = self._connect()
        begun = False
        published_backup: Path | None = None
        try:
            before_lock = self._migration_rows(connection)
            self._validate_migrations(before_lock)
            connection.execute("BEGIN IMMEDIATE")
            begun = True
            rows = self._migration_rows(connection)
            self._validate_migrations(rows)
            pending = MIGRATIONS[len(rows) :]
            if pending and self._meaningful_database(connection):
                published_backup = self._backup_database(
                    connection,
                    from_version=len(rows),
                    to_version=pending[-1].version,
                )
            for migration in pending:
                for statement in migration.statements:
                    self._execute_migration_statement(connection, statement)
                applied_at = _canonical_clock_timestamp(self._clock())
                connection.execute(
                    "INSERT INTO schema_migrations("
                    "version, name, checksum, package_version, applied_at"
                    ") VALUES (?, ?, ?, ?, ?)",
                    (
                        migration.version,
                        migration.name,
                        migration.checksum,
                        self._current_package_version(),
                        applied_at,
                    ),
                )
            self._validate_schema_objects(connection)
            connection.execute("COMMIT")
            begun = False
        except FCPMCPError:
            _rollback(connection, begun)
            if published_backup is not None:
                try:
                    published_backup.unlink(missing_ok=True)
                    _fsync_directory(self.paths.root)
                except OSError:
                    pass
            raise
        except _LEDGER_FAILURES as error:
            _rollback(connection, begun)
            if published_backup is not None:
                try:
                    published_backup.unlink(missing_ok=True)
                    _fsync_directory(self.paths.root)
                except OSError:
                    pass
            raise _ledger_unavailable(error)
        finally:
            connection.close()

    def _write(self, operation: Callable[[sqlite3.Connection], Any]) -> Any:
        connection = self._connect_existing()
        begun = False
        try:
            connection.execute("BEGIN IMMEDIATE")
            begun = True
            result = operation(connection)
            connection.execute("COMMIT")
            begun = False
            return result
        except FCPMCPError:
            _rollback(connection, begun)
            raise
        except _LEDGER_FAILURES as error:
            _rollback(connection, begun)
            raise _ledger_unavailable(error)
        finally:
            connection.close()

    def create_run(
        self,
        *,
        run_id: str,
        graph_version: str,
        run_version: str,
        profile: Profile | str,
        approval_mode: ApprovalMode | str,
        source_path: str,
        destination_path: str,
        event_type: str,
        event_payload: Mapping[str, object],
        idempotency_key: str | None = None,
        request_sha256: str | None = None,
    ) -> IdempotencyResult:
        canonical_run_id = _run_id(run_id)
        graph = _bounded_text(graph_version, field="graph_version", maximum=64)
        run = _bounded_text(run_version, field="run_version", maximum=64)
        closed_profile = _profile(profile)
        mode = _approval_mode(approval_mode)
        source = _bounded_text(source_path, field="source_path", maximum=_MAX_TEXT_CHARS)
        destination = _bounded_text(
            destination_path,
            field="destination_path",
            maximum=_MAX_TEXT_CHARS,
        )
        event_name = _event_type(event_type)
        payload, payload_text = _event_payload(event_payload)
        key = _idempotency_key(idempotency_key) if idempotency_key is not None else None
        request_hash = (
            _sha256(request_sha256, field="request_sha256")
            if request_sha256 is not None
            else None
        )
        if (key is None) != (request_hash is None):
            raise _invalid(
                "idempotency_key and request_sha256 must be provided together"
            )

        def create(connection: sqlite3.Connection) -> IdempotencyResult:
            if key is not None:
                existing_key = connection.execute(
                    "SELECT request_sha256, run_id FROM idempotency_keys WHERE key = ?",
                    (key,),
                ).fetchone()
                if existing_key is not None:
                    if existing_key["request_sha256"] != request_hash:
                        raise _coded(
                            ErrorCode.IDEMPOTENCY_CONFLICT,
                            "idempotency key was used for a different request",
                        )
                    existing_run = self._select_run(connection, existing_key["run_id"])
                    if existing_run is None:
                        raise _MigrationError("idempotency row references a missing run")
                    return IdempotencyResult(existing=True, run=existing_run)
            if self._select_run(connection, canonical_run_id) is not None:
                raise _state_conflict("run already exists")
            timestamp = _canonical_clock_timestamp(self._clock())
            connection.execute(
                """
                INSERT INTO runs(
                    run_id, graph_version, package_version, run_version,
                    state, revision, profile, approval_mode,
                    source_path, destination_path, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    canonical_run_id,
                    graph,
                    self._current_package_version(),
                    run,
                    WorkflowState.PREPARING.value,
                    1,
                    closed_profile.value,
                    mode.value,
                    source,
                    destination,
                    timestamp,
                    timestamp,
                ),
            )
            event = self._append_event_locked(
                connection,
                run_id=canonical_run_id,
                event_type=event_name,
                payload=payload,
                payload_text=payload_text,
                timestamp=timestamp,
                elapsed_ms=None,
            )
            del event
            if key is not None:
                connection.execute(
                    "INSERT INTO idempotency_keys(key, request_sha256, run_id) "
                    "VALUES (?, ?, ?)",
                    (key, request_hash, canonical_run_id),
                )
            created = self._select_run(connection, canonical_run_id)
            if created is None:
                raise _MigrationError("created run disappeared")
            return IdempotencyResult(existing=False, run=created)

        return self._write(create)

    def reserve_idempotency_key(
        self,
        key: str,
        request_sha256: str,
        run_id: str,
    ) -> IdempotencyResult:
        canonical_key = _idempotency_key(key)
        request_hash = _sha256(request_sha256, field="request_sha256")
        canonical_run_id = _run_id(run_id)

        def reserve(connection: sqlite3.Connection) -> IdempotencyResult:
            existing = connection.execute(
                "SELECT request_sha256, run_id FROM idempotency_keys WHERE key = ?",
                (canonical_key,),
            ).fetchone()
            if existing is not None:
                if existing["request_sha256"] != request_hash:
                    raise _coded(
                        ErrorCode.IDEMPOTENCY_CONFLICT,
                        "idempotency key was used for a different request",
                    )
                run = self._select_run(connection, existing["run_id"])
                if run is None:
                    raise _MigrationError("idempotency row references a missing run")
                return IdempotencyResult(existing=True, run=run)
            run = self._select_run(connection, canonical_run_id)
            if run is None:
                raise _state_conflict("run does not exist")
            other = connection.execute(
                "SELECT key FROM idempotency_keys WHERE run_id = ?",
                (canonical_run_id,),
            ).fetchone()
            if other is not None:
                raise _state_conflict("run already has an idempotency key")
            connection.execute(
                "INSERT INTO idempotency_keys(key, request_sha256, run_id) "
                "VALUES (?, ?, ?)",
                (canonical_key, request_hash, canonical_run_id),
            )
            return IdempotencyResult(existing=False, run=run)

        return self._write(reserve)

    def _select_run(
        self,
        connection: sqlite3.Connection,
        run_id: str,
    ) -> LedgerRunRecord | None:
        row = connection.execute(
            "SELECT * FROM runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        return _row_to_run(row) if row is not None else None

    def _run_for_cas(
        self,
        connection: sqlite3.Connection,
        *,
        run_id: str,
        expected_state: WorkflowState,
        expected_revision: int,
    ) -> LedgerRunRecord:
        run = self._select_run(connection, run_id)
        if run is None:
            raise _state_conflict("run does not exist")
        if run.state is not expected_state or run.revision != expected_revision:
            raise _state_conflict("workflow state or revision is stale")
        return run

    def _append_event_locked(
        self,
        connection: sqlite3.Connection,
        *,
        run_id: str,
        event_type: str,
        payload: Mapping[str, object],
        payload_text: str,
        timestamp: str,
        elapsed_ms: int | None,
    ) -> EventRecord:
        tail = connection.execute(
            "SELECT sequence, event_hash FROM events "
            "WHERE run_id = ? ORDER BY sequence DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        if tail is None:
            sequence = 1
            previous_hash = _ZERO_HASH
        else:
            sequence = tail["sequence"] + 1
            previous_hash = tail["event_hash"]
        digest = _event_digest(
            run_id=run_id,
            sequence=sequence,
            event_type=event_type,
            payload=payload,
            timestamp=timestamp,
            elapsed_ms=elapsed_ms,
            previous_hash=previous_hash,
        )
        connection.execute(
            """
            INSERT INTO events(
                run_id, sequence, event_type, payload_text, timestamp,
                elapsed_ms, previous_hash, event_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                sequence,
                event_type,
                payload_text,
                timestamp,
                elapsed_ms,
                previous_hash,
                digest,
            ),
        )
        return EventRecord(
            run_id=run_id,
            sequence=sequence,
            event_type=event_type,
            payload_text=payload_text,
            timestamp=timestamp,
            elapsed_ms=elapsed_ms,
            previous_hash=previous_hash,
            event_hash=digest,
        )

    def _event_mutation(
        self,
        *,
        run_id: str,
        expected_state: WorkflowState,
        expected_revision: int,
        target_state: WorkflowState,
        event_type: str,
        payload: Mapping[str, object],
        payload_text: str,
        elapsed_ms: int | None,
        projection_patch: Mapping[str, object],
    ) -> EventMutationResult:
        def mutate(connection: sqlite3.Connection) -> EventMutationResult:
            current = self._run_for_cas(
                connection,
                run_id=run_id,
                expected_state=expected_state,
                expected_revision=expected_revision,
            )
            timestamp = _canonical_clock_timestamp(self._clock())
            values = dict(projection_patch)
            values["state"] = target_state.value
            values["revision"] = current.revision + 1
            values["updated_at"] = timestamp
            assignments = ", ".join(f"{field} = ?" for field in values)
            parameters = tuple(values.values()) + (
                run_id,
                expected_state.value,
                expected_revision,
            )
            cursor = connection.execute(
                f"UPDATE runs SET {assignments} "
                "WHERE run_id = ? AND state = ? AND revision = ?",
                parameters,
            )
            if cursor.rowcount != 1:
                raise _state_conflict("workflow state or revision is stale")
            event = self._append_event_locked(
                connection,
                run_id=run_id,
                event_type=event_type,
                payload=payload,
                payload_text=payload_text,
                timestamp=timestamp,
                elapsed_ms=elapsed_ms,
            )
            updated = self._select_run(connection, run_id)
            if updated is None:
                raise _MigrationError("updated run disappeared")
            return EventMutationResult(run=updated, event=event)

        return self._write(mutate)

    def append_event(
        self,
        run_id: str,
        *,
        expected_state: WorkflowState | str,
        expected_revision: int,
        event_type: str,
        payload: Mapping[str, object],
        elapsed_ms: int | None = None,
        projection_patch: Mapping[str, object] | None = None,
    ) -> EventMutationResult:
        canonical_run_id = _run_id(run_id)
        state = _workflow_state(expected_state)
        revision = _positive_revision(expected_revision)
        name = _event_type(event_type)
        copied_payload, payload_text = _event_payload(payload)
        elapsed = _elapsed_ms(elapsed_ms)
        patch = _projection_patch(projection_patch)
        return self._event_mutation(
            run_id=canonical_run_id,
            expected_state=state,
            expected_revision=revision,
            target_state=state,
            event_type=name,
            payload=copied_payload,
            payload_text=payload_text,
            elapsed_ms=elapsed,
            projection_patch=patch,
        )

    def transition(
        self,
        run_id: str,
        *,
        expected_state: WorkflowState | str,
        expected_revision: int,
        target_state: WorkflowState | str,
        event_type: str,
        payload: Mapping[str, object],
        elapsed_ms: int | None = None,
        projection_patch: Mapping[str, object] | None = None,
    ) -> EventMutationResult:
        canonical_run_id = _run_id(run_id)
        source = _workflow_state(expected_state)
        target = _workflow_state(target_state)
        if not transition_allowed(source, target):
            raise _state_conflict("workflow transition is not allowed")
        revision = _positive_revision(expected_revision)
        name = _event_type(event_type)
        copied_payload, payload_text = _event_payload(payload)
        elapsed = _elapsed_ms(elapsed_ms)
        patch = _projection_patch(projection_patch)
        return self._event_mutation(
            run_id=canonical_run_id,
            expected_state=source,
            expected_revision=revision,
            target_state=target,
            event_type=name,
            payload=copied_payload,
            payload_text=payload_text,
            elapsed_ms=elapsed,
            projection_patch=patch,
        )

    def record_artifact(
        self,
        metadata: ArtifactMetadataV1,
        *,
        expected_state: WorkflowState | str,
        expected_revision: int,
        event_type: str,
        event_payload: Mapping[str, object],
        elapsed_ms: int | None = None,
    ) -> ArtifactMutationResult:
        canonical_metadata = _artifact_metadata(metadata)
        state = _workflow_state(expected_state)
        revision = _positive_revision(expected_revision)
        name = _event_type(event_type)
        payload, payload_text = _event_payload(event_payload)
        elapsed = _elapsed_ms(elapsed_ms)
        created_at = _canonical_datetime(canonical_metadata.created_at)

        def record(connection: sqlite3.Connection) -> ArtifactMutationResult:
            current = self._run_for_cas(
                connection,
                run_id=canonical_metadata.run_id,
                expected_state=state,
                expected_revision=revision,
            )
            duplicate = connection.execute(
                "SELECT 1 FROM artifacts WHERE run_id = ? AND kind = ?",
                (canonical_metadata.run_id, canonical_metadata.kind.value),
            ).fetchone()
            path_duplicate = connection.execute(
                "SELECT 1 FROM artifacts WHERE relative_path = ?",
                (canonical_metadata.relative_path,),
            ).fetchone()
            if duplicate is not None or path_duplicate is not None:
                raise _state_conflict("artifact is already recorded")
            connection.execute(
                """
                INSERT INTO artifacts(
                    run_id, kind, relative_path, sha256, byte_size, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    canonical_metadata.run_id,
                    canonical_metadata.kind.value,
                    canonical_metadata.relative_path,
                    canonical_metadata.sha256,
                    canonical_metadata.byte_size,
                    created_at,
                ),
            )
            timestamp = _canonical_clock_timestamp(self._clock())
            prefix = canonical_metadata.kind.value
            cursor = connection.execute(
                f"UPDATE runs SET {prefix}_sha256 = ?, {prefix}_size_bytes = ?, "
                "revision = ?, updated_at = ? "
                "WHERE run_id = ? AND state = ? AND revision = ?",
                (
                    canonical_metadata.sha256,
                    canonical_metadata.byte_size,
                    current.revision + 1,
                    timestamp,
                    canonical_metadata.run_id,
                    state.value,
                    revision,
                ),
            )
            if cursor.rowcount != 1:
                raise _state_conflict("workflow state or revision is stale")
            event = self._append_event_locked(
                connection,
                run_id=canonical_metadata.run_id,
                event_type=name,
                payload=payload,
                payload_text=payload_text,
                timestamp=timestamp,
                elapsed_ms=elapsed,
            )
            updated = self._select_run(connection, canonical_metadata.run_id)
            if updated is None:
                raise _MigrationError("updated run disappeared")
            artifact = ArtifactRecord(
                run_id=canonical_metadata.run_id,
                kind=canonical_metadata.kind,
                relative_path=canonical_metadata.relative_path,
                sha256=canonical_metadata.sha256,
                byte_size=canonical_metadata.byte_size,
                created_at=created_at,
            )
            return ArtifactMutationResult(run=updated, artifact=artifact, event=event)

        return self._write(record)

    def record_decision(
        self,
        run_id: str,
        *,
        expected_state: WorkflowState | str,
        expected_revision: int,
        decision: ApprovalDecision | str,
        source: ApprovalSource | str,
        operator: str | None,
        host: str | None,
        terminal_present: bool,
        binding_sha256: str,
        expires_at: str | None,
        approval_summary: str,
        event_type: str,
        event_payload: Mapping[str, object],
        elapsed_ms: int | None = None,
    ) -> DecisionMutationResult:
        canonical_run_id = _run_id(run_id)
        state = _workflow_state(expected_state)
        revision = _positive_revision(expected_revision)
        closed_decision = _approval_decision(decision)
        closed_source = _approval_source(source)
        target = (
            WorkflowState.APPROVED
            if closed_decision is ApprovalDecision.APPROVED
            else WorkflowState.REJECTED
        )
        if not transition_allowed(state, target):
            raise _state_conflict("workflow decision transition is not allowed")
        operator_text = _optional_text(
            operator,
            field="operator",
            maximum=_MAX_METADATA_CHARS,
        )
        host_text = _optional_text(host, field="host", maximum=_MAX_METADATA_CHARS)
        if type(terminal_present) is not bool:
            raise _invalid("terminal_present must be a boolean")
        binding = _sha256(binding_sha256, field="binding_sha256")
        expiry = (
            _canonical_timestamp(expires_at, field="expires_at")
            if expires_at is not None
            else None
        )
        summary = _bounded_text(
            approval_summary,
            field="approval_summary",
            maximum=_MAX_TEXT_CHARS,
        )
        name = _event_type(event_type)
        payload, payload_text = _event_payload(event_payload)
        elapsed = _elapsed_ms(elapsed_ms)

        def record(connection: sqlite3.Connection) -> DecisionMutationResult:
            current = self._run_for_cas(
                connection,
                run_id=canonical_run_id,
                expected_state=state,
                expected_revision=revision,
            )
            if (
                connection.execute(
                    "SELECT 1 FROM approvals WHERE run_id = ?",
                    (canonical_run_id,),
                ).fetchone()
                is not None
            ):
                raise _state_conflict("workflow decision is immutable")
            timestamp = _canonical_clock_timestamp(self._clock())
            connection.execute(
                """
                INSERT INTO approvals(
                    run_id, decision, source, operator, host, terminal_present,
                    binding_sha256, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    canonical_run_id,
                    closed_decision.value,
                    closed_source.value,
                    operator_text,
                    host_text,
                    int(terminal_present),
                    binding,
                    timestamp,
                    expiry,
                ),
            )
            approved_at = (
                timestamp if closed_decision is ApprovalDecision.APPROVED else None
            )
            cursor = connection.execute(
                """
                UPDATE runs SET
                    state = ?, revision = ?, updated_at = ?,
                    approved_at = ?, expires_at = COALESCE(?, expires_at),
                    approval_decision = ?, approval_source = ?, approval_summary = ?
                WHERE run_id = ? AND state = ? AND revision = ?
                """,
                (
                    target.value,
                    current.revision + 1,
                    timestamp,
                    approved_at,
                    expiry,
                    closed_decision.value,
                    closed_source.value,
                    summary,
                    canonical_run_id,
                    state.value,
                    revision,
                ),
            )
            if cursor.rowcount != 1:
                raise _state_conflict("workflow state or revision is stale")
            event = self._append_event_locked(
                connection,
                run_id=canonical_run_id,
                event_type=name,
                payload=payload,
                payload_text=payload_text,
                timestamp=timestamp,
                elapsed_ms=elapsed,
            )
            updated = self._select_run(connection, canonical_run_id)
            if updated is None:
                raise _MigrationError("updated run disappeared")
            approval = ApprovalRecord(
                run_id=canonical_run_id,
                decision=closed_decision,
                source=closed_source,
                operator=operator_text,
                host=host_text,
                terminal_present=terminal_present,
                binding_sha256=binding,
                created_at=timestamp,
                expires_at=expiry,
            )
            return DecisionMutationResult(
                run=updated,
                approval=approval,
                event=event,
            )

        return self._write(record)

    def get_run(self, run_id: str) -> LedgerRunRecord | None:
        canonical_run_id = _run_id(run_id)
        connection = self._connect_existing()
        try:
            return self._select_run(connection, canonical_run_id)
        except FCPMCPError:
            raise
        except _LEDGER_FAILURES as error:
            raise _ledger_unavailable(error)
        finally:
            connection.close()

    def list_runs(
        self,
        *,
        limit: int = 100,
        state: WorkflowState | str | None = None,
    ) -> tuple[LedgerRunRecord, ...]:
        bounded_limit = _read_limit(limit)
        closed_state = _workflow_state(state) if state is not None else None
        connection = self._connect_existing()
        try:
            if closed_state is None:
                cursor = connection.execute(
                    "SELECT * FROM runs ORDER BY created_at DESC, run_id ASC LIMIT ?",
                    (bounded_limit,),
                )
            else:
                cursor = connection.execute(
                    "SELECT * FROM runs WHERE state = ? "
                    "ORDER BY created_at DESC, run_id ASC LIMIT ?",
                    (closed_state.value, bounded_limit),
                )
            return tuple(_row_to_run(row) for row in cursor.fetchmany(bounded_limit))
        except FCPMCPError:
            raise
        except _LEDGER_FAILURES as error:
            raise _ledger_unavailable(error)
        finally:
            connection.close()

    def list_events(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> tuple[EventRecord, ...]:
        canonical_run_id = _run_id(run_id)
        bounded_limit = _read_limit(limit)
        if (
            type(after_sequence) is not int
            or not 0 <= after_sequence <= _SQLITE_MAX_INTEGER
        ):
            raise _invalid("after_sequence must be a nonnegative integer")
        connection = self._connect_existing()
        try:
            cursor = connection.execute(
                "SELECT * FROM events WHERE run_id = ? AND sequence > ? "
                "ORDER BY sequence ASC LIMIT ?",
                (canonical_run_id, after_sequence, bounded_limit),
            )
            return tuple(
                _row_to_event(row) for row in cursor.fetchmany(bounded_limit)
            )
        except FCPMCPError:
            raise
        except _LEDGER_FAILURES as error:
            raise _ledger_unavailable(error)
        finally:
            connection.close()

    def verify_integrity(self, run_id: str | None = None) -> IntegrityResult:
        canonical_run_id = _run_id(run_id) if run_id is not None else None
        connection = self._connect_existing()
        begun = False
        findings: list[IntegrityFinding] = []
        checked_migrations = 0
        checked_runs = 0
        checked_events = 0

        def add(
            code: str,
            summary: str,
            *,
            finding_run_id: str | None = None,
            sequence: int | None = None,
        ) -> None:
            if len(findings) >= _MAX_INTEGRITY_FINDINGS:
                return
            findings.append(
                IntegrityFinding(
                    code=code[:64],
                    summary=summary[:_MAX_TEXT_CHARS],
                    run_id=finding_run_id,
                    sequence=sequence,
                )
            )

        try:
            connection.execute("BEGIN")
            begun = True
            migration_rows = self._migration_rows(connection)
            checked_migrations = len(migration_rows)
            try:
                self._validate_migrations(migration_rows)
                if len(migration_rows) != len(MIGRATIONS):
                    raise _MigrationError("applied migration sequence is incomplete")
            except _MigrationError:
                add("migration_mismatch", "stored migration identity does not match")

            if canonical_run_id is None:
                orphan_cursor = connection.execute(
                    "SELECT e.run_id, e.sequence FROM events AS e "
                    "LEFT JOIN runs AS r ON r.run_id = e.run_id "
                    "WHERE r.run_id IS NULL "
                    "ORDER BY e.run_id ASC, e.sequence ASC"
                )
            else:
                orphan_cursor = connection.execute(
                    "SELECT e.run_id, e.sequence FROM events AS e "
                    "LEFT JOIN runs AS r ON r.run_id = e.run_id "
                    "WHERE r.run_id IS NULL AND e.run_id = ? "
                    "ORDER BY e.sequence ASC",
                    (canonical_run_id,),
                )
            while True:
                orphan_rows = orphan_cursor.fetchmany(100)
                if not orphan_rows:
                    break
                for orphan in orphan_rows:
                    checked_events += 1
                    add(
                        "orphan_event",
                        "event references a missing run",
                        finding_run_id=orphan["run_id"],
                        sequence=orphan["sequence"],
                    )

            if canonical_run_id is None:
                run_cursor = connection.execute(
                    "SELECT run_id, revision FROM runs ORDER BY run_id ASC"
                )
            else:
                run_cursor = connection.execute(
                    "SELECT run_id, revision FROM runs WHERE run_id = ?",
                    (canonical_run_id,),
                )

            while True:
                run_rows = run_cursor.fetchmany(100)
                if not run_rows:
                    break
                for run_row in run_rows:
                    current_run_id = run_row["run_id"]
                    current_revision = run_row["revision"]
                    checked_runs += 1
                    event_cursor = connection.execute(
                        "SELECT run_id, sequence, event_type, payload_text, timestamp, "
                        "elapsed_ms, previous_hash, event_hash "
                        "FROM events WHERE run_id = ? ORDER BY sequence ASC",
                        (current_run_id,),
                    )
                    expected_sequence = 1
                    expected_previous = _ZERO_HASH
                    run_event_count = 0
                    while True:
                        event_rows = event_cursor.fetchmany(100)
                        if not event_rows:
                            break
                        for row in event_rows:
                            run_event_count += 1
                            checked_events += 1
                            raw_sequence = row["sequence"]
                            sequence_value = (
                                raw_sequence if type(raw_sequence) is int else None
                            )
                            if sequence_value != expected_sequence:
                                add(
                                    "sequence_gap",
                                    "event sequence is not contiguous",
                                    finding_run_id=current_run_id,
                                    sequence=sequence_value,
                                )
                            raw_previous = row["previous_hash"]
                            if raw_previous != expected_previous:
                                add(
                                    "previous_hash_mismatch",
                                    "event previous hash edge does not match",
                                    finding_run_id=current_run_id,
                                    sequence=sequence_value,
                                )
                            payload_value: Mapping[str, object] | None = None
                            payload_text = row["payload_text"]
                            if not isinstance(payload_text, str):
                                add(
                                    "invalid_payload",
                                    "event payload text is not text",
                                    finding_run_id=current_run_id,
                                    sequence=sequence_value,
                                )
                            else:
                                try:
                                    parsed = json.loads(
                                        payload_text,
                                        object_pairs_hook=_unique_json_object,
                                        parse_constant=_reject_json_constant,
                                    )
                                    if not isinstance(parsed, dict):
                                        raise TypeError("payload is not an object")
                                    payload_value = parsed
                                    canonical_text = canonical_json(parsed).decode("utf-8")
                                    if canonical_text != payload_text:
                                        add(
                                            "noncanonical_payload",
                                            "event payload text is not canonical",
                                            finding_run_id=current_run_id,
                                            sequence=sequence_value,
                                        )
                                except (TypeError, ValueError, json.JSONDecodeError):
                                    add(
                                        "invalid_payload",
                                        "event payload text cannot be interpreted",
                                        finding_run_id=current_run_id,
                                        sequence=sequence_value,
                                    )
                            elapsed = row["elapsed_ms"]
                            elapsed_valid = elapsed is None or (
                                type(elapsed) is int and elapsed >= 0
                            )
                            if not elapsed_valid:
                                add(
                                    "invalid_elapsed_ms",
                                    "event elapsed duration is invalid",
                                    finding_run_id=current_run_id,
                                    sequence=sequence_value,
                                )
                            stored_fields_valid = (
                                row["run_id"] == current_run_id
                                and sequence_value is not None
                                and sequence_value > 0
                                and isinstance(row["event_type"], str)
                                and 1
                                <= len(row["event_type"])
                                <= _MAX_EVENT_TYPE_CHARS
                                and _stored_timestamp_is_canonical(row["timestamp"])
                                and elapsed_valid
                                and isinstance(raw_previous, str)
                                and _SHA256_RE.fullmatch(raw_previous) is not None
                                and isinstance(row["event_hash"], str)
                                and _SHA256_RE.fullmatch(row["event_hash"]) is not None
                                and isinstance(payload_text, str)
                                and len(payload_text.encode("utf-8"))
                                <= _MAX_EVENT_PAYLOAD_BYTES
                            )
                            if not stored_fields_valid:
                                add(
                                    "invalid_event_fields",
                                    "event fields violate the storage contract",
                                    finding_run_id=current_run_id,
                                    sequence=sequence_value,
                                )
                            if payload_value is not None and sequence_value is not None:
                                try:
                                    recomputed = _event_digest(
                                        run_id=row["run_id"],
                                        sequence=sequence_value,
                                        event_type=row["event_type"],
                                        payload=payload_value,
                                        timestamp=row["timestamp"],
                                        elapsed_ms=elapsed,
                                        previous_hash=raw_previous,
                                    )
                                except (TypeError, ValueError):
                                    add(
                                        "invalid_event_fields",
                                        "event fields cannot be hashed",
                                        finding_run_id=current_run_id,
                                        sequence=sequence_value,
                                    )
                                else:
                                    if recomputed != row["event_hash"]:
                                        add(
                                            "event_hash_mismatch",
                                            "event hash does not match stored fields",
                                            finding_run_id=current_run_id,
                                            sequence=sequence_value,
                                        )
                            expected_sequence += 1
                            if isinstance(row["event_hash"], str):
                                expected_previous = row["event_hash"]
                    if run_event_count == 0:
                        add(
                            "missing_event_chain",
                            "run has no event chain",
                            finding_run_id=current_run_id,
                        )
                    if (
                        type(current_revision) is not int
                        or not 1 <= current_revision <= _SQLITE_MAX_INTEGER
                        or run_event_count != current_revision
                        or expected_sequence - 1 != current_revision
                    ):
                        add(
                            "event_projection_mismatch",
                            "event chain does not match the run revision",
                            finding_run_id=current_run_id,
                        )
            if canonical_run_id is not None and checked_runs == 0:
                add(
                    "run_missing",
                    "requested run does not exist",
                    finding_run_id=canonical_run_id,
                )
            result = IntegrityResult(
                valid=not findings,
                checked_migrations=checked_migrations,
                checked_runs=checked_runs,
                checked_events=checked_events,
                findings=tuple(findings),
            )
            connection.execute("ROLLBACK")
            begun = False
            return result
        except FCPMCPError:
            raise
        except _LEDGER_FAILURES as error:
            raise _ledger_unavailable(error)
        finally:
            _rollback(connection, begun)
            connection.close()


def _row_to_run(row: sqlite3.Row) -> LedgerRunRecord:
    return LedgerRunRecord(
        run_id=row["run_id"],
        graph_version=row["graph_version"],
        package_version=row["package_version"],
        run_version=row["run_version"],
        state=WorkflowState(row["state"]),
        revision=row["revision"],
        profile=Profile(row["profile"]),
        approval_mode=ApprovalMode(row["approval_mode"]),
        source_path=row["source_path"],
        destination_path=row["destination_path"],
        source_sha256=row["source_sha256"],
        destination_sha256=row["destination_sha256"],
        prior_destination_state=(
            PriorDestinationState(row["prior_destination_state"])
            if row["prior_destination_state"] is not None
            else None
        ),
        prior_destination_sha256=row["prior_destination_sha256"],
        plan_sha256=row["plan_sha256"],
        candidate_sha256=row["candidate_sha256"],
        candidate_size_bytes=row["candidate_size_bytes"],
        diff_sha256=row["diff_sha256"],
        diff_size_bytes=row["diff_size_bytes"],
        receipt_sha256=row["receipt_sha256"],
        receipt_size_bytes=row["receipt_size_bytes"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        approved_at=row["approved_at"],
        committed_at=row["committed_at"],
        expires_at=row["expires_at"],
        commit_attempt_id=row["commit_attempt_id"],
        expected_backup_path=row["expected_backup_path"],
        backup_sha256=row["backup_sha256"],
        approval_decision=(
            ApprovalDecision(row["approval_decision"])
            if row["approval_decision"] is not None
            else None
        ),
        approval_source=(
            ApprovalSource(row["approval_source"])
            if row["approval_source"] is not None
            else None
        ),
        approval_summary=row["approval_summary"],
        terminal_error_code=(
            ErrorCode(row["terminal_error_code"])
            if row["terminal_error_code"] is not None
            else None
        ),
        terminal_error_summary=row["terminal_error_summary"],
    )


def _row_to_event(row: sqlite3.Row) -> EventRecord:
    return EventRecord(
        run_id=row["run_id"],
        sequence=row["sequence"],
        event_type=row["event_type"],
        payload_text=row["payload_text"],
        timestamp=row["timestamp"],
        elapsed_ms=row["elapsed_ms"],
        previous_hash=row["previous_hash"],
        event_hash=row["event_hash"],
    )


def _run_id(value: object) -> str:
    if not isinstance(value, str) or not _UUID_RE.fullmatch(value):
        raise _invalid("run_id must use canonical lowercase UUID spelling")
    try:
        parsed = UUID(value)
    except ValueError as error:
        raise _invalid("run_id is not a UUID", error)
    if parsed.int == 0 or str(parsed) != value:
        raise _invalid("run_id must be a non-nil canonical lowercase UUID")
    return value


def _bounded_text(value: object, *, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= maximum:
        raise _invalid(f"{field} must be nonempty bounded text")
    return value


def _optional_text(
    value: object,
    *,
    field: str,
    maximum: int,
) -> str | None:
    if value is None:
        return None
    return _bounded_text(value, field=field, maximum=maximum)


def _sha256(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise _invalid(f"{field} must be a lowercase SHA-256 digest")
    return value


def _profile(value: object) -> Profile:
    if isinstance(value, Profile):
        return value
    if isinstance(value, str):
        try:
            return Profile(value)
        except ValueError:
            pass
    raise _invalid("profile is invalid")


def _approval_mode(value: object) -> ApprovalMode:
    if isinstance(value, ApprovalMode):
        return value
    if isinstance(value, str):
        try:
            return ApprovalMode(value)
        except ValueError:
            pass
    raise _invalid("approval_mode is invalid")


def _workflow_state(value: object) -> WorkflowState:
    if isinstance(value, WorkflowState):
        return value
    if isinstance(value, str):
        try:
            return WorkflowState(value)
        except ValueError:
            pass
    raise _invalid("workflow state is invalid")


def _approval_decision(value: object) -> ApprovalDecision:
    if isinstance(value, ApprovalDecision):
        return value
    if isinstance(value, str):
        try:
            return ApprovalDecision(value)
        except ValueError:
            pass
    raise _invalid("approval decision is invalid")


def _approval_source(value: object) -> ApprovalSource:
    if isinstance(value, ApprovalSource):
        return value
    if isinstance(value, str):
        try:
            return ApprovalSource(value)
        except ValueError:
            pass
    raise _invalid("approval source is invalid")


def _positive_revision(value: object) -> int:
    if type(value) is not int or not 1 <= value <= _SQLITE_MAX_INTEGER:
        raise _invalid("expected_revision must be a positive integer")
    return value


def _read_limit(value: object) -> int:
    if type(value) is not int or not 1 <= value <= _MAX_READ_LIMIT:
        raise _invalid(f"limit must be within 1..{_MAX_READ_LIMIT}")
    return value


def _elapsed_ms(value: object) -> int | None:
    if value is None:
        return None
    if type(value) is not int or not 0 <= value <= _SQLITE_MAX_INTEGER:
        raise _invalid("elapsed_ms must be a nonnegative integer or null")
    return value


def _event_type(value: object) -> str:
    return _bounded_text(
        value,
        field="event_type",
        maximum=_MAX_EVENT_TYPE_CHARS,
    )


def _event_payload(
    value: object,
) -> tuple[Mapping[str, object], str]:
    if not isinstance(value, Mapping):
        raise _invalid("event payload must be a JSON object")
    try:
        encoded = canonical_json(value)
        parsed = json.loads(encoded)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise _invalid("event payload must contain canonical JSON values", error)
    if len(encoded) > _MAX_EVENT_PAYLOAD_BYTES:
        raise _invalid("event payload exceeds the byte limit")
    if not isinstance(parsed, dict):
        raise _invalid("event payload must be a JSON object")
    return parsed, encoded.decode("utf-8")


def _event_digest(
    *,
    run_id: str,
    sequence: int,
    event_type: str,
    payload: Mapping[str, object],
    timestamp: str,
    elapsed_ms: int | None,
    previous_hash: str,
) -> str:
    body = canonical_json(
        {
            "run_id": run_id,
            "sequence": sequence,
            "event_type": event_type,
            "payload": payload,
            "timestamp": timestamp,
            "elapsed_ms": elapsed_ms,
            "previous_hash": previous_hash,
        }
    )
    return hashlib.sha256(body).hexdigest()


def _canonical_clock_timestamp(value: object) -> str:
    if not isinstance(value, datetime):
        raise TypeError("clock must return datetime")
    if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
        raise ValueError("clock must return a UTC-aware datetime")
    return _canonical_datetime(value)


def _canonical_datetime(value: datetime) -> str:
    canonical = value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return canonical


def _canonical_timestamp(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not _UTC_RE.fullmatch(value):
        raise _invalid(f"{field} must use canonical UTC timestamp spelling")
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as error:
        raise _invalid(f"{field} must be a valid UTC timestamp", error)
    canonical = _canonical_datetime(parsed)
    if canonical != value:
        raise _invalid(f"{field} must use canonical UTC timestamp spelling")
    return value


def _stored_timestamp_is_canonical(value: object) -> bool:
    if not isinstance(value, str) or not _UTC_RE.fullmatch(value):
        return False
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError:
        return False
    return _canonical_datetime(parsed) == value


def _idempotency_key(value: object) -> str:
    return _bounded_text(
        value,
        field="idempotency_key",
        maximum=_MAX_IDEMPOTENCY_KEY_CHARS,
    )


def _artifact_metadata(value: object) -> ArtifactMetadataV1:
    if not isinstance(value, ArtifactMetadataV1):
        raise _invalid("metadata must be ArtifactMetadataV1")
    try:
        validated = ArtifactMetadataV1.model_validate(
            value.model_dump(mode="python"),
            strict=True,
        )
    except ValidationError as error:
        raise _invalid("artifact metadata is not canonical", error)
    if validated.byte_size > _SQLITE_MAX_INTEGER:
        raise _invalid("artifact byte_size exceeds the storage limit")
    return validated


def _projection_patch(
    value: Mapping[str, object] | None,
) -> dict[str, object]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise _invalid("projection_patch must be a mapping")
    unknown = set(value) - _PROJECTION_PATCH_FIELDS
    if unknown:
        raise _invalid("projection_patch contains unsupported fields")
    validated: dict[str, object] = {}
    for field, raw in value.items():
        if field.endswith("_sha256"):
            validated[field] = (
                _sha256(raw, field=field) if raw is not None else None
            )
        elif field in {"receipt_size_bytes"}:
            if raw is not None and (
                type(raw) is not int
                or not 0 <= raw <= _SQLITE_MAX_INTEGER
            ):
                raise _invalid(f"{field} must be a nonnegative integer or null")
            validated[field] = raw
        elif field == "prior_destination_state":
            if raw is None:
                validated[field] = None
            elif isinstance(raw, PriorDestinationState):
                validated[field] = raw.value
            elif isinstance(raw, str):
                try:
                    validated[field] = PriorDestinationState(raw).value
                except ValueError as error:
                    raise _invalid("prior_destination_state is invalid", error)
            else:
                raise _invalid("prior_destination_state is invalid")
        elif field in {"expires_at", "committed_at"}:
            validated[field] = (
                _canonical_timestamp(raw, field=field) if raw is not None else None
            )
        elif field == "terminal_error_code":
            if raw is None:
                validated[field] = None
            elif isinstance(raw, ErrorCode):
                validated[field] = raw.value
            elif isinstance(raw, str):
                try:
                    validated[field] = ErrorCode(raw).value
                except ValueError as error:
                    raise _invalid("terminal_error_code is invalid", error)
            else:
                raise _invalid("terminal_error_code is invalid")
        elif field in {
            "commit_attempt_id",
            "expected_backup_path",
            "approval_summary",
            "terminal_error_summary",
        }:
            maximum = (
                _MAX_TEXT_CHARS
                if field
                in {
                    "expected_backup_path",
                    "approval_summary",
                    "terminal_error_summary",
                }
                else _MAX_METADATA_CHARS
            )
            validated[field] = _optional_text(raw, field=field, maximum=maximum)
        else:
            raise _invalid("projection_patch contains unsupported fields")
    return validated


def _unique_json_object(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"invalid JSON constant: {value}")


__all__ = [
    "MIGRATIONS",
    "ApprovalRecord",
    "ArtifactMutationResult",
    "ArtifactRecord",
    "DecisionMutationResult",
    "EventMutationResult",
    "EventRecord",
    "IdempotencyResult",
    "IntegrityFinding",
    "IntegrityResult",
    "LedgerRunRecord",
    "Migration",
    "WorkflowLedger",
    "migration_checksum",
]
