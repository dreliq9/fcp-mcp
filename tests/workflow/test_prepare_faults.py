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


def test_source_aba_never_mixes_hashed_and_parsed_snapshots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.fcpxml"
    destination = tmp_path / "destination.fcpxml"
    source.write_bytes(SOURCE_XML)
    snapshot_b = SOURCE_XML.replace(b'offset="0s"', b'offset="1s"')
    engine, ledger, artifacts = _engine(tmp_path)
    original_parse = engine_module.FCPXMLParser.parse
    calls = 0

    def aba_parse(parser, path):
        nonlocal calls
        calls += 1
        if calls == 1:
            source.write_bytes(snapshot_b)
            try:
                return original_parse(parser, path)
            finally:
                source.write_bytes(SOURCE_XML)
        return original_parse(parser, path)

    monkeypatch.setattr(engine_module.FCPXMLParser, "parse", aba_parse)

    engine.prepare(_request(source, destination))

    diff = ledger.get_artifact(RUN_ID, ArtifactKind.DIFF)
    assert diff is not None
    envelope = json.loads(artifacts.read(_metadata(diff)))
    assert not any(
        change["field"] == "offset"
        for change in envelope["semantic_diff"]["changes"]
    )


def test_candidate_validation_uses_only_unlinked_descriptor_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, _, _ = _engine(tmp_path)
    original = engine.validator.validate_file
    observed: list[Path] = []

    def inspect(path: Path):
        observed.append(Path(path))
        assert str(path).startswith("/dev/fd/")
        assert list(engine.artifacts.paths.root.glob(".candidate-validate-*")) == []
        return original(path)

    monkeypatch.setattr(engine.validator, "validate_file", inspect)

    result = engine._validate_candidate(SOURCE_XML)

    assert result.valid is True
    assert len(observed) == 1


def test_candidate_named_path_recreation_cannot_replace_open_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, _, _ = _engine(tmp_path)
    original_mkstemp = engine_module.tempfile.mkstemp
    raw_path: Path | None = None

    def observe_mkstemp(*args, **kwargs):
        nonlocal raw_path
        descriptor, path = original_mkstemp(*args, **kwargs)
        raw_path = Path(path)
        return descriptor, path

    original_validate = engine.validator.validate_file

    def replace_old_name(path: Path):
        assert raw_path is not None and not raw_path.exists()
        raw_path.write_bytes(b"<attacker/>")
        return original_validate(path)

    monkeypatch.setattr(engine_module.tempfile, "mkstemp", observe_mkstemp)
    monkeypatch.setattr(engine.validator, "validate_file", replace_old_name)
    try:
        result = engine._validate_candidate(SOURCE_XML)
    finally:
        if raw_path is not None:
            raw_path.unlink(missing_ok=True)

    assert result.valid is True


def test_candidate_snapshot_fsync_failure_leaves_no_named_plaintext(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, _, _ = _engine(tmp_path)
    original_fsync = engine_module.os.fsync
    original_mkstemp = engine_module.tempfile.mkstemp
    target_fd: int | None = None

    def observe_mkstemp(*args, **kwargs):
        nonlocal target_fd
        descriptor, path = original_mkstemp(*args, **kwargs)
        target_fd = descriptor
        return descriptor, path

    def fail_fsync(descriptor: int) -> None:
        if descriptor == target_fd:
            raise OSError("injected snapshot fsync failure")
        original_fsync(descriptor)

    monkeypatch.setattr(engine_module.tempfile, "mkstemp", observe_mkstemp)
    monkeypatch.setattr(engine_module.os, "fsync", fail_fsync)

    with pytest.raises(OSError, match="injected snapshot fsync failure"):
        engine._validate_candidate(SOURCE_XML)

    assert list(engine.artifacts.paths.root.glob(".workflow-snapshot-*")) == []


def test_candidate_snapshot_close_failure_preserves_validation_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, _, _ = _engine(tmp_path)
    original_close = engine_module.os.close
    original_mkstemp = engine_module.tempfile.mkstemp
    target_fd: int | None = None
    close_fault_injected = False

    def observe_mkstemp(*args, **kwargs):
        nonlocal target_fd
        descriptor, path = original_mkstemp(*args, **kwargs)
        target_fd = descriptor
        return descriptor, path

    def fail_validation(path: Path):
        raise RuntimeError("injected validator failure")

    def fail_close(descriptor: int) -> None:
        nonlocal close_fault_injected
        original_close(descriptor)
        if descriptor == target_fd:
            close_fault_injected = True
            raise OSError("injected snapshot close failure")

    monkeypatch.setattr(engine_module.tempfile, "mkstemp", observe_mkstemp)
    monkeypatch.setattr(engine.validator, "validate_file", fail_validation)
    monkeypatch.setattr(engine_module.os, "close", fail_close)

    with pytest.raises(FCPMCPError) as error:
        engine._validate_candidate(SOURCE_XML)

    assert error.value.code is ErrorCode.VALIDATION_FAILED
    assert close_fault_injected is True
    assert list(engine.artifacts.paths.root.glob(".workflow-snapshot-*")) == []


def test_snapshot_unlink_retry_never_exposes_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, _, _ = _engine(tmp_path)
    original_unlink = Path.unlink
    injected = False

    def transient_unlink(path: Path, *args, **kwargs):
        nonlocal injected
        if path.name.startswith(".workflow-snapshot-") and not injected:
            injected = True
            raise OSError("injected transient unlink failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", transient_unlink)

    result = engine._validate_candidate(SOURCE_XML)

    assert result.valid is True
    assert injected is True
    assert list(engine.artifacts.paths.root.glob(".workflow-snapshot-*")) == []


def test_candidate_snapshot_write_failure_leaves_no_named_plaintext(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.fcpxml"
    destination = tmp_path / "destination.fcpxml"
    source.write_bytes(SOURCE_XML)
    engine, _, _ = _engine(tmp_path)
    original_mkstemp = engine_module.tempfile.mkstemp
    target_fd: int | None = None
    created = 0

    def observe_mkstemp(*args, **kwargs):
        nonlocal created, target_fd
        descriptor, path = original_mkstemp(*args, **kwargs)
        created += 1
        if created == 2:
            target_fd = descriptor
        return descriptor, path

    original_write = engine_module.os.write

    def fail_diff_write(descriptor: int, payload) -> int:
        if descriptor == target_fd:
            raise OSError("injected candidate snapshot write failure")
        return original_write(descriptor, payload)

    monkeypatch.setattr(engine_module.tempfile, "mkstemp", observe_mkstemp)
    monkeypatch.setattr(engine_module.os, "write", fail_diff_write)

    with pytest.raises(FCPMCPError):
        engine.prepare(_request(source, destination))

    assert list(engine.artifacts.paths.root.glob(".workflow-snapshot-*")) == []
    assert not any(
        SOURCE_XML[:32] in path.read_bytes()
        for path in engine.artifacts.paths.root.iterdir()
        if path.is_file()
    )


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
