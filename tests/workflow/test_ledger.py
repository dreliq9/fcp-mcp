from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import fcp_mcp.workflow.ledger as ledger_module
from fcp_mcp.config import RuntimeConfig
from fcp_mcp.contracts import ErrorCode, FCPMCPError
from fcp_mcp.profiles import ApprovalMode, Profile
from fcp_mcp.workflow.artifacts import (
    ArtifactKind,
    ArtifactMetadataV1,
    StatePaths,
)
from fcp_mcp.workflow.ledger import (
    MIGRATIONS,
    ApprovalRecord,
    ArtifactMutationResult,
    ArtifactRecord,
    DecisionMutationResult,
    EventMutationResult,
    IdempotencyResult,
    IntegrityResult,
    Migration,
    WorkflowLedger,
    migration_checksum,
)
from fcp_mcp.workflow.models import (
    ApprovalDecision,
    ApprovalSource,
    PriorDestinationState,
    WorkflowState,
    canonical_json,
)

RUN_ID = "123e4567-e89b-42d3-a456-426614174000"
RUN_ID_2 = "123e4567-e89b-42d3-a456-426614174001"
RUN_ID_3 = "123e4567-e89b-42d3-a456-426614174002"
ATTEMPT_ID = "223e4567-e89b-42d3-a456-426614174000"
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64
HASH_E = "e" * 64
UTC_1 = datetime(2026, 7, 27, 1, 2, 3, tzinfo=timezone.utc)


class TickClock:
    def __init__(self) -> None:
        self._value = UTC_1
        self._lock = threading.Lock()

    def __call__(self) -> datetime:
        with self._lock:
            current = self._value
            self._value += timedelta(seconds=1)
            return current


def _config(tmp_path: Path) -> RuntimeConfig:
    return RuntimeConfig.from_env(
        {"FCP_MCP_STATE_DIR": str(tmp_path / "state")},
        home=tmp_path,
    )


def _paths(tmp_path: Path) -> StatePaths:
    return StatePaths.from_config(_config(tmp_path))


def _uncreated_paths(tmp_path: Path) -> StatePaths:
    root = tmp_path / "state"
    return StatePaths(
        root=root,
        database=root / "runs.sqlite3",
        artifacts=root / "artifacts",
        locks=root / "locks",
    )


def _ledger(
    tmp_path: Path,
    *,
    clock: TickClock | None = None,
    initialize: bool = True,
) -> WorkflowLedger:
    ledger = WorkflowLedger(
        _paths(tmp_path),
        clock=clock or TickClock(),
        package_version="0.3.0-test",
    )
    if initialize:
        ledger.initialize()
    return ledger


def _create(
    ledger: WorkflowLedger,
    *,
    run_id: str = RUN_ID,
    key: str | None = None,
    request_sha256: str | None = None,
    approval_mode: ApprovalMode = ApprovalMode.CLI,
) -> IdempotencyResult:
    return ledger.create_run(
        run_id=run_id,
        graph_version="1",
        run_version="1",
        profile=Profile.WORKFLOW,
        approval_mode=approval_mode,
        source_path="/private/input.fcpxml",
        destination_path="/private/output.fcpxml",
        idempotency_key=key,
        request_sha256=request_sha256,
        event_type="run_created",
        event_payload={"node": "create", "attempt": 1},
    )


def _record_prepare_evidence(
    ledger: WorkflowLedger,
    *,
    run_id: str = RUN_ID,
    revision: int = 1,
    prior_state: PriorDestinationState = PriorDestinationState.ABSENT,
    prior_sha256: str | None = None,
) -> ArtifactMutationResult:
    inspected = ledger.append_event(
        run_id,
        expected_state=WorkflowState.PREPARING,
        expected_revision=revision,
        event_type="source_inspected",
        payload={
            "source_sha256": HASH_A,
            "prior_destination_state": prior_state,
            "prior_destination_sha256": prior_sha256,
        },
        projection_patch={
            "source_sha256": HASH_A,
            "prior_destination_state": prior_state,
            "prior_destination_sha256": prior_sha256,
        },
    )
    planned = ledger.append_event(
        run_id,
        expected_state=WorkflowState.PREPARING,
        expected_revision=inspected.run.revision,
        event_type="plan_built",
        payload={"plan_sha256": HASH_B},
        projection_patch={"plan_sha256": HASH_B},
    )
    candidate = ledger.record_artifact(
        _metadata(run_id=run_id),
        expected_state=WorkflowState.PREPARING,
        expected_revision=planned.run.revision,
        event_type="candidate_stored",
        event_payload={"sha256": HASH_C},
    )
    diff = ledger.record_artifact(
        _metadata(
            run_id=run_id,
            kind=ArtifactKind.DIFF,
            digest=HASH_D,
            size=17,
        ),
        expected_state=WorkflowState.PREPARING,
        expected_revision=candidate.run.revision,
        event_type="diff_created",
        event_payload={"sha256": HASH_D},
    )
    return diff


def _complete_prepare(
    ledger: WorkflowLedger,
    *,
    run_id: str = RUN_ID,
    revision: int = 1,
    prior_state: PriorDestinationState = PriorDestinationState.ABSENT,
    prior_sha256: str | None = None,
) -> EventMutationResult:
    prepared = _record_prepare_evidence(
        ledger,
        run_id=run_id,
        revision=revision,
        prior_state=prior_state,
        prior_sha256=prior_sha256,
    )
    return ledger.transition(
        run_id,
        expected_state=WorkflowState.PREPARING,
        expected_revision=prepared.run.revision,
        target_state=WorkflowState.AWAITING_APPROVAL,
        event_type="awaiting_approval",
        payload={"expires_at": "2026-07-28T01:02:03Z"},
        projection_patch={"expires_at": "2026-07-28T01:02:03Z"},
    )


def _approve(
    ledger: WorkflowLedger,
    awaiting: EventMutationResult,
    *,
    run_id: str = RUN_ID,
    source: ApprovalSource = ApprovalSource.CLI,
) -> DecisionMutationResult:
    return ledger.record_decision(
        run_id,
        expected_state=WorkflowState.AWAITING_APPROVAL,
        expected_revision=awaiting.run.revision,
        decision=ApprovalDecision.APPROVED,
        source=source,
        operator="editor" if source is ApprovalSource.CLI else None,
        host="workstation" if source is ApprovalSource.CLI else None,
        terminal_present=source is ApprovalSource.CLI,
        binding_sha256=HASH_E,
        expires_at="2026-07-28T01:02:03Z",
        approval_summary="Approved",
        event_type="approval_recorded",
        event_payload={"binding_sha256": HASH_E},
    )


def _metadata(
    *,
    run_id: str = RUN_ID,
    kind: ArtifactKind = ArtifactKind.CANDIDATE,
    digest: str = HASH_C,
    size: int = 9,
) -> ArtifactMetadataV1:
    filename = "candidate.fcpxml" if kind is ArtifactKind.CANDIDATE else "diff.json"
    return ArtifactMetadataV1(
        run_id=run_id,
        kind=kind,
        relative_path=f"artifacts/{run_id}/{filename}",
        sha256=digest,
        byte_size=size,
        created_at=UTC_1,
    )


def _raw(database: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database, isolation_level=None)
    connection.row_factory = sqlite3.Row
    return connection


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def _assert_code(error: pytest.ExceptionInfo[FCPMCPError], code: ErrorCode) -> None:
    assert error.value.code is code


def _drop_triggers(
    connection: sqlite3.Connection,
    *,
    table: str,
    operation: str,
) -> None:
    rows = connection.execute(
        "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND tbl_name = ?",
        (table,),
    ).fetchall()
    matching = [
        row["name"]
        for row in rows
        if f"BEFORE {operation.upper()}" in (row["sql"] or "").upper()
    ]
    assert matching
    for name in matching:
        connection.execute(f'DROP TRIGGER "{name.replace(chr(34), chr(34) * 2)}"')


def test_constructor_does_not_touch_the_filesystem(tmp_path: Path) -> None:
    paths = _uncreated_paths(tmp_path)

    ledger = WorkflowLedger(paths, clock=TickClock(), package_version="0.3.0-test")

    assert ledger.paths is paths
    assert not paths.root.exists()


def test_constructor_requires_validated_state_paths() -> None:
    with pytest.raises(FCPMCPError) as error:
        WorkflowLedger(object())  # type: ignore[arg-type]

    _assert_code(error, ErrorCode.INVALID_ARGUMENTS)


@pytest.mark.parametrize(
    "operation",
    ("get_run", "list_runs", "list_events", "verify_integrity"),
)
def test_read_before_initialize_fails_without_creating_database(
    tmp_path: Path,
    operation: str,
) -> None:
    paths = _paths(tmp_path / operation)
    ledger = WorkflowLedger(paths, clock=TickClock(), package_version="0.3.0-test")
    assert not paths.database.exists()

    with pytest.raises(FCPMCPError) as error:
        if operation == "get_run":
            ledger.get_run(RUN_ID)
        elif operation == "list_runs":
            ledger.list_runs(limit=10)
        elif operation == "list_events":
            ledger.list_events(RUN_ID, limit=10)
        else:
            ledger.verify_integrity()

    _assert_code(error, ErrorCode.LEDGER_UNAVAILABLE)
    assert not paths.database.exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX modes are not portable")
@pytest.mark.parametrize("process_umask", [0o000, 0o077])
def test_initialize_securely_creates_exact_private_database(
    tmp_path: Path,
    process_umask: int,
) -> None:
    paths = _paths(tmp_path)
    ledger = WorkflowLedger(paths, clock=TickClock(), package_version="0.3.0-test")
    previous = os.umask(process_umask)
    try:
        ledger.initialize()
    finally:
        os.umask(previous)

    assert _mode(paths.database) == 0o600


def test_every_configured_connection_uses_explicit_python310_transaction_mode_and_pragmas(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path)

    connection = ledger._connect()
    try:
        assert connection.isolation_level is None
        assert connection.row_factory is sqlite3.Row
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    finally:
        connection.close()


def test_backup_source_destination_and_migration_connections_all_verify_pragmas(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    legacy = _raw(paths.database)
    legacy.execute("CREATE TABLE legacy(value TEXT NOT NULL)")
    legacy.execute("INSERT INTO legacy VALUES ('kept')")
    legacy.close()
    if os.name == "posix":
        os.chmod(paths.database, 0o600)

    original_connect = sqlite3.connect
    checked: list[tuple[object, ...]] = []

    class AuditedConnection(ledger_module._LedgerConnection):
        def close(self) -> None:
            if self.execute("PRAGMA database_list").fetchone() is not None:
                checked.append(
                    (
                        self.isolation_level,
                        self.execute("PRAGMA journal_mode").fetchone()[0],
                        self.execute("PRAGMA synchronous").fetchone()[0],
                        self.execute("PRAGMA foreign_keys").fetchone()[0],
                        self.execute("PRAGMA busy_timeout").fetchone()[0],
                    )
                )
            super().close()

    def audited_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        kwargs["factory"] = AuditedConnection
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(ledger_module.sqlite3, "connect", audited_connect)
    WorkflowLedger(paths, clock=TickClock(), package_version="0.3.0-test").initialize()

    assert len(checked) >= 3
    assert all(values == (None, "delete", 2, 1, 5000) for values in checked)


def test_initialize_creates_exact_six_tables_constraints_and_persistent_triggers(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path)
    connection = _raw(ledger.paths.database)
    try:
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        triggers = connection.execute(
            "SELECT name, tbl_name, sql FROM sqlite_master "
            "WHERE type = 'trigger' ORDER BY name"
        ).fetchall()
        indexes = connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'index' AND name NOT LIKE 'sqlite_autoindex_%'"
        ).fetchall()
    finally:
        connection.close()

    assert tables == {
        "schema_migrations",
        "runs",
        "events",
        "artifacts",
        "approvals",
        "idempotency_keys",
    }
    protected = {row["tbl_name"] for row in triggers}
    assert protected == {
        "schema_migrations",
        "events",
        "artifacts",
        "approvals",
        "idempotency_keys",
    }
    assert all("TEMP" not in row["sql"].upper() for row in triggers)
    assert len(triggers) == 10
    assert {row["name"] for row in indexes} >= {
        "idx_runs_created",
        "idx_runs_state_created",
    }


def test_migration_checksum_has_stable_unambiguous_serialization() -> None:
    assert (
        migration_checksum(1, "initial", ("SELECT 1", "SELECT 2"))
        == "28c57fce233ef9c2cef4dd505c80ade109d1c27278f155b32fcf85668b8f57b5"
    )
    assert MIGRATIONS == (
        Migration(
            version=1,
            name=MIGRATIONS[0].name,
            statements=MIGRATIONS[0].statements,
            checksum=MIGRATIONS[0].checksum,
        ),
    )
    assert MIGRATIONS[0].checksum == migration_checksum(
        MIGRATIONS[0].version,
        MIGRATIONS[0].name,
        MIGRATIONS[0].statements,
    )


def test_initialize_is_idempotent_and_records_package_and_canonical_time(
    tmp_path: Path,
) -> None:
    clock = TickClock()
    ledger = _ledger(tmp_path, clock=clock)

    ledger.initialize()

    connection = _raw(ledger.paths.database)
    try:
        rows = connection.execute("SELECT * FROM schema_migrations").fetchall()
    finally:
        connection.close()
    assert len(rows) == 1
    assert rows[0]["version"] == 1
    assert rows[0]["name"] == MIGRATIONS[0].name
    assert rows[0]["checksum"] == MIGRATIONS[0].checksum
    assert rows[0]["package_version"] == "0.3.0-test"
    assert rows[0]["applied_at"] == "2026-07-27T01:02:03Z"


