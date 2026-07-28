from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from fcp_mcp.config import RuntimeConfig
from fcp_mcp.contracts import ErrorCode, FCPMCPError
from fcp_mcp.fcpxml.diff import append_operation_effects, build_workflow_diff
from fcp_mcp.fcpxml.parser import FCPXMLParser
from fcp_mcp.profiles import ApprovalMode
from fcp_mcp.security.paths import PathPolicy
from fcp_mcp.workflow.artifacts import (
    ArtifactKind,
    ArtifactMetadataV1,
    ArtifactStore,
    StatePaths,
)
from fcp_mcp.workflow.engine import WorkflowEngine
from fcp_mcp.workflow.ledger import ArtifactRecord, WorkflowLedger
from fcp_mcp.workflow.models import (
    AddKeywordOperation,
    AddMarkerOperation,
    PriorDestinationState,
    WorkflowPrepareRequestV1,
    WorkflowState,
    canonical_json,
)

RUN_ID = "123e4567-e89b-42d3-a456-426614174000"
NOW = datetime(2026, 7, 28, 12, 0, 0, tzinfo=timezone.utc)

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


def _config(
    tmp_path: Path,
    *,
    max_artifact_bytes: int = 4 * 1024 * 1024,
    max_diff_bytes: int = 200 * 1024,
) -> RuntimeConfig:
    return RuntimeConfig.from_env(
        {
            "FCP_MCP_OUTPUT_DIR": str(tmp_path),
            "FCP_MCP_ALLOWED_ROOTS": str(tmp_path),
            "FCP_MCP_STATE_DIR": str(tmp_path / "state"),
            "FCP_MCP_WORKFLOW_MAX_SOURCE_BYTES": str(1024 * 1024),
            "FCP_MCP_WORKFLOW_MAX_ARTIFACT_BYTES": str(max_artifact_bytes),
            "FCP_MCP_WORKFLOW_MAX_DIFF_BYTES": str(max_diff_bytes),
            "FCP_MCP_WORKFLOW_APPROVAL_TTL_SECONDS": "600",
        },
        home=tmp_path,
    )


def _engine(
    tmp_path: Path,
    *,
    max_artifact_bytes: int = 4 * 1024 * 1024,
    max_diff_bytes: int = 200 * 1024,
    uuid_factory=lambda: RUN_ID,
    **kwargs,
) -> tuple[WorkflowEngine, WorkflowLedger, ArtifactStore]:
    config = _config(
        tmp_path,
        max_artifact_bytes=max_artifact_bytes,
        max_diff_bytes=max_diff_bytes,
    )
    paths = StatePaths.from_config(config)
    ledger = WorkflowLedger(paths, clock=lambda: NOW, package_version="0.3.0-test")
    ledger.initialize()
    artifacts = ArtifactStore(paths, max_artifact_bytes=config.max_artifact_bytes)
    engine = WorkflowEngine(
        config,
        PathPolicy(config),
        ledger,
        artifacts,
        uuid_factory=uuid_factory,
        utc_clock=lambda: NOW,
        monotonic_clock=lambda: 1.0,
        **kwargs,
    )
    return engine, ledger, artifacts


def _request(
    source: Path,
    destination: Path,
    *,
    idempotency_key: str | None = None,
    expected_source_sha256: str | None = None,
    clip_name: str = "Clip",
    value: str = "Chapter",
    note: str = "safe note",
) -> WorkflowPrepareRequestV1:
    return WorkflowPrepareRequestV1(
        source_path=str(source),
        destination_path=str(destination),
        expected_source_sha256=expected_source_sha256,
        idempotency_key=idempotency_key,
        operations=(
            AddMarkerOperation(
                kind="add_marker",
                clip_name=clip_name,
                start="1001/30000s",
                duration="1001/30000s",
                value=value,
                note=note,
            ),
        ),
    )


def _metadata(record: ArtifactRecord) -> ArtifactMetadataV1:
    return ArtifactMetadataV1(
        run_id=record.run_id,
        kind=record.kind,
        relative_path=record.relative_path,
        sha256=record.sha256,
        byte_size=record.byte_size,
        created_at=datetime.fromisoformat(record.created_at.replace("Z", "+00:00")),
    )


