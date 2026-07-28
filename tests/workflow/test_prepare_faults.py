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


@pytest.mark.parametrize("boundary", ["source_inspected", "plan_normalized"])
def test_repeated_interruptions_close_source_snapshot_exactly_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    descriptors_before = set(engine_module.os.listdir("/dev/fd"))
    original_open = engine_module.os.open
    original_close = engine_module.os.close
    active_snapshot_fds: set[int] = set()
    snapshot_opens = 0
    snapshot_closes = 0

    def observe_open(path, *args, **kwargs):
        nonlocal snapshot_opens
        descriptor = original_open(path, *args, **kwargs)
        if Path(path).name.startswith(".workflow-snapshot-"):
            snapshot_opens += 1
            active_snapshot_fds.add(descriptor)
        return descriptor

    def observe_close(descriptor: int) -> None:
        nonlocal snapshot_closes
        if descriptor in active_snapshot_fds:
            snapshot_closes += 1
            active_snapshot_fds.remove(descriptor)
        original_close(descriptor)

    monkeypatch.setattr(engine_module.os, "open", observe_open)
    monkeypatch.setattr(engine_module.os, "close", observe_close)
    for index in range(5):
        root = tmp_path / str(index)
        root.mkdir()
        source = root / "source.fcpxml"
        destination = root / "destination.fcpxml"
        source.write_bytes(SOURCE_XML)

        def crash(name: str) -> None:
            if name == boundary:
                raise InjectedCrash(name)

        engine, _, _ = _engine(root, fault_hook=crash)
        with pytest.raises(InjectedCrash):
            engine.prepare(_request(source, destination))

        assert not any(
            SOURCE_XML[:32] in path.read_bytes()
            for path in engine.artifacts.paths.root.rglob("*")
            if path.is_file()
        )

    assert set(engine_module.os.listdir("/dev/fd")) == descriptors_before
    assert snapshot_opens == snapshot_closes == 5
    assert active_snapshot_fds == set()


def test_cleanup_baseexception_never_masks_active_interruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.fcpxml"
    destination = tmp_path / "destination.fcpxml"
    source.write_bytes(SOURCE_XML)
    original_open = engine_module.os.open
    original_close = engine_module.os.close
    snapshot_fd: int | None = None
    injected = False

    def observe_open(path, *args, **kwargs):
        nonlocal snapshot_fd
        descriptor = original_open(path, *args, **kwargs)
        if Path(path).name.startswith(".workflow-snapshot-"):
            snapshot_fd = descriptor
        return descriptor

    def fail_snapshot_close(descriptor: int) -> None:
        nonlocal injected
        original_close(descriptor)
        if descriptor == snapshot_fd:
            injected = True
            raise RuntimeError("cleanup secret")

    def crash(name: str) -> None:
        if name == "source_inspected":
            raise InjectedCrash(name)

    monkeypatch.setattr(engine_module.os, "open", observe_open)
    monkeypatch.setattr(engine_module.os, "close", fail_snapshot_close)
    engine, _, _ = _engine(tmp_path, fault_hook=crash)

    with pytest.raises(InjectedCrash, match="source_inspected"):
        engine.prepare(_request(source, destination))

    assert injected is True


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
    root = engine.artifacts.paths.root
    original_unlink = engine_module.os.unlink
    raw_path: Path | None = None

    def observe_unlink(path, *args, **kwargs):
        nonlocal raw_path
        target = _snapshot_attack_path(root, path)
        result = original_unlink(path, *args, **kwargs)
        if target.name.startswith(".workflow-snapshot-"):
            raw_path = target
        return result

    original_validate = engine.validator.validate_file

    def replace_old_name(path: Path):
        assert raw_path is not None and not raw_path.exists()
        raw_path.write_bytes(b"<attacker/>")
        return original_validate(path)

    monkeypatch.setattr(engine_module.os, "unlink", observe_unlink)
    monkeypatch.setattr(engine.validator, "validate_file", replace_old_name)
    try:
        result = engine._validate_candidate(SOURCE_XML)
    finally:
        if raw_path is not None:
            original_unlink(raw_path)

    assert result.valid is True


def _snapshot_attack_path(root: Path, raw_path: object) -> Path:
    path = Path(raw_path)
    return path if path.is_absolute() else root / path


