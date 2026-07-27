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
    ArtifactMutationResult,
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
) -> IdempotencyResult:
    return ledger.create_run(
        run_id=run_id,
        graph_version="1",
        run_version="1",
        profile=Profile.WORKFLOW,
        approval_mode=ApprovalMode.CLI,
        source_path="/private/input.fcpxml",
        destination_path="/private/output.fcpxml",
        idempotency_key=key,
        request_sha256=request_sha256,
        event_type="run_created",
        event_payload={"node": "create", "attempt": 1},
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

    class AuditedConnection(sqlite3.Connection):
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

    class GuardedConnection(sqlite3.Connection):
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
    paths.database.write_bytes(b"")
    if os.name == "posix":
        os.chmod(paths.database, 0o600)
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
    original_open = os.open
    attempts = 0

    def racing_open(path: object, *args: object, **kwargs: object) -> int:
        nonlocal attempts
        if Path(path) == paths.database:
            attempts += 1
            raise FileExistsError("injected create race")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(ledger_module.os, "open", racing_open)

    with pytest.raises(FCPMCPError) as error:
        WorkflowLedger(
            paths,
            clock=TickClock(),
            package_version="0.3.0-test",
        ).initialize()

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
        payload={"sha256": HASH_A},
        elapsed_ms=7,
        projection_patch={
            "source_sha256": HASH_A,
            "prior_destination_state": PriorDestinationState.ABSENT,
        },
    )
    second = ledger.append_event(
        RUN_ID,
        expected_state=WorkflowState.PREPARING,
        expected_revision=2,
        event_type="plan_built",
        payload={"sha256": HASH_B},
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
        "WHEN NEW.event_type = 'reject_me' "
        "BEGIN SELECT RAISE(ABORT, 'injected'); END"
    )
    connection.close()

    with pytest.raises(FCPMCPError) as error:
        ledger.append_event(
            RUN_ID,
            expected_state=WorkflowState.PREPARING,
            expected_revision=1,
            event_type="reject_me",
            payload={"sha256": HASH_A},
            projection_patch={"source_sha256": HASH_A},
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

    mutation = ledger.transition(
        RUN_ID,
        expected_state=WorkflowState.PREPARING,
        expected_revision=1,
        target_state=WorkflowState.AWAITING_APPROVAL,
        event_type="prepare_completed",
        payload={"state": "awaiting_approval"},
        projection_patch={"expires_at": "2026-07-28T01:02:03Z"},
    )

    assert isinstance(mutation, EventMutationResult)
    assert mutation.run.state is WorkflowState.AWAITING_APPROVAL
    assert mutation.run.revision == 2
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
            expected_revision=2,
            event_type="wrong",
            payload={},
        )
    _assert_code(stale_state, ErrorCode.WORKFLOW_STATE_CONFLICT)
    with pytest.raises(FCPMCPError) as illegal:
        ledger.transition(
            RUN_ID,
            expected_state=WorkflowState.AWAITING_APPROVAL,
            expected_revision=2,
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
    ledger.transition(
        RUN_ID,
        expected_state=WorkflowState.PREPARING,
        expected_revision=1,
        target_state=WorkflowState.AWAITING_APPROVAL,
        event_type="prepared",
        payload={},
        projection_patch={"expires_at": "2026-07-28T01:02:03Z"},
    )

    result = ledger.record_decision(
        RUN_ID,
        expected_state=WorkflowState.AWAITING_APPROVAL,
        expected_revision=2,
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
    assert result.run.revision == 3
    assert result.run.approval_decision is ApprovalDecision.APPROVED
    assert result.approval.operator == "editor"
    assert result.approval.terminal_present is True
    assert result.event.sequence == 3
    with pytest.raises(FCPMCPError) as duplicate:
        ledger.record_decision(
            RUN_ID,
            expected_state=WorkflowState.APPROVED,
            expected_revision=3,
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


def test_append_only_triggers_reject_update_and_delete(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path)
    _create(ledger, key="key-1", request_sha256=HASH_A)
    ledger.record_artifact(
        _metadata(),
        expected_state=WorkflowState.PREPARING,
        expected_revision=1,
        event_type="candidate_stored",
        event_payload={},
    )
    ledger.transition(
        RUN_ID,
        expected_state=WorkflowState.PREPARING,
        expected_revision=2,
        target_state=WorkflowState.AWAITING_APPROVAL,
        event_type="prepared",
        payload={},
    )
    ledger.record_decision(
        RUN_ID,
        expected_state=WorkflowState.AWAITING_APPROVAL,
        expected_revision=3,
        decision=ApprovalDecision.REJECTED,
        source=ApprovalSource.CLIENT,
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


def test_reserve_idempotency_key_is_bounded_unique_and_typed(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    _create(ledger)

    first = ledger.reserve_idempotency_key("later-key", HASH_A, RUN_ID)
    second = ledger.reserve_idempotency_key("later-key", HASH_A, RUN_ID)

    assert first.existing is False
    assert second.existing is True
    assert second.run.run_id == RUN_ID
    with pytest.raises(FCPMCPError) as conflict:
        ledger.reserve_idempotency_key("later-key", HASH_B, RUN_ID)
    _assert_code(conflict, ErrorCode.IDEMPOTENCY_CONFLICT)
    with pytest.raises(FCPMCPError) as too_long:
        ledger.reserve_idempotency_key("x" * 129, HASH_A, RUN_ID)
    _assert_code(too_long, ErrorCode.INVALID_ARGUMENTS)


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

    class PausingConnection(sqlite3.Connection):
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