@pytest.mark.parametrize("prior", [None, b"existing destination bytes"])
def test_prepare_has_exact_trajectory_and_never_mutates_destination(
    tmp_path: Path,
    prior: bytes | None,
):
    source = tmp_path / "source.fcpxml"
    destination = tmp_path / "destination.fcpxml"
    source.write_bytes(SOURCE_XML)
    if prior is not None:
        destination.write_bytes(prior)
    engine, ledger, artifacts = _engine(tmp_path)

    preview = engine.prepare(_request(source, destination))

    assert preview.run_id == RUN_ID
    assert preview.state is WorkflowState.AWAITING_APPROVAL
    assert preview.prior_destination_state is (
        PriorDestinationState.ABSENT
        if prior is None
        else PriorDestinationState.PRESENT
    )
    assert destination.exists() is (prior is not None)
    if prior is not None:
        assert destination.read_bytes() == prior
        assert preview.prior_destination_sha256 == hashlib.sha256(prior).hexdigest()
    assert [event.event_type for event in ledger.list_events(RUN_ID)] == [
        "run_created",
        "source_inspected",
        "plan_normalized",
        "dry_run_completed",
        "candidate_validated",
        "diff_created",
        "preview_persisted",
        "awaiting_approval",
    ]
    assert [event.sequence for event in ledger.list_events(RUN_ID)] == list(range(1, 9))
    assert ledger.get_run(RUN_ID).revision == 8
    assert (
        preview.run_uri,
        preview.events_uri,
        preview.diff_uri,
    ) == (
        f"fcp-workflow://runs/{RUN_ID}",
        f"fcp-workflow://runs/{RUN_ID}/events",
        f"fcp-workflow://runs/{RUN_ID}/diff",
    )

    candidate_record = ledger.get_artifact(RUN_ID, ArtifactKind.CANDIDATE)
    diff_record = ledger.get_artifact(RUN_ID, ArtifactKind.DIFF)
    assert candidate_record is not None and diff_record is not None
    candidate = artifacts.read(_metadata(candidate_record))
    diff_bytes = artifacts.read(_metadata(diff_record))
    assert hashlib.sha256(candidate).hexdigest() == preview.candidate_sha256
    assert hashlib.sha256(diff_bytes).hexdigest() == preview.diff_sha256
    candidate_path = tmp_path / "candidate-check.fcpxml"
    candidate_path.write_bytes(candidate)
    parsed = FCPXMLParser().parse(candidate_path)
    assert parsed.all_projects[0].sequence.spine.clips[0].markers[0].value == "Chapter"
    envelope = json.loads(diff_bytes)
    assert envelope["run_id"] == RUN_ID
    assert envelope["source_sha256"] == preview.source_sha256
    assert envelope["candidate_sha256"] == preview.candidate_sha256
    assert envelope["plan_sha256"] == preview.plan_sha256
    assert "diff_sha256" not in envelope
    assert envelope["operation_receipts"][0]["operation_id"] == "op-001"
    marker_changes = [
        change
        for change in envelope["semantic_diff"]["changes"]
        if change["entity_kind"] == "marker"
    ]
    assert len(marker_changes) == 1
    assert "value=Chapter" in marker_changes[0]["after"]
    assert "marker" in preview.summary
    assert "Chapter" in preview.summary


def test_keyword_effect_is_typed_duplicate_aware_and_summarized(tmp_path: Path):
    source = tmp_path / "source.fcpxml"
    destination = tmp_path / "destination.fcpxml"
    source.write_bytes(SOURCE_XML)
    engine, ledger, artifacts = _engine(tmp_path)
    request = WorkflowPrepareRequestV1(
        source_path=str(source),
        destination_path=str(destination),
        operations=(
            AddKeywordOperation(
                kind="add_keyword",
                clip_name="Clip",
                start="0s",
                duration="1001/30000s",
                value="Interview",
            ),
        ),
    )

    preview = engine.prepare(request)

    diff_record = ledger.get_artifact(RUN_ID, ArtifactKind.DIFF)
    assert diff_record is not None
    envelope = json.loads(artifacts.read(_metadata(diff_record)))
    changes = envelope["semantic_diff"]["changes"]
    keyword = next(
        change for change in changes if change["entity_kind"] == "keyword"
    )
    assert keyword["entity_occurrence"] == 1
    assert "value=Interview" in keyword["after"]
    assert keyword["field"] == "entity"
    assert "keyword" in preview.summary
    assert "Interview" in preview.summary