@pytest.mark.parametrize("attack", ["rename", "decoy", "hardlink"])
def test_snapshot_link_attacks_fail_before_plaintext_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    attack: str,
) -> None:
    engine, _, _ = _engine(tmp_path)
    root = engine.artifacts.paths.root
    original_unlink = engine_module.os.unlink
    injected = False
    retained = root / f"attacker-{attack}.fcpxml"

    def attack_unlink(raw_path, *args, **kwargs):
        nonlocal injected
        target = _snapshot_attack_path(root, raw_path)
        if target.name.startswith(".workflow-snapshot-") and not injected:
            injected = True
            if attack == "hardlink":
                engine_module.os.link(target, retained)
            else:
                target.rename(retained)
                if attack == "decoy":
                    target.write_bytes(b"attacker-decoy")
        return original_unlink(raw_path, *args, **kwargs)

    monkeypatch.setattr(engine_module.os, "unlink", attack_unlink)
    try:
        with pytest.raises(FCPMCPError) as error:
            engine._validate_candidate(SOURCE_XML)
        assert error.value.code is ErrorCode.TRANSACTION_FAILED
        assert retained.exists()
        assert SOURCE_XML[:32] not in retained.read_bytes()
        assert not any(
            SOURCE_XML[:32] in path.read_bytes()
            for path in root.iterdir()
            if path.is_file()
        )
    finally:
        retained.unlink(missing_ok=True)
        for path in root.glob(".workflow-snapshot-*"):
            path.unlink(missing_ok=True)

    assert injected is True


def test_snapshot_unlink_failure_is_coded_and_copies_no_plaintext(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, _, _ = _engine(tmp_path)
    root = engine.artifacts.paths.root
    original_unlink = engine_module.os.unlink
    injected = False

    def fail_unlink(raw_path, *args, **kwargs):
        nonlocal injected
        target = _snapshot_attack_path(root, raw_path)
        if target.name.startswith(".workflow-snapshot-"):
            injected = True
            raise OSError("injected unlink failure secret")
        return original_unlink(raw_path, *args, **kwargs)

    monkeypatch.setattr(engine_module.os, "unlink", fail_unlink)
    try:
        with pytest.raises(FCPMCPError) as error:
            engine._validate_candidate(SOURCE_XML)
        assert error.value.code is ErrorCode.TRANSACTION_FAILED
        assert "secret" not in str(error.value)
        assert not any(
            SOURCE_XML[:32] in path.read_bytes()
            for path in root.iterdir()
            if path.is_file()
        )
    finally:
        monkeypatch.setattr(engine_module.os, "unlink", original_unlink)
        for path in root.glob(".workflow-snapshot-*"):
            path.unlink(missing_ok=True)

    assert injected is True


def test_candidate_snapshot_fsync_failure_leaves_no_named_plaintext(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, _, _ = _engine(tmp_path)
    original_fsync = engine_module.os.fsync
    original_open = engine_module.os.open
    target_fd: int | None = None

    def observe_open(path, *args, **kwargs):
        nonlocal target_fd
        descriptor = original_open(path, *args, **kwargs)
        if Path(path).name.startswith(".workflow-snapshot-"):
            target_fd = descriptor
        return descriptor

    def fail_fsync(descriptor: int) -> None:
        if descriptor == target_fd:
            raise OSError("injected snapshot fsync failure")
        original_fsync(descriptor)

    monkeypatch.setattr(engine_module.os, "open", observe_open)
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
    original_open = engine_module.os.open
    target_fd: int | None = None
    close_fault_injected = False

    def observe_open(path, *args, **kwargs):
        nonlocal target_fd
        descriptor = original_open(path, *args, **kwargs)
        if Path(path).name.startswith(".workflow-snapshot-"):
            target_fd = descriptor
        return descriptor

    def fail_validation(path: Path):
        raise RuntimeError("injected validator failure")

    def fail_close(descriptor: int) -> None:
        nonlocal close_fault_injected
        original_close(descriptor)
        if descriptor == target_fd:
            close_fault_injected = True
            raise OSError("injected snapshot close failure")

    monkeypatch.setattr(engine_module.os, "open", observe_open)
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
    root = engine.artifacts.paths.root
    original_unlink = engine_module.os.unlink
    injected = False

    def transient_unlink(path, *args, **kwargs):
        nonlocal injected
        target = _snapshot_attack_path(root, path)
        if target.name.startswith(".workflow-snapshot-") and not injected:
            injected = True
            raise OSError("injected transient unlink failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(engine_module.os, "unlink", transient_unlink)

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
    original_open = engine_module.os.open
    target_fd: int | None = None
    created = 0

    def observe_open(path, *args, **kwargs):
        nonlocal created, target_fd
        descriptor = original_open(path, *args, **kwargs)
        if Path(path).name.startswith(".workflow-snapshot-"):
            created += 1
            if created == 2:
                target_fd = descriptor
        return descriptor

    original_write = engine_module.os.write

    def fail_diff_write(descriptor: int, payload) -> int:
        if descriptor == target_fd:
            raise OSError("injected candidate snapshot write failure")
        return original_write(descriptor, payload)

    monkeypatch.setattr(engine_module.os, "open", observe_open)
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
