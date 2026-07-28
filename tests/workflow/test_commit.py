from __future__ import annotations

import io
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

import pytest

import fcp_mcp.workflow.engine as engine_module
from fcp_mcp.config import RuntimeConfig
from fcp_mcp.contracts import ErrorCode, FCPMCPError
from fcp_mcp.profiles import ApprovalMode
from fcp_mcp.security.paths import PathPolicy
from fcp_mcp.workflow.approval import ClientApproval
from fcp_mcp.workflow.artifacts import ArtifactKind, ArtifactStore, StatePaths
from fcp_mcp.workflow.engine import WorkflowEngine
from fcp_mcp.workflow.ledger import WorkflowLedger
from fcp_mcp.workflow.models import AddMarkerOperation, WorkflowPrepareRequestV1, WorkflowState
from fcp_mcp.workflow.surface import WorkflowRuntime

RUN_ID = "123e4567-e89b-42d3-a456-426614174000"
ATTEMPT_ID = "223e4567-e89b-42d3-a456-426614174000"
NOW = datetime(2026, 7, 28, 12, 0, 0, tzinfo=timezone.utc)
SOURCE_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<fcpxml version="1.11"><resources><format id="r1" frameDuration="1/30s"/>
<asset id="r2" name="Clip" duration="3s"/></resources><event name="Event">
<project name="Project"><sequence format="r1" duration="3s"><spine>
<asset-clip ref="r2" name="Clip" offset="0s" start="0s" duration="3s"/>
</spine></sequence></project></event></fcpxml>"""


def _engine(
    tmp_path: Path,
    *,
    mode: ApprovalMode = ApprovalMode.CLI,
    prior_destination: bytes | None = None,
    ledger_clock: Callable[[], datetime] = lambda: NOW,
    utc_clock: Callable[[], datetime] = lambda: NOW,
) -> tuple[WorkflowEngine, WorkflowLedger, Path, ClientApproval | None]:
    config = RuntimeConfig.from_env(
        {
            "FCP_MCP_OUTPUT_DIR": str(tmp_path),
            "FCP_MCP_ALLOWED_ROOTS": str(tmp_path),
            "FCP_MCP_STATE_DIR": str(tmp_path / "state"),
            "FCP_MCP_WORKFLOW_APPROVAL": mode.value,
        },
        home=tmp_path,
    )
    paths = StatePaths.from_config(config)
    ledger = WorkflowLedger(paths, clock=ledger_clock, package_version="0.3.0-test")
    ledger.initialize()
    engine = WorkflowEngine(
        config,
        PathPolicy(config),
        ledger,
        ArtifactStore(paths, max_artifact_bytes=config.max_artifact_bytes),
        uuid_factory=iter((RUN_ID, ATTEMPT_ID)).__next__,
        utc_clock=utc_clock,
        monotonic_clock=lambda: 1.0,
    )
    source = tmp_path / "source.fcpxml"
    destination = tmp_path / "destination.fcpxml"
    source.write_bytes(SOURCE_XML)
    if prior_destination is not None:
        destination.write_bytes(prior_destination)
    preview = engine.prepare(
        WorkflowPrepareRequestV1(
            source_path=str(source),
            destination_path=str(destination),
            operations=(
                AddMarkerOperation(
                    kind="add_marker",
                    clip_name="Clip",
                    start="1/30s",
                    value="Commit",
                ),
            ),
        )
    )
    if mode is ApprovalMode.CLI:
        engine.approve_cli(
            preview.run_id,
            input_stream=io.StringIO(),
            output_stream=io.StringIO(),
            yes=True,
            expect_candidate_sha256=preview.candidate_sha256,
        )
        approval = None
    else:
        approval = engine.approve_client(
            preview.run_id,
            expect_candidate_sha256=preview.candidate_sha256,
        )
    return engine, ledger, destination, approval


def test_successful_commit_has_exact_trajectory_and_idempotent_receipt(
    tmp_path: Path,
) -> None:
    engine, ledger, destination, _ = _engine(tmp_path)
    receipt = engine.commit(RUN_ID)

    run = ledger.get_run(RUN_ID)
    assert run is not None and run.state is WorkflowState.COMMITTED
    assert destination.read_bytes()
    assert receipt.commit_attempt_id == ATTEMPT_ID
    assert receipt.output_sha256 == run.candidate_sha256
    assert ledger.get_artifact(RUN_ID, ArtifactKind.RECEIPT) is not None
    events = [event.event_type for event in ledger.list_events(RUN_ID, limit=100)]
    assert events[-2:] == ["commit_started", "committed"]
    assert engine.commit(RUN_ID) == receipt


def test_commit_timestamp_preserves_subsecond_order_across_restart(
    tmp_path: Path,
) -> None:
    ledger_now = [datetime(2026, 7, 28, 12, 0, 0, 500000, tzinfo=timezone.utc)]
    committed = datetime(2026, 7, 28, 12, 0, 0, 600000, tzinfo=timezone.utc)
    engine, _, _, _ = _engine(
        tmp_path,
        ledger_clock=lambda: ledger_now[0],
        utc_clock=lambda: committed,
    )
    ledger_now[0] = datetime(
        2026, 7, 28, 12, 0, 0, 700000, tzinfo=timezone.utc
    )

    receipt = engine.commit(RUN_ID)
    restarted = WorkflowRuntime(
        RuntimeConfig.from_env(
            {
                "FCP_MCP_OUTPUT_DIR": str(tmp_path),
                "FCP_MCP_ALLOWED_ROOTS": str(tmp_path),
                "FCP_MCP_STATE_DIR": str(tmp_path / "state"),
            },
            home=tmp_path,
        )
    )

    assert receipt.committed_at == "2026-07-28T12:00:00.600000Z"
    assert restarted.status(RUN_ID).state is WorkflowState.COMMITTED


def test_commit_rehashes_under_lock_and_stales_without_mutation(tmp_path: Path) -> None:
    engine, ledger, destination, _ = _engine(tmp_path)
    before = b"outside change"
    destination.write_bytes(before)

    with pytest.raises(FCPMCPError) as caught:
        engine.commit(RUN_ID)
    assert caught.value.code is ErrorCode.WORKFLOW_STALE
    assert destination.read_bytes() == before
    run = ledger.get_run(RUN_ID)
    assert run is not None and run.state is WorkflowState.STALE
    assert list(tmp_path.glob("destination.fcpxml.bak.*")) == []


def test_client_approval_is_created_and_consumed_only_with_intent(tmp_path: Path) -> None:
    engine, ledger, destination, approval = _engine(
        tmp_path,
        mode=ApprovalMode.CLIENT,
    )
    assert approval is not None
    assert ledger.get_approval(RUN_ID) is None

    receipt = engine.commit(RUN_ID, client_approval=approval)

    assert destination.exists()
    assert receipt.approval_source.value == "client"
    assert ledger.get_approval(RUN_ID) is not None
    assert ledger.get_run(RUN_ID).state is WorkflowState.COMMITTED


@pytest.mark.parametrize(
    "boundary",
    ("commit_started", "destination_replaced", "receipt_body_written"),
)
def test_post_intent_faults_leave_exact_committing_recovery_evidence(
    tmp_path: Path,
    boundary: str,
) -> None:
    engine, ledger, _, _ = _engine(tmp_path)

    def fault(name: str) -> None:
        if name == boundary:
            raise RuntimeError("injected interruption")

    engine.fault_hook = fault
    with pytest.raises(RuntimeError, match="injected interruption"):
        engine.commit(RUN_ID)

    run = ledger.get_run(RUN_ID)
    assert run is not None and run.state is WorkflowState.COMMITTING
    assert run.commit_attempt_id == ATTEMPT_ID
    assert [event.event_type for event in ledger.list_events(RUN_ID, limit=100)][
        -1
    ] == "commit_started"
    assert list(engine.artifacts.paths.locks.glob("*.lock")) == []


def test_finalize_failure_never_claims_committed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, ledger, destination, _ = _engine(tmp_path)

    def fail(*args: object, **kwargs: object) -> object:
        raise FCPMCPError(ErrorCode.LEDGER_UNAVAILABLE, "injected finalize")

    monkeypatch.setattr(ledger, "record_commit_finalized", fail)
    with pytest.raises(FCPMCPError) as caught:
        engine.commit(RUN_ID)
    assert caught.value.code is ErrorCode.LEDGER_UNAVAILABLE
    assert destination.exists()
    assert ledger.get_run(RUN_ID).state is WorkflowState.COMMITTING
    assert list(engine.artifacts.paths.locks.glob("*.lock")) == []


def test_release_failure_after_finalization_preserves_truthful_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    engine, ledger, _, _ = _engine(tmp_path)
    original = engine_module.DestinationLock.release

    def fail_release(lock: object) -> None:
        original(lock)  # type: ignore[arg-type]
        raise FCPMCPError(ErrorCode.RECOVERY_REQUIRED, "injected release")

    monkeypatch.setattr(engine_module.DestinationLock, "release", fail_release)
    receipt = engine.commit(RUN_ID)
    assert receipt.output_sha256
    assert ledger.get_run(RUN_ID).state is WorkflowState.COMMITTED
    assert "commit_lock_release_failed" in capsys.readouterr().err


@pytest.mark.parametrize("kind", (ArtifactKind.CANDIDATE, ArtifactKind.DIFF))
def test_artifact_corruption_under_lock_never_mutates_destination(
    tmp_path: Path,
    kind: ArtifactKind,
) -> None:
    engine, ledger, destination, _ = _engine(tmp_path)
    record = ledger.get_artifact(RUN_ID, kind)
    assert record is not None
    (engine.artifacts.paths.root / record.relative_path).write_bytes(b"tampered")

    with pytest.raises(FCPMCPError) as caught:
        engine.commit(RUN_ID)
    assert caught.value.code is ErrorCode.ARTIFACT_CORRUPT
    assert not destination.exists()
    assert ledger.get_run(RUN_ID).state is WorkflowState.APPROVED
    assert list(engine.artifacts.paths.locks.glob("*.lock")) == []


def test_backup_copy_failure_after_intent_preserves_prior_and_recovery_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, ledger, destination, _ = _engine(
        tmp_path,
        prior_destination=b"prior",
    )

    def fail_copy(*args: object, **kwargs: object) -> object:
        raise OSError("injected backup copy")

    monkeypatch.setattr(engine_module, "commit_fcpxml_bytes", fail_copy)
    with pytest.raises(OSError, match="injected backup copy"):
        engine.commit(RUN_ID)

    run = ledger.get_run(RUN_ID)
    assert run is not None and run.state is WorkflowState.COMMITTING
    assert run.expected_backup_path == f"{destination}.bak.{ATTEMPT_ID}"
    assert run.backup_sha256 is not None
    assert destination.read_bytes() == b"prior"
    assert list(engine.artifacts.paths.locks.glob("*.lock")) == []


def test_lock_acquire_failure_does_not_consume_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, ledger, destination, _ = _engine(tmp_path)

    def fail_acquire(lock: object) -> object:
        raise FCPMCPError(ErrorCode.RECOVERY_REQUIRED, "injected lock conflict")

    monkeypatch.setattr(engine_module.DestinationLock, "acquire", fail_acquire)
    with pytest.raises(FCPMCPError) as caught:
        engine.commit(RUN_ID)
    assert caught.value.code is ErrorCode.RECOVERY_REQUIRED
    assert ledger.get_run(RUN_ID).state is WorkflowState.APPROVED
    assert not destination.exists()


def test_destination_swap_after_intent_never_false_finalizes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, ledger, destination, _ = _engine(
        tmp_path,
        prior_destination=b"prior",
    )
    real_commit = engine_module.commit_fcpxml_bytes

    def swap_then_commit(**kwargs: object) -> object:
        destination.write_bytes(b"swapped-after-intent")
        return real_commit(**kwargs)

    monkeypatch.setattr(engine_module, "commit_fcpxml_bytes", swap_then_commit)
    with pytest.raises(FCPMCPError) as caught:
        engine.commit(RUN_ID)
    assert caught.value.code is ErrorCode.RECOVERY_REQUIRED
    assert ledger.get_run(RUN_ID).state is WorkflowState.COMMITTING
    assert destination.exists()
    assert ledger.get_artifact(RUN_ID, ArtifactKind.RECEIPT) is None


def test_destination_symlink_swap_after_write_never_false_finalizes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, ledger, destination, _ = _engine(tmp_path)
    real_commit = engine_module.commit_fcpxml_bytes
    displaced = tmp_path / "displaced.fcpxml"

    def symlink_after_commit(**kwargs: object) -> object:
        receipt = real_commit(**kwargs)
        destination.rename(displaced)
        destination.symlink_to(displaced)
        return receipt

    monkeypatch.setattr(
        engine_module,
        "commit_fcpxml_bytes",
        symlink_after_commit,
    )

    with pytest.raises(FCPMCPError) as caught:
        engine.commit(RUN_ID)

    assert caught.value.code is ErrorCode.RECOVERY_REQUIRED
    assert ledger.get_run(RUN_ID).state is WorkflowState.COMMITTING
    assert ledger.get_artifact(RUN_ID, ArtifactKind.RECEIPT) is None
