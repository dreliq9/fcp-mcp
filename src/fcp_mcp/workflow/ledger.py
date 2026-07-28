"""Durable, append-only workflow ledger backed by private SQLite storage."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import sqlite3
import stat
import struct
import sys
import threading
import zlib
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager, nullcontext
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
_ROOT_MODE = 0o700
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
_FINGERPRINT_CHUNK_BYTES = 64 * 1024
_MAX_FINGERPRINT_DATABASE_BYTES = 4 * 1024 * 1024 * 1024
_MAX_FINGERPRINT_SCHEMA_OBJECTS = 256
_MAX_FINGERPRINT_SCHEMA_BYTES = 4 * 1024 * 1024
_MAX_FINGERPRINT_METADATA_BYTES = 1024 * 1024
_MAX_FINGERPRINT_TABLES = 64
_MAX_FINGERPRINT_COLUMNS = 128
_MAX_FINGERPRINT_TABLE_ROWS = 1_000_000
_MAX_FINGERPRINT_PRIMARY_KEY_BYTES = _FINGERPRINT_CHUNK_BYTES
_MAX_FINGERPRINT_CELL_BYTES = 2 * 1024 * 1024
_MAX_FINGERPRINT_ROW_BYTES = 4 * 1024 * 1024
_MAX_FINGERPRINT_TABLE_BYTES = 1024 * 1024 * 1024
_MAX_FINGERPRINT_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
_SQLITE_MAX_INTEGER = 2**63 - 1
_BACKUP_PERSISTENT_PRAGMAS = (
    "application_id",
    "user_version",
    "page_size",
    "auto_vacuum",
    "encoding",
)
_BOOTSTRAP_TABLE = "__fcp_ledger_identity"
_BOOTSTRAP_SCHEMA_SQL = (
    "CREATE TABLE __fcp_ledger_identity("
    "token TEXT NOT NULL CHECK(length(token)=64))"
)
_BOOTSTRAP_PLACEHOLDER = b"0" * 64
_BOOTSTRAP_TOKEN_OFFSET = 8128
_BOOTSTRAP_TEMP_PREFIX = ".runs.sqlite3.bootstrap-"
_BOOTSTRAP_LOCK_NAME = ".runs.sqlite3.bootstrap.lock"
_BOOTSTRAP_THREAD_LOCK = threading.Lock()
_BACKUP_LOCK_NAME = ".runs.sqlite3.backup.lock"
_BACKUP_THREAD_LOCK = threading.Lock()
# Generated with SQLite 3.53.0 using a 4096-byte page size, the exact
# _BOOTSTRAP_SCHEMA_SQL above, and one 64-character zero-token row. SQLite's
# version-3 file format is backwards compatible across the supported runtime
# matrix. The compressed fixture is validated without filesystem I/O at import.
_BOOTSTRAP_IMAGE = zlib.decompress(
    base64.b64decode(
        "eNrt18EKAVEUBuBzh1iJne1ZmpQUsVKYbikTYZTdNLiYjCHdkuU8hKfxAh7LCBsp"
        "Cxvl/zr/4pz+Fzijge1rxYvtfuNprlCOhKAmMxEZjzyJOMmX/RODSqdL5lbOHige"
        "AAAAAAAAgH8SpUQ6X6+LqKi9aaBcdzHbuYGaL9Xe9ecq1L4+vj0a1lC2HMlOq21"
        "Lflsp6O1ahezIicO9fpyxbbPVkVa3EKhwqVf3gtmoVU3z/pufKR4AAAAAAAAA+D2"
        "WSESZ8peucc1Fcw=="
    )
)
if (
    len(_BOOTSTRAP_IMAGE) != 8192
    or not _BOOTSTRAP_IMAGE.startswith(b"SQLite format 3\x00")
    or _BOOTSTRAP_IMAGE[16:18] != b"\x10\x00"
    or _BOOTSTRAP_IMAGE.count(_BOOTSTRAP_PLACEHOLDER) != 1
    or _BOOTSTRAP_IMAGE[
        _BOOTSTRAP_TOKEN_OFFSET : _BOOTSTRAP_TOKEN_OFFSET
        + len(_BOOTSTRAP_PLACEHOLDER)
    ]
    != _BOOTSTRAP_PLACEHOLDER
):
    raise RuntimeError("embedded workflow ledger bootstrap image is invalid")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_UTC_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{6})?Z$"
)
_PROJECTED_ARTIFACT_KINDS = (
    ArtifactKind.CANDIDATE,
    ArtifactKind.DIFF,
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


@dataclass
class _RootAnchor:
    path: Path
    descriptor: int
    identity: tuple[int, int, int]
    _closed: bool = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        os.close(self.descriptor)


@dataclass
class _DatabaseLease:
    anchor: _RootAnchor
    name: str
    descriptor: int
    identity: tuple[int, int, int]
    bootstrap_token: str | None
    owns_anchor: bool = True

    def close(self) -> None:
        failure: BaseException | None = None
        try:
            os.close(self.descriptor)
        except OSError as error:
            failure = error
        finally:
            if self.owns_anchor:
                try:
                    self.anchor.close()
                except (OSError, RuntimeError) as error:
                    if failure is None:
                        failure = error
                    else:
                        _attach_cleanup_failures(failure, [error])
        if failure is not None:
            raise failure


class _LedgerConnection(sqlite3.Connection):
    """SQLite connection that retains its validated filesystem authority."""

    _ledger_lease: _DatabaseLease | None = None
    _ledger_read_only = False

    def retain_lease(
        self,
        lease: _DatabaseLease,
        *,
        read_only: bool,
    ) -> None:
        if self._ledger_lease is not None:
            raise RuntimeError("workflow connection already retains a lease")
        self._ledger_lease = lease
        self._ledger_read_only = read_only

    def close(self) -> None:
        primary: BaseException | None = None
        try:
            super().close()
        except sqlite3.Error as error:
            primary = error
        lease, self._ledger_lease = self._ledger_lease, None
        if lease is not None:
            try:
                lease.close()
            except (OSError, RuntimeError) as error:
                if primary is None:
                    primary = error
                else:
                    _attach_cleanup_failures(primary, [error])
        if primary is not None:
            raise primary


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
        run_id TEXT NOT NULL REFERENCES runs(run_id) CHECK (length(run_id) = 36),
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
        run_id TEXT NOT NULL REFERENCES runs(run_id) CHECK (length(run_id) = 36),
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
        run_id TEXT PRIMARY KEY REFERENCES runs(run_id) CHECK (length(run_id) = 36),
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
        run_id TEXT NOT NULL UNIQUE REFERENCES runs(run_id) CHECK (length(run_id) = 36)
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

_FAILURE_EVIDENCE_ARTIFACTS_TABLE = """
    CREATE TABLE artifacts (
        run_id TEXT NOT NULL REFERENCES runs(run_id) CHECK (length(run_id) = 36),
        kind TEXT NOT NULL CHECK (kind IN ('candidate', 'diff', 'failure_evidence')),
        relative_path TEXT NOT NULL UNIQUE CHECK (length(relative_path) BETWEEN 1 AND 255),
        sha256 TEXT NOT NULL CHECK (
            length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*'
        ),
        byte_size INTEGER NOT NULL CHECK (byte_size >= 0),
        created_at TEXT NOT NULL CHECK (length(created_at) BETWEEN 20 AND 27),
        PRIMARY KEY (run_id, kind)
    )
    """.strip()

_ARTIFACTS_NO_UPDATE_TRIGGER = """
    CREATE TRIGGER artifacts_no_update
    BEFORE UPDATE ON artifacts
    BEGIN SELECT RAISE(ABORT, 'artifacts is append-only'); END
    """.strip()

_ARTIFACTS_NO_DELETE_TRIGGER = """
    CREATE TRIGGER artifacts_no_delete
    BEFORE DELETE ON artifacts
    BEGIN SELECT RAISE(ABORT, 'artifacts is append-only'); END
    """.strip()

_MIGRATION_2_STATEMENTS = (
    "DROP TRIGGER artifacts_no_update",
    "DROP TRIGGER artifacts_no_delete",
    "ALTER TABLE artifacts RENAME TO artifacts_v1",
    _FAILURE_EVIDENCE_ARTIFACTS_TABLE,
    """
    INSERT INTO artifacts(run_id, kind, relative_path, sha256, byte_size, created_at)
    SELECT run_id, kind, relative_path, sha256, byte_size, created_at
    FROM artifacts_v1
    """.strip(),
    "DROP TABLE artifacts_v1",
    _ARTIFACTS_NO_UPDATE_TRIGGER,
    _ARTIFACTS_NO_DELETE_TRIGGER,
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
    Migration(
        version=2,
        name="add_prepare_failure_evidence",
        statements=_MIGRATION_2_STATEMENTS,
        checksum=migration_checksum(
            2,
            "add_prepare_failure_evidence",
            _MIGRATION_2_STATEMENTS,
        ),
    ),
)

_ALL_MIGRATION_STATEMENTS = _MIGRATION_1_STATEMENTS + _MIGRATION_2_STATEMENTS
_EXPECTED_TABLE_SQL = {
    statement.split()[2]: statement
    for statement in _ALL_MIGRATION_STATEMENTS
    if statement.startswith("CREATE TABLE ")
}
_EXPECTED_TRIGGER_SQL = {
    statement.split()[2]: statement
    for statement in _ALL_MIGRATION_STATEMENTS
    if statement.startswith("CREATE TRIGGER ")
}
_EXPECTED_TRIGGER_TABLES = {
    name: re.search(r"\bON ([a-z_]+)\b", statement).group(1)
    for name, statement in _EXPECTED_TRIGGER_SQL.items()
}
_EXPECTED_INDEX_SQL = {
    statement.split()[2]: statement
    for statement in _ALL_MIGRATION_STATEMENTS
    if statement.startswith("CREATE INDEX ")
}
_EXPECTED_INDEX_TABLES = {
    name: re.search(r"\bON ([a-z_]+)\b", statement).group(1)
    for name, statement in _EXPECTED_INDEX_SQL.items()
}
_REQUIRED_TABLES = frozenset(_EXPECTED_TABLE_SQL)
_REQUIRED_TRIGGERS = frozenset(_EXPECTED_TRIGGER_SQL)
_REQUIRED_INDEXES = frozenset(_EXPECTED_INDEX_SQL)
_TERMINAL_STATES = frozenset(
    {
        WorkflowState.COMMITTED,
        WorkflowState.FAILED,
        WorkflowState.REJECTED,
        WorkflowState.CANCELLED,
        WorkflowState.EXPIRED,
        WorkflowState.STALE,
        WorkflowState.ROLLED_BACK,
        WorkflowState.RECOVERY_REQUIRED,
    }
)
_TERMINAL_AUDIT_EVENTS = frozenset(
    {"artifact_prune_intent", "artifacts_pruned"}
)
_PRUNE_ELIGIBLE_STATES = frozenset(
    {
        WorkflowState.COMMITTED,
        WorkflowState.FAILED,
        WorkflowState.REJECTED,
        WorkflowState.CANCELLED,
        WorkflowState.EXPIRED,
        WorkflowState.STALE,
        WorkflowState.ROLLED_BACK,
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
_PREPARE_PROJECTION_EVENTS: Mapping[str, frozenset[str]] = {
    "source_sha256": frozenset({"source_inspected"}),
    "prior_destination_state": frozenset({"source_inspected"}),
    "prior_destination_sha256": frozenset({"source_inspected"}),
    "plan_sha256": frozenset({"plan_built", "plan_normalized"}),
    "expires_at": frozenset(
        {
            "awaiting_approval",
            "prepare_completed",
            "prepared",
            "preview_persisted",
        }
    ),
}
_SOURCE_INSPECTION_FIELDS = frozenset(
    {
        "source_sha256",
        "prior_destination_state",
        "prior_destination_sha256",
    }
)
_COMMIT_INTENT_PROJECTION_EVENTS: Mapping[str, frozenset[str]] = {
    "commit_attempt_id": frozenset({"commit_started"}),
    "expected_backup_path": frozenset({"commit_started"}),
    "backup_sha256": frozenset({"backup_created", "commit_started"}),
}
_COMMIT_RESULT_PROJECTION_EVENTS: Mapping[str, frozenset[str]] = {
    "destination_sha256": frozenset({"commit_completed", "committed"}),
    "backup_sha256": frozenset({"commit_completed", "committed"}),
    "receipt_sha256": frozenset({"commit_completed", "committed"}),
    "receipt_size_bytes": frozenset({"commit_completed", "committed"}),
    "committed_at": frozenset({"commit_completed", "committed"}),
}
_COMMIT_INTENT_FIELDS = frozenset(_COMMIT_INTENT_PROJECTION_EVENTS)
_COMMIT_RESULT_FIELDS = frozenset(_COMMIT_RESULT_PROJECTION_EVENTS)
_PREPARE_PROJECTION_EVENT_NAMES = frozenset(
    event_type
    for event_types in _PREPARE_PROJECTION_EVENTS.values()
    for event_type in event_types
)
_COMMIT_INTENT_EVENT_NAMES = frozenset(
    event_type
    for event_types in _COMMIT_INTENT_PROJECTION_EVENTS.values()
    for event_type in event_types
)
_COMMIT_RESULT_EVENT_NAMES = frozenset(
    event_type
    for event_types in _COMMIT_RESULT_PROJECTION_EVENTS.values()
    for event_type in event_types
)
_PROJECTION_EVENT_NAMES = (
    _PREPARE_PROJECTION_EVENT_NAMES
    | _COMMIT_INTENT_EVENT_NAMES
    | _COMMIT_RESULT_EVENT_NAMES
)
_TERMINAL_ERROR_FIELDS = frozenset(
    {"terminal_error_code", "terminal_error_summary"}
)
_APPROVAL_PROJECTION_FIELDS = frozenset({"approval_summary"})
_ERROR_ALLOWED_STATES = frozenset(
    {
        WorkflowState.FAILED,
        WorkflowState.STALE,
        WorkflowState.ROLLED_BACK,
        WorkflowState.RECOVERY_REQUIRED,
    }
)
_ERROR_REQUIRED_STATES = frozenset(
    {WorkflowState.FAILED, WorkflowState.RECOVERY_REQUIRED}
)
_PREPARED_STATES = frozenset(
    {
        WorkflowState.AWAITING_APPROVAL,
        WorkflowState.APPROVED,
        WorkflowState.COMMITTING,
        WorkflowState.COMMITTED,
        WorkflowState.REJECTED,
        WorkflowState.EXPIRED,
        WorkflowState.STALE,
        WorkflowState.ROLLED_BACK,
        WorkflowState.RECOVERY_REQUIRED,
    }
)
_APPROVED_STATES = frozenset(
    {
        WorkflowState.APPROVED,
        WorkflowState.COMMITTING,
        WorkflowState.COMMITTED,
        WorkflowState.STALE,
        WorkflowState.ROLLED_BACK,
        WorkflowState.RECOVERY_REQUIRED,
    }
)


def _stat_identity(result: os.stat_result) -> tuple[int, int, int]:
    return (
        result.st_dev,
        result.st_ino,
        stat.S_IFMT(result.st_mode),
    )


def _fingerprint_frame(tag: bytes, payload: bytes) -> bytes:
    return _fingerprint_frame_header(tag, len(payload)) + payload


def _fingerprint_frame_header(tag: bytes, payload_size: int) -> bytes:
    if len(tag) != 1:
        raise _MigrationError("database fingerprint tag is invalid")
    if type(payload_size) is not int or not 0 <= payload_size <= 2**64 - 1:
        raise _MigrationError("database fingerprint payload size is invalid")
    return tag + payload_size.to_bytes(8, "big")


def _fingerprint_value(value: object) -> bytes:
    if value is None:
        return _fingerprint_frame(b"N", b"")
    if type(value) is int:
        if not -(2**63) <= value <= 2**63 - 1:
            raise _MigrationError("database integer is outside SQLite range")
        return _fingerprint_frame(b"I", value.to_bytes(8, "big", signed=True))
    if type(value) is float:
        return _fingerprint_frame(b"R", struct.pack(">d", value))
    if isinstance(value, str):
        return _fingerprint_frame(b"T", value.encode("utf-8"))
    if isinstance(value, (bytes, bytearray, memoryview)):
        return _fingerprint_frame(b"B", bytes(value))
    raise _MigrationError("database value has an unsupported SQLite type")


def _quote_sqlite_identifier(value: object) -> str:
    if not isinstance(value, str) or "\x00" in value:
        raise _MigrationError("database identifier is invalid")
    return '"' + value.replace('"', '""') + '"'


def _sqlite_value_size(expression: str) -> str:
    return (
        f"(CASE WHEN {expression} IS NULL THEN 0 "
        f"ELSE length(CAST({expression} AS BLOB)) END)"
    )


def _attach_cleanup_failures(
    primary: BaseException,
    failures: Sequence[BaseException],
) -> None:
    if not failures:
        return
    bounded = tuple(
        failure
        if isinstance(failure, _MigrationError)
        else _MigrationError(type(failure).__name__[:255])
        for failure in failures
    )
    try:
        existing = primary.__dict__.get("cleanup_failures", ())
        if not isinstance(existing, tuple):
            existing = ()
        primary.__dict__["cleanup_failures"] = existing + bounded
    except (AttributeError, TypeError):
        pass


def _close_preserving_primary(
    resource: object,
    primary: BaseException | None,
) -> None:
    try:
        resource.close()  # type: ignore[attr-defined]
    except _LEDGER_FAILURES as error:
        if primary is None:
            raise
        _attach_cleanup_failures(primary, [error])


def _close_public_connection(
    connection: sqlite3.Connection,
    primary: BaseException | None,
) -> None:
    try:
        connection.close()
    except _LEDGER_FAILURES as error:
        if primary is None:
            raise _ledger_unavailable(error) from error
        bounded = _MigrationError("connection close failed")
        bounded.__cause__ = error
        _attach_cleanup_failures(primary, [bounded])


def _rollback(
    connection: sqlite3.Connection,
    begun: bool,
) -> sqlite3.Error | None:
    if not begun:
        return None
    try:
        connection.execute("ROLLBACK")
    except sqlite3.Error as error:
        return error
    return None


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | os.O_DIRECTORY
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _validate_projection_policy(
    *,
    source: WorkflowState,
    target: WorkflowState,
    event_type: str,
    patch: Mapping[str, object],
) -> None:
    if source in _TERMINAL_STATES:
        if (
            source is target
            and source in _PRUNE_ELIGIBLE_STATES
            and event_type in _TERMINAL_AUDIT_EVENTS
            and not patch
        ):
            return
        raise _state_conflict("terminal workflow runs are immutable")
    if target in {WorkflowState.APPROVED, WorkflowState.REJECTED}:
        raise _state_conflict("approval transitions require record_decision")
    fields = frozenset(patch)
    if fields & _APPROVAL_PROJECTION_FIELDS:
        raise _state_conflict("approval fields require record_decision")
    if event_type in _PREPARE_PROJECTION_EVENT_NAMES:
        expected_fields = frozenset(
            field
            for field, event_types in _PREPARE_PROJECTION_EVENTS.items()
            if event_type in event_types
        )
        if source is not WorkflowState.PREPARING or fields != expected_fields:
            raise _state_conflict(
                "prepare evidence events require their authoritative projection"
            )
    elif event_type in _COMMIT_INTENT_EVENT_NAMES:
        if not (
            event_type == "commit_started"
            and source is WorkflowState.APPROVED
            and target is WorkflowState.COMMITTING
            and fields == _COMMIT_INTENT_FIELDS
        ):
            raise _state_conflict(
                "commit intent events require their authoritative transition"
            )
    elif event_type in _COMMIT_RESULT_EVENT_NAMES and not (
        source is WorkflowState.COMMITTING
        and target is WorkflowState.COMMITTED
        and fields == _COMMIT_RESULT_FIELDS
    ):
        raise _state_conflict(
            "commit result events require their authoritative transition"
        )

    allowed: set[str] = set()
    if source is WorkflowState.PREPARING:
        inspected_fields = fields & _SOURCE_INSPECTION_FIELDS
        if (
            event_type == "source_inspected"
            and inspected_fields
            and inspected_fields != _SOURCE_INSPECTION_FIELDS
        ):
            raise _state_conflict(
                "source_inspected requires complete source evidence"
            )
        for field, event_types in _PREPARE_PROJECTION_EVENTS.items():
            if event_type in event_types:
                allowed.add(field)
    if source is WorkflowState.APPROVED and target is WorkflowState.COMMITTING:
        if event_type != "commit_started" or fields != _COMMIT_INTENT_FIELDS:
            raise _state_conflict(
                "commit_started requires complete commit intent evidence"
            )
        for field, event_types in _COMMIT_INTENT_PROJECTION_EVENTS.items():
            if event_type in event_types:
                allowed.add(field)
    if source is WorkflowState.COMMITTING and target is WorkflowState.COMMITTED:
        if (
            event_type not in {"commit_completed", "committed"}
            or fields != _COMMIT_RESULT_FIELDS
        ):
            raise _state_conflict(
                "committed transitions require complete receipt evidence"
            )
        for field, event_types in _COMMIT_RESULT_PROJECTION_EVENTS.items():
            if event_type in event_types:
                allowed.add(field)
        required_non_null = _COMMIT_RESULT_FIELDS - {"backup_sha256"}
        if any(patch.get(field) is None for field in required_non_null):
            raise _state_conflict(
                "committed transitions require non-null receipt evidence"
            )
    supplied_error_fields = fields & _TERMINAL_ERROR_FIELDS
    if supplied_error_fields and supplied_error_fields != _TERMINAL_ERROR_FIELDS:
        raise _state_conflict("terminal error code and summary are an atomic pair")
    if target in _ERROR_ALLOWED_STATES:
        allowed.update(_TERMINAL_ERROR_FIELDS)
        if target in _ERROR_REQUIRED_STATES and (
            supplied_error_fields != _TERMINAL_ERROR_FIELDS
            or any(patch.get(field) is None for field in _TERMINAL_ERROR_FIELDS)
        ):
            raise _state_conflict(
                "failed and recovery-required states require a terminal error"
            )
    if fields - allowed:
        raise _state_conflict(
            "projection fields are not allowed for this workflow event"
        )


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

    def _open_root_anchor(self) -> _RootAnchor:
        root = self.paths.root
        if not root.is_absolute():
            raise _MigrationError("workflow state root is not absolute")
        flags = (
            os.O_RDONLY
            | os.O_CLOEXEC
            | os.O_DIRECTORY
            | os.O_NOFOLLOW
        )
        parts = root.parts
        if not parts:
            raise _MigrationError("workflow state root is empty")
        current = os.open(parts[0], flags)
        try:
            for component in parts[1:]:
                following = os.open(component, flags, dir_fd=current)
                os.close(current)
                current = following
            opened = os.fstat(current)
            current_path = root.lstat()
            if (
                not stat.S_ISDIR(opened.st_mode)
                or stat.S_IMODE(opened.st_mode) != _ROOT_MODE
                or _stat_identity(current_path) != _stat_identity(opened)
            ):
                raise _MigrationError("unsafe state root")
            return _RootAnchor(
                path=root,
                descriptor=current,
                identity=_stat_identity(opened),
            )
        except BaseException:
            os.close(current)
            raise

    def _validate_anchor_path(self, anchor: _RootAnchor) -> None:
        current = anchor.path.lstat()
        if (
            stat.S_ISLNK(current.st_mode)
            or _stat_identity(current) != anchor.identity
        ):
            raise _MigrationError("workflow state root changed during access")

    def _root_names(self, anchor: _RootAnchor) -> tuple[str, ...]:
        return tuple(os.listdir(anchor.descriptor))

    def _stat_at(
        self,
        anchor: _RootAnchor,
        name: str,
    ) -> os.stat_result:
        return os.stat(name, dir_fd=anchor.descriptor, follow_symlinks=False)

    def _open_at(
        self,
        anchor: _RootAnchor,
        name: str,
        *,
        write: bool,
        exclusive: bool = False,
    ) -> int:
        flags = os.O_RDWR if write else os.O_RDONLY
        flags |= os.O_CLOEXEC | os.O_NOFOLLOW
        if exclusive:
            flags |= os.O_CREAT | os.O_EXCL
        return os.open(
            name,
            flags,
            _DATABASE_MODE,
            dir_fd=anchor.descriptor,
        )

    def _unlink_at(self, anchor: _RootAnchor, name: str) -> None:
        os.unlink(name, dir_fd=anchor.descriptor)

    def _link_at(self, anchor: _RootAnchor, source: str, target: str) -> None:
        os.link(
            source,
            target,
            src_dir_fd=anchor.descriptor,
            dst_dir_fd=anchor.descriptor,
            follow_symlinks=False,
        )

    def _replace_at(self, anchor: _RootAnchor, source: str, target: str) -> None:
        os.replace(
            source,
            target,
            src_dir_fd=anchor.descriptor,
            dst_dir_fd=anchor.descriptor,
        )

    def _fsync_anchor(self, anchor: _RootAnchor) -> None:
        os.fsync(anchor.descriptor)

    def _validate_database_stat(self, result: os.stat_result) -> None:
        if (
            stat.S_ISLNK(result.st_mode)
            or not stat.S_ISREG(result.st_mode)
        ):
            raise _MigrationError("unsafe workflow database entry")
        if stat.S_IMODE(result.st_mode) != _DATABASE_MODE:
            raise _MigrationError("unsafe workflow database mode")

    @contextmanager
    def _bootstrap_cleanup_guard(self, anchor: _RootAnchor):
        with _BOOTSTRAP_THREAD_LOCK:
            descriptor: int | None = None
            primary: BaseException | None = None
            locked = False
            try:
                try:
                    descriptor = self._open_at(
                        anchor,
                        _BOOTSTRAP_LOCK_NAME,
                        write=True,
                        exclusive=True,
                    )
                except FileExistsError:
                    descriptor = self._open_at(
                        anchor,
                        _BOOTSTRAP_LOCK_NAME,
                        write=True,
                    )
                path_result = self._stat_at(anchor, _BOOTSTRAP_LOCK_NAME)
                opened_result = os.fstat(descriptor)
                self._validate_database_stat(path_result)
                self._validate_database_stat(opened_result)
                if _stat_identity(path_result) != _stat_identity(opened_result):
                    raise _MigrationError("bootstrap lock identity changed")
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX)
                locked = True
                yield
            except BaseException as error:
                primary = error
                raise
            finally:
                failures: list[BaseException] = []
                if descriptor is not None and locked:
                    try:
                        import fcntl

                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                    except (OSError, RuntimeError) as error:
                        failures.append(error)
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except OSError as error:
                        failures.append(error)
                if failures:
                    if primary is not None:
                        _attach_cleanup_failures(primary, failures)
                    else:
                        error = _MigrationError(
                            "bootstrap lock cleanup failed"
                        )
                        _attach_cleanup_failures(error, failures)
                        raise error

    def _cleanup_stale_bootstrap_entries(self, anchor: _RootAnchor) -> None:
        removed = False
        for name in self._root_names(anchor):
            if not name.startswith(_BOOTSTRAP_TEMP_PREFIX):
                continue
            try:
                candidate = self._stat_at(anchor, name)
            except FileNotFoundError:
                continue
            self._validate_database_stat(candidate)
            try:
                self._unlink_at(anchor, name)
            except FileNotFoundError:
                continue
            removed = True
        if removed:
            self._fsync_anchor(anchor)

    def _new_bootstrap_image(self) -> tuple[str, bytes]:
        token = secrets.token_hex(32)
        token_bytes = token.encode("ascii")
        image = (
            _BOOTSTRAP_IMAGE[:_BOOTSTRAP_TOKEN_OFFSET]
            + token_bytes
            + _BOOTSTRAP_IMAGE[
                _BOOTSTRAP_TOKEN_OFFSET + len(_BOOTSTRAP_PLACEHOLDER) :
            ]
        )
        return token, image

    def _write_bootstrap_image(self, descriptor: int, image: bytes) -> None:
        offset = 0
        while offset < len(image):
            written = os.write(descriptor, image[offset:])
            if written <= 0:
                raise OSError("bootstrap write did not make progress")
            offset += written

    def _publish_bootstrap(self, anchor: _RootAnchor) -> None:
        _, image = self._new_bootstrap_image()
        temporary = f"{_BOOTSTRAP_TEMP_PREFIX}{secrets.token_hex(8)}.tmp"
        descriptor = self._open_at(
            anchor,
            temporary,
            write=True,
            exclusive=True,
        )
        published = False
        primary: BaseException | None = None
        try:
            os.fchmod(descriptor, _DATABASE_MODE)
            self._write_bootstrap_image(descriptor, image)
            os.fsync(descriptor)
            self._link_at(anchor, temporary, _DATABASE_NAME)
            published = True
            self._unlink_at(anchor, temporary)
            self._fsync_anchor(anchor)
        except BaseException as error:
            primary = error
            cleanup_failures: list[BaseException] = []
            try:
                self._unlink_at(anchor, temporary)
            except FileNotFoundError:
                pass
            except _LEDGER_FAILURES as cleanup_error:
                cleanup_failures.append(cleanup_error)
            _attach_cleanup_failures(error, cleanup_failures)
            raise
        finally:
            try:
                os.close(descriptor)
            except OSError as cleanup_error:
                if primary is not None:
                    _attach_cleanup_failures(primary, [cleanup_error])
                else:
                    raise
        if not published:
            raise _MigrationError("bootstrap database was not published")

    def _read_descriptor(self, descriptor: int, size: int) -> bytes:
        position = os.lseek(descriptor, 0, os.SEEK_CUR)
        try:
            os.lseek(descriptor, 0, os.SEEK_SET)
            chunks: list[bytes] = []
            remaining = size
            while remaining:
                chunk = os.read(descriptor, remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            return b"".join(chunks)
        finally:
            os.lseek(descriptor, position, os.SEEK_SET)

    def _bootstrap_token_from_descriptor(
        self,
        descriptor: int,
        result: os.stat_result,
    ) -> str | None:
        if result.st_size == 0:
            raise _MigrationError("preexisting empty database is unsafe")
        header = self._read_descriptor(descriptor, min(result.st_size, 8192))
        if not header.startswith(b"SQLite format 3\x00"):
            raise _MigrationError("workflow database header is invalid")
        if result.st_size != len(_BOOTSTRAP_IMAGE):
            return None
        token_bytes = header[
            _BOOTSTRAP_TOKEN_OFFSET : _BOOTSTRAP_TOKEN_OFFSET
            + len(_BOOTSTRAP_PLACEHOLDER)
        ]
        if re.fullmatch(rb"[0-9a-f]{64}", token_bytes) is None:
            return None
        normalized = (
            header[:_BOOTSTRAP_TOKEN_OFFSET]
            + _BOOTSTRAP_PLACEHOLDER
            + header[_BOOTSTRAP_TOKEN_OFFSET + len(_BOOTSTRAP_PLACEHOLDER) :]
        )
        if normalized != _BOOTSTRAP_IMAGE:
            return None
        return token_bytes.decode("ascii")

    def _secure_database_entry(
        self,
        *,
        create: bool,
        write: bool,
    ) -> _DatabaseLease:
        self._database_path()
        anchor = self._open_root_anchor()
        try:
            guard = (
                self._bootstrap_cleanup_guard(anchor)
                if create or write
                else nullcontext()
            )
            with guard:
                if create or write:
                    self._cleanup_stale_bootstrap_entries(anchor)
                for attempt in range(_MAX_CREATE_RACE_RETRIES):
                    try:
                        result = self._stat_at(anchor, _DATABASE_NAME)
                    except FileNotFoundError:
                        if not create:
                            raise FileNotFoundError("workflow database is missing")
                        try:
                            self._publish_bootstrap(anchor)
                        except FileExistsError:
                            if attempt + 1 == _MAX_CREATE_RACE_RETRIES:
                                raise
                            continue
                        result = self._stat_at(anchor, _DATABASE_NAME)
                    break
                else:
                    raise _MigrationError("database creation race did not settle")
            self._validate_database_stat(result)
            descriptor = self._open_at(
                anchor,
                _DATABASE_NAME,
                write=write,
            )
            try:
                opened = os.fstat(descriptor)
                self._validate_database_stat(result)
                self._validate_database_stat(opened)
                if _stat_identity(opened) != _stat_identity(result):
                    raise _MigrationError(
                        "database entry changed while it was opened"
                    )
                token = self._bootstrap_token_from_descriptor(
                    descriptor,
                    opened,
                )
                self._validate_anchor_path(anchor)
                return _DatabaseLease(
                    anchor=anchor,
                    name=_DATABASE_NAME,
                    descriptor=descriptor,
                    identity=_stat_identity(opened),
                    bootstrap_token=token,
                )
            except BaseException as error:
                try:
                    os.close(descriptor)
                except OSError as cleanup_error:
                    _attach_cleanup_failures(error, [cleanup_error])
                raise
        except BaseException as error:
            _close_preserving_primary(anchor, error)
            raise

    def _validate_internal_database_entry(self, path: Path) -> os.stat_result:
        result = path.lstat()
        self._validate_database_stat(result)
        return result

    def _configure_write_connection(self, connection: sqlite3.Connection) -> None:
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

    def _configure_read_connection(self, connection: sqlite3.Connection) -> None:
        connection.row_factory = sqlite3.Row
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        if journal_mode != "delete":
            raise _MigrationError("workflow database journal mode is not delete")
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        observed = (
            connection.execute("PRAGMA query_only").fetchone()[0],
            connection.execute("PRAGMA journal_mode").fetchone()[0],
            connection.execute("PRAGMA synchronous").fetchone()[0],
            connection.execute("PRAGMA foreign_keys").fetchone()[0],
            connection.execute("PRAGMA busy_timeout").fetchone()[0],
        )
        if observed != (1, "delete", 2, 1, _BUSY_TIMEOUT_MS):
            raise _MigrationError("read-only SQLite contract was not applied")

    def _validate_connection_identity(
        self,
        lease: _DatabaseLease,
        opened_path: Path | None,
    ) -> None:
        opened_descriptor = os.fstat(lease.descriptor)
        current = self._stat_at(lease.anchor, lease.name)
        self._validate_database_stat(opened_descriptor)
        self._validate_database_stat(current)
        if (
            _stat_identity(opened_descriptor) != lease.identity
            or _stat_identity(current) != lease.identity
        ):
            raise _MigrationError("database entry changed while it was opened")
        self._validate_anchor_path(lease.anchor)
        if opened_path is not None:
            opened = self._validate_internal_database_entry(opened_path)
            if _stat_identity(opened) != lease.identity:
                raise _MigrationError("SQLite opened a different database entry")

    def _verify_bootstrap_connection(
        self,
        connection: sqlite3.Connection,
        expected_token: str,
    ) -> bool:
        try:
            objects = tuple(
                connection.execute(
                    "SELECT type, name, tbl_name, sql FROM sqlite_master "
                    "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
                ).fetchmany(3)
            )
            tokens = tuple(
                connection.execute(
                    f"SELECT token FROM {_BOOTSTRAP_TABLE}"
                ).fetchmany(2)
            )
        except sqlite3.Error:
            migrated = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                "AND name = 'schema_migrations'"
            ).fetchone()
            if migrated is not None:
                return False
            raise
        expected_object = (
            "table",
            _BOOTSTRAP_TABLE,
            _BOOTSTRAP_TABLE,
            _BOOTSTRAP_SCHEMA_SQL,
        )
        observed_objects = tuple(
            (row["type"], row["name"], row["tbl_name"], row["sql"])
            for row in objects
        )
        if (
            observed_objects != (expected_object,)
            or len(tokens) != 1
            or not isinstance(tokens[0]["token"], str)
            or not secrets.compare_digest(tokens[0]["token"], expected_token)
        ):
            raise _MigrationError("bootstrap database identity mismatch")
        return True

    def _probe_write_identity(self, connection: sqlite3.Connection) -> None:
        probe_name = f"__fcp_identity_probe_{secrets.token_hex(8)}"
        begun = False
        try:
            connection.execute("BEGIN IMMEDIATE")
            begun = True
            connection.execute(f'CREATE TABLE main."{probe_name}"(value INTEGER)')
            connection.execute("ROLLBACK")
            begun = False
        except _LEDGER_FAILURES as error:
            rollback_error = _rollback(connection, begun)
            begun = False
            if rollback_error is not None:
                _attach_cleanup_failures(error, [rollback_error])
            raise
        finally:
            if begun:
                rollback_error = _rollback(connection, begun)
                if rollback_error is not None:
                    raise rollback_error

    def _connect_lease(
        self,
        lease: _DatabaseLease,
        *,
        read_only: bool,
    ) -> sqlite3.Connection:
        connection: sqlite3.Connection | None = None
        try:
            if read_only:
                descriptor_path = (
                    Path(f"/proc/self/fd/{lease.descriptor}")
                    if Path("/proc/self/fd").is_dir()
                    else Path(f"/dev/fd/{lease.descriptor}")
                )
                database_uri = f"{descriptor_path.as_uri()}?mode=ro"
            else:
                mode = "ro" if read_only else "rw"
                database_uri = f"{(lease.anchor.path / lease.name).as_uri()}?mode={mode}"
            connection = sqlite3.connect(
                database_uri,
                timeout=_BUSY_TIMEOUT_MS / 1000,
                isolation_level=None,
                uri=True,
                factory=_LedgerConnection,
            )
            connection.row_factory = sqlite3.Row
            database_row = connection.execute("PRAGMA database_list").fetchone()
            if database_row is None or not database_row["file"]:
                raise _MigrationError("SQLite did not identify the opened database")
            opened_path = None if read_only else Path(database_row["file"])
            self._validate_connection_identity(lease, opened_path)
            bootstrap = False
            if lease.bootstrap_token is not None:
                bootstrap = self._verify_bootstrap_connection(
                    connection,
                    lease.bootstrap_token,
                )
            elif (
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE name = ?",
                    (_BOOTSTRAP_TABLE,),
                ).fetchone()
                is not None
            ):
                raise _MigrationError("unverified bootstrap database")
            if read_only:
                self._configure_read_connection(connection)
            else:
                if not bootstrap:
                    self._probe_write_identity(connection)
                    self._validate_connection_identity(lease, opened_path)
                self._configure_write_connection(connection)
            return connection
        except BaseException as error:
            if connection is not None:
                _close_preserving_primary(connection, error)
            raise

    def _connect(self) -> sqlite3.Connection:
        lease: _DatabaseLease | None = None
        try:
            lease = self._secure_database_entry(create=True, write=True)
            connection = self._connect_lease(lease, read_only=False)
            if not isinstance(connection, _LedgerConnection):
                raise _MigrationError("SQLite connection factory was bypassed")
            connection.retain_lease(lease, read_only=False)
            lease = None
            return connection
        except FCPMCPError:
            raise
        except _LEDGER_FAILURES as error:
            raise _ledger_unavailable(error)
        finally:
            if lease is not None:
                primary = sys.exc_info()[1]
                try:
                    _close_preserving_primary(lease, primary)
                except _LEDGER_FAILURES as error:
                    raise _ledger_unavailable(error)

    def _connect_existing(self) -> sqlite3.Connection:
        lease: _DatabaseLease | None = None
        try:
            lease = self._secure_database_entry(create=False, write=False)
            connection = self._connect_lease(lease, read_only=True)
            if not isinstance(connection, _LedgerConnection):
                raise _MigrationError("SQLite connection factory was bypassed")
            connection.retain_lease(lease, read_only=True)
            lease = None
            return connection
        except FCPMCPError:
            raise
        except _LEDGER_FAILURES as error:
            raise _ledger_unavailable(error)
        finally:
            if lease is not None:
                primary = sys.exc_info()[1]
                try:
                    _close_preserving_primary(lease, primary)
                except _LEDGER_FAILURES as error:
                    raise _ledger_unavailable(error)

    def _connect_write_existing(self) -> sqlite3.Connection:
        lease: _DatabaseLease | None = None
        try:
            lease = self._secure_database_entry(create=False, write=True)
            connection = self._connect_lease(lease, read_only=False)
            if not isinstance(connection, _LedgerConnection):
                raise _MigrationError("SQLite connection factory was bypassed")
            connection.retain_lease(lease, read_only=False)
            lease = None
            return connection
        except FCPMCPError:
            raise
        except _LEDGER_FAILURES as error:
            raise _ledger_unavailable(error)
        finally:
            if lease is not None:
                primary = sys.exc_info()[1]
                try:
                    _close_preserving_primary(lease, primary)
                except _LEDGER_FAILURES as error:
                    raise _ledger_unavailable(error)

    def _retained_lease(
        self,
        connection: sqlite3.Connection,
    ) -> _DatabaseLease:
        if (
            not isinstance(connection, _LedgerConnection)
            or connection._ledger_lease is None
        ):
            raise _MigrationError(
                "workflow connection lost its filesystem authority"
            )
        return connection._ledger_lease

    def _validate_retained_connection(
        self,
        connection: sqlite3.Connection,
    ) -> None:
        lease = self._retained_lease(connection)
        opened_path = (
            None
            if isinstance(connection, _LedgerConnection)
            and connection._ledger_read_only
            else lease.anchor.path / lease.name
        )
        self._validate_connection_identity(lease, opened_path)

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
                "WHERE name NOT LIKE 'sqlite_%' AND name != ? LIMIT 1",
                (_BOOTSTRAP_TABLE,),
            ).fetchone()
            is not None
        )

    def _backup_schema_identity(
        self,
        connection: sqlite3.Connection,
    ) -> str:
        bounds = connection.execute(
            "SELECT count(*), "
            "coalesce(sum("
            "length(CAST(type AS BLOB)) + length(CAST(name AS BLOB)) + "
            "length(CAST(tbl_name AS BLOB)) + "
            "coalesce(length(CAST(sql AS BLOB)), 0)"
            "), 0), "
            "coalesce(max(max("
            "length(CAST(type AS BLOB)), length(CAST(name AS BLOB)), "
            "length(CAST(tbl_name AS BLOB)), "
            "coalesce(length(CAST(sql AS BLOB)), 0)"
            ")), 0) "
            "FROM sqlite_master"
        ).fetchone()
        if (
            bounds is None
            or type(bounds[0]) is not int
            or type(bounds[1]) is not int
            or type(bounds[2]) is not int
            or bounds[0] > _MAX_FINGERPRINT_SCHEMA_OBJECTS
            or bounds[1] > _MAX_FINGERPRINT_SCHEMA_BYTES
            or bounds[2] > _MAX_FINGERPRINT_METADATA_BYTES
        ):
            raise _MigrationError("database schema exceeds fingerprint limits")
        rows: list[tuple[object, ...]] = []
        cursor = connection.execute(
            "SELECT type, name, tbl_name, rootpage, sql FROM sqlite_master"
        )
        while True:
            row = cursor.fetchone()
            if row is None:
                break
            rows.append(tuple(row))
        if len(rows) != bounds[0]:
            raise _MigrationError("database schema changed during fingerprint")
        rows.sort(key=lambda row: (str(row[0]).encode(), str(row[1]).encode()))
        digest = hashlib.sha256()
        for row in rows:
            digest.update(_fingerprint_frame(b"S", b""))
            for value in row:
                digest.update(_fingerprint_value(value))
        return digest.hexdigest()

    def _backup_migration_identity(
        self,
        connection: sqlite3.Connection,
    ) -> tuple[tuple[object, ...], ...]:
        return tuple(tuple(row) for row in self._migration_rows(connection))

    def _backup_content_sha256(
        self,
        connection: sqlite3.Connection,
    ) -> str:
        page_count = connection.execute("PRAGMA page_count").fetchone()
        page_size = connection.execute("PRAGMA page_size").fetchone()
        if (
            page_count is None
            or page_size is None
            or type(page_count[0]) is not int
            or type(page_size[0]) is not int
            or page_count[0] < 0
            or page_size[0] <= 0
            or page_count[0] * page_size[0] > _MAX_FINGERPRINT_DATABASE_BYTES
        ):
            raise _MigrationError("database exceeds fingerprint file limit")
        digest = hashlib.sha256()
        digest.update(_fingerprint_frame(b"V", b"fcp-backup-fingerprint-v3"))
        for pragma in _BACKUP_PERSISTENT_PRAGMAS:
            row = connection.execute(f"PRAGMA {pragma}").fetchone()
            if row is None or len(row) != 1:
                raise _MigrationError("persistent database pragma is unavailable")
            if pragma == "encoding" and row[0] != "UTF-8":
                raise _MigrationError("database text encoding is unsupported")
            digest.update(_fingerprint_frame(b"P", pragma.encode("ascii")))
            digest.update(_fingerprint_value(row[0]))

        table_bounds = connection.execute(
            "SELECT count(*), coalesce(max(length(CAST(name AS BLOB))), 0) "
            "FROM sqlite_master WHERE type = 'table'"
        ).fetchone()
        if (
            table_bounds is None
            or type(table_bounds[0]) is not int
            or type(table_bounds[1]) is not int
            or table_bounds[0] > _MAX_FINGERPRINT_TABLES
            or table_bounds[1] > _MAX_FINGERPRINT_METADATA_BYTES
        ):
            raise _MigrationError("database table inventory is unsupported")
        table_names: list[str] = []
        table_cursor = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
        while True:
            table_row = table_cursor.fetchone()
            if table_row is None:
                break
            if not isinstance(table_row[0], str):
                raise _MigrationError("database table name is invalid")
            table_names.append(table_row[0])
        if len(table_names) != table_bounds[0]:
            raise _MigrationError("database table inventory changed")
        table_names.sort(key=lambda value: value.encode("utf-8"))

        table_count = 0
        total_value_bytes = 0
        for table_name in table_names:
            quoted_table = _quote_sqlite_identifier(table_name)
            digest.update(
                _fingerprint_frame(
                    b"T",
                    table_name.encode("utf-8"),
                )
            )
            columns: list[sqlite3.Row] = []
            column_cursor = connection.execute(
                "SELECT cid, name, type, \"notnull\", dflt_value, "
                "pk, hidden FROM pragma_table_xinfo(?)",
                (table_name,),
            )
            while len(columns) <= _MAX_FINGERPRINT_COLUMNS:
                column = column_cursor.fetchone()
                if column is None:
                    break
                columns.append(column)
            if not columns or len(columns) > _MAX_FINGERPRINT_COLUMNS:
                raise _MigrationError(
                    "database table column count is unsupported"
                )
            columns.sort(key=lambda column: int(column["cid"]))
            column_names: list[str] = []
            primary_key_columns: list[tuple[int, str]] = []
            for column in columns:
                column_name = column["name"]
                quoted_column = _quote_sqlite_identifier(column_name)
                column_names.append(quoted_column)
                digest.update(_fingerprint_frame(b"C", b""))
                for value in column:
                    if isinstance(value, (str, bytes)) and (
                        len(value) > _MAX_FINGERPRINT_METADATA_BYTES
                    ):
                        raise _MigrationError(
                            "database column metadata is unsupported"
                        )
                    digest.update(_fingerprint_value(value))
                if type(column["pk"]) is int and column["pk"] > 0:
                    primary_key_columns.append(
                        (column["pk"], quoted_column)
                    )

            table_list_rows = tuple(
                connection.execute(
                    "SELECT type, wr, strict FROM pragma_table_list "
                    "WHERE schema = 'main' AND name = ?",
                    (table_name,),
                ).fetchmany(2)
            )
            if len(table_list_rows) != 1 or table_list_rows[0]["type"] != "table":
                raise _MigrationError("database table shape is unsupported")
            table_list_row = table_list_rows[0]
            without_rowid = table_list_row["wr"] == 1
            digest.update(_fingerprint_frame(b"M", b""))
            for value in table_list_row:
                digest.update(_fingerprint_value(value))

            value_sizes = [
                _sqlite_value_size(column)
                for column in column_names
            ]
            row_size = " + ".join(value_sizes)
            row_count_bound = connection.execute(
                f"SELECT count(*) FROM (SELECT 1 FROM {quoted_table} LIMIT ?)",
                (_MAX_FINGERPRINT_TABLE_ROWS + 1,),
            ).fetchone()
            if (
                row_count_bound is None
                or type(row_count_bound[0]) is not int
                or row_count_bound[0] > _MAX_FINGERPRINT_TABLE_ROWS
            ):
                raise _MigrationError("database table exceeds fingerprint row limit")
            aggregate_bounds = connection.execute(
                f"SELECT coalesce(sum({row_size}), 0), "
                f"coalesce(max({row_size}), 0), "
                + ", ".join(
                    f"coalesce(max({size}), 0)"
                    for size in value_sizes
                )
                + f" FROM {quoted_table}"
            ).fetchone()
            bounds = (
                (row_count_bound[0], *tuple(aggregate_bounds))
                if aggregate_bounds is not None
                else ()
            )
            if (
                not bounds
                or any(type(value) is not int for value in bounds)
                or bounds[1] > _MAX_FINGERPRINT_TABLE_BYTES
                or bounds[2] > _MAX_FINGERPRINT_ROW_BYTES
                or any(
                    value > _MAX_FINGERPRINT_CELL_BYTES
                    for value in bounds[3:]
                )
            ):
                raise _MigrationError("database table exceeds fingerprint limits")
            total_value_bytes += bounds[1]
            if total_value_bytes > _MAX_FINGERPRINT_TOTAL_BYTES:
                raise _MigrationError("database exceeds fingerprint byte limit")

            if without_rowid:
                identity_columns, order_clause = (
                    self._without_rowid_fingerprint_order(
                        connection,
                        table_name,
                        quoted_table,
                        primary_key_columns,
                    )
                )
                for _, quoted_column in sorted(primary_key_columns):
                    column_position = column_names.index(quoted_column)
                    if (
                        bounds[3 + column_position]
                        > _MAX_FINGERPRINT_PRIMARY_KEY_BYTES
                    ):
                        raise _MigrationError(
                            "database primary key exceeds fingerprint limit"
                        )
                identity_select = ", ".join(identity_columns)
            else:
                observed_names = {
                    str(column["name"]).casefold()
                    for column in columns
                }
                rowid_name = next(
                    (
                        candidate
                        for candidate in ("_rowid_", "rowid", "oid")
                        if candidate.casefold() not in observed_names
                    ),
                    None,
                )
                if rowid_name is None:
                    raise _MigrationError(
                        "rowid table hides its row identity"
                    )
                quoted_rowid = _quote_sqlite_identifier(rowid_name)
                identity_columns = [quoted_rowid]
                identity_select = quoted_rowid
                order_clause = quoted_rowid

            identity_sql = (
                f"SELECT {identity_select} FROM {quoted_table} "
                f"ORDER BY {order_clause}"
            )
            self._reject_fingerprint_temp_sort(connection, identity_sql)
            identity_cursor = connection.execute(identity_sql)
            row_count = 0
            while True:
                identity = identity_cursor.fetchone()
                if identity is None:
                    break
                identity_values = tuple(identity)
                if without_rowid:
                    if len(identity_values) != len(identity_columns):
                        raise _MigrationError(
                            "database fingerprint identity is invalid"
                        )
                    where_clause = " AND ".join(
                        f"{column} = ?"
                        for column in identity_columns
                    )
                    where_parameters = identity_values
                else:
                    if len(identity_values) != 1 or type(identity_values[0]) is not int:
                        raise _MigrationError(
                            "database fingerprint rowid is invalid"
                        )
                    where_clause = f"{identity_columns[0]} = ?"
                    where_parameters = identity_values
                digest.update(_fingerprint_frame(b"W", b""))
                for identity_column in identity_columns:
                    self._stream_fingerprint_cell(
                        connection,
                        digest,
                        table=quoted_table,
                        column=identity_column,
                        where_clause=where_clause,
                        where_parameters=where_parameters,
                        frame_tag=b"K",
                    )
                for column in column_names:
                    self._stream_fingerprint_cell(
                        connection,
                        digest,
                        table=quoted_table,
                        column=column,
                        where_clause=where_clause,
                        where_parameters=where_parameters,
                        frame_tag=b"D",
                    )
                row_count += 1
            if row_count != bounds[0]:
                raise _MigrationError("database row count changed during fingerprint")
            digest.update(
                _fingerprint_frame(
                    b"N",
                    row_count.to_bytes(8, "big"),
                )
            )
            table_count += 1
        digest.update(
            _fingerprint_frame(
                b"Z",
                table_count.to_bytes(8, "big"),
            )
        )
        return digest.hexdigest()

    def _without_rowid_fingerprint_order(
        self,
        connection: sqlite3.Connection,
        table_name: str,
        quoted_table: str,
        primary_key_columns: Sequence[tuple[int, str]],
    ) -> tuple[list[str], str]:
        if not primary_key_columns:
            raise _MigrationError("WITHOUT ROWID table lacks a primary key")
        indexes = tuple(
            connection.execute(
                "SELECT name FROM pragma_index_list(?) WHERE origin = 'pk'",
                (table_name,),
            ).fetchmany(2)
        )
        if len(indexes) != 1 or not isinstance(indexes[0]["name"], str):
            raise _MigrationError("WITHOUT ROWID primary key is unsupported")
        index_rows = list(
            connection.execute(
                "SELECT seqno, cid, name, desc, coll, key "
                "FROM pragma_index_xinfo(?)",
                (indexes[0]["name"],),
            )
        )
        key_rows = sorted(
            (row for row in index_rows if row["key"] == 1),
            key=lambda row: int(row["seqno"]),
        )
        expected = [
            quoted_column
            for _, quoted_column in sorted(primary_key_columns)
        ]
        observed = [
            _quote_sqlite_identifier(row["name"])
            for row in key_rows
        ]
        if observed != expected:
            raise _MigrationError("WITHOUT ROWID primary key is unsupported")
        order_terms: list[str] = []
        for row, column in zip(key_rows, observed):
            collation = row["coll"]
            if collation not in {"BINARY", "NOCASE", "RTRIM"}:
                raise _MigrationError(
                    "WITHOUT ROWID collation is unsupported"
                )
            direction = " DESC" if row["desc"] == 1 else ""
            order_terms.append(
                f"{column} COLLATE {_quote_sqlite_identifier(collation)}"
                f"{direction}"
            )
        order_clause = ", ".join(order_terms)
        identity_sql = (
            f"SELECT {', '.join(observed)} FROM {quoted_table} "
            f"ORDER BY {order_clause}"
        )
        self._reject_fingerprint_temp_sort(connection, identity_sql)
        return observed, order_clause

    def _reject_fingerprint_temp_sort(
        self,
        connection: sqlite3.Connection,
        sql: str,
    ) -> None:
        plan = connection.execute(f"EXPLAIN QUERY PLAN {sql}").fetchall()
        if any(
            "USE TEMP B-TREE" in str(row["detail"]).upper()
            for row in plan
        ):
            raise _MigrationError("database ordering requires temporary storage")

    def _stream_fingerprint_cell(
        self,
        connection: sqlite3.Connection,
        digest: Any,
        *,
        table: str,
        column: str,
        where_clause: str,
        where_parameters: Sequence[object],
        frame_tag: bytes,
    ) -> None:
        metadata = connection.execute(
            f"SELECT typeof({column}), {_sqlite_value_size(column)} "
            f"FROM {table} WHERE {where_clause}",
            tuple(where_parameters),
        ).fetchone()
        if (
            metadata is None
            or not isinstance(metadata[0], str)
            or type(metadata[1]) is not int
            or not 0 <= metadata[1] <= _MAX_FINGERPRINT_CELL_BYTES
        ):
            raise _MigrationError("database cell exceeds fingerprint limit")
        storage_type = metadata[0]
        size = metadata[1]
        tags = {
            "null": b"N",
            "integer": b"I",
            "real": b"R",
            "text": b"T",
            "blob": b"B",
        }
        value_tag = tags.get(storage_type)
        if value_tag is None:
            raise _MigrationError("database cell type is unsupported")
        if storage_type in {"null", "integer", "real"}:
            row = connection.execute(
                f"SELECT {column} FROM {table} WHERE {where_clause}",
                tuple(where_parameters),
            ).fetchone()
            if row is None:
                raise _MigrationError("database row changed during fingerprint")
            encoded = _fingerprint_value(row[0])
            digest.update(_fingerprint_frame(frame_tag, encoded))
            return

        encoded_size = 1 + 8 + size
        digest.update(_fingerprint_frame_header(frame_tag, encoded_size))
        digest.update(_fingerprint_frame_header(value_tag, size))
        offset = 1
        remaining = size
        while remaining:
            requested = min(remaining, _FINGERPRINT_CHUNK_BYTES)
            row = connection.execute(
                f"SELECT substr(CAST({column} AS BLOB), ?, ?) "
                f"FROM {table} WHERE {where_clause}",
                (offset, requested, *where_parameters),
            ).fetchone()
            if (
                row is None
                or not isinstance(row[0], bytes)
                or len(row[0]) != requested
            ):
                raise _MigrationError("database cell changed during fingerprint")
            digest.update(row[0])
            offset += requested
            remaining -= requested

    def _validate_backup_content(
        self,
        connection: sqlite3.Connection,
        *,
        source_schema: str,
        source_migrations: tuple[tuple[object, ...], ...],
        source_content_sha256: str,
    ) -> None:
        integrity_rows = tuple(
            row[0]
            for row in connection.execute("PRAGMA integrity_check").fetchmany(2)
        )
        if integrity_rows != ("ok",):
            raise _MigrationError("backup database integrity check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise _MigrationError("backup database foreign key check failed")
        if self._backup_schema_identity(connection) != source_schema:
            raise _MigrationError("backup database schema identity changed")
        if self._backup_migration_identity(connection) != source_migrations:
            raise _MigrationError("backup database migration identity changed")
        if self._backup_content_sha256(connection) != source_content_sha256:
            raise _MigrationError("backup database content differs from source")

    def _drop_verified_bootstrap(
        self,
        connection: sqlite3.Connection,
    ) -> None:
        row = connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE name = ?",
            (_BOOTSTRAP_TABLE,),
        ).fetchone()
        if row is None:
            return
        observed = (row["type"], row["name"], row["tbl_name"], row["sql"])
        expected = (
            "table",
            _BOOTSTRAP_TABLE,
            _BOOTSTRAP_TABLE,
            _BOOTSTRAP_SCHEMA_SQL,
        )
        tokens = tuple(
            connection.execute(
                f"SELECT token FROM {_BOOTSTRAP_TABLE}"
            ).fetchmany(2)
        )
        if (
            observed != expected
            or len(tokens) != 1
            or not isinstance(tokens[0]["token"], str)
            or _SHA256_RE.fullmatch(tokens[0]["token"]) is None
        ):
            raise _MigrationError("unverified bootstrap database")
        connection.execute(f"DROP TABLE {_BOOTSTRAP_TABLE}")

    def _backup_database(
        self,
        migration_connection: sqlite3.Connection,
        *,
        from_version: int,
        to_version: int,
    ) -> Path:
        migration_lease = self._retained_lease(migration_connection)
        self._validate_retained_connection(migration_connection)
        published: Path | None = None
        try:
            with self._backup_publication_guard(migration_lease.anchor):
                published = self._backup_database_guarded(
                    migration_connection,
                    migration_lease=migration_lease,
                    from_version=from_version,
                    to_version=to_version,
                )
            return published
        except BaseException as error:
            if published is not None:
                try:
                    self._remove_published_backup(
                        published,
                        anchor=migration_lease.anchor,
                    )
                except _LEDGER_FAILURES as cleanup_error:
                    _attach_cleanup_failures(error, [cleanup_error])
            raise

    def _backup_database_guarded(
        self,
        migration_connection: sqlite3.Connection,
        *,
        migration_lease: _DatabaseLease,
        from_version: int,
        to_version: int,
    ) -> Path:
        timestamp = _canonical_clock_timestamp(self._clock()).replace(":", "").replace("-", "")
        token = secrets.token_hex(8)
        stem = (
            f"{_DATABASE_NAME}.backup-v{from_version}-to-v{to_version}-"
            f"{timestamp}-{token}"
        )
        temporary_name = f".{stem}.tmp"
        anchor = migration_lease.anchor
        source_descriptor: int | None = None
        destination_descriptor: int | None = None
        source: sqlite3.Connection | None = None
        destination: sqlite3.Connection | None = None
        validation: sqlite3.Connection | None = None
        replaced = False
        primary: BaseException | None = None
        try:
            destination_descriptor = self._open_at(
                anchor,
                temporary_name,
                write=True,
                exclusive=True,
            )
            os.fchmod(destination_descriptor, _DATABASE_MODE)
            destination_token, bootstrap_image = self._new_bootstrap_image()
            self._write_bootstrap_image(
                destination_descriptor,
                bootstrap_image,
            )
            os.fsync(destination_descriptor)
            destination_result = os.fstat(destination_descriptor)
            self._validate_database_stat(destination_result)
            destination_lease = _DatabaseLease(
                anchor=anchor,
                name=temporary_name,
                descriptor=destination_descriptor,
                identity=_stat_identity(destination_result),
                bootstrap_token=destination_token,
                owns_anchor=False,
            )
            source_result = self._stat_at(anchor, _DATABASE_NAME)
            self._validate_database_stat(source_result)
            if _stat_identity(source_result) != migration_lease.identity:
                raise _MigrationError("backup source is not the migration database")
            source_descriptor = self._open_at(
                anchor,
                _DATABASE_NAME,
                write=False,
            )
            opened_source = os.fstat(source_descriptor)
            if _stat_identity(opened_source) != _stat_identity(source_result):
                raise _MigrationError("backup source changed while it was opened")
            source_lease = _DatabaseLease(
                anchor=anchor,
                name=_DATABASE_NAME,
                descriptor=source_descriptor,
                identity=_stat_identity(opened_source),
                bootstrap_token=self._bootstrap_token_from_descriptor(
                    source_descriptor,
                    opened_source,
                ),
                owns_anchor=False,
            )
            source = self._connect_lease(source_lease, read_only=True)
            destination = self._connect_lease(destination_lease, read_only=False)
            source_schema = self._backup_schema_identity(source)
            source_migrations = self._backup_migration_identity(source)
            source_content_sha256 = self._backup_content_sha256(source)
            self._validate_connection_identity(source_lease, None)
            self._validate_connection_identity(
                destination_lease,
                anchor.path / temporary_name,
            )
            source.backup(destination)
            self._validate_connection_identity(
                destination_lease,
                anchor.path / temporary_name,
            )
            destination.close()
            destination = None
            source.close()
            source = None
            os.fsync(destination_descriptor)
            current_destination = self._stat_at(anchor, temporary_name)
            self._validate_database_stat(current_destination)
            if _stat_identity(current_destination) != _stat_identity(
                destination_result
            ):
                raise _MigrationError("backup destination changed before publish")
            self._bootstrap_token_from_descriptor(
                destination_descriptor,
                current_destination,
            )
            validation_lease = _DatabaseLease(
                anchor=anchor,
                name=temporary_name,
                descriptor=destination_descriptor,
                identity=_stat_identity(current_destination),
                bootstrap_token=None,
                owns_anchor=False,
            )
            validation = self._connect_lease(
                validation_lease,
                read_only=True,
            )
            self._validate_backup_content(
                validation,
                source_schema=source_schema,
                source_migrations=source_migrations,
                source_content_sha256=source_content_sha256,
            )
            self._validate_connection_identity(
                validation_lease,
                None,
            )
            self._replace_at(anchor, temporary_name, stem)
            replaced = True
            published_result = self._stat_at(anchor, stem)
            self._validate_database_stat(published_result)
            if _stat_identity(published_result) != _stat_identity(destination_result):
                raise _MigrationError("published backup identity changed")
            published_lease = _DatabaseLease(
                anchor=anchor,
                name=stem,
                descriptor=destination_descriptor,
                identity=_stat_identity(published_result),
                bootstrap_token=None,
                owns_anchor=False,
            )
            self._validate_connection_identity(
                published_lease,
                None,
            )
            self._validate_backup_content(
                validation,
                source_schema=source_schema,
                source_migrations=source_migrations,
                source_content_sha256=source_content_sha256,
            )
            self._validate_connection_identity(
                published_lease,
                None,
            )
            validation.close()
            validation = None
            os.fsync(destination_descriptor)
            self._fsync_anchor(anchor)
            self._validate_retained_connection(migration_connection)
            return anchor.path / stem
        except BaseException as error:
            primary = error
            cleanup_failures: list[BaseException] = []
            for opened_connection in (validation, destination, source):
                if opened_connection is None:
                    continue
                try:
                    opened_connection.close()
                except _LEDGER_FAILURES as cleanup_error:
                    cleanup_failures.append(cleanup_error)
            for candidate in (temporary_name, stem if replaced else None):
                if candidate is None:
                    continue
                try:
                    self._unlink_at(anchor, candidate)
                except FileNotFoundError:
                    pass
                except _LEDGER_FAILURES as cleanup_error:
                    cleanup_failures.append(cleanup_error)
            try:
                self._fsync_anchor(anchor)
            except _LEDGER_FAILURES as cleanup_error:
                cleanup_failures.append(cleanup_error)
            _attach_cleanup_failures(error, cleanup_failures)
            raise
        finally:
            close_failures: list[BaseException] = []
            for descriptor in (destination_descriptor, source_descriptor):
                if descriptor is None:
                    continue
                try:
                    os.close(descriptor)
                except OSError as cleanup_error:
                    close_failures.append(cleanup_error)
            if close_failures:
                if primary is not None:
                    _attach_cleanup_failures(primary, close_failures)
                else:
                    error = close_failures[0]
                    _attach_cleanup_failures(error, close_failures[1:])
                    raise error

    @contextmanager
    def _backup_publication_guard(self, anchor: _RootAnchor):
        with _BACKUP_THREAD_LOCK:
            descriptor: int | None = None
            primary: BaseException | None = None
            locked = False
            try:
                try:
                    descriptor = self._open_at(
                        anchor,
                        _BACKUP_LOCK_NAME,
                        write=True,
                        exclusive=True,
                    )
                except FileExistsError:
                    descriptor = self._open_at(
                        anchor,
                        _BACKUP_LOCK_NAME,
                        write=True,
                    )
                path_result = self._stat_at(anchor, _BACKUP_LOCK_NAME)
                opened_result = os.fstat(descriptor)
                self._validate_database_stat(path_result)
                self._validate_database_stat(opened_result)
                if _stat_identity(path_result) != _stat_identity(opened_result):
                    raise _MigrationError("backup lock identity changed")
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX)
                locked = True
                yield
            except BaseException as error:
                primary = error
                raise
            finally:
                failures: list[BaseException] = []
                if descriptor is not None and locked:
                    try:
                        import fcntl

                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                    except (OSError, RuntimeError) as error:
                        failures.append(error)
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except OSError as error:
                        failures.append(error)
                if failures:
                    if primary is not None:
                        _attach_cleanup_failures(primary, failures)
                    else:
                        error = _MigrationError(
                            "backup lock cleanup failed"
                        )
                        _attach_cleanup_failures(error, failures)
                        raise error

    def _execute_migration_statement(
        self,
        connection: sqlite3.Connection,
        statement: str,
    ) -> None:
        connection.execute(statement)

    def _validate_schema_objects(self, connection: sqlite3.Connection) -> None:
        rows = tuple(
            connection.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master "
                "WHERE type IN ('table', 'trigger', 'index')"
            ).fetchmany(500)
        )
        tables = {
            row["name"]: row
            for row in rows
            if row["type"] == "table" and row["name"] in _REQUIRED_TABLES
        }
        if set(tables) != _REQUIRED_TABLES:
            raise _MigrationError("current migration is missing schema objects")
        for name, expected_sql in _EXPECTED_TABLE_SQL.items():
            row = tables[name]
            if row["tbl_name"] != name or row["sql"] != expected_sql:
                raise _MigrationError("required table definition changed")

        triggers = {
            row["name"]: row
            for row in rows
            if row["type"] == "trigger" and row["tbl_name"] in _REQUIRED_TABLES
        }
        if set(triggers) != _REQUIRED_TRIGGERS:
            raise _MigrationError("append-only trigger set changed")
        for name, expected_sql in _EXPECTED_TRIGGER_SQL.items():
            row = triggers[name]
            if (
                row["tbl_name"] != _EXPECTED_TRIGGER_TABLES[name]
                or row["sql"] != expected_sql
            ):
                raise _MigrationError("append-only trigger definition changed")

        indexes = {
            row["name"]: row
            for row in rows
            if row["type"] == "index"
            and row["tbl_name"] in _REQUIRED_TABLES
            and not row["name"].startswith("sqlite_autoindex_")
        }
        if set(indexes) != _REQUIRED_INDEXES:
            raise _MigrationError("required table index set changed")
        for name, expected_sql in _EXPECTED_INDEX_SQL.items():
            row = indexes[name]
            if (
                row["tbl_name"] != _EXPECTED_INDEX_TABLES[name]
                or row["sql"] != expected_sql
            ):
                raise _MigrationError("required index definition changed")

    def _required_tables_match(self, connection: sqlite3.Connection) -> bool:
        rows = tuple(
            connection.execute(
                "SELECT name, tbl_name, sql FROM sqlite_master "
                "WHERE type = 'table' AND name IN "
                f"({', '.join('?' for _ in _REQUIRED_TABLES)})",
                tuple(sorted(_REQUIRED_TABLES)),
            ).fetchmany(len(_REQUIRED_TABLES) + 1)
        )
        observed = {
            row["name"]: (row["tbl_name"], row["sql"]) for row in rows
        }
        return observed == {
            name: (name, expected_sql)
            for name, expected_sql in _EXPECTED_TABLE_SQL.items()
        }

    def _remove_published_backup(
        self,
        backup: Path,
        *,
        anchor: _RootAnchor | None = None,
    ) -> None:
        if backup.parent != self.paths.root or not backup.name.startswith(
            f"{_DATABASE_NAME}.backup-"
        ):
            raise _MigrationError("backup cleanup target is outside state root")
        owned_anchor = anchor is None
        if anchor is None:
            anchor = self._open_root_anchor()
        try:
            self._validate_anchor_path(anchor)
            result = self._stat_at(anchor, backup.name)
            self._validate_database_stat(result)
            self._unlink_at(anchor, backup.name)
            self._fsync_anchor(anchor)
        finally:
            if owned_anchor:
                anchor.close()

    def initialize(self) -> None:
        """Configure the database and apply all pending migrations atomically."""
        connection = self._connect()
        begun = False
        published_backup: Path | None = None
        primary: BaseException | None = None
        try:
            retained_lease = self._retained_lease(connection)
            self._validate_retained_connection(connection)
            before_lock = self._migration_rows(connection)
            self._validate_migrations(before_lock)
            connection.execute("BEGIN IMMEDIATE")
            begun = True
            self._drop_verified_bootstrap(connection)
            rows = self._migration_rows(connection)
            self._validate_migrations(rows)
            pending = MIGRATIONS[len(rows) :]
            if pending and self._meaningful_database(connection):
                published_backup = self._backup_database(
                    connection,
                    from_version=len(rows),
                    to_version=pending[-1].version,
                )
            applied_at = (
                _canonical_clock_timestamp(self._clock())
                if pending
                else None
            )
            for migration in pending:
                for statement in migration.statements:
                    self._execute_migration_statement(connection, statement)
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
            self._validate_retained_connection(connection)
            connection.execute("COMMIT")
            begun = False
            self._validate_retained_connection(connection)
        except FCPMCPError as error:
            primary = error
            rollback_error = _rollback(connection, begun)
            if rollback_error is not None:
                _attach_cleanup_failures(error, [rollback_error])
            if published_backup is not None:
                try:
                    self._remove_published_backup(
                        published_backup,
                        anchor=retained_lease.anchor,
                    )
                except _LEDGER_FAILURES as cleanup_error:
                    _attach_cleanup_failures(error, [cleanup_error])
            raise
        except _LEDGER_FAILURES as error:
            rollback_error = _rollback(connection, begun)
            if rollback_error is not None:
                _attach_cleanup_failures(error, [rollback_error])
            if published_backup is not None:
                try:
                    self._remove_published_backup(
                        published_backup,
                        anchor=retained_lease.anchor,
                    )
                except _LEDGER_FAILURES as cleanup_error:
                    _attach_cleanup_failures(error, [cleanup_error])
            primary = _ledger_unavailable(error)
            raise primary
        finally:
            _close_public_connection(connection, primary)

    def _write(self, operation: Callable[[sqlite3.Connection], Any]) -> Any:
        connection = self._connect_write_existing()
        begun = False
        primary: BaseException | None = None
        try:
            self._validate_retained_connection(connection)
            connection.execute("BEGIN IMMEDIATE")
            begun = True
            result = operation(connection)
            self._validate_retained_connection(connection)
            connection.execute("COMMIT")
            begun = False
            self._validate_retained_connection(connection)
            return result
        except FCPMCPError as error:
            primary = error
            rollback_error = _rollback(connection, begun)
            if rollback_error is not None:
                _attach_cleanup_failures(error, [rollback_error])
            raise
        except _LEDGER_FAILURES as error:
            rollback_error = _rollback(connection, begun)
            if rollback_error is not None:
                _attach_cleanup_failures(error, [rollback_error])
            primary = _ledger_unavailable(error)
            raise primary
        finally:
            _close_public_connection(connection, primary)

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
        *,
        expected_state: WorkflowState | str,
        expected_revision: int,
        event_type: str,
        event_payload: Mapping[str, object],
        elapsed_ms: int | None = None,
    ) -> IdempotencyResult:
        canonical_key = _idempotency_key(key)
        request_hash = _sha256(request_sha256, field="request_sha256")
        canonical_run_id = _run_id(run_id)
        state = _workflow_state(expected_state)
        revision = _positive_revision(expected_revision)
        name = _event_type(event_type)
        payload, payload_text = _event_payload(event_payload)
        elapsed = _elapsed_ms(elapsed_ms)
        if state is not WorkflowState.PREPARING:
            raise _state_conflict(
                "idempotency keys can only be reserved while preparing"
            )

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
            run = self._run_for_cas(
                connection,
                run_id=canonical_run_id,
                expected_state=state,
                expected_revision=revision,
            )
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
            timestamp = _canonical_clock_timestamp(self._clock())
            cursor = connection.execute(
                "UPDATE runs SET revision = ?, updated_at = ? "
                "WHERE run_id = ? AND state = ? AND revision = ?",
                (
                    run.revision + 1,
                    timestamp,
                    canonical_run_id,
                    state.value,
                    revision,
                ),
            )
            if cursor.rowcount != 1:
                raise _state_conflict("workflow state or revision is stale")
            self._append_event_locked(
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
            return IdempotencyResult(existing=False, run=updated)

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

    def _projected_value(
        self,
        run: LedgerRunRecord,
        patch: Mapping[str, object],
        field: str,
    ) -> object:
        return patch[field] if field in patch else getattr(run, field)

    def _require_complete_prepare(
        self,
        connection: sqlite3.Connection,
        run: LedgerRunRecord,
        patch: Mapping[str, object],
        *,
        require_expiry: bool,
    ) -> None:
        values = {
            field: self._projected_value(run, patch, field)
            for field in (
                "source_sha256",
                "prior_destination_state",
                "prior_destination_sha256",
                "plan_sha256",
                "candidate_sha256",
                "candidate_size_bytes",
                "diff_sha256",
                "diff_size_bytes",
                "expires_at",
            )
        }
        if any(
            values[field] is None
            for field in (
                "source_sha256",
                "prior_destination_state",
                "plan_sha256",
                "candidate_sha256",
                "candidate_size_bytes",
                "diff_sha256",
                "diff_size_bytes",
            )
        ):
            raise _state_conflict(
                "workflow state requires complete successful-prepare evidence"
            )
        prior_state = values["prior_destination_state"]
        if isinstance(prior_state, PriorDestinationState):
            prior_state = prior_state.value
        prior_sha256 = values["prior_destination_sha256"]
        if (
            prior_state == PriorDestinationState.ABSENT.value
            and prior_sha256 is not None
        ) or (
            prior_state == PriorDestinationState.PRESENT.value
            and prior_sha256 is None
        ) or prior_state not in {
            PriorDestinationState.ABSENT.value,
            PriorDestinationState.PRESENT.value,
        }:
            raise _state_conflict("prior destination evidence is inconsistent")
        if require_expiry and values["expires_at"] is None:
            raise _state_conflict("approval-bearing state requires expires_at")

        rows = tuple(
            connection.execute(
                "SELECT kind, sha256, byte_size FROM artifacts "
                "WHERE run_id = ? ORDER BY kind",
                (run.run_id,),
            ).fetchmany(3)
        )
        artifacts = {row["kind"]: row for row in rows}
        for kind in _PROJECTED_ARTIFACT_KINDS:
            artifact = artifacts.get(kind.value)
            if artifact is None or (
                artifact["sha256"] != values[f"{kind.value}_sha256"]
                or artifact["byte_size"] != values[f"{kind.value}_size_bytes"]
            ):
                raise _state_conflict(
                    "prepared projection requires matching artifact evidence"
                )

    def _validate_commit_intent(
        self,
        current: LedgerRunRecord,
        payload: Mapping[str, object],
        patch: Mapping[str, object],
    ) -> None:
        attempt = patch["commit_attempt_id"]
        if (
            not isinstance(attempt, str)
            or _UUID_RE.fullmatch(attempt) is None
        ):
            raise _state_conflict("commit attempt ID must be a canonical UUID")
        try:
            parsed_attempt = UUID(attempt)
        except ValueError as error:
            raise _state_conflict("commit attempt ID must be a canonical UUID") from error
        if parsed_attempt.int == 0 or str(parsed_attempt) != attempt:
            raise _state_conflict("commit attempt ID must be a canonical UUID")

        expected_backup = patch["expected_backup_path"]
        backup_sha256 = patch["backup_sha256"]
        if current.prior_destination_state is PriorDestinationState.ABSENT:
            consistent = expected_backup is None and backup_sha256 is None
        elif current.prior_destination_state is PriorDestinationState.PRESENT:
            consistent = (
                current.prior_destination_sha256 is not None
                and expected_backup
                == f"{current.destination_path}.bak.{attempt}"
                and backup_sha256 == current.prior_destination_sha256
            )
        else:
            consistent = False
        if not consistent:
            raise _state_conflict("commit intent contradicts prior destination evidence")
        if any(payload[field] != patch[field] for field in _COMMIT_INTENT_FIELDS):
            raise _state_conflict(
                "commit_started payload must match projected commit evidence"
            )

    def _validate_event_mutation(
        self,
        connection: sqlite3.Connection,
        *,
        current: LedgerRunRecord,
        target: WorkflowState,
        event_type: str,
        payload: Mapping[str, object],
        patch: Mapping[str, object],
    ) -> None:
        if (
            event_type in _PROJECTION_EVENT_NAMES
            and frozenset(payload) != frozenset(patch)
        ):
            raise _state_conflict(
                "projection event payload keys must match its projection"
            )
        if current.state is WorkflowState.PREPARING:
            event_prepare_fields = {
                field
                for field, event_types in _PREPARE_PROJECTION_EVENTS.items()
                if event_type in event_types
            }
            projected_prepare_fields = {
                field
                for field in patch
                if field in event_prepare_fields
            }
            payload_prepare_fields = {
                field
                for field in payload
                if field in event_prepare_fields
            }
            if any(
                field not in payload or payload[field] != patch[field]
                for field in projected_prepare_fields
            ) or payload_prepare_fields != projected_prepare_fields:
                raise _state_conflict(
                    "prepare payload must match projected prepare evidence"
                )
        if target is WorkflowState.AWAITING_APPROVAL:
            self._require_complete_prepare(
                connection,
                current,
                patch,
                require_expiry=True,
            )
        if (
            current.state is WorkflowState.APPROVED
            and target is WorkflowState.COMMITTING
            and event_type == "commit_started"
        ):
            self._validate_commit_intent(current, payload, patch)
        if (
            current.state is WorkflowState.COMMITTING
            and target is WorkflowState.COMMITTED
        ):
            if current.commit_attempt_id is None:
                raise _state_conflict("committed state requires commit intent evidence")
            if patch["destination_sha256"] != current.candidate_sha256:
                raise _state_conflict(
                    "committed destination hash must equal the candidate hash"
                )
            if patch["backup_sha256"] != current.backup_sha256:
                raise _state_conflict(
                    "committed backup hash must match commit intent evidence"
                )
            if any(
                field not in payload or payload[field] != patch[field]
                for field in _COMMIT_RESULT_FIELDS
            ):
                raise _state_conflict(
                    "committed payload must match projected receipt evidence"
                )

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
            self._validate_event_mutation(
                connection,
                current=current,
                target=target_state,
                event_type=event_type,
                payload=payload,
                patch=projection_patch,
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
        _validate_projection_policy(
            source=state,
            target=state,
            event_type=name,
            patch=patch,
        )
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
        if (
            source is WorkflowState.PREPARING
            and target is WorkflowState.FAILED
            and name == "failed"
        ):
            raise _state_conflict(
                "failed prepare events require record_prepare_failure"
            )
        copied_payload, payload_text = _event_payload(payload)
        elapsed = _elapsed_ms(elapsed_ms)
        patch = _projection_patch(projection_patch)
        _validate_projection_policy(
            source=source,
            target=target,
            event_type=name,
            patch=patch,
        )
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
        if canonical_metadata.kind is ArtifactKind.FAILURE_EVIDENCE:
            raise _state_conflict(
                "failure evidence requires record_prepare_failure"
            )
        state = _workflow_state(expected_state)
        if state is not WorkflowState.PREPARING:
            raise _state_conflict(
                "artifacts can only be recorded while preparing"
            )
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

    def record_prepare_failure(
        self,
        metadata: ArtifactMetadataV1,
        *,
        expected_revision: int,
        error_code: ErrorCode | str,
        error_summary: str,
        event_type: str,
        event_payload: Mapping[str, object],
        elapsed_ms: int | None = None,
    ) -> ArtifactMutationResult:
        """Atomically record private prepare failure evidence and fail the run."""
        canonical_metadata = _artifact_metadata(metadata)
        if canonical_metadata.kind is not ArtifactKind.FAILURE_EVIDENCE:
            raise _state_conflict(
                "prepare failure requires failure evidence metadata"
            )
        revision = _positive_revision(expected_revision)
        name = _event_type(event_type)
        if name != "failed":
            raise _state_conflict("prepare failure event type must be failed")
        payload, payload_text = _event_payload(event_payload)
        patch = _projection_patch(
            {
                "terminal_error_code": error_code,
                "terminal_error_summary": error_summary,
            }
        )
        if payload != {"error_code": patch["terminal_error_code"]}:
            raise _state_conflict(
                "prepare failure event payload must contain only error_code"
            )
        elapsed = _elapsed_ms(elapsed_ms)
        created_at = _canonical_datetime(canonical_metadata.created_at)
        _validate_projection_policy(
            source=WorkflowState.PREPARING,
            target=WorkflowState.FAILED,
            event_type=name,
            patch=patch,
        )

        def record(connection: sqlite3.Connection) -> ArtifactMutationResult:
            current = self._run_for_cas(
                connection,
                run_id=canonical_metadata.run_id,
                expected_state=WorkflowState.PREPARING,
                expected_revision=revision,
            )
            duplicate = connection.execute(
                "SELECT 1 FROM artifacts WHERE run_id = ? AND kind = ?",
                (
                    canonical_metadata.run_id,
                    ArtifactKind.FAILURE_EVIDENCE.value,
                ),
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
            cursor = connection.execute(
                "UPDATE runs SET state = ?, revision = ?, updated_at = ?, "
                "terminal_error_code = ?, terminal_error_summary = ? "
                "WHERE run_id = ? AND state = ? AND revision = ?",
                (
                    WorkflowState.FAILED.value,
                    current.revision + 1,
                    timestamp,
                    patch["terminal_error_code"],
                    patch["terminal_error_summary"],
                    canonical_metadata.run_id,
                    WorkflowState.PREPARING.value,
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
            return ArtifactMutationResult(
                run=updated,
                artifact=artifact,
                event=event,
            )

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
        if not isinstance(event_payload, Mapping):
            raise _invalid("event payload must be a JSON object")
        decision_payload = dict(event_payload)
        supplied_binding = decision_payload.get("binding_sha256")
        if supplied_binding is not None and supplied_binding != binding:
            raise _invalid(
                "decision event binding_sha256 must match the approval binding"
            )
        decision_payload["binding_sha256"] = binding
        payload, payload_text = _event_payload(decision_payload)
        elapsed = _elapsed_ms(elapsed_ms)

        def record(connection: sqlite3.Connection) -> DecisionMutationResult:
            current = self._run_for_cas(
                connection,
                run_id=canonical_run_id,
                expected_state=state,
                expected_revision=revision,
            )
            expected_source = ApprovalSource(current.approval_mode.value)
            if closed_source is not expected_source:
                raise _state_conflict(
                    "approval source contradicts the configured approval mode"
                )
            self._require_complete_prepare(
                connection,
                current,
                {"expires_at": expiry} if expiry is not None else {},
                require_expiry=True,
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
        primary: BaseException | None = None
        try:
            return self._select_run(connection, canonical_run_id)
        except FCPMCPError as error:
            primary = error
            raise
        except _LEDGER_FAILURES as error:
            primary = _ledger_unavailable(error)
            raise primary
        finally:
            _close_public_connection(connection, primary)

    def get_artifact(
        self,
        run_id: str,
        kind: ArtifactKind | str,
    ) -> ArtifactRecord | None:
        canonical_run_id = _run_id(run_id)
        closed_kind = _artifact_kind_value(kind)
        connection = self._connect_existing()
        primary: BaseException | None = None
        try:
            row = connection.execute(
                "SELECT * FROM artifacts WHERE run_id = ? AND kind = ?",
                (canonical_run_id, closed_kind.value),
            ).fetchone()
            return _row_to_artifact(row) if row is not None else None
        except FCPMCPError as error:
            primary = error
            raise
        except _LEDGER_FAILURES as error:
            primary = _ledger_unavailable(error)
            raise primary
        finally:
            _close_public_connection(connection, primary)

    def list_artifacts(
        self,
        run_id: str,
        *,
        after_kind: ArtifactKind | str | None = None,
        limit: int = 100,
    ) -> tuple[ArtifactRecord, ...]:
        canonical_run_id = _run_id(run_id)
        bounded_limit = _read_limit(limit)
        closed_after = (
            _artifact_kind_value(after_kind) if after_kind is not None else None
        )
        connection = self._connect_existing()
        primary: BaseException | None = None
        try:
            if closed_after is None:
                cursor = connection.execute(
                    "SELECT * FROM artifacts WHERE run_id = ? "
                    "ORDER BY kind ASC LIMIT ?",
                    (canonical_run_id, bounded_limit),
                )
            else:
                cursor = connection.execute(
                    "SELECT * FROM artifacts WHERE run_id = ? AND kind > ? "
                    "ORDER BY kind ASC LIMIT ?",
                    (canonical_run_id, closed_after.value, bounded_limit),
                )
            return tuple(
                _row_to_artifact(row) for row in cursor.fetchmany(bounded_limit)
            )
        except FCPMCPError as error:
            primary = error
            raise
        except _LEDGER_FAILURES as error:
            primary = _ledger_unavailable(error)
            raise primary
        finally:
            _close_public_connection(connection, primary)

    def get_approval(self, run_id: str) -> ApprovalRecord | None:
        canonical_run_id = _run_id(run_id)
        connection = self._connect_existing()
        primary: BaseException | None = None
        try:
            row = connection.execute(
                "SELECT * FROM approvals WHERE run_id = ?",
                (canonical_run_id,),
            ).fetchone()
            return _row_to_approval(row) if row is not None else None
        except FCPMCPError as error:
            primary = error
            raise
        except _LEDGER_FAILURES as error:
            primary = _ledger_unavailable(error)
            raise primary
        finally:
            _close_public_connection(connection, primary)

    def list_runs(
        self,
        *,
        limit: int = 100,
        state: WorkflowState | str | None = None,
    ) -> tuple[LedgerRunRecord, ...]:
        bounded_limit = _read_limit(limit)
        closed_state = _workflow_state(state) if state is not None else None
        connection = self._connect_existing()
        primary: BaseException | None = None
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
        except FCPMCPError as error:
            primary = error
            raise
        except _LEDGER_FAILURES as error:
            primary = _ledger_unavailable(error)
            raise primary
        finally:
            _close_public_connection(connection, primary)

    def list_terminal_runs_before(
        self,
        cutoff: str,
        *,
        after_updated_at: str | None = None,
        after_run_id: str | None = None,
        limit: int = 100,
    ) -> tuple[LedgerRunRecord, ...]:
        canonical_cutoff = _canonical_timestamp(cutoff, field="cutoff")
        bounded_limit = _read_limit(limit)
        if (after_updated_at is None) != (after_run_id is None):
            raise _invalid(
                "after_updated_at and after_run_id must be provided together"
            )
        cursor_timestamp = (
            _canonical_timestamp(after_updated_at, field="after_updated_at")
            if after_updated_at is not None
            else None
        )
        cursor_run_id = (
            _run_id(after_run_id) if after_run_id is not None else None
        )
        states = tuple(sorted(state.value for state in _PRUNE_ELIGIBLE_STATES))
        placeholders = ", ".join("?" for _ in states)
        connection = self._connect_existing()
        primary: BaseException | None = None
        try:
            if cursor_timestamp is None:
                parameters: tuple[object, ...] = (
                    *states,
                    canonical_cutoff,
                    bounded_limit,
                )
                sql = (
                    f"SELECT * FROM runs WHERE state IN ({placeholders}) "
                    "AND updated_at < ? "
                    "ORDER BY updated_at ASC, run_id ASC LIMIT ?"
                )
            else:
                parameters = (
                    *states,
                    canonical_cutoff,
                    cursor_timestamp,
                    cursor_timestamp,
                    cursor_run_id,
                    bounded_limit,
                )
                sql = (
                    f"SELECT * FROM runs WHERE state IN ({placeholders}) "
                    "AND updated_at < ? AND "
                    "(updated_at > ? OR (updated_at = ? AND run_id > ?)) "
                    "ORDER BY updated_at ASC, run_id ASC LIMIT ?"
                )
            cursor = connection.execute(sql, parameters)
            return tuple(
                _row_to_run(row) for row in cursor.fetchmany(bounded_limit)
            )
        except FCPMCPError as error:
            primary = error
            raise
        except _LEDGER_FAILURES as error:
            primary = _ledger_unavailable(error)
            raise primary
        finally:
            _close_public_connection(connection, primary)

    def list_pending_prune_runs(
        self,
        *,
        limit: int = 100,
    ) -> tuple[LedgerRunRecord, ...]:
        """List terminal runs whose latest prune intent is not completed.

        This traversal is deliberately independent of ``runs.updated_at``:
        appending the intent advances that timestamp, but must not make an
        interrupted prune undiscoverable on restart.
        """
        bounded_limit = _read_limit(limit)
        states = tuple(sorted(state.value for state in _PRUNE_ELIGIBLE_STATES))
        placeholders = ", ".join("?" for _ in states)
        connection = self._connect_existing()
        primary: BaseException | None = None
        try:
            rows = connection.execute(
                f"SELECT r.* FROM runs AS r "
                f"WHERE r.state IN ({placeholders}) "
                "AND COALESCE(("
                "SELECT MAX(i.sequence) FROM events AS i "
                "WHERE i.run_id = r.run_id "
                "AND i.event_type = 'artifact_prune_intent'"
                "), 0) > COALESCE(("
                "SELECT MAX(p.sequence) FROM events AS p "
                "WHERE p.run_id = r.run_id "
                "AND p.event_type = 'artifacts_pruned'"
                "), 0) "
                "ORDER BY r.run_id ASC LIMIT ?",
                (*states, bounded_limit),
            ).fetchmany(bounded_limit)
            return tuple(_row_to_run(row) for row in rows)
        except FCPMCPError as error:
            primary = error
            raise
        except _LEDGER_FAILURES as error:
            primary = _ledger_unavailable(error)
            raise primary
        finally:
            _close_public_connection(connection, primary)

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
        primary: BaseException | None = None
        try:
            cursor = connection.execute(
                "SELECT * FROM events WHERE run_id = ? AND sequence > ? "
                "ORDER BY sequence ASC LIMIT ?",
                (canonical_run_id, after_sequence, bounded_limit),
            )
            return tuple(
                _row_to_event(row) for row in cursor.fetchmany(bounded_limit)
            )
        except FCPMCPError as error:
            primary = error
            raise
        except _LEDGER_FAILURES as error:
            primary = _ledger_unavailable(error)
            raise primary
        finally:
            _close_public_connection(connection, primary)

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
                    run_id=_safe_finding_run_id(finding_run_id),
                    sequence=_safe_finding_sequence(sequence),
                )
            )

        try:
            connection.execute("BEGIN")
            begun = True
            try:
                self._validate_schema_objects(connection)
            except _MigrationError:
                add("schema_mismatch", "stored schema objects do not match")
                if not self._required_tables_match(connection):
                    result = IntegrityResult(
                        valid=False,
                        checked_migrations=0,
                        checked_runs=0,
                        checked_events=0,
                        findings=tuple(findings),
                    )
                    connection.execute("ROLLBACK")
                    begun = False
                    return result
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
                    "SELECT * FROM runs ORDER BY run_id ASC"
                )
            else:
                run_cursor = connection.execute(
                    "SELECT * FROM runs WHERE run_id = ?",
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
                    prepare_event_evidence: dict[str, object] = {}
                    commit_intent_event_evidence: dict[str, object] = {}
                    commit_result_event_evidence: dict[str, object] = {}
                    failure_event_payloads: list[Mapping[str, object]] = []
                    approval_bindings: set[str] = set()
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
                                    if not _json_text_is_safe(parsed):
                                        raise ValueError(
                                            "payload contains unsafe text"
                                        )
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
                                and _text_is_safe(row["event_type"])
                                and _stored_timestamp_is_canonical(row["timestamp"])
                                and elapsed_valid
                                and isinstance(raw_previous, str)
                                and _SHA256_RE.fullmatch(raw_previous) is not None
                                and isinstance(row["event_hash"], str)
                                and _SHA256_RE.fullmatch(row["event_hash"]) is not None
                                and isinstance(payload_text, str)
                                and (
                                    _safe_utf8_length(payload_text)
                                    is not None
                                )
                                and (
                                    _safe_utf8_length(payload_text) or 0
                                )
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
                                event_name = row["event_type"]
                                for field, event_types in (
                                    _PREPARE_PROJECTION_EVENTS.items()
                                ):
                                    if (
                                        event_name in event_types
                                        and field in payload_value
                                    ):
                                        prepare_event_evidence[field] = (
                                            payload_value[field]
                                        )
                                if event_name == "commit_started" and all(
                                    field in payload_value
                                    for field in _COMMIT_INTENT_FIELDS
                                ):
                                    for field in _COMMIT_INTENT_FIELDS:
                                        commit_intent_event_evidence[field] = (
                                            payload_value[field]
                                        )
                                if event_name in {
                                    "commit_completed",
                                    "committed",
                                } and all(
                                    field in payload_value
                                    for field in _COMMIT_RESULT_FIELDS
                                ):
                                    for field in _COMMIT_RESULT_FIELDS:
                                        commit_result_event_evidence[field] = (
                                            payload_value[field]
                                        )
                                if event_name == "failed":
                                    failure_event_payloads.append(payload_value)
                                binding = payload_value.get("binding_sha256")
                                if (
                                    isinstance(binding, str)
                                    and _SHA256_RE.fullmatch(binding)
                                ):
                                    approval_bindings.add(binding)
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
                    for evidence in (
                        prepare_event_evidence,
                        commit_intent_event_evidence,
                        commit_result_event_evidence,
                    ):
                        for field, expected_value in evidence.items():
                            if run_row[field] != expected_value:
                                add(
                                    "projection_invariant",
                                    "event evidence does not match the run projection",
                                    finding_run_id=current_run_id,
                                )
                    for field in _PREPARE_PROJECTION_EVENTS:
                        if (
                            run_row[field] is not None
                            and field not in prepare_event_evidence
                        ):
                            add(
                                "projection_invariant",
                                "run projection lacks its required event evidence",
                                finding_run_id=current_run_id,
                            )

                    artifact_rows = tuple(
                        connection.execute(
                            "SELECT * FROM artifacts WHERE run_id = ? "
                            "ORDER BY kind ASC",
                            (current_run_id,),
                        ).fetchmany(10)
                    )
                    artifacts_by_kind = {
                        row["kind"]: row for row in artifact_rows
                    }
                    for kind in _PROJECTED_ARTIFACT_KINDS:
                        artifact = artifacts_by_kind.get(kind.value)
                        projected_hash = run_row[f"{kind.value}_sha256"]
                        projected_size = run_row[f"{kind.value}_size_bytes"]
                        if artifact is None:
                            matches = (
                                projected_hash is None
                                and projected_size is None
                            )
                        else:
                            matches = (
                                projected_hash == artifact["sha256"]
                                and projected_size == artifact["byte_size"]
                            )
                        if not matches:
                            add(
                                "projection_invariant",
                                "artifact metadata does not match the run projection",
                                finding_run_id=current_run_id,
                            )

                    approval_row = connection.execute(
                        "SELECT * FROM approvals WHERE run_id = ?",
                        (current_run_id,),
                    ).fetchone()
                    raw_state = run_row["state"]
                    try:
                        current_state = WorkflowState(raw_state)
                    except (TypeError, ValueError):
                        current_state = None
                        add(
                            "projection_invariant",
                            "run state is invalid",
                            finding_run_id=current_run_id,
                        )
                    failure_artifact = artifacts_by_kind.get(
                        ArtifactKind.FAILURE_EVIDENCE.value
                    )
                    if failure_event_payloads:
                        failure_payload_valid = (
                            len(failure_event_payloads) == 1
                            and set(failure_event_payloads[0]) == {"error_code"}
                            and failure_event_payloads[0]["error_code"]
                            == run_row["terminal_error_code"]
                        )
                        if (
                            current_state is not WorkflowState.FAILED
                            or failure_artifact is None
                            or not failure_payload_valid
                        ):
                            add(
                                "failure_evidence_invariant",
                                "failed prepare event lacks matching private evidence",
                                finding_run_id=current_run_id,
                            )
                    if failure_artifact is not None and (
                        current_state is not WorkflowState.FAILED
                        or not failure_event_payloads
                    ):
                        add(
                            "failure_evidence_invariant",
                            "private failure evidence contradicts workflow history",
                            finding_run_id=current_run_id,
                        )
                    if current_state in _PREPARED_STATES:
                        required_prepare_fields = (
                            "source_sha256",
                            "prior_destination_state",
                            "plan_sha256",
                            "candidate_sha256",
                            "candidate_size_bytes",
                            "diff_sha256",
                            "diff_size_bytes",
                            "expires_at",
                        )
                        prior_state = run_row["prior_destination_state"]
                        prior_sha256 = run_row["prior_destination_sha256"]
                        prepare_complete = all(
                            run_row[field] is not None
                            for field in required_prepare_fields
                        ) and (
                            (
                                prior_state
                                == PriorDestinationState.ABSENT.value
                                and prior_sha256 is None
                            )
                            or (
                                prior_state
                                == PriorDestinationState.PRESENT.value
                                and prior_sha256 is not None
                            )
                        )
                        if not prepare_complete or any(
                            kind.value not in artifacts_by_kind
                            for kind in _PROJECTED_ARTIFACT_KINDS
                        ) or any(
                            field not in prepare_event_evidence
                            for field in _PREPARE_PROJECTION_EVENTS
                        ):
                            add(
                                "projection_invariant",
                                "prepared state lacks complete prepare evidence",
                                finding_run_id=current_run_id,
                            )
                    if approval_row is None:
                        approval_projection_present = any(
                            run_row[field] is not None
                            for field in (
                                "approved_at",
                                "approval_decision",
                                "approval_source",
                                "approval_summary",
                            )
                        )
                        if approval_projection_present or current_state in {
                            WorkflowState.APPROVED,
                            WorkflowState.COMMITTING,
                            WorkflowState.COMMITTED,
                            WorkflowState.REJECTED,
                            WorkflowState.STALE,
                            WorkflowState.ROLLED_BACK,
                            WorkflowState.RECOVERY_REQUIRED,
                        }:
                            add(
                                "projection_invariant",
                                "approval projection lacks immutable approval evidence",
                                finding_run_id=current_run_id,
                            )
                    else:
                        approval_matches = (
                            run_row["approval_decision"]
                            == approval_row["decision"]
                            and run_row["approval_source"]
                            == approval_row["source"]
                            and approval_row["binding_sha256"]
                            in approval_bindings
                        )
                        expected_source = (
                            ApprovalSource.CLI.value
                            if run_row["approval_mode"] == ApprovalMode.CLI.value
                            else ApprovalSource.CLIENT.value
                        )
                        approval_matches = approval_matches and (
                            approval_row["source"] == expected_source
                        )
                        if approval_row["decision"] == ApprovalDecision.APPROVED.value:
                            approval_matches = approval_matches and (
                                run_row["approved_at"]
                                == approval_row["created_at"]
                            )
                        else:
                            approval_matches = approval_matches and (
                                run_row["approved_at"] is None
                            )
                        if (
                            approval_row["expires_at"] is not None
                            and run_row["expires_at"]
                            != approval_row["expires_at"]
                        ):
                            approval_matches = False
                        if not approval_matches:
                            add(
                                "projection_invariant",
                                "approval evidence does not match the run projection",
                                finding_run_id=current_run_id,
                            )
                        if current_state in _APPROVED_STATES and (
                            approval_row["decision"]
                            != ApprovalDecision.APPROVED.value
                        ):
                            add(
                                "projection_invariant",
                                "approved state lacks an approved decision",
                                finding_run_id=current_run_id,
                            )
                        if (
                            current_state is WorkflowState.REJECTED
                            and approval_row["decision"]
                            != ApprovalDecision.REJECTED.value
                        ):
                            add(
                                "projection_invariant",
                                "rejected state lacks a rejected decision",
                                finding_run_id=current_run_id,
                            )

                    committed_at = run_row["committed_at"]
                    if (
                        committed_at is not None
                        and current_state is not WorkflowState.COMMITTED
                    ) or (
                        current_state is WorkflowState.COMMITTED
                        and committed_at is None
                    ):
                        add(
                            "projection_invariant",
                            "committed timestamp contradicts workflow state",
                            finding_run_id=current_run_id,
                        )
                    commit_evidence_present = any(
                        run_row[field] is not None
                        for field in (
                            "commit_attempt_id",
                            "expected_backup_path",
                            "backup_sha256",
                        )
                    )
                    commit_states = {
                        WorkflowState.COMMITTING,
                        WorkflowState.COMMITTED,
                        WorkflowState.ROLLED_BACK,
                        WorkflowState.RECOVERY_REQUIRED,
                    }
                    if commit_evidence_present and current_state not in commit_states:
                        add(
                            "projection_invariant",
                            "commit evidence contradicts workflow state",
                            finding_run_id=current_run_id,
                        )
                    if current_state in commit_states:
                        attempt = run_row["commit_attempt_id"]
                        try:
                            parsed_attempt = (
                                UUID(attempt)
                                if isinstance(attempt, str)
                                else None
                            )
                        except ValueError:
                            parsed_attempt = None
                        canonical_attempt = (
                            parsed_attempt is not None
                            and parsed_attempt.int != 0
                            and str(parsed_attempt) == attempt
                        )
                        if (
                            run_row["prior_destination_state"]
                            == PriorDestinationState.ABSENT.value
                        ):
                            backup_consistent = (
                                run_row["expected_backup_path"] is None
                                and run_row["backup_sha256"] is None
                            )
                        elif (
                            run_row["prior_destination_state"]
                            == PriorDestinationState.PRESENT.value
                            and canonical_attempt
                        ):
                            backup_consistent = (
                                run_row["expected_backup_path"]
                                == (
                                    f"{run_row['destination_path']}.bak."
                                    f"{attempt}"
                                )
                                and run_row["backup_sha256"]
                                == run_row["prior_destination_sha256"]
                            )
                        else:
                            backup_consistent = False
                        if (
                            not canonical_attempt
                            or not backup_consistent
                            or any(
                                field not in commit_intent_event_evidence
                                for field in _COMMIT_INTENT_FIELDS
                            )
                        ):
                            add(
                                "projection_invariant",
                                "commit state lacks consistent intent evidence",
                                finding_run_id=current_run_id,
                            )
                    required_result_fields = (
                        _COMMIT_RESULT_FIELDS - {"backup_sha256"}
                    )
                    result_fields_present = tuple(
                        run_row[field] is not None
                        for field in required_result_fields
                    )
                    if current_state is WorkflowState.COMMITTED:
                        if (
                            not all(result_fields_present)
                            or any(
                                field not in commit_result_event_evidence
                                for field in _COMMIT_RESULT_FIELDS
                            )
                            or run_row["destination_sha256"]
                            != run_row["candidate_sha256"]
                        ):
                            add(
                                "projection_invariant",
                                "committed state lacks complete result evidence",
                                finding_run_id=current_run_id,
                            )
                    elif any(result_fields_present):
                        add(
                            "projection_invariant",
                            "commit result evidence exists before final commit",
                            finding_run_id=current_run_id,
                        )
                    terminal_error_present = (
                        run_row["terminal_error_code"] is not None,
                        run_row["terminal_error_summary"] is not None,
                    )
                    if terminal_error_present[0] != terminal_error_present[1] or (
                        any(terminal_error_present)
                        and current_state
                        not in _ERROR_ALLOWED_STATES
                    ) or (
                        current_state in _ERROR_REQUIRED_STATES
                        and not all(terminal_error_present)
                    ):
                        add(
                            "projection_invariant",
                            "terminal error evidence contradicts workflow state",
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
            primary = sys.exc_info()[1]
            rollback_error = _rollback(connection, begun)
            if rollback_error is not None:
                if primary is not None:
                    _attach_cleanup_failures(primary, [rollback_error])
                else:
                    primary = _ledger_unavailable(rollback_error)
            _close_public_connection(connection, primary)
            if rollback_error is not None and sys.exc_info()[1] is None:
                raise primary


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


def _row_to_artifact(row: sqlite3.Row) -> ArtifactRecord:
    return ArtifactRecord(
        run_id=row["run_id"],
        kind=ArtifactKind(row["kind"]),
        relative_path=row["relative_path"],
        sha256=row["sha256"],
        byte_size=row["byte_size"],
        created_at=row["created_at"],
    )


def _row_to_approval(row: sqlite3.Row) -> ApprovalRecord:
    return ApprovalRecord(
        run_id=row["run_id"],
        decision=ApprovalDecision(row["decision"]),
        source=ApprovalSource(row["source"]),
        operator=row["operator"],
        host=row["host"],
        terminal_present=bool(row["terminal_present"]),
        binding_sha256=row["binding_sha256"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
    )


def _safe_finding_run_id(value: object) -> str | None:
    if not isinstance(value, str) or not _UUID_RE.fullmatch(value):
        return None
    try:
        parsed = UUID(value)
    except ValueError:
        return None
    if parsed.int == 0 or str(parsed) != value or not _text_is_safe(value):
        return None
    return value


def _safe_finding_sequence(value: object) -> int | None:
    if type(value) is not int or not 1 <= value <= _SQLITE_MAX_INTEGER:
        return None
    return value


def _safe_utf8_length(value: object) -> int | None:
    if not isinstance(value, str) or "\x00" in value:
        return None
    try:
        return len(value.encode("utf-8", errors="strict"))
    except UnicodeEncodeError:
        return None


def _text_is_safe(value: object) -> bool:
    return _safe_utf8_length(value) is not None


def _json_text_is_safe(value: object) -> bool:
    if isinstance(value, str):
        return _text_is_safe(value)
    if isinstance(value, Mapping):
        return all(
            _text_is_safe(key) and _json_text_is_safe(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return all(_json_text_is_safe(item) for item in value)
    return True


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


def _artifact_kind_value(value: object) -> ArtifactKind:
    if isinstance(value, ArtifactKind):
        return value
    if isinstance(value, str):
        try:
            return ArtifactKind(value)
        except ValueError:
            pass
    raise _invalid("artifact kind is invalid")


def _bounded_text(value: object, *, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= maximum:
        raise _invalid(f"{field} must be nonempty bounded text")
    if "\x00" in value:
        raise _invalid(f"{field} must not contain NUL")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise _invalid(f"{field} must be valid UTF-8 text", error)
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
    _validate_json_text(parsed)
    return parsed, encoded.decode("utf-8")


def _validate_json_text(value: object) -> None:
    if isinstance(value, str):
        if "\x00" in value:
            raise _invalid("event payload text must not contain NUL")
        try:
            value.encode("utf-8", errors="strict")
        except UnicodeEncodeError as error:
            raise _invalid("event payload text must be valid UTF-8", error)
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            _validate_json_text(key)
            _validate_json_text(item)
        return
    if isinstance(value, list):
        for item in value:
            _validate_json_text(item)


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