def test_concurrent_initializers_apply_migration_once(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def initialize() -> None:
        try:
            barrier.wait()
            WorkflowLedger(
                paths,
                clock=TickClock(),
                package_version="0.3.0-test",
            ).initialize()
        except BaseException as error:  # noqa: BLE001 - test captures thread failures
            errors.append(error)

    threads = [threading.Thread(target=initialize) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
    assert all(not thread.is_alive() for thread in threads)
    connection = _raw(paths.database)
    try:
        assert connection.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == 1
    finally:
        connection.close()


def test_existing_meaningful_database_is_backed_up_before_migration(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    legacy = _raw(paths.database)
    legacy.execute("CREATE TABLE legacy(value TEXT NOT NULL)")
    legacy.execute("INSERT INTO legacy VALUES ('snapshot')")
    legacy.close()
    if os.name == "posix":
        os.chmod(paths.database, 0o600)

    ledger = WorkflowLedger(paths, clock=TickClock(), package_version="0.3.0-test")
    ledger.initialize()

    backups = list(paths.root.glob("runs.sqlite3.backup-v0-to-v1-*"))
    assert len(backups) == 1
    if os.name == "posix":
        assert _mode(backups[0]) == 0o600
    snapshot = _raw(backups[0])
    try:
        assert snapshot.execute("SELECT value FROM legacy").fetchone()[0] == "snapshot"
        assert (
            snapshot.execute(
                "SELECT count(*) FROM sqlite_master "
                "WHERE type = 'table' AND name = 'schema_migrations'"
            ).fetchone()[0]
            == 0
        )
    finally:
        snapshot.close()
    current = _raw(paths.database)
    try:
        assert current.execute("SELECT value FROM legacy").fetchone()[0] == "snapshot"
        assert current.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == 1
    finally:
        current.close()


def test_migration_backup_runs_while_the_migration_write_lock_is_held(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    legacy = _raw(paths.database)
    legacy.execute("CREATE TABLE legacy(value TEXT NOT NULL)")
    legacy.close()
    if os.name == "posix":
        os.chmod(paths.database, 0o600)
    ledger = WorkflowLedger(paths, clock=TickClock(), package_version="0.3.0-test")
    original = ledger._backup_database
    lock_errors: list[sqlite3.OperationalError] = []

    def observe_lock(
        migration_connection: sqlite3.Connection,
        *,
        from_version: int,
        to_version: int,
    ) -> Path:
        assert migration_connection.in_transaction
        contender = sqlite3.connect(
            paths.database,
            timeout=0.05,
            isolation_level=None,
        )
        try:
            contender.execute("PRAGMA busy_timeout=50")
            with pytest.raises(sqlite3.OperationalError) as error:
                contender.execute("BEGIN IMMEDIATE")
            lock_errors.append(error.value)
        finally:
            contender.close()
        return original(
            migration_connection,
            from_version=from_version,
            to_version=to_version,
        )

    monkeypatch.setattr(ledger, "_backup_database", observe_lock)

    ledger.initialize()

    assert len(lock_errors) == 1
    assert "locked" in str(lock_errors[0]).lower()


def test_brand_new_empty_database_does_not_get_a_migration_backup(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)

    assert list(ledger.paths.root.glob("runs.sqlite3.backup-*")) == []


def test_backup_failure_rolls_back_and_publishes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    connection = _raw(paths.database)
    connection.execute("CREATE TABLE legacy(value TEXT)")
    connection.close()
    if os.name == "posix":
        os.chmod(paths.database, 0o600)
    ledger = WorkflowLedger(paths, clock=TickClock(), package_version="0.3.0-test")

    def fail_backup(*args: object, **kwargs: object) -> Path:
        raise OSError("sensitive backup detail")

    monkeypatch.setattr(ledger, "_backup_database", fail_backup)
    with pytest.raises(FCPMCPError) as error:
        ledger.initialize()

    _assert_code(error, ErrorCode.LEDGER_UNAVAILABLE)
    assert error.value.__cause__ is not None
    assert "sensitive" not in error.value.message
    assert str(paths.database) not in error.value.message
    assert list(paths.root.glob("runs.sqlite3.backup-*")) == []
    current = _raw(paths.database)
    try:
        assert (
            current.execute(
                "SELECT count(*) FROM sqlite_master "
                "WHERE type = 'table' AND name = 'schema_migrations'"
            ).fetchone()[0]
            == 0
        )
    finally:
        current.close()


def test_migration_statement_failure_rolls_back_and_removes_published_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    connection = _raw(paths.database)
    connection.execute("CREATE TABLE legacy(value TEXT)")
    connection.close()
    if os.name == "posix":
        os.chmod(paths.database, 0o600)
    ledger = WorkflowLedger(paths, clock=TickClock(), package_version="0.3.0-test")
    original = ledger._execute_migration_statement
    calls = 0

    def fail_second(connection: sqlite3.Connection, statement: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise sqlite3.OperationalError("injected migration failure")
        original(connection, statement)

    monkeypatch.setattr(ledger, "_execute_migration_statement", fail_second)
    with pytest.raises(FCPMCPError) as error:
        ledger.initialize()

    _assert_code(error, ErrorCode.LEDGER_UNAVAILABLE)
    assert list(paths.root.glob("runs.sqlite3.backup-*")) == []
    current = _raw(paths.database)
    try:
        assert (
            current.execute(
                "SELECT count(*) FROM sqlite_master "
                "WHERE type = 'table' AND name = 'schema_migrations'"
            ).fetchone()[0]
            == 0
        )
        assert current.execute("SELECT count(*) FROM legacy").fetchone()[0] == 0
    finally:
        current.close()


def test_migration_failure_attaches_residual_backup_cleanup_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    connection = _raw(paths.database)
    connection.execute("CREATE TABLE legacy(value TEXT)")
    connection.close()
    if os.name == "posix":
        os.chmod(paths.database, 0o600)
    ledger = WorkflowLedger(paths, clock=TickClock(), package_version="0.3.0-test")
    original_statement = ledger._execute_migration_statement
    statement_calls = 0

    def fail_second_statement(
        connection: sqlite3.Connection,
        statement: str,
    ) -> None:
        nonlocal statement_calls
        statement_calls += 1
        if statement_calls == 2:
            raise sqlite3.OperationalError("injected migration failure")
        original_statement(connection, statement)

    original_unlink_at = ledger._unlink_at

    def fail_published_backup_unlink(anchor: object, name: str) -> None:
        if ".backup-" in name and not name.endswith(".tmp"):
            raise OSError("injected backup cleanup failure")
        original_unlink_at(anchor, name)  # type: ignore[arg-type]

    monkeypatch.setattr(ledger, "_execute_migration_statement", fail_second_statement)
    monkeypatch.setattr(
        ledger,
        "_unlink_at",
        fail_published_backup_unlink,
    )

    with pytest.raises(FCPMCPError) as error:
        ledger.initialize()

    _assert_code(error, ErrorCode.LEDGER_UNAVAILABLE)
    cause = error.value.__cause__
    assert cause is not None
    cleanup_failures = getattr(cause, "cleanup_failures", ())
    assert cleanup_failures
    assert all(len(str(failure)) <= 255 for failure in cleanup_failures)
    assert len(list(paths.root.glob("runs.sqlite3.backup-v0-to-v1-*"))) == 1
    current = _raw(paths.database)
    try:
        assert (
            current.execute(
                "SELECT count(*) FROM sqlite_master "
                "WHERE type = 'table' AND name = 'schema_migrations'"
            ).fetchone()[0]
            == 0
        )
        assert (
            current.execute(
                "SELECT count(*) FROM sqlite_master "
                "WHERE type = 'table' AND name = 'legacy'"
            ).fetchone()[0]
            == 1
        )
    finally:
        current.close()


def test_post_migration_schema_validation_failure_rolls_back_before_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    connection = _raw(paths.database)
    connection.execute("CREATE TABLE legacy(value TEXT)")
    connection.close()
    if os.name == "posix":
        os.chmod(paths.database, 0o600)
    ledger = WorkflowLedger(paths, clock=TickClock(), package_version="0.3.0-test")

    def fail_validation(connection: sqlite3.Connection) -> None:
        assert connection.in_transaction
        raise RuntimeError("injected post-migration validation failure")

    monkeypatch.setattr(ledger, "_validate_schema_objects", fail_validation)
    with pytest.raises(FCPMCPError) as error:
        ledger.initialize()

    _assert_code(error, ErrorCode.LEDGER_UNAVAILABLE)
    assert list(paths.root.glob("runs.sqlite3.backup-*")) == []
    current = _raw(paths.database)
    try:
        assert (
            current.execute(
                "SELECT count(*) FROM sqlite_master "
                "WHERE type = 'table' AND name = 'schema_migrations'"
            ).fetchone()[0]
            == 0
        )
        assert (
            current.execute(
                "SELECT count(*) FROM sqlite_master "
                "WHERE type = 'table' AND name = 'legacy'"
            ).fetchone()[0]
            == 1
        )
    finally:
        current.close()


def test_migration_never_uses_executescript(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_connect = sqlite3.connect

    class GuardedConnection(ledger_module._LedgerConnection):
        def executescript(self, sql_script: str) -> sqlite3.Cursor:
            raise AssertionError("executescript must not be called")

    def guarded_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        kwargs["factory"] = GuardedConnection
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(ledger_module.sqlite3, "connect", guarded_connect)

    _ledger(tmp_path)


def test_reinitialize_fails_closed_when_an_append_only_trigger_is_missing(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path)
    connection = _raw(ledger.paths.database)
    connection.execute("DROP TRIGGER events_no_update")
    connection.close()

    with pytest.raises(FCPMCPError) as error:
        WorkflowLedger(
            ledger.paths,
            clock=TickClock(),
            package_version="0.3.0-test",
        ).initialize()

    _assert_code(error, ErrorCode.LEDGER_UNAVAILABLE)


@pytest.mark.parametrize(
    ("trigger_name", "table", "operation"),
    [
        ("schema_migrations_no_update", "schema_migrations", "UPDATE"),
        ("schema_migrations_no_delete", "schema_migrations", "DELETE"),
        ("events_no_update", "events", "UPDATE"),
        ("events_no_delete", "events", "DELETE"),
        ("artifacts_no_update", "artifacts", "UPDATE"),
        ("artifacts_no_delete", "artifacts", "DELETE"),
        ("approvals_no_update", "approvals", "UPDATE"),
        ("approvals_no_delete", "approvals", "DELETE"),
        ("idempotency_keys_no_update", "idempotency_keys", "UPDATE"),
        ("idempotency_keys_no_delete", "idempotency_keys", "DELETE"),
    ],
)
def test_same_name_noop_trigger_substitution_is_detected(
    tmp_path: Path,
    trigger_name: str,
    table: str,
    operation: str,
) -> None:
    ledger = _ledger(tmp_path)
    connection = _raw(ledger.paths.database)
    try:
        connection.execute(f'DROP TRIGGER "{trigger_name}"')
        connection.execute(
            f'CREATE TRIGGER "{trigger_name}" BEFORE {operation} ON "{table}" '
            "BEGIN SELECT 1; END"
        )
    finally:
        connection.close()

    result = ledger.verify_integrity()
    assert result.valid is False
    assert {finding.code for finding in result.findings} >= {"schema_mismatch"}

    with pytest.raises(FCPMCPError) as error:
        ledger.initialize()
    _assert_code(error, ErrorCode.LEDGER_UNAVAILABLE)


def test_same_name_weak_required_table_substitution_is_detected(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path)
    connection = _raw(ledger.paths.database)
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("CREATE TABLE saved_runs AS SELECT * FROM runs")
        connection.execute("DROP TABLE runs")
        connection.execute("CREATE TABLE runs AS SELECT * FROM saved_runs")
        connection.execute("DROP TABLE saved_runs")
    finally:
        connection.close()

    result = ledger.verify_integrity()
    assert result.valid is False
    assert {finding.code for finding in result.findings} >= {"schema_mismatch"}

    with pytest.raises(FCPMCPError) as error:
        ledger.initialize()
    _assert_code(error, ErrorCode.LEDGER_UNAVAILABLE)


def test_unexpected_trigger_or_index_on_required_table_is_detected(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path)
    connection = _raw(ledger.paths.database)
    try:
        connection.execute(
            "CREATE TRIGGER runs_shadow_guard BEFORE UPDATE ON runs "
            "BEGIN SELECT 1; END"
        )
        connection.execute("CREATE INDEX runs_shadow_index ON runs(updated_at)")
    finally:
        connection.close()

    result = ledger.verify_integrity()
    assert result.valid is False
    assert {finding.code for finding in result.findings} >= {"schema_mismatch"}


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("name", "attacker"),
        ("checksum", "f" * 64),
    ],
)
def test_stored_migration_identity_mismatch_blocks_startup(
    tmp_path: Path,
    column: str,
    value: str,
) -> None:
    ledger = _ledger(tmp_path)
    connection = _raw(ledger.paths.database)
    try:
        _drop_triggers(connection, table="schema_migrations", operation="update")
        connection.execute(f"UPDATE schema_migrations SET {column} = ?", (value,))
    finally:
        connection.close()

    with pytest.raises(FCPMCPError) as error:
        WorkflowLedger(
            ledger.paths,
            clock=TickClock(),
            package_version="0.3.0-test",
        ).initialize()

    _assert_code(error, ErrorCode.LEDGER_UNAVAILABLE)
    assert error.value.__cause__ is not None


@pytest.mark.parametrize("versions", [(2,), (1, 3)])
def test_unknown_future_or_version_gap_blocks_startup(
    tmp_path: Path,
    versions: tuple[int, ...],
) -> None:
    paths = _paths(tmp_path)
    connection = _raw(paths.database)
    connection.execute(
        "CREATE TABLE schema_migrations("
        "version INTEGER PRIMARY KEY, name TEXT NOT NULL, checksum TEXT NOT NULL, "
        "package_version TEXT NOT NULL, applied_at TEXT NOT NULL)"
    )
    for version in versions:
        connection.execute(
            "INSERT INTO schema_migrations VALUES (?, ?, ?, ?, ?)",
            (version, "future", "f" * 64, "future", "2026-07-27T00:00:00Z"),
        )
    connection.close()
    if os.name == "posix":
        os.chmod(paths.database, 0o600)

    with pytest.raises(FCPMCPError) as error:
        WorkflowLedger(
            paths,
            clock=TickClock(),
            package_version="0.3.0-test",
        ).initialize()

    _assert_code(error, ErrorCode.LEDGER_UNAVAILABLE)


def test_database_symlink_substitution_is_rejected_before_connect(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    outside = tmp_path / "outside.sqlite3"
    outside.write_bytes(b"not sqlite")
    paths.database.symlink_to(outside)
    ledger = WorkflowLedger(paths, clock=TickClock(), package_version="0.3.0-test")

    with pytest.raises(FCPMCPError) as error:
        ledger.initialize()

    _assert_code(error, ErrorCode.LEDGER_UNAVAILABLE)
    assert str(paths.database) not in error.value.message
    assert outside.read_bytes() == b"not sqlite"


def test_database_symlink_substitution_between_check_and_connect_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    outside = tmp_path / "outside.sqlite3"
    external = _raw(outside)
    external.execute("CREATE TABLE sentinel(value TEXT)")
    external.close()
    if os.name == "posix":
        os.chmod(outside, 0o600)
    original_connect = sqlite3.connect
    swapped = False

    def swapping_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        nonlocal swapped
        if not swapped:
            paths.database.unlink()
            paths.database.symlink_to(outside)
            swapped = True
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(ledger_module.sqlite3, "connect", swapping_connect)

    with pytest.raises(FCPMCPError) as error:
        WorkflowLedger(
            paths,
            clock=TickClock(),
            package_version="0.3.0-test",
        ).initialize()

    _assert_code(error, ErrorCode.LEDGER_UNAVAILABLE)
    assert paths.database.is_symlink()
    external = _raw(outside)
    try:
        assert (
            external.execute(
                "SELECT count(*) FROM sqlite_master "
                "WHERE type = 'table' AND name = 'schema_migrations'"
            ).fetchone()[0]
            == 0
        )
        assert (
            external.execute(
                "SELECT count(*) FROM sqlite_master "
                "WHERE type = 'table' AND name = 'sentinel'"
            ).fetchone()[0]
            == 1
        )
    finally:
        external.close()


def test_repeated_database_creation_race_is_bounded_and_coded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    ledger = WorkflowLedger(
        paths,
        clock=TickClock(),
        package_version="0.3.0-test",
    )
    attempts = 0

    def racing_publish(
        anchor: object,
        source: str,
        target: str,
    ) -> None:
        del anchor, source
        nonlocal attempts
        if target == paths.database.name:
            attempts += 1
            raise FileExistsError("injected create race")

    monkeypatch.setattr(ledger, "_link_at", racing_publish)

    with pytest.raises(FCPMCPError) as error:
        ledger.initialize()

    _assert_code(error, ErrorCode.LEDGER_UNAVAILABLE)
    assert type(error.value.__cause__) is FileExistsError
    assert attempts == 3
    assert str(paths.database) not in error.value.message
    assert not paths.database.exists()


def test_database_wrong_filesystem_type_is_rejected_before_connect(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.database.mkdir()

    with pytest.raises(FCPMCPError) as error:
        WorkflowLedger(
            paths,
            clock=TickClock(),
            package_version="0.3.0-test",
        ).initialize()

    _assert_code(error, ErrorCode.LEDGER_UNAVAILABLE)


@pytest.mark.skipif(os.name != "posix", reason="POSIX modes are not portable")
def test_unsafe_existing_database_mode_is_rejected_without_chmod(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.database.write_bytes(b"")
    os.chmod(paths.database, 0o644)

    with pytest.raises(FCPMCPError) as error:
        WorkflowLedger(
            paths,
            clock=TickClock(),
            package_version="0.3.0-test",
        ).initialize()

    _assert_code(error, ErrorCode.LEDGER_UNAVAILABLE)
    assert _mode(paths.database) == 0o644


def test_preexisting_empty_database_is_rejected_without_rewriting(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    paths.database.write_bytes(b"")
    if os.name == "posix":
        os.chmod(paths.database, 0o600)

    with pytest.raises(FCPMCPError) as error:
        WorkflowLedger(
            paths,
            clock=TickClock(),
            package_version="0.3.0-test",
        ).initialize()

    _assert_code(error, ErrorCode.LEDGER_UNAVAILABLE)
    assert paths.database.read_bytes() == b""


@pytest.mark.skipif(os.name != "posix", reason="POSIX modes are not portable")
def test_direct_state_paths_with_world_accessible_root_are_rejected(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    root.mkdir(mode=0o777)
    os.chmod(root, 0o777)
    paths = StatePaths(
        root=root,
        database=root / "runs.sqlite3",
        artifacts=root / "artifacts",
        locks=root / "locks",
    )

    with pytest.raises(FCPMCPError) as error:
        WorkflowLedger(
            paths,
            clock=TickClock(),
            package_version="0.3.0-test",
        ).initialize()

    _assert_code(error, ErrorCode.LEDGER_UNAVAILABLE)
    assert _mode(root) == 0o777
    assert not paths.database.exists()


@pytest.mark.skipif(os.name != "posix", reason="requires descriptor-relative open")
def test_root_substitution_before_database_open_cannot_redirect_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    parked_root = tmp_path / "parked-state"
    outside_root = tmp_path / "outside"
    outside_root.mkdir(mode=0o700)
    os.chmod(outside_root, 0o700)
    original_open = os.open
    swapped = False

    def swapping_open(path: object, *args: object, **kwargs: object) -> int:
        nonlocal swapped
        if not swapped and Path(path).name == "runs.sqlite3":
            os.replace(paths.root, parked_root)
            paths.root.symlink_to(outside_root, target_is_directory=True)
            swapped = True
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(ledger_module.os, "open", swapping_open)
    try:
        with pytest.raises(FCPMCPError) as error:
            WorkflowLedger(
                paths,
                clock=TickClock(),
                package_version="0.3.0-test",
            ).initialize()
        _assert_code(error, ErrorCode.LEDGER_UNAVAILABLE)
        assert not (outside_root / "runs.sqlite3").exists()
    finally:
        if paths.root.is_symlink():
            paths.root.unlink()
        if parked_root.exists():
            os.replace(parked_root, paths.root)


@pytest.mark.skipif(os.name != "posix", reason="requires hard-link race harness")
def test_empty_database_connect_redirection_cannot_receive_migrations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    outside = tmp_path / "outside.sqlite3"
    displaced = tmp_path / "outside-opened.sqlite3"
    outside.write_bytes(b"")
    os.chmod(outside, 0o600)
    original_connect = sqlite3.connect
    redirected = False

    def redirecting_connect(
        database: object,
        *args: object,
        **kwargs: object,
    ) -> sqlite3.Connection:
        nonlocal redirected
        if redirected:
            return original_connect(database, *args, **kwargs)
        redirected = True
        connection = original_connect(
            f"{outside.as_uri()}?mode=rw",
            *args,
            **kwargs,
        )
        os.replace(outside, displaced)
        os.link(paths.database, outside)
        return connection

    monkeypatch.setattr(ledger_module.sqlite3, "connect", redirecting_connect)

    with pytest.raises(FCPMCPError) as error:
        WorkflowLedger(
            paths,
            clock=TickClock(),
            package_version="0.3.0-test",
        ).initialize()

    _assert_code(error, ErrorCode.LEDGER_UNAVAILABLE)
    assert displaced.read_bytes() == b""
    assert os.path.samefile(paths.database, outside)


def test_embedded_bootstrap_template_has_exact_provenance_and_shape(
    tmp_path: Path,
) -> None:
    image = ledger_module._BOOTSTRAP_IMAGE
    placeholder = ledger_module._BOOTSTRAP_PLACEHOLDER
    token_offset = ledger_module._BOOTSTRAP_TOKEN_OFFSET
    assert len(image) == 8192
    assert image.startswith(b"SQLite format 3\x00")
    assert image[16:18] == b"\x10\x00"
    assert image.count(placeholder) == 1
    assert image[token_offset : token_offset + len(placeholder)] == placeholder

    database = tmp_path / "bootstrap.sqlite3"
    database.write_bytes(image)
    connection = _raw(database)
    try:
        objects = connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master"
        ).fetchall()
        tokens = connection.execute(
            "SELECT token FROM __fcp_ledger_identity"
        ).fetchall()
    finally:
        connection.close()

    expected_schema = (
        "CREATE TABLE __fcp_ledger_identity("
        "token TEXT NOT NULL CHECK(length(token)=64))"
    )
    assert [tuple(row) for row in objects] == [
        (
            "table",
            "__fcp_ledger_identity",
            "__fcp_ledger_identity",
            expected_schema,
        )
    ]
    assert [row["token"] for row in tokens] == ["0" * 64]


def test_bootstrap_partial_write_is_never_published(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    ledger = WorkflowLedger(paths, clock=TickClock(), package_version="0.3.0-test")

    def interrupt_write(descriptor: int, image: bytes) -> None:
        assert len(image) == 8192
        os.write(descriptor, image[:100])
        raise OSError("injected bootstrap write interruption")

    monkeypatch.setattr(
        ledger,
        "_write_bootstrap_image",
        interrupt_write,
        raising=False,
    )

    with pytest.raises(FCPMCPError) as error:
        ledger.initialize()

    _assert_code(error, ErrorCode.LEDGER_UNAVAILABLE)
    assert not paths.database.exists()
    assert not any(
        path.name.startswith(".runs.sqlite3.bootstrap-")
        for path in paths.root.iterdir()
    )


def test_foreign_bootstrap_identity_is_rejected_without_rewrite(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    image = bytearray(ledger_module._BOOTSTRAP_IMAGE)
    image[ledger_module._BOOTSTRAP_TOKEN_OFFSET] = ord("g")
    paths.database.write_bytes(image)
    if os.name == "posix":
        os.chmod(paths.database, 0o600)
    before = paths.database.read_bytes()

    with pytest.raises(FCPMCPError) as error:
        WorkflowLedger(
            paths,
            clock=TickClock(),
            package_version="0.3.0-test",
        ).initialize()

    _assert_code(error, ErrorCode.LEDGER_UNAVAILABLE)
    assert paths.database.read_bytes() == before


def test_stale_bootstrap_publish_hardlink_is_cleaned_before_sqlite_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    first = WorkflowLedger(paths, clock=TickClock(), package_version="0.3.0-test")
    original_unlink = getattr(first, "_unlink_at", None)

    def interrupt_temp_unlink(anchor: object, name: str) -> None:
        if name.startswith(".runs.sqlite3.bootstrap-"):
            raise OSError("injected post-publish cleanup interruption")
        assert original_unlink is not None
        original_unlink(anchor, name)

    monkeypatch.setattr(first, "_unlink_at", interrupt_temp_unlink, raising=False)
    with pytest.raises(FCPMCPError) as error:
        first.initialize()
    _assert_code(error, ErrorCode.LEDGER_UNAVAILABLE)
    stale = [
        path
        for path in paths.root.iterdir()
        if path.name.startswith(".runs.sqlite3.bootstrap-")
    ]
    assert len(stale) == 1
    assert os.path.samefile(paths.database, stale[0])

    monkeypatch.undo()
    observed_connect_entries: list[set[str]] = []
    original_connect = sqlite3.connect

    def observe_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        observed_connect_entries.append(
            {path.name for path in paths.root.iterdir()}
        )
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(ledger_module.sqlite3, "connect", observe_connect)
    WorkflowLedger(
        paths,
        clock=TickClock(),
        package_version="0.3.0-test",
    ).initialize()

    assert observed_connect_entries
    assert all(
        not any(name.startswith(".runs.sqlite3.bootstrap-") for name in entries)
        for entries in observed_connect_entries
    )
    assert not any(
        path.name.startswith(".runs.sqlite3.bootstrap-")
        for path in paths.root.iterdir()
    )


def test_create_run_writes_sequence_one_with_exact_canonical_payload_and_hash(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path)

    result = ledger.create_run(
        run_id=RUN_ID,
        graph_version="1",
        run_version="1",
        profile=Profile.WORKFLOW,
        approval_mode=ApprovalMode.CLI,
        source_path="/private/input.fcpxml",
        destination_path="/private/output.fcpxml",
        event_type="run_created",
        event_payload={"z": 1, "a": "café"},
    )

    assert result.existing is False
    assert result.run.run_id == RUN_ID
    assert result.run.revision == 1
    events = ledger.list_events(RUN_ID, limit=10)
    assert len(events) == 1
    event = events[0]
    assert event.sequence == 1
    assert event.payload_text == '{"a":"café","z":1}'
    assert event.previous_hash == "0" * 64
    expected = hashlib.sha256(
        canonical_json(
            {
                "run_id": RUN_ID,
                "sequence": 1,
                "event_type": "run_created",
                "payload": {"a": "café", "z": 1},
                "timestamp": "2026-07-27T01:02:04Z",
                "elapsed_ms": None,
                "previous_hash": "0" * 64,
            }
        )
    ).hexdigest()
    assert event.event_hash == expected


def test_sequences_are_contiguous_and_independent_per_run(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    _create(ledger, run_id=RUN_ID)
    _create(ledger, run_id=RUN_ID_2)

    first = ledger.append_event(
        RUN_ID,
        expected_state=WorkflowState.PREPARING,
        expected_revision=1,
        event_type="source_inspected",
        payload={
            "source_sha256": HASH_A,
            "prior_destination_state": PriorDestinationState.ABSENT,
            "prior_destination_sha256": None,
        },
        elapsed_ms=7,
        projection_patch={
            "source_sha256": HASH_A,
            "prior_destination_state": PriorDestinationState.ABSENT,
            "prior_destination_sha256": None,
        },
    )
    second = ledger.append_event(
        RUN_ID,
        expected_state=WorkflowState.PREPARING,
        expected_revision=2,
        event_type="plan_built",
        payload={"plan_sha256": HASH_B},
        projection_patch={"plan_sha256": HASH_B},
    )

    assert isinstance(first, EventMutationResult)
    assert first.run.revision == 2
    assert second.run.revision == 3
    assert [event.sequence for event in ledger.list_events(RUN_ID, limit=10)] == [1, 2, 3]
    assert [event.sequence for event in ledger.list_events(RUN_ID_2, limit=10)] == [1]
    assert (
        ledger.list_events(RUN_ID, limit=10)[1].previous_hash
        == ledger.list_events(RUN_ID, limit=10)[0].event_hash
    )


def test_append_event_projection_event_and_revision_are_one_atomic_change(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path)
    _create(ledger)
    connection = _raw(ledger.paths.database)
    connection.execute(
        "CREATE TRIGGER reject_injected_event BEFORE INSERT ON events "
        "WHEN NEW.event_type = 'source_inspected' "
        "BEGIN SELECT RAISE(ABORT, 'injected'); END"
    )
    connection.close()

    with pytest.raises(FCPMCPError) as error:
        ledger.append_event(
            RUN_ID,
            expected_state=WorkflowState.PREPARING,
            expected_revision=1,
            event_type="source_inspected",
            payload={
                "source_sha256": HASH_A,
                "prior_destination_state": PriorDestinationState.ABSENT,
                "prior_destination_sha256": None,
            },
            projection_patch={
                "source_sha256": HASH_A,
                "prior_destination_state": PriorDestinationState.ABSENT,
                "prior_destination_sha256": None,
            },
        )

    _assert_code(error, ErrorCode.LEDGER_UNAVAILABLE)
    run = ledger.get_run(RUN_ID)
    assert run is not None
    assert run.revision == 1
    assert run.source_sha256 is None
    assert len(ledger.list_events(RUN_ID, limit=10)) == 1


def test_transition_validates_legal_edge_and_compare_and_set(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    _create(ledger)

    mutation = _complete_prepare(ledger)

    assert isinstance(mutation, EventMutationResult)
    assert mutation.run.state is WorkflowState.AWAITING_APPROVAL
    assert mutation.run.revision == 6
    with pytest.raises(FCPMCPError) as stale_revision:
        ledger.transition(
            RUN_ID,
            expected_state=WorkflowState.AWAITING_APPROVAL,
            expected_revision=1,
            target_state=WorkflowState.APPROVED,
            event_type="wrong",
            payload={},
        )
    _assert_code(stale_revision, ErrorCode.WORKFLOW_STATE_CONFLICT)
    with pytest.raises(FCPMCPError) as stale_state:
        ledger.append_event(
            RUN_ID,
            expected_state=WorkflowState.PREPARING,
            expected_revision=mutation.run.revision,
            event_type="wrong",
            payload={},
        )
    _assert_code(stale_state, ErrorCode.WORKFLOW_STATE_CONFLICT)
    with pytest.raises(FCPMCPError) as illegal:
        ledger.transition(
            RUN_ID,
            expected_state=WorkflowState.AWAITING_APPROVAL,
            expected_revision=mutation.run.revision,
            target_state=WorkflowState.COMMITTED,
            event_type="wrong",
            payload={},
        )
    _assert_code(illegal, ErrorCode.WORKFLOW_STATE_CONFLICT)


def test_record_artifact_atomically_updates_projection_and_appends_event(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path)
    _create(ledger)
    metadata = _metadata()

    result = ledger.record_artifact(
        metadata,
        expected_state=WorkflowState.PREPARING,
        expected_revision=1,
        event_type="candidate_stored",
        event_payload={"sha256": HASH_C, "byte_size": 9},
    )

    assert isinstance(result, ArtifactMutationResult)
    assert result.artifact.sha256 == HASH_C
    assert result.run.candidate_sha256 == HASH_C
    assert result.run.candidate_size_bytes == 9
    assert result.run.revision == 2
    assert result.event.sequence == 2
    connection = _raw(ledger.paths.database)
    try:
        assert connection.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 1
    finally:
        connection.close()


def test_record_artifact_rolls_back_projection_when_event_insert_fails(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path)
    _create(ledger)
    connection = _raw(ledger.paths.database)
    connection.execute(
        "CREATE TRIGGER reject_artifact_event BEFORE INSERT ON events "
        "WHEN NEW.event_type = 'reject_artifact' "
        "BEGIN SELECT RAISE(ABORT, 'injected'); END"
    )
    connection.close()

    with pytest.raises(FCPMCPError):
        ledger.record_artifact(
            _metadata(),
            expected_state=WorkflowState.PREPARING,
            expected_revision=1,
            event_type="reject_artifact",
            event_payload={},
        )

    run = ledger.get_run(RUN_ID)
    assert run is not None
    assert run.revision == 1
    assert run.candidate_sha256 is None
    connection = _raw(ledger.paths.database)
    try:
        assert connection.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 0
    finally:
        connection.close()


def test_record_decision_is_one_approval_transition_event_transaction(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path)
    _create(ledger)
    awaiting = _complete_prepare(ledger)

    result = ledger.record_decision(
        RUN_ID,
        expected_state=WorkflowState.AWAITING_APPROVAL,
        expected_revision=awaiting.run.revision,
        decision=ApprovalDecision.APPROVED,
        source=ApprovalSource.CLI,
        operator="editor",
        host="workstation",
        terminal_present=True,
        binding_sha256=HASH_D,
        expires_at="2026-07-28T01:02:03Z",
        approval_summary="Approved in local terminal",
        event_type="approval_recorded",
        event_payload={"binding_sha256": HASH_D},
    )

    assert isinstance(result, DecisionMutationResult)
    assert result.run.state is WorkflowState.APPROVED
    assert result.run.revision == awaiting.run.revision + 1
    assert result.run.approval_decision is ApprovalDecision.APPROVED
    assert result.approval.operator == "editor"
    assert result.approval.terminal_present is True
    assert result.event.sequence == 7
    with pytest.raises(FCPMCPError) as duplicate:
        ledger.record_decision(
            RUN_ID,
            expected_state=WorkflowState.APPROVED,
            expected_revision=result.run.revision,
            decision=ApprovalDecision.REJECTED,
            source=ApprovalSource.CLI,
            operator=None,
            host=None,
            terminal_present=True,
            binding_sha256=HASH_D,
            expires_at=None,
            approval_summary="changed",
            event_type="approval_changed",
            event_payload={},
        )
    _assert_code(duplicate, ErrorCode.WORKFLOW_STATE_CONFLICT)


def test_approved_and_terminal_runs_cannot_mutate_prepare_evidence(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path)
    _create(ledger)
    approved = _approve(ledger, _complete_prepare(ledger))

    with pytest.raises(FCPMCPError) as prepare_patch:
        ledger.append_event(
            RUN_ID,
            expected_state=WorkflowState.APPROVED,
            expected_revision=approved.run.revision,
            event_type="plan_built",
            payload={"sha256": HASH_A},
            projection_patch={
                "plan_sha256": HASH_A,
                "committed_at": "2026-07-27T02:02:03Z",
            },
        )
    _assert_code(prepare_patch, ErrorCode.WORKFLOW_STATE_CONFLICT)

    cancelled = ledger.transition(
        RUN_ID,
        expected_state=WorkflowState.APPROVED,
        expected_revision=approved.run.revision,
        target_state=WorkflowState.CANCELLED,
        event_type="cancelled",
        payload={},
    )
    with pytest.raises(FCPMCPError) as terminal_append:
        ledger.append_event(
            RUN_ID,
            expected_state=WorkflowState.CANCELLED,
            expected_revision=cancelled.run.revision,
            event_type="late_event",
            payload={},
        )
    _assert_code(terminal_append, ErrorCode.WORKFLOW_STATE_CONFLICT)

    stored = ledger.get_run(RUN_ID)
    assert stored is not None
    assert stored.plan_sha256 == HASH_B
    assert stored.committed_at is None
    assert stored.revision == cancelled.run.revision


@pytest.mark.parametrize(
    "state",
    [WorkflowState.AWAITING_APPROVAL, WorkflowState.APPROVED],
)
def test_artifacts_cannot_be_recorded_after_prepare(
    tmp_path: Path,
    state: WorkflowState,
) -> None:
    ledger = _ledger(tmp_path)
    _create(ledger)
    awaiting = _complete_prepare(ledger)
    revision = awaiting.run.revision
    if state is WorkflowState.APPROVED:
        decision = _approve(ledger, awaiting)
        revision = decision.run.revision

    with pytest.raises(FCPMCPError) as error:
        ledger.record_artifact(
            _metadata(),
            expected_state=state,
            expected_revision=revision,
            event_type="candidate_stored",
            event_payload={},
        )
    _assert_code(error, ErrorCode.WORKFLOW_STATE_CONFLICT)
    assert ledger.get_run(RUN_ID).revision == revision  # type: ignore[union-attr]


def test_integrity_detects_projection_and_approval_phase_inconsistency(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path)
    _create(ledger)
    _approve(ledger, _complete_prepare(ledger))
    connection = _raw(ledger.paths.database)
    try:
        connection.execute(
            "UPDATE runs SET plan_sha256 = ?, committed_at = ? WHERE run_id = ?",
            (HASH_A, "2026-07-27T02:02:03Z", RUN_ID),
        )
    finally:
        connection.close()

    result = ledger.verify_integrity(RUN_ID)

    assert result.valid is False
    assert {finding.code for finding in result.findings} >= {
        "projection_invariant",
    }


def test_append_only_triggers_reject_update_and_delete(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path)
    _create(ledger, key="key-1", request_sha256=HASH_A)
    awaiting = _complete_prepare(ledger)
    ledger.record_decision(
        RUN_ID,
        expected_state=WorkflowState.AWAITING_APPROVAL,
        expected_revision=awaiting.run.revision,
        decision=ApprovalDecision.REJECTED,
        source=ApprovalSource.CLI,
        operator=None,
        host=None,
        terminal_present=False,
        binding_sha256=HASH_D,
        expires_at=None,
        approval_summary="Rejected",
        event_type="rejected",
        event_payload={},
    )
    connection = _raw(ledger.paths.database)
    statements = (
        ("UPDATE events SET event_type = event_type", ()),
        ("DELETE FROM events", ()),
        ("UPDATE artifacts SET sha256 = sha256", ()),
        ("DELETE FROM artifacts", ()),
        ("UPDATE approvals SET decision = decision", ()),
        ("DELETE FROM approvals", ()),
        ("UPDATE schema_migrations SET checksum = checksum", ()),
        ("DELETE FROM schema_migrations", ()),
        ("UPDATE idempotency_keys SET request_sha256 = request_sha256", ()),
        ("DELETE FROM idempotency_keys", ()),
    )
    try:
        for statement, parameters in statements:
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(statement, parameters)
    finally:
        connection.close()


def test_create_run_idempotency_same_request_returns_original_without_orphan(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path)
    first = _create(ledger, run_id=RUN_ID, key="prepare-1", request_sha256=HASH_A)

    second = _create(
        ledger,
        run_id=RUN_ID_2,
        key="prepare-1",
        request_sha256=HASH_A,
    )

    assert first.existing is False
    assert second.existing is True
    assert second.run.run_id == RUN_ID
    assert ledger.get_run(RUN_ID_2) is None
    assert [run.run_id for run in ledger.list_runs(limit=10)] == [RUN_ID]


def test_create_run_idempotency_different_request_conflicts(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    _create(ledger, key="prepare-1", request_sha256=HASH_A)

    with pytest.raises(FCPMCPError) as error:
        _create(
            ledger,
            run_id=RUN_ID_2,
            key="prepare-1",
            request_sha256=HASH_B,
        )

    _assert_code(error, ErrorCode.IDEMPOTENCY_CONFLICT)
    assert ledger.get_run(RUN_ID_2) is None


def test_create_run_and_idempotency_reservation_roll_back_together(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path)
    connection = _raw(ledger.paths.database)
    connection.execute(
        "CREATE TRIGGER reject_key BEFORE INSERT ON idempotency_keys "
        "WHEN NEW.key = 'reject' "
        "BEGIN SELECT RAISE(ABORT, 'injected'); END"
    )
    connection.close()

    with pytest.raises(FCPMCPError) as error:
        _create(ledger, key="reject", request_sha256=HASH_A)

    _assert_code(error, ErrorCode.LEDGER_UNAVAILABLE)
    assert ledger.get_run(RUN_ID) is None
    assert ledger.list_runs(limit=10) == ()


def test_concurrent_same_key_same_request_creates_exactly_one_run(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    WorkflowLedger(paths, clock=TickClock(), package_version="0.3.0-test").initialize()
    barrier = threading.Barrier(2)
    results: list[IdempotencyResult] = []
    errors: list[BaseException] = []

    def create(run_id: str) -> None:
        ledger = WorkflowLedger(paths, clock=TickClock(), package_version="0.3.0-test")
        try:
            barrier.wait()
            results.append(
                _create(
                    ledger,
                    run_id=run_id,
                    key="shared",
                    request_sha256=HASH_A,
                )
            )
        except BaseException as error:  # noqa: BLE001 - test captures thread failures
            errors.append(error)

    threads = [
        threading.Thread(target=create, args=(RUN_ID,)),
        threading.Thread(target=create, args=(RUN_ID_2,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
    assert len(results) == 2
    assert {result.existing for result in results} == {False, True}
    assert len({result.run.run_id for result in results}) == 1
    verifier = WorkflowLedger(paths, clock=TickClock(), package_version="0.3.0-test")
    assert len(verifier.list_runs(limit=10)) == 1


def test_reserve_idempotency_key_is_audited_cas_and_prepare_only(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path)
    _create(ledger)

    first = ledger.reserve_idempotency_key(
        "later-key",
        HASH_A,
        RUN_ID,
        expected_state=WorkflowState.PREPARING,
        expected_revision=1,
        event_type="idempotency_reserved",
        event_payload={"key": "later-key"},
    )
    second = ledger.reserve_idempotency_key(
        "later-key",
        HASH_A,
        RUN_ID,
        expected_state=WorkflowState.PREPARING,
        expected_revision=1,
        event_type="idempotency_reserved",
        event_payload={"key": "later-key"},
    )

    assert first.existing is False
    assert first.run.revision == 2
    assert len(ledger.list_events(RUN_ID, limit=10)) == 2
    assert second.existing is True
    assert second.run.run_id == RUN_ID
    with pytest.raises(FCPMCPError) as conflict:
        ledger.reserve_idempotency_key(
            "later-key",
            HASH_B,
            RUN_ID,
            expected_state=WorkflowState.PREPARING,
            expected_revision=2,
            event_type="idempotency_reserved",
            event_payload={},
        )
    _assert_code(conflict, ErrorCode.IDEMPOTENCY_CONFLICT)
    with pytest.raises(FCPMCPError) as too_long:
        ledger.reserve_idempotency_key(
            "x" * 129,
            HASH_A,
            RUN_ID,
            expected_state=WorkflowState.PREPARING,
            expected_revision=2,
            event_type="idempotency_reserved",
            event_payload={},
        )
    _assert_code(too_long, ErrorCode.INVALID_ARGUMENTS)

    awaiting = _complete_prepare(ledger, revision=2)
    with pytest.raises(FCPMCPError) as wrong_phase:
        ledger.reserve_idempotency_key(
            "late-key",
            HASH_A,
            RUN_ID,
            expected_state=WorkflowState.AWAITING_APPROVAL,
            expected_revision=awaiting.run.revision,
            event_type="idempotency_reserved",
            event_payload={},
        )
    _assert_code(wrong_phase, ErrorCode.WORKFLOW_STATE_CONFLICT)


def test_real_concurrent_writer_serializes_then_commits(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    _create(ledger)
    holder = _raw(ledger.paths.database)
    holder.execute("PRAGMA busy_timeout = 5000")
    holder.execute("BEGIN IMMEDIATE")
    result: list[EventMutationResult] = []
    errors: list[BaseException] = []

    def append() -> None:
        try:
            result.append(
                ledger.append_event(
                    RUN_ID,
                    expected_state=WorkflowState.PREPARING,
                    expected_revision=1,
                    event_type="serialized",
                    payload={},
                )
            )
        except BaseException as error:  # noqa: BLE001 - test captures thread failures
            errors.append(error)

    thread = threading.Thread(target=append)
    thread.start()
    time.sleep(0.2)
    assert thread.is_alive()
    holder.execute("COMMIT")
    holder.close()
    thread.join(timeout=10)

    assert errors == []
    assert result[0].run.revision == 2


def test_real_busy_timeout_reports_failure_without_false_commit(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    _create(ledger)
    holder = _raw(ledger.paths.database)
    holder.execute("BEGIN IMMEDIATE")
    started = time.monotonic()
    try:
        with pytest.raises(FCPMCPError) as error:
            ledger.append_event(
                RUN_ID,
                expected_state=WorkflowState.PREPARING,
                expected_revision=1,
                event_type="must_not_commit",
                payload={},
            )
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    elapsed = time.monotonic() - started

    _assert_code(error, ErrorCode.LEDGER_UNAVAILABLE)
    assert error.value.__cause__ is not None
    assert elapsed >= 4.5
    run = ledger.get_run(RUN_ID)
    assert run is not None and run.revision == 1
    assert [event.event_type for event in ledger.list_events(RUN_ID, limit=10)] == [
        "run_created"
    ]


def test_integrity_verifier_accepts_valid_chains_and_is_read_only(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    _create(ledger)
    ledger.append_event(
        RUN_ID,
        expected_state=WorkflowState.PREPARING,
        expected_revision=1,
        event_type="milestone",
        payload={"sha256": HASH_A},
        elapsed_ms=0,
    )
    before = (
        ledger.paths.database.read_bytes(),
        ledger.paths.database.stat().st_mtime_ns,
    )

    result = ledger.verify_integrity()

    after = (
        ledger.paths.database.read_bytes(),
        ledger.paths.database.stat().st_mtime_ns,
    )
    assert isinstance(result, IntegrityResult)
    assert result.valid is True
    assert result.checked_migrations == 1
    assert result.checked_runs == 1
    assert result.checked_events == 2
    assert result.findings == ()
    assert after == before


def test_integrity_verifier_uses_one_snapshot_during_concurrent_append(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = _ledger(tmp_path)
    _create(ledger)
    original_connect = sqlite3.connect
    event_select_entered = threading.Event()
    resume_event_select = threading.Event()
    paused = False

    class PausingConnection(ledger_module._LedgerConnection):
        def execute(
            self,
            sql: str,
            parameters: object = (),
        ) -> sqlite3.Cursor:
            nonlocal paused
            if (
                not paused
                and "FROM events WHERE run_id = ? ORDER BY sequence ASC" in sql
            ):
                paused = True
                event_select_entered.set()
                assert resume_event_select.wait(timeout=10)
            return super().execute(sql, parameters)

    def pausing_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        kwargs["factory"] = PausingConnection
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(ledger_module.sqlite3, "connect", pausing_connect)
    integrity_results: list[IntegrityResult] = []
    append_results: list[EventMutationResult] = []
    errors: list[BaseException] = []

    def verify() -> None:
        try:
            integrity_results.append(ledger.verify_integrity(RUN_ID))
        except BaseException as error:  # noqa: BLE001 - captures thread failure
            errors.append(error)

    def append() -> None:
        try:
            append_results.append(
                ledger.append_event(
                    RUN_ID,
                    expected_state=WorkflowState.PREPARING,
                    expected_revision=1,
                    event_type="concurrent",
                    payload={},
                )
            )
        except BaseException as error:  # noqa: BLE001 - captures thread failure
            errors.append(error)

    verify_thread = threading.Thread(target=verify)
    verify_thread.start()
    assert event_select_entered.wait(timeout=10)
    append_thread = threading.Thread(target=append)
    append_thread.start()
    time.sleep(0.2)
    resume_event_select.set()
    verify_thread.join(timeout=10)
    append_thread.join(timeout=10)

    assert not verify_thread.is_alive()
    assert not append_thread.is_alive()
    assert errors == []
    assert integrity_results[0].valid is True
    assert integrity_results[0].checked_events == 1
    assert append_results[0].run.revision == 2


def test_integrity_verifier_detects_noncanonical_payload_text(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    _create(ledger)
    connection = _raw(ledger.paths.database)
    try:
        _drop_triggers(connection, table="events", operation="update")
        connection.execute(
            "UPDATE events SET payload_text = ? WHERE run_id = ? AND sequence = 1",
            ('{ "attempt": 1, "node": "create" }', RUN_ID),
        )
    finally:
        connection.close()

    result = ledger.verify_integrity(RUN_ID)

    assert result.valid is False
    assert {finding.code for finding in result.findings} >= {"noncanonical_payload"}


def test_integrity_verifier_rejects_noncanonical_timestamp_even_with_matching_hash(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path)
    _create(ledger)
    connection = _raw(ledger.paths.database)
    try:
        _drop_triggers(connection, table="events", operation="update")
        row = connection.execute(
            "SELECT * FROM events WHERE run_id = ? AND sequence = 1",
            (RUN_ID,),
        ).fetchone()
        alternative_timestamp = "2026-07-27T01:02:04.000000Z"
        digest = hashlib.sha256(
            canonical_json(
                {
                    "run_id": row["run_id"],
                    "sequence": row["sequence"],
                    "event_type": row["event_type"],
                    "payload": json.loads(row["payload_text"]),
                    "timestamp": alternative_timestamp,
                    "elapsed_ms": row["elapsed_ms"],
                    "previous_hash": row["previous_hash"],
                }
            )
        ).hexdigest()
        connection.execute(
            "UPDATE events SET timestamp = ?, event_hash = ? "
            "WHERE run_id = ? AND sequence = 1",
            (alternative_timestamp, digest, RUN_ID),
        )
    finally:
        connection.close()

    result = ledger.verify_integrity(RUN_ID)

    assert result.valid is False
    assert {finding.code for finding in result.findings} >= {"invalid_event_fields"}


def test_integrity_verifier_detects_zero_event_run(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    _create(ledger)
    connection = _raw(ledger.paths.database)
    try:
        _drop_triggers(connection, table="events", operation="delete")
        connection.execute("DELETE FROM events WHERE run_id = ?", (RUN_ID,))
    finally:
        connection.close()

    result = ledger.verify_integrity(RUN_ID)

    assert result.valid is False
    assert result.checked_runs == 1
    assert result.checked_events == 0
    assert {finding.code for finding in result.findings} >= {"missing_event_chain"}


def test_integrity_verifier_detects_deleted_event_chain_suffix(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    _create(ledger)
    ledger.append_event(
        RUN_ID,
        expected_state=WorkflowState.PREPARING,
        expected_revision=1,
        event_type="milestone",
        payload={},
    )
    connection = _raw(ledger.paths.database)
    try:
        _drop_triggers(connection, table="events", operation="delete")
        connection.execute(
            "DELETE FROM events WHERE run_id = ? AND sequence = 2",
            (RUN_ID,),
        )
    finally:
        connection.close()

    result = ledger.verify_integrity(RUN_ID)

    assert result.valid is False
    assert result.checked_events == 1
    assert {finding.code for finding in result.findings} >= {
        "event_projection_mismatch"
    }


def test_integrity_verifier_detects_orphan_event_rows(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    payload: dict[str, object] = {"orphan": True}
    timestamp = "2026-07-27T01:02:03Z"
    digest = hashlib.sha256(
        canonical_json(
            {
                "run_id": RUN_ID_3,
                "sequence": 1,
                "event_type": "orphan",
                "payload": payload,
                "timestamp": timestamp,
                "elapsed_ms": None,
                "previous_hash": "0" * 64,
            }
        )
    ).hexdigest()
    connection = _raw(ledger.paths.database)
    try:
        connection.execute(
            "INSERT INTO events("
            "run_id, sequence, event_type, payload_text, timestamp, elapsed_ms, "
            "previous_hash, event_hash"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                RUN_ID_3,
                1,
                "orphan",
                '{"orphan":true}',
                timestamp,
                None,
                "0" * 64,
                digest,
            ),
        )
    finally:
        connection.close()

    result = ledger.verify_integrity()

    assert result.valid is False
    assert result.checked_runs == 0
    assert result.checked_events == 1
    assert {finding.code for finding in result.findings} >= {"orphan_event"}


def test_child_tables_constrain_run_identity_length(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    connection = _raw(ledger.paths.database)
    try:
        definitions = {
            row["name"]: row["sql"]
            for row in connection.execute(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type = 'table' AND name IN "
                "('events', 'artifacts', 'approvals', 'idempotency_keys')"
            )
        }
    finally:
        connection.close()

    assert set(definitions) == {
        "events",
        "artifacts",
        "approvals",
        "idempotency_keys",
    }
    assert all("length(run_id) = 36" in sql for sql in definitions.values())


def test_integrity_sanitizes_hostile_orphan_identity_and_sequence(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path)
    hostile_run_id = "x" * 200_000
    connection = _raw(ledger.paths.database)
    try:
        connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute(
            "INSERT INTO events("
            "run_id, sequence, event_type, payload_text, timestamp, elapsed_ms, "
            "previous_hash, event_hash"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                hostile_run_id,
                1,
                "orphan",
                "{}",
                "2026-07-27T01:02:03Z",
                None,
                "0" * 64,
                "f" * 64,
            ),
        )
    finally:
        connection.close()

    result = ledger.verify_integrity()

    assert result.valid is False
    orphan = next(finding for finding in result.findings if finding.code == "orphan_event")
    assert orphan.run_id is None or (
        len(orphan.run_id) <= 36
        and "\x00" not in orphan.run_id
        and orphan.run_id.encode("utf-8")
    )
    assert orphan.sequence == 1


def test_integrity_verifier_returns_typed_migration_tamper_finding(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path)
    connection = _raw(ledger.paths.database)
    try:
        _drop_triggers(connection, table="schema_migrations", operation="update")
        connection.execute("UPDATE schema_migrations SET checksum = ?", ("f" * 64,))
    finally:
        connection.close()

    result = ledger.verify_integrity()

    assert result.valid is False
    assert {finding.code for finding in result.findings} >= {"migration_mismatch"}


def test_integrity_verifier_rejects_missing_applied_migration_row(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path)
    connection = _raw(ledger.paths.database)
    try:
        _drop_triggers(connection, table="schema_migrations", operation="delete")
        connection.execute("DELETE FROM schema_migrations")
    finally:
        connection.close()

    result = ledger.verify_integrity()

    assert result.valid is False
    assert result.checked_migrations == 0
    assert {finding.code for finding in result.findings} >= {"migration_mismatch"}


def test_integrity_findings_are_bounded_under_many_corrupt_events(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path)
    _create(ledger)
    _create(ledger, run_id=RUN_ID_2)
    connection = _raw(ledger.paths.database)
    try:
        rows = [
            (
                RUN_ID,
                sequence,
                "corrupt",
                "{}",
                "2026-07-27T01:02:03Z",
                None,
                "a" * 64,
                "f" * 64,
            )
            for sequence in range(2, 152)
        ]
        connection.executemany(
            "INSERT INTO events("
            "run_id, sequence, event_type, payload_text, timestamp, elapsed_ms, "
            "previous_hash, event_hash"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
    finally:
        connection.close()

    result = ledger.verify_integrity(RUN_ID)

    assert result.valid is False
    assert len(result.findings) == 100


def test_integrity_finding_cap_does_not_stop_verifying_requested_runs(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path)
    _create(ledger)
    _create(ledger, run_id=RUN_ID_2)
    connection = _raw(ledger.paths.database)
    try:
        rows = [
            (
                RUN_ID,
                sequence,
                "corrupt",
                "{}",
                "2026-07-27T01:02:03Z",
                None,
                "a" * 64,
                "f" * 64,
            )
            for sequence in range(2, 152)
        ]
        connection.executemany(
            "INSERT INTO events("
            "run_id, sequence, event_type, payload_text, timestamp, elapsed_ms, "
            "previous_hash, event_hash"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
    finally:
        connection.close()

    result = ledger.verify_integrity()

    assert len(result.findings) == 100
    assert result.checked_runs == 2
    assert result.checked_events == 152


def test_list_runs_and_events_are_bounded_and_deterministic(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    _create(ledger, run_id=RUN_ID)
    _create(ledger, run_id=RUN_ID_2)
    _create(ledger, run_id=RUN_ID_3)
    ledger.append_event(
        RUN_ID,
        expected_state=WorkflowState.PREPARING,
        expected_revision=1,
        event_type="two",
        payload={},
    )
    ledger.append_event(
        RUN_ID,
        expected_state=WorkflowState.PREPARING,
        expected_revision=2,
        event_type="three",
        payload={},
    )

    assert [run.run_id for run in ledger.list_runs(limit=2)] == [RUN_ID_3, RUN_ID_2]
    assert [
        event.sequence
        for event in ledger.list_events(RUN_ID, after_sequence=1, limit=1)
    ] == [2]
    with pytest.raises(FCPMCPError) as zero:
        ledger.list_runs(limit=0)
    _assert_code(zero, ErrorCode.INVALID_ARGUMENTS)
    with pytest.raises(FCPMCPError) as too_many:
        ledger.list_events(RUN_ID, limit=1001)
    _assert_code(too_many, ErrorCode.INVALID_ARGUMENTS)


def _database_entry_snapshot(root: Path) -> dict[str, tuple[bytes, int, int]]:
    return {
        path.name: (
            path.read_bytes(),
            path.stat().st_mtime_ns,
            path.stat().st_ctime_ns,
        )
        for path in root.iterdir()
        if path.is_file() and path.name.startswith("runs.sqlite3")
    }


def test_read_apis_do_not_rewrite_a_nonconforming_wal_database(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path)
    _create(ledger)
    connection = _raw(ledger.paths.database)
    try:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    finally:
        connection.close()
    before_entries = {path.name for path in ledger.paths.root.iterdir()}
    before = _database_entry_snapshot(ledger.paths.root)

    with pytest.raises(FCPMCPError) as error:
        ledger.list_runs(limit=10)

    _assert_code(error, ErrorCode.LEDGER_UNAVAILABLE)
    assert {path.name for path in ledger.paths.root.iterdir()} == before_entries
    assert _database_entry_snapshot(ledger.paths.root) == before


def test_healthy_read_apis_leave_database_aliases_bytes_and_times_unchanged(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path)
    _create(ledger)
    before_entries = {path.name for path in ledger.paths.root.iterdir()}
    before = _database_entry_snapshot(ledger.paths.root)

    assert ledger.get_run(RUN_ID) is not None
    assert ledger.list_runs(limit=10)
    assert ledger.list_events(RUN_ID, limit=10)
    assert ledger.get_artifact(RUN_ID, ArtifactKind.CANDIDATE) is None
    assert ledger.list_artifacts(RUN_ID, limit=10) == ()
    assert ledger.get_approval(RUN_ID) is None
    assert ledger.verify_integrity(RUN_ID).valid is True

    assert {path.name for path in ledger.paths.root.iterdir()} == before_entries
    assert _database_entry_snapshot(ledger.paths.root) == before
    assert not any(
        path.name.startswith(("fd", "proc", "dev"))
        for path in ledger.paths.root.iterdir()
    )


def test_restart_read_surface_returns_typed_artifacts_and_approval(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path)
    _create(ledger)
    awaiting = _complete_prepare(ledger)
    decision = ledger.record_decision(
        RUN_ID,
        expected_state=WorkflowState.AWAITING_APPROVAL,
        expected_revision=awaiting.run.revision,
        decision=ApprovalDecision.APPROVED,
        source=ApprovalSource.CLI,
        operator="editor",
        host="workstation",
        terminal_present=True,
        binding_sha256=HASH_E,
        expires_at="2026-07-28T01:02:03Z",
        approval_summary="Approved",
        event_type="approval_recorded",
        event_payload={"binding_sha256": HASH_E},
    )
    restarted = WorkflowLedger(
        ledger.paths,
        clock=TickClock(),
        package_version="0.3.0-test",
    )

    stored_candidate = restarted.get_artifact(RUN_ID, ArtifactKind.CANDIDATE)
    artifacts = restarted.list_artifacts(RUN_ID, limit=1)
    remaining = restarted.list_artifacts(
        RUN_ID,
        after_kind=artifacts[-1].kind,
        limit=10,
    )
    approval = restarted.get_approval(RUN_ID)

    assert isinstance(stored_candidate, ArtifactRecord)
    assert stored_candidate.sha256 == HASH_C
    assert [record.kind for record in artifacts + remaining] == [
        ArtifactKind.CANDIDATE,
        ArtifactKind.DIFF,
    ]
    assert isinstance(approval, ApprovalRecord)
    assert approval == decision.approval


def test_terminal_cutoff_keyset_reaches_more_than_one_thousand_runs(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path)
    timestamp = "2020-01-01T00:00:00Z"
    rows = [
        (
            f"00000000-0000-4000-8000-{number:012x}",
            "1",
            "0.3.0-test",
            "1",
            WorkflowState.FAILED.value,
            1,
            Profile.WORKFLOW.value,
            ApprovalMode.CLI.value,
            "/private/input.fcpxml",
            "/private/output.fcpxml",
            timestamp,
            timestamp,
        )
        for number in range(1, 1006)
    ]
    connection = _raw(ledger.paths.database)
    try:
        connection.executemany(
            "INSERT INTO runs("
            "run_id, graph_version, package_version, run_version, state, revision, "
            "profile, approval_mode, source_path, destination_path, created_at, updated_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
    finally:
        connection.close()

    first = ledger.list_terminal_runs_before(
        "2026-01-01T00:00:00Z",
        limit=1000,
    )
    second = ledger.list_terminal_runs_before(
        "2026-01-01T00:00:00Z",
        after_updated_at=first[-1].updated_at,
        after_run_id=first[-1].run_id,
        limit=1000,
    )

    assert len(first) == 1000
    assert len(second) == 5
    assert len({run.run_id for run in first + second}) == 1005
    assert [run.run_id for run in first + second] == sorted(
        run.run_id for run in first + second
    )


def test_invalid_values_fail_before_sql_with_stable_caller_codes(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    with pytest.raises(FCPMCPError) as run_id:
        _create(ledger, run_id="not-a-uuid")
    _assert_code(run_id, ErrorCode.INVALID_ARGUMENTS)
    _create(ledger)
    with pytest.raises(FCPMCPError) as payload:
        ledger.append_event(
            RUN_ID,
            expected_state=WorkflowState.PREPARING,
            expected_revision=1,
            event_type="bad",
            payload={"body": "x" * (1024 * 1024)},
        )
    _assert_code(payload, ErrorCode.INVALID_ARGUMENTS)
    with pytest.raises(FCPMCPError) as elapsed:
        ledger.append_event(
            RUN_ID,
            expected_state=WorkflowState.PREPARING,
            expected_revision=1,
            event_type="bad",
            payload={},
            elapsed_ms=-1,
        )
    _assert_code(elapsed, ErrorCode.INVALID_ARGUMENTS)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("event_type", "\x00"),
        ("source_path", "\ud800"),
    ],
)
def test_public_text_rejects_nul_and_non_utf8_before_sql(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    ledger = _ledger(tmp_path)
    arguments: dict[str, object] = {
        "run_id": RUN_ID,
        "graph_version": "1",
        "run_version": "1",
        "profile": Profile.WORKFLOW,
        "approval_mode": ApprovalMode.CLI,
        "source_path": "/private/input.fcpxml",
        "destination_path": "/private/output.fcpxml",
        "event_type": "run_created",
        "event_payload": {},
    }
    arguments[field] = value

    with pytest.raises(FCPMCPError) as error:
        ledger.create_run(**arguments)  # type: ignore[arg-type]

    _assert_code(error, ErrorCode.INVALID_ARGUMENTS)
    assert ledger.list_runs(limit=10) == ()


@pytest.mark.parametrize("bad_text", ["\x00", "\ud800"])
def test_event_payload_text_rejects_unsafe_unicode_before_sql(
    tmp_path: Path,
    bad_text: str,
) -> None:
    ledger = _ledger(tmp_path)
    _create(ledger)

    with pytest.raises(FCPMCPError) as error:
        ledger.append_event(
            RUN_ID,
            expected_state=WorkflowState.PREPARING,
            expected_revision=1,
            event_type="unsafe_payload",
            payload={"text": bad_text},
        )

    _assert_code(error, ErrorCode.INVALID_ARGUMENTS)
    run = ledger.get_run(RUN_ID)
    assert run is not None and run.revision == 1


def test_text_character_limits_match_sqlite_unicode_length(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    _create(ledger)

    accepted = ledger.append_event(
        RUN_ID,
        expected_state=WorkflowState.PREPARING,
        expected_revision=1,
        event_type="😀" * 128,
        payload={},
    )
    assert accepted.event.event_type == "😀" * 128
    with pytest.raises(FCPMCPError) as error:
        ledger.append_event(
            RUN_ID,
            expected_state=WorkflowState.PREPARING,
            expected_revision=2,
            event_type="😀" * 129,
            payload={},
        )
    _assert_code(error, ErrorCode.INVALID_ARGUMENTS)


def test_integers_outside_sqlite_signed_range_fail_before_sql(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path)
    _create(ledger)
    too_large = 2**63

    with pytest.raises(FCPMCPError) as revision:
        ledger.append_event(
            RUN_ID,
            expected_state=WorkflowState.PREPARING,
            expected_revision=too_large,
            event_type="bad",
            payload={},
        )
    _assert_code(revision, ErrorCode.INVALID_ARGUMENTS)

    with pytest.raises(FCPMCPError) as elapsed:
        ledger.append_event(
            RUN_ID,
            expected_state=WorkflowState.PREPARING,
            expected_revision=1,
            event_type="bad",
            payload={},
            elapsed_ms=too_large,
        )
    _assert_code(elapsed, ErrorCode.INVALID_ARGUMENTS)

    with pytest.raises(FCPMCPError) as patch_size:
        ledger.append_event(
            RUN_ID,
            expected_state=WorkflowState.PREPARING,
            expected_revision=1,
            event_type="bad",
            payload={},
            projection_patch={"receipt_size_bytes": too_large},
        )
    _assert_code(patch_size, ErrorCode.INVALID_ARGUMENTS)

    with pytest.raises(FCPMCPError) as artifact_size:
        ledger.record_artifact(
            _metadata(size=too_large),
            expected_state=WorkflowState.PREPARING,
            expected_revision=1,
            event_type="bad",
            event_payload={},
        )
    _assert_code(artifact_size, ErrorCode.INVALID_ARGUMENTS)

    with pytest.raises(FCPMCPError) as after_sequence:
        ledger.list_events(RUN_ID, after_sequence=too_large, limit=1)
    _assert_code(after_sequence, ErrorCode.INVALID_ARGUMENTS)

    run = ledger.get_run(RUN_ID)
    assert run is not None and run.revision == 1
    assert len(ledger.list_events(RUN_ID, limit=10)) == 1


def test_ledger_import_is_sdk_server_engine_approval_locking_and_recovery_independent(
    tmp_path: Path,
) -> None:
    script = """
import builtins
import sys

blocked = (
    "mcp",
    "fcp_mcp.server",
    "fcp_mcp.workflow.engine",
    "fcp_mcp.workflow.approval",
    "fcp_mcp.workflow.locking",
    "fcp_mcp.workflow.recovery",
)
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if any(name == item or name.startswith(item + ".") for item in blocked):
        raise AssertionError("forbidden import: " + name)
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
import fcp_mcp.workflow.ledger
"""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(Path(__file__).parents[2] / "src")

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


def test_awaiting_approval_requires_complete_prepare_artifacts_and_expiry(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path)
    _create(ledger)

    with pytest.raises(FCPMCPError) as incomplete:
        ledger.transition(
            RUN_ID,
            expected_state=WorkflowState.PREPARING,
            expected_revision=1,
            target_state=WorkflowState.AWAITING_APPROVAL,
            event_type="awaiting_approval",
            payload={"expires_at": "2026-07-28T01:02:03Z"},
            projection_patch={"expires_at": "2026-07-28T01:02:03Z"},
        )
    _assert_code(incomplete, ErrorCode.WORKFLOW_STATE_CONFLICT)

    prepared = _record_prepare_evidence(ledger)
    with pytest.raises(FCPMCPError) as missing_expiry:
        ledger.transition(
            RUN_ID,
            expected_state=WorkflowState.PREPARING,
            expected_revision=prepared.run.revision,
            target_state=WorkflowState.AWAITING_APPROVAL,
            event_type="awaiting_approval",
            payload={},
        )
    _assert_code(missing_expiry, ErrorCode.WORKFLOW_STATE_CONFLICT)


@pytest.mark.parametrize(
    ("event_type", "payload", "patch"),
    (
        (
            "source_inspected",
            {"source_sha256": HASH_A},
            {
                "source_sha256": HASH_A,
                "prior_destination_state": PriorDestinationState.ABSENT,
                "prior_destination_sha256": None,
            },
        ),
        (
            "plan_built",
            {"plan_sha256": HASH_A},
            {"plan_sha256": HASH_B},
        ),
        (
            "awaiting_approval",
            {},
            {"expires_at": "2026-07-28T01:02:03Z"},
        ),
        (
            "plan_built",
            {"plan_sha256": HASH_A},
            {},
        ),
    ),
)
def test_prepare_projection_payload_must_carry_exact_owned_evidence(
    tmp_path: Path,
    event_type: str,
    payload: dict[str, object],
    patch: dict[str, object],
) -> None:
    ledger = _ledger(tmp_path)
    _create(ledger)

    with pytest.raises(FCPMCPError) as error:
        ledger.append_event(
            RUN_ID,
            expected_state=WorkflowState.PREPARING,
            expected_revision=1,
            event_type=event_type,
            payload=payload,
            projection_patch=patch,
        )

    _assert_code(error, ErrorCode.WORKFLOW_STATE_CONFLICT)


@pytest.mark.parametrize(
    ("mode", "source"),
    (
        (ApprovalMode.CLI, ApprovalSource.CLIENT),
        (ApprovalMode.CLIENT, ApprovalSource.CLI),
    ),
)
def test_decision_source_must_match_run_approval_mode(
    tmp_path: Path,
    mode: ApprovalMode,
    source: ApprovalSource,
) -> None:
    ledger = _ledger(tmp_path)
    _create(ledger, approval_mode=mode)
    awaiting = _complete_prepare(ledger)

    with pytest.raises(FCPMCPError) as error:
        _approve(ledger, awaiting, source=source)

    _assert_code(error, ErrorCode.WORKFLOW_STATE_CONFLICT)
    assert ledger.get_approval(RUN_ID) is None


def test_commit_started_requires_exact_complete_consistent_evidence(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path)
    _create(ledger)
    approved = _approve(ledger, _complete_prepare(ledger))
    complete_patch = {
        "commit_attempt_id": ATTEMPT_ID,
        "expected_backup_path": None,
        "backup_sha256": None,
    }

    with pytest.raises(FCPMCPError) as missing:
        ledger.transition(
            RUN_ID,
            expected_state=WorkflowState.APPROVED,
            expected_revision=approved.run.revision,
            target_state=WorkflowState.COMMITTING,
            event_type="commit_started",
            payload=complete_patch,
            projection_patch={"commit_attempt_id": ATTEMPT_ID},
        )
    _assert_code(missing, ErrorCode.WORKFLOW_STATE_CONFLICT)

    with pytest.raises(FCPMCPError) as mismatched:
        ledger.transition(
            RUN_ID,
            expected_state=WorkflowState.APPROVED,
            expected_revision=approved.run.revision,
            target_state=WorkflowState.COMMITTING,
            event_type="commit_started",
            payload={**complete_patch, "commit_attempt_id": RUN_ID_2},
            projection_patch=complete_patch,
        )
    _assert_code(mismatched, ErrorCode.WORKFLOW_STATE_CONFLICT)

    result = ledger.transition(
        RUN_ID,
        expected_state=WorkflowState.APPROVED,
        expected_revision=approved.run.revision,
        target_state=WorkflowState.COMMITTING,
        event_type="commit_started",
        payload=complete_patch,
        projection_patch=complete_patch,
    )
    assert result.run.commit_attempt_id == ATTEMPT_ID
    assert result.run.expected_backup_path is None
    assert result.run.backup_sha256 is None


def test_present_destination_commit_intent_binds_deterministic_backup(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path)
    _create(ledger)
    approved = _approve(
        ledger,
        _complete_prepare(
            ledger,
            prior_state=PriorDestinationState.PRESENT,
            prior_sha256=HASH_A,
        ),
    )
    expected_backup = (
        f"{approved.run.destination_path}.bak.{ATTEMPT_ID}"
    )

    with pytest.raises(FCPMCPError) as wrong_path:
        ledger.transition(
            RUN_ID,
            expected_state=WorkflowState.APPROVED,
            expected_revision=approved.run.revision,
            target_state=WorkflowState.COMMITTING,
            event_type="commit_started",
            payload={
                "commit_attempt_id": ATTEMPT_ID,
                "expected_backup_path": "/private/wrong.bak",
                "backup_sha256": HASH_A,
            },
            projection_patch={
                "commit_attempt_id": ATTEMPT_ID,
                "expected_backup_path": "/private/wrong.bak",
                "backup_sha256": HASH_A,
            },
        )
    _assert_code(wrong_path, ErrorCode.WORKFLOW_STATE_CONFLICT)

    result = ledger.transition(
        RUN_ID,
        expected_state=WorkflowState.APPROVED,
        expected_revision=approved.run.revision,
        target_state=WorkflowState.COMMITTING,
        event_type="commit_started",
        payload={
            "commit_attempt_id": ATTEMPT_ID,
            "expected_backup_path": expected_backup,
            "backup_sha256": HASH_A,
        },
        projection_patch={
            "commit_attempt_id": ATTEMPT_ID,
            "expected_backup_path": expected_backup,
            "backup_sha256": HASH_A,
        },
    )
    assert result.run.expected_backup_path == expected_backup
    assert result.run.backup_sha256 == HASH_A


def test_receipt_evidence_is_forbidden_during_prepare_and_required_on_commit(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path)
    _create(ledger)

    with pytest.raises(FCPMCPError) as prepare_receipt:
        ledger.append_event(
            RUN_ID,
            expected_state=WorkflowState.PREPARING,
            expected_revision=1,
            event_type="dry_run_completed",
            payload={"sha256": HASH_E},
            projection_patch={
                "receipt_sha256": HASH_E,
                "receipt_size_bytes": 42,
            },
        )
    _assert_code(prepare_receipt, ErrorCode.WORKFLOW_STATE_CONFLICT)

    approved = _approve(ledger, _complete_prepare(ledger))
    committing = ledger.transition(
        RUN_ID,
        expected_state=WorkflowState.APPROVED,
        expected_revision=approved.run.revision,
        target_state=WorkflowState.COMMITTING,
        event_type="commit_started",
        payload={
            "commit_attempt_id": ATTEMPT_ID,
            "expected_backup_path": None,
            "backup_sha256": None,
        },
        projection_patch={
            "commit_attempt_id": ATTEMPT_ID,
            "expected_backup_path": None,
            "backup_sha256": None,
        },
    )
    with pytest.raises(FCPMCPError) as incomplete:
        ledger.transition(
            RUN_ID,
            expected_state=WorkflowState.COMMITTING,
            expected_revision=committing.run.revision,
            target_state=WorkflowState.COMMITTED,
            event_type="committed",
            payload={"sha256": HASH_C},
            projection_patch={
                "destination_sha256": HASH_C,
                "committed_at": "2026-07-27T02:02:03Z",
            },
        )
    _assert_code(incomplete, ErrorCode.WORKFLOW_STATE_CONFLICT)

    committed = ledger.transition(
        RUN_ID,
        expected_state=WorkflowState.COMMITTING,
        expected_revision=committing.run.revision,
        target_state=WorkflowState.COMMITTED,
        event_type="committed",
        payload={
            "destination_sha256": HASH_C,
            "backup_sha256": None,
            "receipt_sha256": HASH_E,
            "receipt_size_bytes": 42,
            "committed_at": "2026-07-27T02:02:03Z",
        },
        projection_patch={
            "destination_sha256": HASH_C,
            "backup_sha256": None,
            "receipt_sha256": HASH_E,
            "receipt_size_bytes": 42,
            "committed_at": "2026-07-27T02:02:03Z",
        },
    )
    assert committed.run.receipt_sha256 == HASH_E
    assert committed.run.receipt_size_bytes == 42


def test_present_destination_final_commit_echoes_authenticated_backup(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path)
    _create(ledger)
    approved = _approve(
        ledger,
        _complete_prepare(
            ledger,
            prior_state=PriorDestinationState.PRESENT,
            prior_sha256=HASH_A,
        ),
    )
    expected_backup = f"/private/output.fcpxml.bak.{ATTEMPT_ID}"
    committing = ledger.transition(
        RUN_ID,
        expected_state=WorkflowState.APPROVED,
        expected_revision=approved.run.revision,
        target_state=WorkflowState.COMMITTING,
        event_type="commit_started",
        payload={
            "commit_attempt_id": ATTEMPT_ID,
            "expected_backup_path": expected_backup,
            "backup_sha256": HASH_A,
        },
        projection_patch={
            "commit_attempt_id": ATTEMPT_ID,
            "expected_backup_path": expected_backup,
            "backup_sha256": HASH_A,
        },
    )

    for final_backup in (None, HASH_B):
        with pytest.raises(FCPMCPError) as mismatch:
            ledger.transition(
                RUN_ID,
                expected_state=WorkflowState.COMMITTING,
                expected_revision=committing.run.revision,
                target_state=WorkflowState.COMMITTED,
                event_type="committed",
                payload={
                    "destination_sha256": HASH_C,
                    "backup_sha256": final_backup,
                    "receipt_sha256": HASH_E,
                    "receipt_size_bytes": 42,
                    "committed_at": "2026-07-27T02:02:03Z",
                },
                projection_patch={
                    "destination_sha256": HASH_C,
                    "backup_sha256": final_backup,
                    "receipt_sha256": HASH_E,
                    "receipt_size_bytes": 42,
                    "committed_at": "2026-07-27T02:02:03Z",
                },
            )
        _assert_code(mismatch, ErrorCode.WORKFLOW_STATE_CONFLICT)

    committed = ledger.transition(
        RUN_ID,
        expected_state=WorkflowState.COMMITTING,
        expected_revision=committing.run.revision,
        target_state=WorkflowState.COMMITTED,
        event_type="committed",
        payload={
            "destination_sha256": HASH_C,
            "backup_sha256": HASH_A,
            "receipt_sha256": HASH_E,
            "receipt_size_bytes": 42,
            "committed_at": "2026-07-27T02:02:03Z",
        },
        projection_patch={
            "destination_sha256": HASH_C,
            "backup_sha256": HASH_A,
            "receipt_sha256": HASH_E,
            "receipt_size_bytes": 42,
            "committed_at": "2026-07-27T02:02:03Z",
        },
    )

    assert committed.run.backup_sha256 == HASH_A
    assert ledger.verify_integrity(RUN_ID).valid is True

    connection = _raw(ledger.paths.database)
    try:
        trigger_rows = connection.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type = 'trigger' AND tbl_name = 'events'"
        ).fetchall()
        update_triggers = tuple(
            row
            for row in trigger_rows
            if "BEFORE UPDATE" in row["sql"].upper()
        )
        assert update_triggers
        for trigger in update_triggers:
            connection.execute(f'DROP TRIGGER "{trigger["name"]}"')
        final_event = connection.execute(
            "SELECT * FROM events WHERE run_id = ? ORDER BY sequence DESC LIMIT 1",
            (RUN_ID,),
        ).fetchone()
        assert final_event is not None
        mutated_payload = json.loads(final_event["payload_text"])
        mutated_payload["backup_sha256"] = HASH_B
        mutated_hash = ledger_module._event_digest(
            run_id=RUN_ID,
            sequence=final_event["sequence"],
            event_type=final_event["event_type"],
            payload=mutated_payload,
            timestamp=final_event["timestamp"],
            elapsed_ms=final_event["elapsed_ms"],
            previous_hash=final_event["previous_hash"],
        )
        connection.execute(
            "UPDATE events SET payload_text = ?, event_hash = ? "
            "WHERE run_id = ? AND sequence = ?",
            (
                canonical_json(mutated_payload).decode("utf-8"),
                mutated_hash,
                RUN_ID,
                final_event["sequence"],
            ),
        )
        for trigger in update_triggers:
            connection.execute(trigger["sql"])
    finally:
        connection.close()

    result = ledger.verify_integrity(RUN_ID)
    assert result.valid is False
    assert {finding.code for finding in result.findings} == {
        "projection_invariant"
    }


@pytest.mark.parametrize(
    ("target", "patch", "allowed"),
    (
        (WorkflowState.FAILED, {}, False),
        (
            WorkflowState.FAILED,
            {
                "terminal_error_code": ErrorCode.OPERATION_FAILED,
                "terminal_error_summary": "prepare failed",
            },
            True,
        ),
        (
            WorkflowState.STALE,
            {
                "terminal_error_code": ErrorCode.WORKFLOW_STALE,
                "terminal_error_summary": "destination changed",
            },
            True,
        ),
        (WorkflowState.ROLLED_BACK, {}, True),
        (WorkflowState.RECOVERY_REQUIRED, {}, False),
        (
            WorkflowState.CANCELLED,
            {
                "terminal_error_code": ErrorCode.OPERATION_FAILED,
                "terminal_error_summary": "not allowed",
            },
            False,
        ),
    ),
)
def test_terminal_error_policy_matches_public_status_contract(
    tmp_path: Path,
    target: WorkflowState,
    patch: dict[str, object],
    allowed: bool,
) -> None:
    ledger = _ledger(tmp_path)
    _create(ledger)
    source = WorkflowState.PREPARING
    revision = 1
    if target in {
        WorkflowState.STALE,
        WorkflowState.ROLLED_BACK,
        WorkflowState.RECOVERY_REQUIRED,
    }:
        approved = _approve(ledger, _complete_prepare(ledger))
        source = WorkflowState.APPROVED
        revision = approved.run.revision
        if target in {
            WorkflowState.ROLLED_BACK,
            WorkflowState.RECOVERY_REQUIRED,
        }:
            committing = ledger.transition(
                RUN_ID,
                expected_state=WorkflowState.APPROVED,
                expected_revision=revision,
                target_state=WorkflowState.COMMITTING,
                event_type="commit_started",
                payload={
                    "commit_attempt_id": ATTEMPT_ID,
                    "expected_backup_path": None,
                    "backup_sha256": None,
                },
                projection_patch={
                    "commit_attempt_id": ATTEMPT_ID,
                    "expected_backup_path": None,
                    "backup_sha256": None,
                },
            )
            source = WorkflowState.COMMITTING
            revision = committing.run.revision

    mutation = lambda: ledger.transition(
        RUN_ID,
        expected_state=source,
        expected_revision=revision,
        target_state=target,
        event_type=f"became_{target.value}",
        payload={},
        projection_patch=patch,
    )
    if allowed:
        result = mutation()
        assert result.run.state is target
    else:
        with pytest.raises(FCPMCPError) as error:
            mutation()
        _assert_code(error, ErrorCode.WORKFLOW_STATE_CONFLICT)


def test_terminal_prune_audits_are_closed_and_interrupted_intents_are_resumable(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path)
    _create(ledger)
    failed = ledger.transition(
        RUN_ID,
        expected_state=WorkflowState.PREPARING,
        expected_revision=1,
        target_state=WorkflowState.FAILED,
        event_type="prepare_failed",
        payload={},
        projection_patch={
            "terminal_error_code": ErrorCode.OPERATION_FAILED,
            "terminal_error_summary": "prepare failed",
        },
    )
    intent = ledger.append_event(
        RUN_ID,
        expected_state=WorkflowState.FAILED,
        expected_revision=failed.run.revision,
        event_type="artifact_prune_intent",
        payload={"artifacts": []},
    )

    pending = ledger.list_pending_prune_runs(limit=10)
    assert [run.run_id for run in pending] == [RUN_ID]
    with pytest.raises(FCPMCPError) as arbitrary:
        ledger.append_event(
            RUN_ID,
            expected_state=WorkflowState.FAILED,
            expected_revision=intent.run.revision,
            event_type="late_mutation",
            payload={},
        )
    _assert_code(arbitrary, ErrorCode.WORKFLOW_STATE_CONFLICT)

    ledger.append_event(
        RUN_ID,
        expected_state=WorkflowState.FAILED,
        expected_revision=intent.run.revision,
        event_type="artifacts_pruned",
        payload={"artifacts": []},
    )
    assert ledger.list_pending_prune_runs(limit=10) == ()


def test_integrity_enforces_prepare_approval_commit_and_terminal_invariants(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path)
    _create(ledger, approval_mode=ApprovalMode.CLIENT)
    approved = _approve(
        ledger,
        _complete_prepare(ledger),
        source=ApprovalSource.CLIENT,
    )
    connection = _raw(ledger.paths.database)
    try:
        connection.execute(
            "UPDATE runs SET approval_source = ?, "
            "receipt_sha256 = ?, receipt_size_bytes = ?, "
            "terminal_error_code = ?, terminal_error_summary = ? "
            "WHERE run_id = ?",
            (
                ApprovalSource.CLI.value,
                HASH_A,
                10,
                ErrorCode.OPERATION_FAILED.value,
                "not terminal",
                RUN_ID,
            ),
        )
    finally:
        connection.close()

    result = ledger.verify_integrity(RUN_ID)

    assert approved.run.state is WorkflowState.APPROVED
    assert result.valid is False
    assert {finding.code for finding in result.findings} == {
        "projection_invariant"
    }


def test_read_paths_never_run_stale_bootstrap_cleanup_but_write_paths_do(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = _ledger(tmp_path)
    _create(ledger)
    stale = ledger.paths.root / ".runs.sqlite3.bootstrap-stale.tmp"
    stale.write_bytes(ledger_module._BOOTSTRAP_IMAGE)
    if os.name == "posix":
        os.chmod(stale, 0o600)

    with monkeypatch.context() as read_patch:
        def reject_cleanup(anchor: object) -> None:
            del anchor
            raise AssertionError("read path invoked mutating cleanup")

        read_patch.setattr(
            ledger,
            "_cleanup_stale_bootstrap_entries",
            reject_cleanup,
        )
        assert ledger.get_run(RUN_ID) is not None
    assert stale.exists()

    ledger.append_event(
        RUN_ID,
        expected_state=WorkflowState.PREPARING,
        expected_revision=1,
        event_type="write_boundary",
        payload={},
    )
    assert not stale.exists()


def test_backup_destination_is_token_authenticated_before_sqlite_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    legacy = _raw(paths.database)
    legacy.execute("CREATE TABLE legacy(value TEXT NOT NULL)")
    legacy.close()
    if os.name == "posix":
        os.chmod(paths.database, 0o600)
    ledger = WorkflowLedger(paths, clock=TickClock(), package_version="0.3.0-test")
    original = ledger._connect_lease
    authenticated: list[str] = []
    validated: list[str] = []

    def require_authenticated_destination(
        lease: ledger_module._DatabaseLease,
        *,
        read_only: bool,
    ) -> sqlite3.Connection:
        if lease.name.endswith(".tmp"):
            if lease.bootstrap_token is not None:
                assert read_only is False
                observed = ledger._bootstrap_token_from_descriptor(
                    lease.descriptor,
                    os.fstat(lease.descriptor),
                )
                assert observed == lease.bootstrap_token
                authenticated.append(lease.name)
            else:
                assert read_only is True
                validated.append(lease.name)
        return original(lease, read_only=read_only)

    monkeypatch.setattr(ledger, "_connect_lease", require_authenticated_destination)

    ledger.initialize()

    assert len(authenticated) == 1
    assert validated == authenticated


@pytest.mark.skipif(os.name != "posix", reason="requires retained destination fd")
def test_backup_destination_same_inode_corruption_is_rejected_before_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    legacy = _raw(paths.database)
    legacy.execute("CREATE TABLE legacy(value TEXT NOT NULL)")
    legacy.execute("INSERT INTO legacy VALUES ('source')")
    legacy.close()
    os.chmod(paths.database, 0o600)
    ledger = WorkflowLedger(paths, clock=TickClock(), package_version="0.3.0-test")
    original_connect = ledger._connect_lease
    original_backup = ledger_module._LedgerConnection.backup
    destination_descriptor: int | None = None

    def capture_destination(
        lease: ledger_module._DatabaseLease,
        *,
        read_only: bool,
    ) -> sqlite3.Connection:
        nonlocal destination_descriptor
        if lease.name.endswith(".tmp"):
            destination_descriptor = lease.descriptor
        return original_connect(lease, read_only=read_only)

    def corrupt_after_backup(
        source: ledger_module._LedgerConnection,
        target: sqlite3.Connection,
        *args: object,
        **kwargs: object,
    ) -> None:
        original_backup(source, target, *args, **kwargs)
        assert destination_descriptor is not None
        os.pwrite(destination_descriptor, b"BROKEN", 0)
        os.fsync(destination_descriptor)

    monkeypatch.setattr(ledger, "_connect_lease", capture_destination)
    monkeypatch.setattr(
        ledger_module._LedgerConnection,
        "backup",
        corrupt_after_backup,
    )

    with pytest.raises(FCPMCPError) as error:
        ledger.initialize()

    _assert_code(error, ErrorCode.LEDGER_UNAVAILABLE)
    assert paths.database.read_bytes().startswith(b"SQLite format 3\x00")
    assert list(paths.root.glob("runs.sqlite3.backup-*")) == []
    assert list(paths.root.glob(".runs.sqlite3.backup-*.tmp")) == []


@pytest.mark.skipif(os.name != "posix", reason="requires open-inode displacement")
def test_backup_destination_displacement_is_rejected_before_backup_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    legacy = _raw(paths.database)
    legacy.execute("CREATE TABLE legacy(value TEXT NOT NULL)")
    legacy.execute("INSERT INTO legacy VALUES ('source')")
    legacy.close()
    os.chmod(paths.database, 0o600)
    ledger = WorkflowLedger(paths, clock=TickClock(), package_version="0.3.0-test")
    original = ledger._connect_lease
    attacker_path = paths.root / "attacker.sqlite3"
    displaced = False

    def displace_destination(
        lease: ledger_module._DatabaseLease,
        *,
        read_only: bool,
    ) -> sqlite3.Connection:
        nonlocal displaced
        if lease.name.endswith(".tmp") and not displaced:
            displaced = True
            temporary = paths.root / lease.name
            os.replace(temporary, paths.root / "displaced.sqlite3")
            attacker = _raw(temporary)
            attacker.execute("CREATE TABLE sentinel(value TEXT)")
            attacker.close()
            os.chmod(temporary, 0o600)
            attacker_path.write_bytes(temporary.read_bytes())
        return original(lease, read_only=read_only)

    monkeypatch.setattr(ledger, "_connect_lease", displace_destination)

    with pytest.raises(FCPMCPError) as error:
        ledger.initialize()

    _assert_code(error, ErrorCode.LEDGER_UNAVAILABLE)
    attacker = _raw(attacker_path)
    try:
        assert attacker.execute(
            "SELECT count(*) FROM sqlite_master WHERE name = 'sentinel'"
        ).fetchone()[0] == 1
        assert attacker.execute(
            "SELECT count(*) FROM sqlite_master WHERE name = 'legacy'"
        ).fetchone()[0] == 0
    finally:
        attacker.close()
    assert list(paths.root.glob("runs.sqlite3.backup-*")) == []


def test_ledger_has_no_windows_or_pathname_fallback_implementation() -> None:
    source = Path(ledger_module.__file__).read_text(encoding="utf-8")
    for marker in (
        "WinDLL",
        "msvcrt",
        "_windows_",
        "FILE_SHARE_",
        "reparse",
        "_fallback_open_file",
        'os.name == "nt"',
    ):
        assert marker not in source


def test_sqlite_second_open_retains_authenticated_main_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = _ledger(tmp_path)
    lease = ledger._secure_database_entry(create=False, write=True)
    descriptor = lease.descriptor
    original_connect = sqlite3.connect
    observed = False

    def observe_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        nonlocal observed
        assert descriptor is not None
        os.fstat(descriptor)
        observed = True
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(ledger_module.sqlite3, "connect", observe_connect)
    connection = ledger._connect_lease(lease, read_only=False)
    try:
        assert observed
        assert lease.descriptor == descriptor
        os.fstat(descriptor)
    finally:
        connection.close()
        lease.close()


@pytest.mark.parametrize("primary_failure", (False, True))
def test_public_close_failures_are_sanitized_without_masking_primary_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    primary_failure: bool,
) -> None:
    ledger = _ledger(tmp_path)
    _create(ledger)
    original_connect = sqlite3.connect

    class CloseFailureConnection(ledger_module._LedgerConnection):
        def close(self) -> None:
            super().close()
            raise OSError("/private/sensitive/close-path")

    def close_failing_connect(
        *args: object,
        **kwargs: object,
    ) -> sqlite3.Connection:
        kwargs["factory"] = CloseFailureConnection
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(ledger_module.sqlite3, "connect", close_failing_connect)

    with pytest.raises(FCPMCPError) as error:
        if primary_failure:
            ledger.append_event(
                RUN_ID,
                expected_state=WorkflowState.PREPARING,
                expected_revision=2,
                event_type="source_inspected",
                payload={
                    "source_sha256": HASH_A,
                    "prior_destination_state": PriorDestinationState.ABSENT,
                    "prior_destination_sha256": None,
                },
                projection_patch={
                    "source_sha256": HASH_A,
                    "prior_destination_state": PriorDestinationState.ABSENT,
                    "prior_destination_sha256": None,
                },
            )
        else:
            ledger.get_run(RUN_ID)

    expected = (
        ErrorCode.WORKFLOW_STATE_CONFLICT
        if primary_failure
        else ErrorCode.LEDGER_UNAVAILABLE
    )
    _assert_code(error, expected)
    assert "/private/sensitive" not in str(error.value)
    cleanup_failures = getattr(error.value, "cleanup_failures", ())
    if primary_failure:
        assert cleanup_failures
        assert all("/private/sensitive" not in str(item) for item in cleanup_failures)
