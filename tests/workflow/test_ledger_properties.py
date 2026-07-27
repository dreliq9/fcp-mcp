from __future__ import annotations

import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from fcp_mcp.config import RuntimeConfig
from fcp_mcp.profiles import ApprovalMode, Profile
from fcp_mcp.workflow.artifacts import StatePaths
from fcp_mcp.workflow.ledger import WorkflowLedger
from fcp_mcp.workflow.models import WorkflowState, canonical_json

RUN_ID = "123e4567-e89b-42d3-a456-426614174000"
RUN_ID_2 = "123e4567-e89b-42d3-a456-426614174001"
UTC = datetime(2026, 7, 27, 1, 2, 3, tzinfo=timezone.utc)


def _ledger(root: Path) -> WorkflowLedger:
    root = root.resolve()
    config = RuntimeConfig.from_env(
        {"FCP_MCP_STATE_DIR": str(root / "state")},
        home=root,
    )
    paths = StatePaths.from_config(config)
    ledger = WorkflowLedger(
        paths,
        clock=lambda: UTC,
        package_version="0.3.0-test",
    )
    ledger.initialize()
    return ledger


def _create(
    ledger: WorkflowLedger,
    run_id: str,
    payload: dict[str, object] | None = None,
) -> None:
    ledger.create_run(
        run_id=run_id,
        graph_version="1",
        run_version="1",
        profile=Profile.WORKFLOW,
        approval_mode=ApprovalMode.CLI,
        source_path="/private/input.fcpxml",
        destination_path="/private/output.fcpxml",
        event_type="run_created",
        event_payload=(
            payload if payload is not None else {"node": "create", "attempt": 1}
        ),
    )


def _connection(ledger: WorkflowLedger) -> sqlite3.Connection:
    connection = sqlite3.connect(ledger.paths.database, isolation_level=None)
    connection.row_factory = sqlite3.Row
    return connection


def _drop_event_trigger(connection: sqlite3.Connection, operation: str) -> None:
    rows = connection.execute(
        "SELECT name, sql FROM sqlite_master "
        "WHERE type = 'trigger' AND tbl_name = 'events'"
    ).fetchall()
    names = [
        row["name"]
        for row in rows
        if f"BEFORE {operation.upper()}" in row["sql"].upper()
    ]
    assert names
    for name in names:
        connection.execute(f'DROP TRIGGER "{name}"')


@given(
    st.sampled_from(
        (
            "run_id",
            "sequence",
            "event_type",
            "payload_text",
            "payload_value",
            "timestamp",
            "elapsed_ms",
            "previous_hash",
            "event_hash",
        )
    )
)
@settings(max_examples=18, deadline=None)
def test_verifier_detects_property_mutation_of_every_bound_event_field(
    mutation: str,
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        ledger = _ledger(Path(directory))
        _create(ledger, RUN_ID)
        connection = _connection(ledger)
        try:
            _drop_event_trigger(connection, "update")
            if mutation == "run_id":
                _create(ledger, RUN_ID_2)
                _drop_event_trigger(connection, "delete")
                connection.execute(
                    "DELETE FROM events WHERE run_id = ?",
                    (RUN_ID_2,),
                )
                connection.execute(
                    "UPDATE events SET run_id = ? WHERE run_id = ?",
                    (RUN_ID_2, RUN_ID),
                )
            elif mutation == "sequence":
                connection.execute(
                    "UPDATE events SET sequence = 2 WHERE run_id = ?",
                    (RUN_ID,),
                )
            elif mutation == "event_type":
                connection.execute(
                    "UPDATE events SET event_type = 'changed' WHERE run_id = ?",
                    (RUN_ID,),
                )
            elif mutation == "payload_text":
                connection.execute(
                    "UPDATE events SET payload_text = ? WHERE run_id = ?",
                    ('{ "attempt": 1, "node": "create" }', RUN_ID),
                )
            elif mutation == "payload_value":
                connection.execute(
                    "UPDATE events SET payload_text = ? WHERE run_id = ?",
                    ('{"attempt":2,"node":"create"}', RUN_ID),
                )
            elif mutation == "timestamp":
                connection.execute(
                    "UPDATE events SET timestamp = ? WHERE run_id = ?",
                    ("2026-07-27T02:02:03Z", RUN_ID),
                )
            elif mutation == "elapsed_ms":
                connection.execute(
                    "UPDATE events SET elapsed_ms = 1 WHERE run_id = ?",
                    (RUN_ID,),
                )
            elif mutation == "previous_hash":
                connection.execute(
                    "UPDATE events SET previous_hash = ? WHERE run_id = ?",
                    ("a" * 64, RUN_ID),
                )
            else:
                connection.execute(
                    "UPDATE events SET event_hash = ? WHERE run_id = ?",
                    ("f" * 64, RUN_ID),
                )
        finally:
            connection.close()

        result = ledger.verify_integrity()

        assert result.valid is False
        assert result.findings


json_scalar = st.none() | st.booleans() | st.integers(
    min_value=-(2**31),
    max_value=2**31 - 1,
) | st.text(max_size=40)
json_value = st.recursive(
    json_scalar,
    lambda children: st.lists(children, max_size=5)
    | st.dictionaries(st.text(min_size=1, max_size=12), children, max_size=5),
    max_leaves=20,
)


@given(st.dictionaries(st.text(min_size=1, max_size=12), json_value, max_size=8))
@settings(max_examples=30, deadline=None)
def test_canonical_payloads_round_trip_through_valid_event_chains(
    payload: dict[str, object],
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        ledger = _ledger(Path(directory))
        _create(ledger, RUN_ID, payload)

        events = ledger.list_events(RUN_ID, limit=1)
        result = ledger.verify_integrity(RUN_ID)

        assert events[0].payload_text == canonical_json(payload).decode("utf-8")
        assert result.valid is True
        assert result.checked_events == 1


@given(
    st.sampled_from(
        (
            '{ "node": "create", "attempt": 1 }',
            '{"attempt":1,"attempt":1,"node":"create"}',
            '{"attempt":1e0,"node":"create"}',
        )
    )
)
@settings(max_examples=6, deadline=None)
def test_alternative_json_spellings_never_alias_canonical_storage(
    alternative: str,
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        ledger = _ledger(Path(directory))
        _create(ledger, RUN_ID)
        connection = _connection(ledger)
        try:
            _drop_event_trigger(connection, "update")
            connection.execute(
                "UPDATE events SET payload_text = ? WHERE run_id = ?",
                (alternative, RUN_ID),
            )
        finally:
            connection.close()

        result = ledger.verify_integrity(RUN_ID)

        assert result.valid is False
        assert {finding.code for finding in result.findings} & {
            "noncanonical_payload",
            "invalid_payload",
        }