def test_prepare_rejects_pre_run_path_and_configured_operation_failures(tmp_path: Path):
    source = tmp_path / "source.fcpxml"
    source.write_bytes(SOURCE_XML)
    engine, ledger, _ = _engine(tmp_path)
    same_file = _request(source, source)

    with pytest.raises(FCPMCPError) as error:
        engine.prepare(same_file)
    assert error.value.code is ErrorCode.SAME_FILE_FORBIDDEN
    assert ledger.list_runs(limit=10) == ()

    missing = _request(tmp_path / "missing.fcpxml", tmp_path / "out.fcpxml")
    with pytest.raises(FCPMCPError) as error:
        engine.prepare(missing)
    assert error.value.code is ErrorCode.SOURCE_NOT_FOUND
    assert ledger.list_runs(limit=10) == ()


@pytest.mark.parametrize(
    ("request_mutator", "code"),
    [
        (
            lambda source, destination: _request(
                source,
                destination,
                expected_source_sha256="0" * 64,
            ),
            ErrorCode.WORKFLOW_STALE,
        ),
        (
            lambda source, destination: _request(
                source,
                destination,
                clip_name="missing",
            ),
            ErrorCode.TARGET_NOT_FOUND,
        ),
    ],
)
def test_post_creation_failures_are_coded_terminal_and_have_no_phantom_nodes(
    tmp_path: Path,
    request_mutator,
    code: ErrorCode,
):
    source = tmp_path / "source.fcpxml"
    destination = tmp_path / "destination.fcpxml"
    source.write_bytes(SOURCE_XML)
    engine, ledger, artifacts = _engine(tmp_path)

    with pytest.raises(FCPMCPError) as error:
        engine.prepare(request_mutator(source, destination))

    assert error.value.code is code
    run = ledger.get_run(RUN_ID)
    assert run is not None and run.state is WorkflowState.FAILED
    events = ledger.list_events(RUN_ID)
    assert events[-1].event_type == "failed"
    assert events[-1].payload["error_code"] == code.value
    assert "candidate_validated" not in [event.event_type for event in events]
    assert not destination.exists()
    failure_record = ledger.get_artifact(RUN_ID, ArtifactKind.FAILURE_EVIDENCE)
    assert failure_record is not None
    failure_evidence = json.loads(artifacts.read(_metadata(failure_record)))
    assert failure_evidence["schema_version"] == "1"
    assert failure_evidence["run_id"] == RUN_ID
    assert failure_evidence["error_code"] == code.value
    if code is ErrorCode.WORKFLOW_STALE:
        assert failure_evidence["operation_receipts"] == []
    else:
        assert failure_evidence["operation_receipts"][-1]["disposition"] == "failed"
        assert failure_evidence["operation_receipts"][-1]["error_code"] == code.value
    assert set(failure_evidence) == {
        "schema_version",
        "graph_version",
        "run_version",
        "run_id",
        "plan_sha256",
        "error_code",
        "operation_receipts",
    }


def test_failure_evidence_persistence_failure_preserves_primary_without_false_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.fcpxml"
    destination = tmp_path / "destination.fcpxml"
    source.write_bytes(SOURCE_XML)
    engine, ledger, artifacts = _engine(tmp_path)
    original_write = artifacts.write

    def reject_failure_evidence(
        run_id: str,
        kind: ArtifactKind,
        body: bytes,
    ) -> ArtifactMetadataV1:
        if kind is ArtifactKind.FAILURE_EVIDENCE:
            raise FCPMCPError(
                ErrorCode.ARTIFACT_CORRUPT,
                "private failure evidence unavailable",
            )
        return original_write(run_id, kind, body)

    monkeypatch.setattr(artifacts, "write", reject_failure_evidence)
    request = _request(
        source,
        destination,
        expected_source_sha256="0" * 64,
    )

    with pytest.raises(FCPMCPError) as error:
        engine.prepare(request)

    assert error.value.code is ErrorCode.WORKFLOW_STALE
    run = ledger.get_run(RUN_ID)
    assert run is not None and run.state is WorkflowState.PREPARING
    assert run.terminal_error_code is None
    assert ledger.get_artifact(RUN_ID, ArtifactKind.FAILURE_EVIDENCE) is None
    assert [event.event_type for event in ledger.list_events(RUN_ID)] == [
        "run_created",
        "source_inspected",
    ]


class SecondaryAbort(BaseException):
    pass


