from __future__ import annotations

import io
from datetime import datetime, timezone
from pathlib import Path

import pytest

from fcp_mcp.config import RuntimeConfig
from fcp_mcp.profiles import ApprovalMode
from fcp_mcp.security.paths import PathPolicy
from fcp_mcp.workflow.artifacts import ArtifactKind, ArtifactStore, StatePaths
from fcp_mcp.workflow.engine import WorkflowEngine
from fcp_mcp.workflow.ledger import WorkflowLedger
from fcp_mcp.workflow.locking import DestinationLock
from fcp_mcp.workflow.models import (
    AddMarkerOperation,
    FindingDisposition,
    RecoveryBranch,
    WorkflowPrepareRequestV1,
    WorkflowState,
)

RUN_ID = "123e4567-e89b-42d3-a456-426614174000"
ATTEMPT_ID = "223e4567-e89b-42d3-a456-426614174000"
NOW = datetime(2026, 7, 28, 12, 0, 0, tzinfo=timezone.utc)
SOURCE_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<fcpxml version="1.11"><resources><format id="r1" frameDuration="1/30s"/>
<asset id="r2" name="Clip" duration="3s"/></resources><event name="Event">
<project name="Project"><sequence format="r1" duration="3s"><spine>
<asset-clip ref="r2" name="Clip" offset="0s" start="0s" duration="3s"/>
</spine></sequence></project></event></fcpxml>"""


def make_engine(
    tmp_path: Path,
    *,
    prior_destination: bytes | None = None,
    prepare: bool = True,
) -> tuple[WorkflowEngine, WorkflowLedger, Path]:
    config = RuntimeConfig.from_env(
        {
            "FCP_MCP_OUTPUT_DIR": str(tmp_path),
            "FCP_MCP_ALLOWED_ROOTS": str(tmp_path),
            "FCP_MCP_STATE_DIR": str(tmp_path / "state"),
            "FCP_MCP_WORKFLOW_APPROVAL": ApprovalMode.CLI.value,
        },
        home=tmp_path,
    )
    paths = StatePaths.from_config(config)
    ledger = WorkflowLedger(paths, clock=lambda: NOW, package_version="0.3.0-test")
    ledger.initialize()
    engine = WorkflowEngine(
        config,
        PathPolicy(config),
        ledger,
        ArtifactStore(paths, max_artifact_bytes=config.max_artifact_bytes),
        uuid_factory=iter((RUN_ID, ATTEMPT_ID)).__next__,
        utc_clock=lambda: NOW,
        monotonic_clock=lambda: 1.0,
    )
    destination = tmp_path / "destination.fcpxml"
    if not prepare:
        return engine, ledger, destination
    source = tmp_path / "source.fcpxml"
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
                    value="Recovery",
                ),
            ),
        )
    )
    engine.approve_cli(
        preview.run_id,
        input_stream=io.StringIO(),
        output_stream=io.StringIO(),
        yes=True,
        expect_candidate_sha256=preview.candidate_sha256,
    )
    return engine, ledger, destination


def interrupt_commit(
    engine: WorkflowEngine,
    boundary: str,
) -> None:
    def fault(name: str) -> None:
        if name == boundary:
            raise RuntimeError("injected process interruption")

    engine.fault_hook = fault
    with pytest.raises(RuntimeError, match="injected process interruption"):
        engine.commit(RUN_ID)


def test_assess_is_pure_and_classifies_installed_candidate(tmp_path: Path) -> None:
    engine, _, destination = make_engine(tmp_path)
    interrupt_commit(engine, "destination_replaced")
    restarted, restarted_ledger, _ = make_engine(tmp_path, prepare=False)
    before_run = restarted_ledger.get_run(RUN_ID)
    before_events = restarted_ledger.list_events(RUN_ID, limit=100)
    before_database = restarted.artifacts.paths.database.read_bytes()
    before_destination = destination.read_bytes()
    before_state_directory_mtime = restarted.artifacts.paths.root.stat().st_mtime_ns

    assessment = restarted.assess(RUN_ID)

    assert assessment.recommended_branch is RecoveryBranch.FINALIZE_COMMITTED
    assert restarted_ledger.get_run(RUN_ID) == before_run
    assert restarted_ledger.list_events(RUN_ID, limit=100) == before_events
    assert restarted.artifacts.paths.database.read_bytes() == before_database
    assert destination.read_bytes() == before_destination
    assert (
        restarted.artifacts.paths.root.stat().st_mtime_ns
        == before_state_directory_mtime
    )


@pytest.mark.parametrize(
    ("prior", "expected"),
    (
        (None, RecoveryBranch.MARK_ROLLED_BACK),
        (b"prior destination", RecoveryBranch.MARK_ROLLED_BACK),
    ),
)
def test_assess_classifies_pre_mutation_commit_intent(
    tmp_path: Path,
    prior: bytes | None,
    expected: RecoveryBranch,
) -> None:
    engine, _, _ = make_engine(tmp_path, prior_destination=prior)
    interrupt_commit(engine, "commit_started")
    restarted, _, _ = make_engine(tmp_path, prepare=False)

    assert restarted.assess(RUN_ID).recommended_branch is expected


def test_assess_requires_matching_backup_before_finalizing(tmp_path: Path) -> None:
    engine, ledger, _ = make_engine(tmp_path, prior_destination=b"prior destination")
    interrupt_commit(engine, "destination_replaced")
    run = ledger.get_run(RUN_ID)
    assert run is not None and run.expected_backup_path is not None
    Path(run.expected_backup_path).unlink()
    restarted, _, _ = make_engine(tmp_path, prepare=False)

    assessment = restarted.assess(RUN_ID)

    assert assessment.recommended_branch is RecoveryBranch.MARK_RECOVERY_REQUIRED
    assert assessment.ambiguity_reasons


def test_reconcile_finalizes_or_marks_rollback_from_durable_evidence(
    tmp_path: Path,
) -> None:
    engine, _, destination = make_engine(tmp_path)
    interrupt_commit(engine, "destination_replaced")
    restarted, ledger, _ = make_engine(tmp_path, prepare=False)

    committed = restarted.reconcile(RUN_ID)

    assert committed.state is WorkflowState.COMMITTED
    assert committed.destination_sha256 == committed.candidate_sha256
    assert ledger.get_artifact(RUN_ID, ArtifactKind.RECEIPT) is not None
    assert destination.exists()
    assert [event.event_type for event in ledger.list_events(RUN_ID, limit=100)][
        -1
    ] == "committed"

    other = tmp_path / "other"
    other.mkdir()
    engine, _, destination = make_engine(other)
    interrupt_commit(engine, "commit_started")
    restarted, ledger, _ = make_engine(other, prepare=False)

    rolled_back = restarted.reconcile(RUN_ID)

    assert rolled_back.state is WorkflowState.ROLLED_BACK
    assert not destination.exists()
    assert [event.event_type for event in ledger.list_events(RUN_ID, limit=100)][
        -1
    ] == "commit_reconciled_rolled_back"


def test_reconcile_removes_only_proven_dead_orphan_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, ledger, destination = make_engine(tmp_path)
    orphan = DestinationLock(
        engine.artifacts.paths,
        destination,
        RUN_ID,
        pid=999_999_999,
    ).acquire()
    monkeypatch.setattr(
        "fcp_mcp.workflow.recovery.assess_owner_liveness",
        lambda pid, host: "dead",
    )

    result = engine.reconcile(RUN_ID)

    assert result.state is WorkflowState.APPROVED
    assert not orphan.path.exists()
    assert ledger.get_run(RUN_ID).state is WorkflowState.APPROVED


@pytest.mark.parametrize("owner_state", ("alive", "unknown"))
def test_reconcile_preserves_live_or_ambiguous_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    owner_state: str,
) -> None:
    engine, ledger, destination = make_engine(tmp_path)
    lock = DestinationLock(
        engine.artifacts.paths,
        destination,
        RUN_ID,
        pid=999_999_999,
    ).acquire()
    monkeypatch.setattr(
        "fcp_mcp.workflow.recovery.assess_owner_liveness",
        lambda pid, host: owner_state,
    )
    before = ledger.get_run(RUN_ID)
    before_events = ledger.list_events(RUN_ID, limit=100)

    result = engine.reconcile(RUN_ID)

    assert result == before
    assert lock.path.exists()
    assert ledger.list_events(RUN_ID, limit=100) == before_events


def test_verify_selected_committed_run_and_detect_destination_drift(
    tmp_path: Path,
) -> None:
    engine, ledger, destination = make_engine(tmp_path)
    engine.commit(RUN_ID)
    before_run = ledger.get_run(RUN_ID)
    before_events = ledger.list_events(RUN_ID, limit=100)
    before_database = engine.artifacts.paths.database.read_bytes()
    before_artifacts = {
        path.relative_to(engine.artifacts.paths.artifacts): path.read_bytes()
        for path in engine.artifacts.paths.artifacts.rglob("*")
        if path.is_file()
    }
    before_destination = destination.read_bytes()

    verified = engine.verify(RUN_ID)

    assert verified.overall is FindingDisposition.PASS
    assert verified.destination.disposition is FindingDisposition.PASS
    assert ledger.get_run(RUN_ID) == before_run
    assert ledger.list_events(RUN_ID, limit=100) == before_events
    assert engine.artifacts.paths.database.read_bytes() == before_database
    assert {
        path.relative_to(engine.artifacts.paths.artifacts): path.read_bytes()
        for path in engine.artifacts.paths.artifacts.rglob("*")
        if path.is_file()
    } == before_artifacts
    assert destination.read_bytes() == before_destination

    destination.write_bytes(b"outside mutation")
    drifted = engine.verify(RUN_ID)
    assert drifted.destination.disposition is FindingDisposition.FAIL
    assert drifted.overall is FindingDisposition.FAIL


def test_verify_detects_missing_committed_backup(tmp_path: Path) -> None:
    engine, ledger, _ = make_engine(
        tmp_path,
        prior_destination=b"prior destination",
    )
    receipt = engine.commit(RUN_ID)
    assert receipt.backup_path is not None
    Path(receipt.backup_path).unlink()
    before_run = ledger.get_run(RUN_ID)

    verified = engine.verify(RUN_ID)

    assert verified.receipt.disposition is FindingDisposition.FAIL
    assert verified.overall is FindingDisposition.FAIL
    assert ledger.get_run(RUN_ID) == before_run
