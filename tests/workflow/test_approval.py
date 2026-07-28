from __future__ import annotations

import hashlib
import io
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from fcp_mcp.config import RuntimeConfig
from fcp_mcp.contracts import ErrorCode, FCPMCPError
from fcp_mcp.profiles import ApprovalMode
from fcp_mcp.security.paths import PathPolicy
from fcp_mcp.workflow.approval import (
    ClientApproval,
    approval_binding,
    effective_state,
)
from fcp_mcp.workflow.approval import (
    approve_cli as approve_cli_policy,
)
from fcp_mcp.workflow.approval import (
    approve_client as approve_client_policy,
)
from fcp_mcp.workflow.approval import (
    reject as reject_policy,
)
from fcp_mcp.workflow.artifacts import ArtifactStore, StatePaths
from fcp_mcp.workflow.engine import SCHEMA_VERSION, WorkflowEngine
from fcp_mcp.workflow.ledger import (
    LedgerRunRecord,
    WorkflowLedger,
    approval_binding_for_run,
)
from fcp_mcp.workflow.models import (
    AddMarkerOperation,
    ApprovalDecision,
    ApprovalSource,
    ApprovalStrength,
    PriorDestinationState,
    WorkflowPrepareRequestV1,
    WorkflowState,
    canonical_json,
)

RUN_ID = "123e4567-e89b-42d3-a456-426614174000"
NOW = datetime(2026, 7, 28, 12, 0, 0, tzinfo=timezone.utc)
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64

SOURCE_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<fcpxml version="1.11">
  <resources>
    <format id="r1" frameDuration="1001/30000s" width="1920" height="1080"/>
    <asset id="r2" name="Clip" duration="3003/1000s" hasVideo="1" hasAudio="1"/>
  </resources>
  <event name="Event">
    <project name="Project">
      <sequence format="r1" duration="3003/1000s">
        <spine>
          <asset-clip ref="r2" name="Clip" offset="0s" start="0s"
                      duration="3003/1000s"/>
        </spine>
      </sequence>
    </project>
  </event>