@pytest.mark.parametrize(
    ("failure_point", "secondary_type"),
    [
        ("artifact_write", OSError),
        ("artifact_read", RuntimeError),
        ("ledger", SecondaryAbort),
    ],
)
def test_any_secondary_failure_preserves_primary_code_and_sanitized_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
    secondary_type: type[BaseException],
) -> None:
    source = tmp_path / "source.fcpxml"
    destination = tmp_path / "destination.fcpxml"
    source.write_bytes(SOURCE_XML)
    engine, ledger, artifacts = _engine(tmp_path)
    secret = "secondary-secret-must-not-escape"
    original_write = artifacts.write
    original_read = artifacts.read
    original_record = ledger.record_prepare_failure

    def fail_write(run_id: str, kind: ArtifactKind, body: bytes):
        if kind is ArtifactKind.FAILURE_EVIDENCE and failure_point == "artifact_write":
            raise secondary_type(secret)
        return original_write(run_id, kind, body)

    def fail_read(metadata: ArtifactMetadataV1):
        if (
            metadata.kind is ArtifactKind.FAILURE_EVIDENCE
            and failure_point == "artifact_read"
        ):
            raise secondary_type(secret)
        return original_read(metadata)

    def fail_record(*args, **kwargs):
        if failure_point == "ledger":
            raise secondary_type(secret)
        return original_record(*args, **kwargs)

    monkeypatch.setattr(artifacts, "write", fail_write)
    monkeypatch.setattr(artifacts, "read", fail_read)
    monkeypatch.setattr(ledger, "record_prepare_failure", fail_record)

    with pytest.raises(FCPMCPError) as error:
        engine.prepare(
            _request(
                source,
                destination,
                expected_source_sha256="0" * 64,
            )
        )

    assert error.value.code is ErrorCode.WORKFLOW_STALE
    primary = error.value.__cause__
    assert isinstance(primary, FCPMCPError)
    assert primary.code is ErrorCode.WORKFLOW_STALE
    secondary = primary.__context__
    assert isinstance(secondary, FCPMCPError)
    assert secondary.code is ErrorCode.TRANSACTION_FAILED
    assert secret not in str(error.value)
    assert secret not in str(primary)
    assert secret not in str(secondary)
    run = ledger.get_run(RUN_ID)
    assert run is not None and run.state is WorkflowState.PREPARING
    assert run.terminal_error_code is None
    assert [event.event_type for event in ledger.list_events(RUN_ID)] == [
        "run_created",
        "source_inspected",
    ]


def test_exact_idempotency_duplicate_rehydrates_without_second_execution(tmp_path: Path):
    source = tmp_path / "source.fcpxml"
    destination = tmp_path / "destination.fcpxml"
    source.write_bytes(SOURCE_XML)
    generated = iter((RUN_ID, "123e4567-e89b-42d3-a456-426614174001"))
    engine, ledger, _ = _engine(tmp_path, uuid_factory=lambda: next(generated))
    request = _request(source, destination, idempotency_key="same-request")

    first = engine.prepare(request)
    second = engine.prepare(request)

    assert second == first
    assert len(ledger.list_events(RUN_ID)) == 8
    assert ledger.get_run("123e4567-e89b-42d3-a456-426614174001") is None


@pytest.mark.parametrize("clip_name", ["Clip", "missing"])
def test_prepare_calls_execute_plan_exactly_once_for_success_and_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clip_name: str,
) -> None:
    source = tmp_path / "source.fcpxml"
    destination = tmp_path / "destination.fcpxml"
    source.write_bytes(SOURCE_XML)
    engine, _, _ = _engine(tmp_path)
    original = engine.execute_plan
    calls = 0

    def counted(path, plan):
        nonlocal calls
        calls += 1
        return original(path, plan)

    monkeypatch.setattr(engine, "execute_plan", counted)
    if clip_name == "missing":
        with pytest.raises(FCPMCPError):
            engine.prepare(_request(source, destination, clip_name=clip_name))
    else:
        engine.prepare(_request(source, destination, clip_name=clip_name))

    assert calls == 1


def test_idempotency_conflict_and_nonawaiting_duplicate_are_stable(tmp_path: Path):
    source = tmp_path / "source.fcpxml"
    destination = tmp_path / "destination.fcpxml"
    source.write_bytes(SOURCE_XML)
    engine, ledger, _ = _engine(tmp_path)
    first = _request(source, destination, idempotency_key="key")
    engine.prepare(first)

    with pytest.raises(FCPMCPError) as error:
        engine.prepare(
            _request(
                source,
                destination,
                idempotency_key="key",
                value="different",
            )
        )
    assert error.value.code is ErrorCode.IDEMPOTENCY_CONFLICT

    run = ledger.get_run(RUN_ID)
    assert run is not None
    ledger.transition(
        RUN_ID,
        expected_state=WorkflowState.AWAITING_APPROVAL,
        expected_revision=run.revision,
        target_state=WorkflowState.CANCELLED,
        event_type="cancelled",
        payload={},
    )
    with pytest.raises(FCPMCPError) as error:
        engine.prepare(first)
    assert error.value.code is ErrorCode.WORKFLOW_STATE_CONFLICT
    assert RUN_ID in error.value.message
    assert "cancelled" in error.value.message


