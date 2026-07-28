from __future__ import annotations

import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from fcp_mcp.config import RuntimeConfig
from fcp_mcp.profiles import ApprovalMode, Profile
from fcp_mcp.workflow.artifacts import (
    ArtifactKind,
    ArtifactMetadataV1,
    StatePaths,
)
from fcp_mcp.workflow.ledger import WorkflowLedger
from fcp_mcp.workflow.models import (
    ApprovalDecision,
    ApprovalSource,
    PriorDestinationState,
    WorkflowState,
    canonical_json,
)

RUN_ID = "123e4567-e89b-42d3-a456-426614174000"
RUN_ID_2 = "123e4567-e89b-42d3-a456-426614174001"
UTC = datetime(2026, 7, 27, 1, 2, 3, tzinfo=timezone.utc)
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64
HASH_E = "e" * 64
ATTEMPT_ID = "223e4567-e89b-42d3-a456-426614174000"


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


def _create_committed_run(ledger: WorkflowLedger) -> None:
    _create(ledger, RUN_ID)
    inspected = ledger.append_event(
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
    planned = ledger.append_event(
        RUN_ID,
        expected_state=WorkflowState.PREPARING,
        expected_revision=inspected.run.revision,
        event_type="plan_built",
        payload={"plan_sha256": HASH_B},
        projection_patch={"plan_sha256": HASH_B},
    )
    candidate = ledger.record_artifact(
        ArtifactMetadataV1(
            run_id=RUN_ID,
            kind=ArtifactKind.CANDIDATE,
            relative_path=f"artifacts/{RUN_ID}/candidate.fcpxml",
            sha256=HASH_C,
            byte_size=9,
            created_at=UTC,
        ),
        expected_state=WorkflowState.PREPARING,
        expected_revision=planned.run.revision,
        event_type="candidate_stored",
        event_payload={"sha256": HASH_C},
    )
    diff = ledger.record_artifact(
        ArtifactMetadataV1(
            run_id=RUN_ID,
            kind=ArtifactKind.DIFF,
            relative_path=f"artifacts/{RUN_ID}/diff.json",
            sha256=HASH_D,
            byte_size=17,
            created_at=UTC,
        ),
        expected_state=WorkflowState.PREPARING,
        expected_revision=candidate.run.revision,
        event_type="diff_created",
        event_payload={"sha256": HASH_D},
    )
    awaiting = ledger.transition(
        RUN_ID,
        expected_state=WorkflowState.PREPARING,
        expected_revision=diff.run.revision,
        target_state=WorkflowState.AWAITING_APPROVAL,
        event_type="awaiting_approval",
        payload={"expires_at": "2026-07-28T01:02:03Z"},
        projection_patch={"expires_at": "2026-07-28T01:02:03Z"},
    )
    approved = ledger.record_approval_or_expire(
        RUN_ID,
        expected_revision=awaiting.run.revision,
        operator="editor",
        host="workstation",
        terminal_present=True,
        plan_schema_version="1",
    )
    assert approved.expired is False
    assert approved.approval is not None
    assert approved.approval.binding_sha256 == ledger.approval_binding(
        RUN_ID,
        plan_schema_version="1",
    )
    assert approved.approval.created_at == "2026-07-27T01:02:03Z"
    assert approved.approval.expires_at == awaiting.run.expires_at
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
    ledger.transition(
        RUN_ID,
        expected_state=WorkflowState.COMMITTING,
        expected_revision=committing.run.revision,
        target_state=WorkflowState.COMMITTED,
        event_type="commit_completed",
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


safe_text = st.text(
    alphabet=st.characters(
        blacklist_characters="\x00",
        blacklist_categories=("Cs",),
    ),
    max_size=40,
)
safe_key = st.text(
    alphabet=st.characters(
        blacklist_characters="\x00",
        blacklist_categories=("Cs",),
    ),
    min_size=1,
    max_size=12,
)
json_scalar = st.none() | st.booleans() | st.integers(
    min_value=-(2**31),
    max_value=2**31 - 1,
) | safe_text
json_value = st.recursive(
    json_scalar,
    lambda children: st.lists(children, max_size=5)
    | st.dictionaries(safe_key, children, max_size=5),
    max_leaves=20,
)


@given(st.dictionaries(safe_key, json_value, max_size=8))
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


@given(
    st.sampled_from(
        (
            ("source_sha256", None),
            ("prior_destination_state", None),
            ("plan_sha256", None),
            ("expires_at", None),
            ("approval_source", ApprovalSource.CLIENT.value),
            ("commit_attempt_id", None),
            ("destination_sha256", None),
            ("backup_sha256", HASH_A),
            ("committed_at", None),
            ("receipt_sha256", None),
            ("receipt_size_bytes", None),
        )
    )
)
@settings(max_examples=20, deadline=None)
def test_verifier_rejects_every_committed_projection_invariant_mutation(
    mutation: tuple[str, object],
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        ledger = _ledger(Path(directory))
        _create_committed_run(ledger)
        assert ledger.verify_integrity(RUN_ID).valid is True

        field, value = mutation
        connection = _connection(ledger)
        try:
            connection.execute(
                f'UPDATE runs SET "{field}" = ? WHERE run_id = ?',
                (value, RUN_ID),
            )
        finally:
            connection.close()

        result = ledger.verify_integrity(RUN_ID)

        assert result.valid is False
        assert {finding.code for finding in result.findings} == {
            "projection_invariant"
        }


@given(
    st.sampled_from(
        (
            ("expires_at", "2026-08-30T01:02:03Z"),
            (
                "prior_destination_evidence",
                PriorDestinationState.PRESENT.value,
            ),
        )
    ),
    st.booleans(),
)
@settings(max_examples=8, deadline=None)
def test_verifier_rejects_valid_to_valid_prepare_projection_mutations(
    mutation: tuple[str, str],
    approved_state: bool,
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        ledger = _ledger(Path(directory))
        _create(ledger, RUN_ID)
        inspected = ledger.append_event(
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
        planned = ledger.append_event(
            RUN_ID,
            expected_state=WorkflowState.PREPARING,
            expected_revision=inspected.run.revision,
            event_type="plan_built",
            payload={"plan_sha256": HASH_B},
            projection_patch={"plan_sha256": HASH_B},
        )
        candidate = ledger.record_artifact(
            ArtifactMetadataV1(
                run_id=RUN_ID,
                kind=ArtifactKind.CANDIDATE,
                relative_path=f"artifacts/{RUN_ID}/candidate.fcpxml",
                sha256=HASH_C,
                byte_size=9,
                created_at=UTC,
            ),
            expected_state=WorkflowState.PREPARING,
            expected_revision=planned.run.revision,
            event_type="candidate_stored",
            event_payload={"sha256": HASH_C},
        )
        diff = ledger.record_artifact(
            ArtifactMetadataV1(
                run_id=RUN_ID,
                kind=ArtifactKind.DIFF,
                relative_path=f"artifacts/{RUN_ID}/diff.json",
                sha256=HASH_D,
                byte_size=17,
                created_at=UTC,
            ),
            expected_state=WorkflowState.PREPARING,
            expected_revision=candidate.run.revision,
            event_type="diff_created",
            event_payload={"sha256": HASH_D},
        )
        awaiting = ledger.transition(
            RUN_ID,
            expected_state=WorkflowState.PREPARING,
            expected_revision=diff.run.revision,
            target_state=WorkflowState.AWAITING_APPROVAL,
            event_type="awaiting_approval",
            payload={"expires_at": "2026-07-28T01:02:03Z"},
            projection_patch={"expires_at": "2026-07-28T01:02:03Z"},
        )
        if approved_state:
            approved = ledger.record_approval_or_expire(
                RUN_ID,
                expected_revision=awaiting.run.revision,
                operator="editor",
                host="workstation",
                terminal_present=True,
                plan_schema_version="1",
            )
            assert approved.expired is False
            assert approved.approval is not None
            assert approved.approval.binding_sha256 == ledger.approval_binding(
                RUN_ID,
                plan_schema_version="1",
            )
            assert approved.approval.created_at == "2026-07-27T01:02:03Z"
            assert approved.approval.expires_at == awaiting.run.expires_at
        assert ledger.verify_integrity(RUN_ID).valid is True

        field, value = mutation
        connection = _connection(ledger)
        try:
            if field == "prior_destination_evidence":
                connection.execute(
                    "UPDATE runs SET prior_destination_state = ?, "
                    "prior_destination_sha256 = ? WHERE run_id = ?",
                    (value, HASH_E, RUN_ID),
                )
            else:
                connection.execute(
                    f'UPDATE runs SET "{field}" = ? WHERE run_id = ?',
                    (value, RUN_ID),
                )
        finally:
            connection.close()

        result = ledger.verify_integrity(RUN_ID)

        assert result.valid is False
        assert {finding.code for finding in result.findings} == {
            "projection_invariant"
        }