</fcpxml>
"""


class _TTY(io.StringIO):
    def isatty(self) -> bool:
        return True


class _BrokenTTY(_TTY):
    def readline(self, *args, **kwargs) -> str:
        raise OSError("terminal disappeared")


class _UnknownTerminal(io.StringIO):
    def isatty(self) -> bool:
        raise OSError("terminal status unavailable")


class _ExplodingStream(_TTY):
    def __init__(self, boundary: str):
        super().__init__("approve\n")
        self.boundary = boundary

    def isatty(self):
        if self.boundary == "isatty":
            raise RuntimeError("/private/raw terminal identity")
        return True

    def write(self, value):
        if self.boundary == "write":
            raise RuntimeError("/private/raw terminal write")
        return super().write(value)

    def flush(self):
        if self.boundary == "flush":
            raise RuntimeError("/private/raw terminal flush")
        return super().flush()

    def readline(self, *args, **kwargs):
        if self.boundary == "read":
            raise RuntimeError("/private/raw terminal read")
        if self.boundary == "type":
            return object()
        return super().readline(*args, **kwargs)


def _config(tmp_path: Path, mode: ApprovalMode = ApprovalMode.CLI) -> RuntimeConfig:
    return RuntimeConfig.from_env(
        {
            "FCP_MCP_OUTPUT_DIR": str(tmp_path),
            "FCP_MCP_ALLOWED_ROOTS": str(tmp_path),
            "FCP_MCP_STATE_DIR": str(tmp_path / "state"),
            "FCP_MCP_WORKFLOW_APPROVAL": mode.value,
            "FCP_MCP_WORKFLOW_APPROVAL_TTL_SECONDS": "600",
        },
        home=tmp_path,
    )


def _prepared(
    tmp_path: Path,
    *,
    mode: ApprovalMode = ApprovalMode.CLI,
    now: list[datetime] | None = None,
    run_id: str = RUN_ID,
) -> tuple[WorkflowEngine, WorkflowLedger, LedgerRunRecord]:
    current = now or [NOW]
    config = _config(tmp_path, mode)
    paths = StatePaths.from_config(config)
    ledger = WorkflowLedger(paths, clock=lambda: current[0], package_version="0.3.0-test")
    ledger.initialize()
    artifacts = ArtifactStore(paths, max_artifact_bytes=config.max_artifact_bytes)
    engine = WorkflowEngine(
        config,
        PathPolicy(config),
        ledger,
        artifacts,
        uuid_factory=lambda: run_id,
        utc_clock=lambda: current[0],
        monotonic_clock=lambda: 1.0,
    )
    source = tmp_path / "source.fcpxml"
    source.write_bytes(SOURCE_XML)
    preview = engine.prepare(
        WorkflowPrepareRequestV1(
            source_path=str(source),
            destination_path=str(tmp_path / "destination.fcpxml"),
            operations=(
                AddMarkerOperation(
                    kind="add_marker",
                    clip_name="Clip",
                    start="1001/30000s",
                    value="Approval",
                ),
            ),
        )
    )
    run = ledger.get_run(preview.run_id)
    assert run is not None
    return engine, ledger, run


def _assert_code(error: pytest.ExceptionInfo[FCPMCPError], code: ErrorCode) -> None:
    assert error.value.code is code


def _approve_interactively(engine: WorkflowEngine, run_id: str = RUN_ID):
    return engine.approve_cli(
        run_id,
        input_stream=_TTY("approve\n"),
        output_stream=_TTY(),
        operator="local-editor",
        host="local-mac",
    )


def test_approval_binding_uses_exact_closed_canonical_evidence(tmp_path: Path):
    _, ledger, run = _prepared(tmp_path)
    expected_fields = {
        "run_id": run.run_id,
        "graph_version": run.graph_version,
        "plan_schema_version": SCHEMA_VERSION,
        "source_path_sha256": hashlib.sha256(run.source_path.encode("utf-8")).hexdigest(),
        "destination_path_sha256": hashlib.sha256(run.destination_path.encode("utf-8")).hexdigest(),
        "source_sha256": run.source_sha256,
        "prior_destination_state": run.prior_destination_state.value,
        "prior_destination_sha256": run.prior_destination_sha256,
        "plan_sha256": run.plan_sha256,
        "candidate_sha256": run.candidate_sha256,
        "diff_sha256": run.diff_sha256,
        "approval_mode": run.approval_mode.value,
        "expires_at": run.expires_at,
    }
    expected = hashlib.sha256(canonical_json(expected_fields)).hexdigest()

    assert (
        approval_binding(
            ledger,
            run.run_id,
            plan_schema_version=SCHEMA_VERSION,
        )
        == expected
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("run_id", "223e4567-e89b-42d3-a456-426614174000"),
        ("graph_version", "2"),
        ("source_path", "/private/changed-source.fcpxml"),
        ("destination_path", "/private/changed-destination.fcpxml"),
        ("source_sha256", HASH_A),
        ("prior_destination_state", PriorDestinationState.PRESENT),
        ("prior_destination_sha256", HASH_B),
        ("plan_sha256", HASH_B),
        ("candidate_sha256", HASH_C),
        ("diff_sha256", HASH_D),
        ("approval_mode", ApprovalMode.CLIENT),
        ("expires_at", "2026-07-28T12:11:00Z"),
    ],
)
def test_approval_binding_changes_when_any_run_field_changes(
    tmp_path: Path,
    field: str,
    value: object,
):
    _, _, run = _prepared(tmp_path)
    original = approval_binding_for_run(
        run,
        plan_schema_version=SCHEMA_VERSION,
    )

    assert (
        approval_binding_for_run(
            replace(run, **{field: value}),
            plan_schema_version=SCHEMA_VERSION,
        )
        != original
    )


def test_approval_binding_changes_with_plan_schema_version(tmp_path: Path):
    _, _, run = _prepared(tmp_path)

    assert approval_binding_for_run(
        run,
        plan_schema_version="2",
    ) != approval_binding_for_run(
        run,
        plan_schema_version=SCHEMA_VERSION,
    )


def test_approval_binding_preserves_exact_unicode_path_bytes(tmp_path: Path):
    _, _, run = _prepared(tmp_path)
    composed = replace(run, source_path="/private/Caf\u00e9.fcpxml")
    decomposed = replace(run, source_path="/private/Cafe\u0301.fcpxml")

    composed_binding = approval_binding_for_run(
        composed,
        plan_schema_version=SCHEMA_VERSION,
    )
    decomposed_binding = approval_binding_for_run(
        decomposed,
        plan_schema_version=SCHEMA_VERSION,
    )

    assert composed_binding != decomposed_binding
    assert hashlib.sha256(composed.source_path.encode("utf-8")).hexdigest() != hashlib.sha256(
        decomposed.source_path.encode("utf-8")
    ).hexdigest()


@pytest.mark.parametrize("version", ["", "v" * 65, 1])
def test_approval_binding_rejects_invalid_plan_schema_version(
    tmp_path: Path,
    version: object,
):
    _, _, run = _prepared(tmp_path)

    with pytest.raises(FCPMCPError) as error:
        approval_binding_for_run(run, plan_schema_version=version)

    _assert_code(error, ErrorCode.INVALID_ARGUMENTS)


def test_approval_binding_rejects_wrong_type_and_incomplete_evidence(tmp_path: Path):
    _, _, run = _prepared(tmp_path)

    with pytest.raises(FCPMCPError) as wrong_type:
        approval_binding_for_run(object(), plan_schema_version=SCHEMA_VERSION)
    _assert_code(wrong_type, ErrorCode.INVALID_ARGUMENTS)

    with pytest.raises(FCPMCPError) as incomplete:
        approval_binding_for_run(
            replace(run, diff_sha256=None),
            plan_schema_version=SCHEMA_VERSION,
        )
    _assert_code(incomplete, ErrorCode.WORKFLOW_STATE_CONFLICT)


def test_interactive_cli_approval_records_terminal_metadata_and_bounded_event(
    tmp_path: Path,
):
    engine, ledger, run = _prepared(tmp_path)
    output = _TTY()

    result = engine.approve_cli(
        run.run_id,
        input_stream=_TTY("approve\n"),
        output_stream=output,
        operator="local-editor",
        host="local-mac",
    )

    assert result.run.state is WorkflowState.APPROVED
    assert result.approval.decision is ApprovalDecision.APPROVED
    assert result.approval.source is ApprovalSource.CLI
    assert result.approval.terminal_present is True
    assert result.approval.operator == "local-editor"
    assert result.approval.host == "local-mac"
    assert run.candidate_sha256 in output.getvalue()
    event = ledger.list_events(run.run_id)[-1]
    assert event.event_type == "approval_recorded"
    assert set(event.payload) == {"binding_sha256", "decision", "source"}
    assert "local-editor" not in event.payload_text
    assert "local-mac" not in event.payload_text
    assert run.source_path not in event.payload_text
    assert run.destination_path not in event.payload_text


@pytest.mark.parametrize("answer", ["", "yes\n", "APPROVE\n", "no\n"])
def test_interactive_cli_requires_exact_confirmation(tmp_path: Path, answer: str):
    engine, ledger, run = _prepared(tmp_path)

    with pytest.raises(FCPMCPError) as error:
        engine.approve_cli(
            run.run_id,
            input_stream=_TTY(answer),
            output_stream=_TTY(),
            operator="editor",
            host="mac",
        )

    _assert_code(error, ErrorCode.APPROVAL_REQUIRED)
    assert ledger.get_approval(run.run_id) is None
    assert ledger.get_run(run.run_id).state is WorkflowState.AWAITING_APPROVAL


def test_interactive_cli_read_failure_does_not_approve(tmp_path: Path):
    engine, ledger, run = _prepared(tmp_path)

    with pytest.raises(FCPMCPError) as error:
        engine.approve_cli(
            run.run_id,
            input_stream=_BrokenTTY(),
            output_stream=_TTY(),
        )

    _assert_code(error, ErrorCode.APPROVAL_REQUIRED)
    assert ledger.get_approval(run.run_id) is None


@pytest.mark.parametrize(
    ("yes", "expected"),
    [
        (False, None),
        (True, None),
        (False, HASH_A),
    ],
)
def test_noninteractive_cli_requires_yes_and_exact_candidate_hash(
    tmp_path: Path,
    yes: bool,
    expected: str | None,
):
    engine, ledger, run = _prepared(tmp_path)

    with pytest.raises(FCPMCPError) as error:
        engine.approve_cli(
            run.run_id,
            input_stream=io.StringIO(),
            output_stream=io.StringIO(),
            yes=yes,
            expect_candidate_sha256=expected,
            operator="automation",
            host="mac",
        )

    _assert_code(error, ErrorCode.APPROVAL_REQUIRED)
    assert ledger.get_approval(run.run_id) is None


def test_noninteractive_cli_rejects_mismatched_candidate_precondition(tmp_path: Path):
    engine, ledger, run = _prepared(tmp_path)

    with pytest.raises(FCPMCPError) as error:
        engine.approve_cli(
            run.run_id,
            input_stream=io.StringIO(),
            output_stream=io.StringIO(),
            yes=True,
            expect_candidate_sha256=HASH_A,
            operator="automation",
            host="mac",
        )

    _assert_code(error, ErrorCode.WORKFLOW_STALE)
    assert ledger.get_approval(run.run_id) is None


def test_noninteractive_cli_approval_records_terminal_absent(tmp_path: Path):
    engine, _, run = _prepared(tmp_path)

    result = engine.approve_cli(
        run.run_id,
        input_stream=io.StringIO(),
        output_stream=io.StringIO(),
        yes=True,
        expect_candidate_sha256=run.candidate_sha256,
        operator="automation",
        host="mac",
    )

    assert result.approval.terminal_present is False


def test_unavailable_terminal_detection_returns_bounded_coded_error(tmp_path: Path):
    engine, ledger, run = _prepared(tmp_path)

    with pytest.raises(FCPMCPError) as error:
        engine.approve_cli(
            run.run_id,
            input_stream=_UnknownTerminal(),
            output_stream=_TTY(),
            yes=True,
            expect_candidate_sha256=run.candidate_sha256,
        )

    _assert_code(error, ErrorCode.APPROVAL_REQUIRED)
    assert ledger.get_approval(run.run_id) is None


def test_client_approval_is_immutable_commit_request_metadata_not_a_decision(
    tmp_path: Path,
):
    engine, ledger, run = _prepared(tmp_path, mode=ApprovalMode.CLIENT)

    approval = engine.approve_client(
        run.run_id,
        expect_candidate_sha256=run.candidate_sha256,
    )

    assert approval == ClientApproval(
        run_id=run.run_id,
        binding_sha256=approval_binding(
            ledger,
            run.run_id,
            plan_schema_version=SCHEMA_VERSION,
        ),
        source=ApprovalSource.CLIENT,
        strength=ApprovalStrength.CLIENT_UNVERIFIED_HUMAN,
        expires_at=run.expires_at,
    )
    assert ledger.get_approval(run.run_id) is None
    assert ledger.get_run(run.run_id).state is WorkflowState.AWAITING_APPROVAL


def test_client_approval_requires_client_mode_and_exact_candidate(tmp_path: Path):
    cli_engine, cli_ledger, cli_run = _prepared(tmp_path / "cli")
    with pytest.raises(FCPMCPError) as wrong_mode:
        cli_engine.approve_client(
            cli_run.run_id,
            expect_candidate_sha256=cli_run.candidate_sha256,
        )
    _assert_code(wrong_mode, ErrorCode.WORKFLOW_STATE_CONFLICT)
    assert cli_ledger.get_approval(cli_run.run_id) is None

    client_engine, client_ledger, client_run = _prepared(
        tmp_path / "client", mode=ApprovalMode.CLIENT
    )
    with pytest.raises(FCPMCPError) as stale:
        client_engine.approve_client(
            client_run.run_id,
            expect_candidate_sha256=HASH_A,
        )
    _assert_code(stale, ErrorCode.WORKFLOW_STALE)
    assert client_ledger.get_approval(client_run.run_id) is None


def test_approval_is_immutable_and_replay_is_rejected(tmp_path: Path):
    engine, ledger, run = _prepared(tmp_path)
    first = _approve_interactively(engine, run.run_id)

    with pytest.raises(FCPMCPError) as replay:
        _approve_interactively(engine, run.run_id)

    _assert_code(replay, ErrorCode.WORKFLOW_STATE_CONFLICT)
    assert ledger.get_approval(run.run_id) == first.approval
    assert ledger.get_run(run.run_id).state is WorkflowState.APPROVED


def test_concurrent_approval_race_has_one_immutable_winner(tmp_path: Path):
    engine, ledger, run = _prepared(tmp_path)
    barrier = threading.Barrier(2)
    outcomes: list[object] = []

    def approve() -> None:
        barrier.wait()
        try:
            outcomes.append(_approve_interactively(engine, run.run_id))
        except FCPMCPError as error:
            outcomes.append(error)

    threads = [threading.Thread(target=approve) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sum(not isinstance(item, FCPMCPError) for item in outcomes) == 1
    failures = [item for item in outcomes if isinstance(item, FCPMCPError)]
    assert [item.code for item in failures] == [ErrorCode.WORKFLOW_STATE_CONFLICT]
    assert ledger.get_approval(run.run_id) is not None
    assert ledger.get_run(run.run_id).state is WorkflowState.APPROVED


def test_reject_is_immutable_and_records_bounded_path_free_event(tmp_path: Path):
    engine, ledger, run = _prepared(tmp_path)

    result = engine.reject(run.run_id, reason="do not apply this edit")

    assert result.run.state is WorkflowState.REJECTED
    assert result.approval.decision is ApprovalDecision.REJECTED
    event = ledger.list_events(run.run_id)[-1]
    assert event.event_type == "approval_rejected"
    assert set(event.payload) == {
        "binding_sha256",
        "decision",
        "reason_present",
        "source",
    }
    assert "do not apply this edit" not in event.payload_text
    with pytest.raises(FCPMCPError) as replay:
        engine.reject(run.run_id)
    _assert_code(replay, ErrorCode.WORKFLOW_STATE_CONFLICT)


def test_cancel_preserves_evidence_and_records_separate_event(tmp_path: Path):
    engine, ledger, run = _prepared(tmp_path)

    result = engine.cancel(run.run_id, reason="changed my mind")

    assert result.state is WorkflowState.CANCELLED
    assert result.candidate_sha256 == run.candidate_sha256
    assert result.diff_sha256 == run.diff_sha256
    assert ledger.get_approval(run.run_id) is None
    event = ledger.list_events(run.run_id)[-1]
    assert event.event_type == "cancelled"
    assert event.payload == {"reason_present": True}
    assert "changed my mind" not in event.payload_text


def test_illegal_state_decision_is_rejected(tmp_path: Path):
    engine, _, run = _prepared(tmp_path)
    engine.cancel(run.run_id)

    with pytest.raises(FCPMCPError) as error:
        _approve_interactively(engine, run.run_id)

    _assert_code(error, ErrorCode.WORKFLOW_STATE_CONFLICT)


@pytest.mark.parametrize("action", ["reject", "cancel"])
def test_decision_reasons_are_bounded(tmp_path: Path, action: str):
    engine, ledger, run = _prepared(tmp_path)

    with pytest.raises(FCPMCPError) as error:
        getattr(engine, action)(run.run_id, reason="x" * 4097)

    _assert_code(error, ErrorCode.INVALID_ARGUMENTS)
    assert ledger.get_run(run.run_id).state is WorkflowState.AWAITING_APPROVAL


def test_effective_expiry_is_read_only(tmp_path: Path):
    now = [NOW]
    engine, ledger, run = _prepared(tmp_path, now=now)
    event_count = len(ledger.list_events(run.run_id))
    now[0] = datetime(2026, 7, 28, 12, 10, 0, tzinfo=timezone.utc)

    assert effective_state(ledger, run.run_id, now=now[0]) is WorkflowState.EXPIRED
    assert engine.effective_state(run.run_id) is WorkflowState.EXPIRED
    assert ledger.get_run(run.run_id).state is WorkflowState.AWAITING_APPROVAL
    assert len(ledger.list_events(run.run_id)) == event_count


def test_effective_state_rejects_invalid_run_clock_and_expiry(tmp_path: Path):
    _, ledger, run = _prepared(tmp_path)

    with pytest.raises(FCPMCPError) as wrong_run:
        effective_state(ledger, object(), now=NOW)
    _assert_code(wrong_run, ErrorCode.INVALID_ARGUMENTS)

    with pytest.raises(FCPMCPError) as wrong_clock:
        effective_state(ledger, run.run_id, now="not-a-clock")
    _assert_code(wrong_clock, ErrorCode.INVALID_ARGUMENTS)

    with pytest.raises(FCPMCPError) as naive_clock:
        effective_state(ledger, run.run_id, now=NOW.replace(tzinfo=None))
    _assert_code(naive_clock, ErrorCode.INVALID_ARGUMENTS)


def test_expired_approve_atomically_transitions_then_raises(tmp_path: Path):
    now = [NOW]
    engine, ledger, run = _prepared(tmp_path, now=now)
    now[0] = datetime(2026, 7, 28, 12, 10, 0, tzinfo=timezone.utc)

    with pytest.raises(FCPMCPError) as error:
        _approve_interactively(engine, run.run_id)

    _assert_code(error, ErrorCode.APPROVAL_EXPIRED)
    expired = ledger.get_run(run.run_id)
    assert expired.state is WorkflowState.EXPIRED
    assert expired.revision == run.revision + 1
    assert ledger.get_approval(run.run_id) is None
    event = ledger.list_events(run.run_id)[-1]
    assert event.event_type == "approval_expired"
    assert event.payload == {"expires_at": run.expires_at}


def test_approved_record_is_not_consumed_before_committing_cas(tmp_path: Path):
    engine, ledger, run = _prepared(tmp_path)

    approved = _approve_interactively(engine, run.run_id)

    assert approved.run.state is WorkflowState.APPROVED
    assert ledger.get_approval(run.run_id) == approved.approval
    assert ledger.get_run(run.run_id).state is WorkflowState.APPROVED


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("candidate_sha256", HASH_A),
        ("diff_sha256", HASH_B),
        ("source_path", "/private/forged-source.fcpxml"),
        ("destination_path", "/private/forged-destination.fcpxml"),
        ("expires_at", "2026-07-28T12:11:00Z"),
        ("approval_mode", ApprovalMode.CLIENT),
    ],
)
def test_public_cli_policy_ignores_same_revision_forged_snapshots(
    tmp_path: Path,
    field: str,
    value: object,
):
    _, ledger, durable = _prepared(tmp_path)
    forged = replace(durable, **{field: value})

    result = approve_cli_policy(
        ledger,
        forged.run_id,
        plan_schema_version=SCHEMA_VERSION,
        input_stream=_TTY("approve\n"),
        output_stream=_TTY(),
    )

    durable_binding = approval_binding(
        ledger,
        durable.run_id,
        plan_schema_version=SCHEMA_VERSION,
    )
    assert result.approval.binding_sha256 == durable_binding
    assert result.approval.binding_sha256 != hashlib.sha256(
        canonical_json(
            {
                "run_id": forged.run_id,
                "graph_version": forged.graph_version,
                "plan_schema_version": SCHEMA_VERSION,
                "source_path_sha256": hashlib.sha256(
                    forged.source_path.encode("utf-8")
                ).hexdigest(),
                "destination_path_sha256": hashlib.sha256(
                    forged.destination_path.encode("utf-8")
                ).hexdigest(),
                "source_sha256": forged.source_sha256,
                "prior_destination_state": forged.prior_destination_state.value,
                "prior_destination_sha256": forged.prior_destination_sha256,
                "plan_sha256": forged.plan_sha256,
                "candidate_sha256": forged.candidate_sha256,
                "diff_sha256": forged.diff_sha256,
                "approval_mode": forged.approval_mode.value,
                "expires_at": forged.expires_at,
            }
        )
    ).hexdigest()


def test_reject_and_client_staging_reload_authoritative_rows(tmp_path: Path):
    _, reject_ledger, reject_run = _prepared(tmp_path / "reject")
    forged_reject = replace(reject_run, diff_sha256=HASH_A)
    rejected = reject_policy(
        reject_ledger,
        forged_reject.run_id,
        plan_schema_version=SCHEMA_VERSION,
    )
    assert rejected.approval.binding_sha256 == approval_binding(
        reject_ledger,
        reject_run.run_id,
        plan_schema_version=SCHEMA_VERSION,
    )

    _, client_ledger, client_run = _prepared(
        tmp_path / "client",
        mode=ApprovalMode.CLIENT,
    )
    forged_client = replace(client_run, candidate_sha256=HASH_A)
    staged = approve_client_policy(
        client_ledger,
        forged_client.run_id,
        plan_schema_version=SCHEMA_VERSION,
        expect_candidate_sha256=client_run.candidate_sha256,
    )
    assert staged.binding_sha256 == approval_binding(
        client_ledger,
        client_run.run_id,
        plan_schema_version=SCHEMA_VERSION,
    )


def test_ledger_clock_is_authoritative_at_expiry_boundary(tmp_path: Path):
    engine, ledger, run = _prepared(tmp_path)
    expiry = datetime.fromisoformat(run.expires_at.removesuffix("Z") + "+00:00")
    engine.utc_clock = lambda: expiry.replace(microsecond=0) - timedelta(microseconds=1)
    ledger._clock = lambda: expiry

    with pytest.raises(FCPMCPError) as error:
        _approve_interactively(engine, run.run_id)

    _assert_code(error, ErrorCode.APPROVAL_EXPIRED)
    assert ledger.get_run(run.run_id).state is WorkflowState.EXPIRED
    assert ledger.get_approval(run.run_id) is None
    assert [event.event_type for event in ledger.list_events(run.run_id)].count(
        "approval_expired"
    ) == 1


@pytest.mark.parametrize("boundary", ["isatty", "write", "flush", "read", "type"])
def test_terminal_boundary_failures_are_bounded_coded_and_nonmutating(
    tmp_path: Path,
    boundary: str,
):
    engine, ledger, run = _prepared(tmp_path)
    stream = _ExplodingStream(boundary)
    input_stream = stream if boundary in {"isatty", "read", "type"} else _TTY("approve\n")
    output_stream = stream if boundary in {"write", "flush"} else _TTY()

    with pytest.raises(FCPMCPError) as error:
        engine.approve_cli(
            run.run_id,
            input_stream=input_stream,
            output_stream=output_stream,
        )

    _assert_code(error, ErrorCode.APPROVAL_REQUIRED)
    assert "/private/" not in str(error.value)
    assert ledger.get_run(run.run_id).state is WorkflowState.AWAITING_APPROVAL
    assert ledger.get_approval(run.run_id) is None


def test_reject_terminal_presence_is_derived_from_streams(tmp_path: Path):
    engine, _, run = _prepared(tmp_path)

    result = engine.reject(
        run.run_id,
        input_stream=_TTY(),
        output_stream=_TTY(),
    )

    assert result.approval.terminal_present is True
