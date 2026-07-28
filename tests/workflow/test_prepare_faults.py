from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_prepare import RUN_ID, SOURCE_XML, _engine, _metadata, _request

import fcp_mcp.workflow.engine as engine_module
from fcp_mcp.contracts import ErrorCode, FCPMCPError
from fcp_mcp.workflow.artifacts import ArtifactKind
from fcp_mcp.workflow.engine import WorkflowEngine
from fcp_mcp.workflow.models import WorkflowState


class InjectedCrash(BaseException):
    pass


@pytest.mark.parametrize(
    "boundary",
    [
        "run_created",
        "source_inspected",
        "plan_normalized",
        "dry_run_completed",
        "candidate_body_written",
        "candidate_validated",
        "diff_body_written",
        "diff_created",
        "preview_persisted",
    ],
)
def test_crash_after_every_prepare_boundary_never_guesses_success(
    tmp_path: Path,
    boundary: str,
):
    source = tmp_path / "source.fcpxml"
    destination = tmp_path / "destination.fcpxml"
    source.write_bytes(SOURCE_XML)

    def crash(name: str) -> None:
        if name == boundary:
            raise InjectedCrash(name)

    engine, ledger, _ = _engine(tmp_path, fault_hook=crash)

    with pytest.raises(InjectedCrash):
        engine.prepare(_request(source, destination))

    run = ledger.get_run(RUN_ID)
    assert run is not None and run.state is WorkflowState.PREPARING
    assert not destination.exists()
    assert "awaiting_approval" not in [
        event.event_type for event in ledger.list_events(RUN_ID)
    ]

    restarted = WorkflowEngine(
        engine.config,
        engine.path_policy,
        ledger,
        engine.artifacts,
        uuid_factory=lambda: "123e4567-e89b-42d3-a456-426614174099",
        utc_clock=engine.utc_clock,
        monotonic_clock=engine.monotonic_clock,
    )
    if boundary == "run_created":
        with pytest.raises(FCPMCPError) as error:
            restarted.reconcile_preparing(RUN_ID)
        assert error.value.code is ErrorCode.RECOVERY_REQUIRED
        assert ledger.get_run(RUN_ID).state is WorkflowState.PREPARING
    else:
        reconciled = restarted.reconcile_preparing(RUN_ID)
        assert reconciled.state is WorkflowState.FAILED
        assert ledger.list_events(RUN_ID)[-1].event_type == "prepare_reconciled_failed"
        assert restarted.reconcile_preparing(RUN_ID).state is WorkflowState.FAILED
        assert [
            event.event_type for event in ledger.list_events(RUN_ID)
        ].count("prepare_reconciled_failed") == 1


def test_reconciliation_leaves_ambiguous_changed_destination_preparing(tmp_path: Path):
    source = tmp_path / "source.fcpxml"
    destination = tmp_path / "destination.fcpxml"
    source.write_bytes(SOURCE_XML)

    def crash(name: str) -> None:
        if name == "source_inspected":
            raise InjectedCrash(name)

    engine, ledger, _ = _engine(tmp_path, fault_hook=crash)
    with pytest.raises(InjectedCrash):
        engine.prepare(_request(source, destination))
    destination.write_bytes(b"created after inspection")

    with pytest.raises(FCPMCPError) as error:
        engine.reconcile_preparing(RUN_ID)

    assert error.value.code is ErrorCode.RECOVERY_REQUIRED
    assert ledger.get_run(RUN_ID).state is WorkflowState.PREPARING


def test_source_drift_after_execution_fails_stale_without_destination_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    source = tmp_path / "source.fcpxml"
    destination = tmp_path / "destination.fcpxml"
    source.write_bytes(SOURCE_XML)
    engine, ledger, artifacts = _engine(tmp_path)
    original = engine.execute_plan

    def drifting_execute(path, plan):
        result = original(path, plan)
        source.write_bytes(SOURCE_XML.replace(b'name="Clip"', b'name="Drft"'))
        return result

    monkeypatch.setattr(engine, "execute_plan", drifting_execute)

    with pytest.raises(FCPMCPError) as error:
        engine.prepare(_request(source, destination))

    assert error.value.code is ErrorCode.WORKFLOW_STALE
    assert ledger.get_run(RUN_ID).state is WorkflowState.FAILED
    failure_record = ledger.get_artifact(RUN_ID, ArtifactKind.FAILURE_EVIDENCE)
    assert failure_record is not None
    evidence = json.loads(artifacts.read(_metadata(failure_record)))
    assert [
        receipt["disposition"]
        for receipt in evidence["operation_receipts"]
    ] == ["succeeded"]
    assert not destination.exists()


def test_destination_is_never_opened_for_write(tmp_path: Path, monkeypatch):
    source = tmp_path / "source.fcpxml"
    destination = tmp_path / "destination.fcpxml"
    source.write_bytes(SOURCE_XML)
    engine, _, _ = _engine(tmp_path)
    opened_for_write = []
    original_open = Path.open

    def guarded_open(path, mode="r", *args, **kwargs):
        if Path(path) == destination and any(flag in mode for flag in "wax+"):
            opened_for_write.append(mode)
            raise AssertionError("prepare opened destination for write")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    engine.prepare(_request(source, destination))

    assert opened_for_write == []
    assert not destination.exists()


def test_private_parser_copy_handles_short_writes(tmp_path: Path, monkeypatch):
    source = tmp_path / "source.fcpxml"
    destination = tmp_path / "destination.fcpxml"
    source.write_bytes(SOURCE_XML)
    engine, _, _ = _engine(tmp_path)
    real_write = engine_module.os.write

    def short_write(descriptor: int, payload) -> int:
        return real_write(descriptor, payload[: max(1, len(payload) // 2)])

    monkeypatch.setattr(engine_module.os, "write", short_write)

    preview = engine.prepare(_request(source, destination))

    assert preview.state is WorkflowState.AWAITING_APPROVAL