def test_concurrent_duplicate_callers_create_one_run_and_one_graph(tmp_path: Path):
    source = tmp_path / "source.fcpxml"
    destination = tmp_path / "destination.fcpxml"
    source.write_bytes(SOURCE_XML)
    identifiers = iter(
        (
            RUN_ID,
            "123e4567-e89b-42d3-a456-426614174001",
            "123e4567-e89b-42d3-a456-426614174002",
        )
    )
    id_lock = threading.Lock()

    def next_id():
        with id_lock:
            return next(identifiers)

    engine, ledger, _ = _engine(tmp_path, uuid_factory=next_id)
    request = _request(source, destination, idempotency_key="concurrent")
    barrier = threading.Barrier(2)
    results = []
    errors = []

    def call():
        barrier.wait()
        try:
            results.append(engine.prepare(request))
        except FCPMCPError as error:
            errors.append(error)

    threads = [threading.Thread(target=call) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == 1
    assert len(errors) == 1
    assert errors[0].code is ErrorCode.WORKFLOW_STATE_CONFLICT
    runs = ledger.list_runs(limit=10)
    assert len(runs) == 1
    assert [
        event.event_type for event in ledger.list_events(runs[0].run_id)
    ].count("run_created") == 1


def test_typed_diff_is_duplicate_aware_rational_and_canonical(tmp_path: Path):
    before = tmp_path / "before.fcpxml"
    after = tmp_path / "after.fcpxml"
    before.write_text(
        """<fcpxml version="1.11"><event name="E">
<project name="Same"><sequence><spine><asset-clip name="Clip" ref="r1" offset="0s" start="0s" duration="1/3s"/></spine></sequence></project>
<project name="Same"><sequence><spine><asset-clip name="Clip" ref="r1" offset="1/3s" start="0s" duration="1/3s"/></spine></sequence></project>
</event></fcpxml>""",
        encoding="utf-8",
    )
    after.write_text(
        """<fcpxml version="1.11"><event name="E">
<project name="Same"><sequence><spine><asset-clip name="Clip" ref="r1" offset="0s" start="0s" duration="2/6s"/></spine></sequence></project>
<project name="Same"><sequence><spine><asset-clip name="Clip" ref="r1" offset="2/3s" start="0s" duration="1/3s"/></spine></sequence></project>
</event></fcpxml>""",
        encoding="utf-8",
    )

    diff = build_workflow_diff(FCPXMLParser().parse(before), FCPXMLParser().parse(after))
    payload = diff.to_mapping()

    assert payload["project_count_source"] == 2
    assert payload["project_count_candidate"] == 2
    assert any(
        change["project_occurrence"] == 2
        and change["field"] == "offset"
        and change["before"] == "1/3s"
        and change["after"] == "2/3s"
        for change in payload["changes"]
    )
    assert canonical_json(payload) == canonical_json(dict(reversed(tuple(payload.items()))))


def test_typed_diff_binds_all_verified_operation_effects(tmp_path: Path):
    document_path = tmp_path / "same.fcpxml"
    document_path.write_bytes(SOURCE_XML)
    document = FCPXMLParser().parse(document_path)
    receipts = (
        SimpleNamespace(
            operation_id="op-001",
            kind="add_transition",
            changes=(
                SimpleNamespace(field="transition_count", before="0", after="1"),
            ),
        ),
        SimpleNamespace(
            operation_id="op-002",
            kind="change_speed",
            changes=(
                SimpleNamespace(field="duration", before="2s", after="1s"),
                SimpleNamespace(field="speed_factor", before=None, after="2.0"),
            ),
        ),
    )

    diff = append_operation_effects(
        build_workflow_diff(document, document),
        receipts,
    )
    payload = diff.to_mapping()

    assert payload["change_count"] == 3
    assert [
        (
            change["entity_kind"],
            change["entity_name"],
            change["field"],
            change["before"],
            change["after"],
        )
        for change in payload["changes"]
    ] == [
        ("operation_effect", "op-001", "add_transition.transition_count", "0", "1"),
        ("operation_effect", "op-002", "change_speed.duration", "2s", "1s"),
        ("operation_effect", "op-002", "change_speed.speed_factor", None, "2.0"),
    ]
