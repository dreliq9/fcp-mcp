from __future__ import annotations

import copy
import math
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import TypeAdapter, ValidationError

from fcp_mcp.contracts import ErrorCode
from fcp_mcp.profiles import ApprovalMode
from fcp_mcp.workflow.models import (
    AddKeywordOperation,
    AddMarkerOperation,
    AddTransitionOperation,
    ApprovalDecision,
    ApprovalSource,
    ApprovalStrength,
    ArtifactAvailabilityV1,
    ArtifactState,
    AssignRoleOperation,
    BatchApplyTransitionOperation,
    BatchAssignRolesOperation,
    BatchRenameClipsOperation,
    BatchRoleAssignmentRule,
    ChangeSpeedOperation,
    DeleteClipsOperation,
    FillGapsOperation,
    FindingDisposition,
    FixFlashFramesOperation,
    LockState,
    ObservedFileState,
    OperationDisposition,
    OperationReceiptV1,
    PriorDestinationState,
    PruneDisposition,
    RecoveryAssessmentV1,
    RecoveryBranch,
    ReorderClipsOperation,
    SplitClipOperation,
    TrimClipOperation,
    ValidationIssueV1,
    ValidationResultV1,
    VerificationFindingV1,
    WorkflowCancelResultV1,
    WorkflowCommitReceiptV1,
    WorkflowOperation,
    WorkflowPlanV1,
    WorkflowPrepareRequestV1,
    WorkflowPreviewV1,
    WorkflowPruneResultV1,
    WorkflowState,
    WorkflowStatusV1,
    WorkflowTerminalErrorV1,
    WorkflowVerificationResultV1,
    canonical_json,
    sha256_canonical,
)

RUN_ID = "123e4567-e89b-12d3-a456-426614174000"
ATTEMPT_ID = "123e4567-e89b-12d3-a456-426614174001"
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64
UTC_1 = "2026-07-27T01:02:03Z"
UTC_2 = "2026-07-27T02:02:03Z"
UTC_3 = "2026-07-27T03:02:03Z"


def _operation_payloads() -> list[dict[str, object]]:
    return [
        {
            "kind": "add_marker",
            "clip_name": "Interview_A",
            "start": "0s",
            "value": "Start",
        },
        {"kind": "add_keyword", "clip_name": "Interview_A", "value": "select"},
        {"kind": "trim_clip", "clip_name": "Interview_A", "new_duration": "5s"},
        {"kind": "split_clip", "clip_name": "Interview_A", "split_at": "2s"},
        {"kind": "delete_clips", "clip_names": ["B-roll 1"]},
        {"kind": "reorder_clips", "clip_names": ["A", "B"]},
        {"kind": "add_transition", "after_clip_name": "Interview_A"},
        {"kind": "change_speed", "clip_name": "Interview_A", "speed_factor": 1.25},
        {"kind": "assign_role", "clip_name": "Interview_A", "role": "Dialogue"},
        {
            "kind": "batch_assign_roles",
            "rules": [{"match": "Interview", "role": "Dialogue"}],
        },
        {"kind": "batch_rename_clips", "pattern": "old", "replacement": "new"},
        {"kind": "fill_gaps", "fill_ref": "r2"},
        {"kind": "fix_flash_frames"},
        {
            "kind": "batch_apply_transition",
            "duration": "30030/30000s",
            "name": "Cross Dissolve",
            "ref": "",
        },
    ]


def _receipt() -> OperationReceiptV1:
    return OperationReceiptV1(
        operation_id="op-001",
        kind="add_marker",
        disposition=OperationDisposition.SUCCEEDED,
        affected_count=1,
        affected_entities=("Interview_A",),
    )


def _validation() -> ValidationResultV1:
    return ValidationResultV1(valid=True, issues=())


def _preview(**changes: object) -> WorkflowPreviewV1:
    payload: dict[str, object] = {
        "schema_version": "1",
        "graph_version": "1",
        "package_version": "0.3.0",
        "run_version": "1",
        "run_id": RUN_ID,
        "state": WorkflowState.AWAITING_APPROVAL,
        "source_path": "/tmp/source.fcpxml",
        "destination_path": "/tmp/output.fcpxml",
        "source_sha256": HASH_A,
        "prior_destination_state": PriorDestinationState.ABSENT,
        "prior_destination_sha256": None,
        "plan_sha256": HASH_B,
        "candidate_sha256": HASH_C,
        "candidate_size_bytes": 1024,
        "diff_sha256": HASH_D,
        "diff_size_bytes": 256,
        "validation": _validation(),
        "operation_receipts": (_receipt(),),
        "summary": "One marker will be added.",
        "warnings": (),
        "approval_mode": ApprovalMode.CLI,
        "approval_expires_at": UTC_3,
        "run_uri": f"fcp-workflow://runs/{RUN_ID}",
        "events_uri": f"fcp-workflow://runs/{RUN_ID}/events",
        "diff_uri": f"fcp-workflow://runs/{RUN_ID}/diff",
    }
    payload.update(changes)
    return WorkflowPreviewV1(**payload)


def _status(**changes: object) -> WorkflowStatusV1:
    payload: dict[str, object] = {
        "schema_version": "1",
        "graph_version": "1",
        "package_version": "0.3.0",
        "run_version": "1",
        "run_id": RUN_ID,
        "state": WorkflowState.AWAITING_APPROVAL,
        "source_path": "/tmp/source.fcpxml",
        "destination_path": "/tmp/output.fcpxml",
        "source_sha256": HASH_A,
        "prior_destination_state": PriorDestinationState.ABSENT,
        "prior_destination_sha256": None,
        "plan_sha256": HASH_B,
        "candidate_sha256": HASH_C,
        "diff_sha256": HASH_D,
        "revision": 2,
        "approval_decision": None,
        "approval_source": None,
        "created_at": UTC_1,
        "updated_at": UTC_2,
        "approved_at": None,
        "committed_at": None,
        "expires_at": UTC_3,
        "terminal_error": None,
        "candidate_artifact": ArtifactAvailabilityV1(
            state=ArtifactState.PRESENT, sha256=HASH_C, size_bytes=1024
        ),
        "diff_artifact": ArtifactAvailabilityV1(
            state=ArtifactState.PRESENT, sha256=HASH_D, size_bytes=256
        ),
        "receipt_artifact": ArtifactAvailabilityV1(state=ArtifactState.ABSENT),
        "recovery": None,
        "warnings": (),
        "run_uri": f"fcp-workflow://runs/{RUN_ID}",
        "events_uri": f"fcp-workflow://runs/{RUN_ID}/events",
        "diff_uri": f"fcp-workflow://runs/{RUN_ID}/diff",
    }
    payload.update(changes)
    return WorkflowStatusV1(**payload)


@pytest.mark.parametrize("payload", _operation_payloads())
def test_all_fourteen_operations_round_trip_through_closed_union(payload):
    adapter = TypeAdapter(WorkflowOperation)
    operation = adapter.validate_python(payload)

    assert adapter.validate_python(operation.model_dump(mode="json")) == operation
    assert operation.kind == payload["kind"]


def test_operation_defaults_are_exact():
    marker = AddMarkerOperation(
        kind="add_marker", clip_name="A", start="0s", value="Start"
    )
    keyword = AddKeywordOperation(kind="add_keyword", clip_name="A", value="select")
    transition = AddTransitionOperation(kind="add_transition", after_clip_name="A")
    fill = FillGapsOperation(kind="fill_gaps", fill_ref="r2")
    flash = FixFlashFramesOperation(kind="fix_flash_frames")

    assert (marker.note, marker.marker_type, marker.duration) == ("", "standard", "1/1s")
    assert (keyword.start, keyword.duration) == ("0s", None)
    assert (transition.duration, transition.name, transition.ref) == (
        "30030/30000s",
        "Cross Dissolve",
        "",
    )
    assert fill.fill_name == "Fill"
    assert (flash.min_frames, flash.frame_duration) == (3, "1001/30000s")


def test_plan_preserves_operation_order_and_has_no_caller_operation_ids():
    payloads = _operation_payloads()
    plan = WorkflowPlanV1.model_validate(
        {"schema_version": "1", "operations": payloads}
    )

    assert [operation.kind for operation in plan.operations] == [
        payload["kind"] for payload in payloads
    ]
    assert all("operation_id" not in operation.model_dump() for operation in plan.operations)

    payloads[0]["operation_id"] = "attacker"
    with pytest.raises(ValidationError):
        WorkflowPlanV1.model_validate(
            {"schema_version": "1", "operations": payloads}
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "unknown", "clip_name": "A"},
        {"kind": "add_marker", "clip_name": "A", "start": "0s", "value": "x", "x": 1},
        {"kind": "add_marker", "clip_name": "", "start": "0s", "value": "x"},
        {"kind": "trim_clip", "clip_name": "A"},
        {"kind": "delete_clips", "clip_names": []},
        {"kind": "delete_clips", "clip_names": ["A", "A"]},
        {"kind": "delete_clips", "clip_names": ["A", ""]},
        {"kind": "reorder_clips", "clip_names": ["A", "A"]},
        {"kind": "change_speed", "clip_name": "A", "speed_factor": 0.0},
        {"kind": "change_speed", "clip_name": "A", "speed_factor": -1.0},
        {"kind": "change_speed", "clip_name": "A", "speed_factor": math.inf},
        {"kind": "change_speed", "clip_name": "A", "speed_factor": math.nan},
        {"kind": "fix_flash_frames", "min_frames": 0},
        {"kind": "fix_flash_frames", "min_frames": 1.5},
        {"kind": "fix_flash_frames", "min_frames": True},
        {"kind": "batch_assign_roles", "rules": []},
        {"kind": "batch_assign_roles", "rules": [{"match": "", "role": "Dialogue"}]},
    ],
)
def test_invalid_operation_payloads_are_rejected(payload):
    with pytest.raises(ValidationError):
        TypeAdapter(WorkflowOperation).validate_python(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "add_marker", "clip_name": 7, "start": "0s", "value": "x"},
        {"kind": "change_speed", "clip_name": "A", "speed_factor": "1.5"},
        {"kind": "fix_flash_frames", "min_frames": "3"},
    ],
)
def test_operation_primitives_are_strict(payload):
    with pytest.raises(ValidationError):
        TypeAdapter(WorkflowOperation).validate_python(payload)


def test_prepare_request_has_a_hard_ceiling_and_explicit_configured_limit():
    operation = AddKeywordOperation(kind="add_keyword", clip_name="A", value="select")
    request = WorkflowPrepareRequestV1(
        source_path="/tmp/source.fcpxml",
        destination_path="/tmp/output.fcpxml",
        operations=(operation, operation),
    )
    assert request.enforce_operation_limit(2) is request

    with pytest.raises(ValueError, match="configured maximum"):
        request.enforce_operation_limit(1)
    with pytest.raises(ValidationError):
        WorkflowPrepareRequestV1.model_validate(
            {
                "source_path": "/tmp/source.fcpxml",
                "destination_path": "/tmp/output.fcpxml",
                "operations": [_operation_payloads()[0], _operation_payloads()[1]],
            },
            context={"max_operations": 1},
        )
    with pytest.raises(ValidationError):
        WorkflowPrepareRequestV1(
            source_path="/tmp/source.fcpxml",
            destination_path="/tmp/output.fcpxml",
            operations=tuple(operation for _ in range(1001)),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("expected_source_sha256", "A" * 64),
        ("expected_source_sha256", "a" * 63),
        ("idempotency_key", ""),
        ("idempotency_key", "x" * 129),
        ("source_path", ""),
        ("destination_path", ""),
    ],
)
def test_prepare_request_bounds_are_enforced(field, value):
    payload = {
        "source_path": "/tmp/source.fcpxml",
        "destination_path": "/tmp/output.fcpxml",
        "operations": [_operation_payloads()[0]],
        field: value,
    }
    with pytest.raises(ValidationError):
        WorkflowPrepareRequestV1.model_validate(payload)


def test_canonical_json_is_nested_key_order_independent_and_nonmutating():
    left = {"z": [{"β": 2, "a": 1}], "a": {"y": False, "x": None}}
    right = {"a": {"x": None, "y": False}, "z": [{"a": 1, "β": 2}]}
    original = copy.deepcopy(left)

    assert canonical_json(left) == canonical_json(right)
    assert sha256_canonical(left) == sha256_canonical(right)
    assert left == original


def test_canonical_json_uses_exact_compact_unicode_bytes_and_model_json_mode():
    assert canonical_json({"snowman": "☃", "é": "café"}) == (
        '{"snowman":"☃","é":"café"}'.encode()
    )
    plan = WorkflowPlanV1(
        operations=(
            AddMarkerOperation(
                kind="add_marker", clip_name="A", start="0s", value="☃"
            ),
        )
    )
    assert sha256_canonical(plan) == sha256_canonical(plan.model_dump(mode="json"))


@pytest.mark.parametrize(
    "value",
    [
        {"bad": math.nan},
        {"bad": math.inf},
        {"bad": -math.inf},
        {"bad": Decimal("1.0")},
        {"bad": datetime.now(timezone.utc)},
        {"bad": {1, 2}},
        {1: "non-string-key"},
        ["top-level-list"],
    ],
)
def test_canonical_json_rejects_non_json_or_non_mapping_inputs(value):
    with pytest.raises((TypeError, ValueError)):
        canonical_json(value)


@pytest.mark.parametrize(
    "run_id",
    [
        "123E4567-E89B-12D3-A456-426614174000",
        "{123e4567-e89b-12d3-a456-426614174000}",
        "123e4567e89b12d3a456426614174000",
        "00000000-0000-0000-0000-000000000000",
        "not-a-uuid",
    ],
)
def test_run_ids_require_non_nil_canonical_lowercase_uuid(run_id):
    with pytest.raises(ValidationError):
        WorkflowCancelResultV1(
            run_id=run_id,
            state=WorkflowState.CANCELLED,
            reason="operator request",
            cancelled_at=UTC_1,
        )


def test_uuid_validator_accepts_canonical_non_nil_spelling():
    result = WorkflowCancelResultV1(
        run_id=RUN_ID,
        state=WorkflowState.CANCELLED,
        reason="operator request",
        cancelled_at=UTC_1,
    )
    assert str(UUID(result.run_id)) == result.run_id


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-07-27T01:02:03",
        "2026-07-27T01:02:03+00:00",
        "2026-07-26T19:02:03-06:00",
        "2026-07-27 01:02:03Z",
        "2026-07-27T01:02:03.000000Z",
        datetime(2026, 7, 27, 1, 2, 3, tzinfo=timezone.utc),
    ],
)
def test_timestamps_require_canonical_utc_z_strings(timestamp):
    with pytest.raises(ValidationError):
        WorkflowCancelResultV1(
            run_id=RUN_ID,
            state=WorkflowState.CANCELLED,
            reason="operator request",
            cancelled_at=timestamp,
        )


def test_preview_binds_prior_destination_hash_and_resource_uris():
    with pytest.raises(ValidationError):
        _preview(
            prior_destination_state=PriorDestinationState.ABSENT,
            prior_destination_sha256=HASH_A,
        )
    with pytest.raises(ValidationError):
        _preview(
            prior_destination_state=PriorDestinationState.PRESENT,
            prior_destination_sha256=None,
        )
    with pytest.raises(ValidationError):
        _preview(events_uri=f"fcp-workflow://runs/{ATTEMPT_ID}/events")


@pytest.mark.parametrize(
    "changes",
    [
        {"candidate_size_bytes": 0},
        {"diff_size_bytes": -1},
        {"summary": ""},
        {"summary": "x" * 4097},
        {"warnings": tuple("x" for _ in range(101))},
        {"source_sha256": "A" * 64},
        {"state": WorkflowState.PREPARING},
    ],
)
def test_preview_rejects_invalid_bounds_and_contradictions(changes):
    with pytest.raises(ValidationError):
        _preview(**changes)


def test_artifact_availability_rejects_contradictory_state_and_metadata():
    with pytest.raises(ValidationError):
        ArtifactAvailabilityV1(state=ArtifactState.ABSENT, sha256=HASH_A)
    with pytest.raises(ValidationError):
        ArtifactAvailabilityV1(state=ArtifactState.PRESENT)
    with pytest.raises(ValidationError):
        ArtifactAvailabilityV1(
            state=ArtifactState.PRESENT, sha256=HASH_A, size_bytes=None
        )


def test_status_enforces_state_specific_approval_commit_and_error_fields():
    _status()
    _status(
        state=WorkflowState.COMMITTED,
        approval_decision=ApprovalDecision.APPROVED,
        approval_source=ApprovalSource.CLI,
        approved_at=UTC_2,
        committed_at=UTC_3,
        expires_at=None,
        receipt_artifact=ArtifactAvailabilityV1(
            state=ArtifactState.PRESENT, sha256=HASH_A, size_bytes=512
        ),
        updated_at=UTC_3,
    )

    with pytest.raises(ValidationError):
        _status(state=WorkflowState.COMMITTED, committed_at=None)
    with pytest.raises(ValidationError):
        _status(
            state=WorkflowState.PREPARING,
            approval_decision=ApprovalDecision.APPROVED,
            approval_source=ApprovalSource.CLI,
            approved_at=UTC_2,
        )
    with pytest.raises(ValidationError):
        _status(
            terminal_error=WorkflowTerminalErrorV1(
                code=ErrorCode.OPERATION_FAILED, summary="operation failed"
            )
        )
    with pytest.raises(ValidationError):
        _status(
            state=WorkflowState.FAILED,
            terminal_error=None,
        )
    with pytest.raises(ValidationError):
        _status(
            prior_destination_state=PriorDestinationState.PRESENT,
            prior_destination_sha256=None,
        )
    with pytest.raises(ValidationError):
        _status(diff_uri=f"fcp-workflow://runs/{ATTEMPT_ID}/diff")


def test_commit_receipt_binds_success_hashes_backup_and_approval_strength():
    receipt = WorkflowCommitReceiptV1(
        run_id=RUN_ID,
        commit_attempt_id=ATTEMPT_ID,
        candidate_sha256=HASH_A,
        output_sha256=HASH_A,
        source_sha256=HASH_B,
        prior_destination_sha256=HASH_C,
        destination_path="/tmp/output.fcpxml",
        backup_path="/tmp/output.fcpxml.backup",
        validation_warnings=(),
        approval_source=ApprovalSource.CLI,
        approval_strength=ApprovalStrength.STRONG,
        approval_binding_sha256=HASH_D,
        committed_at=UTC_3,
        receipt_sha256=HASH_B,
    )
    assert receipt.output_sha256 == receipt.candidate_sha256

    with pytest.raises(ValidationError):
        WorkflowCommitReceiptV1(
            **{
                **receipt.model_dump(),
                "output_sha256": HASH_D,
            }
        )
    with pytest.raises(ValidationError):
        WorkflowCommitReceiptV1(
            **{
                **receipt.model_dump(),
                "backup_path": None,
            }
        )
    with pytest.raises(ValidationError):
        WorkflowCommitReceiptV1(
            **{
                **receipt.model_dump(),
                "approval_source": ApprovalSource.CLIENT,
                "approval_strength": ApprovalStrength.STRONG,
            }
        )


def test_recovery_assessment_is_data_only_and_deeply_immutable():
    evidence = ["destination matches candidate"]
    ambiguity = ["backup owner is unknown"]
    assessment = RecoveryAssessmentV1(
        destination_state=ObservedFileState.PRESENT,
        destination_sha256=HASH_A,
        backup_state=ObservedFileState.PRESENT,
        backup_sha256=HASH_B,
        lock_state=LockState.UNKNOWN,
        recommended_branch=RecoveryBranch.MARK_RECOVERY_REQUIRED,
        evidence=evidence,
        ambiguity_reasons=ambiguity,
    )
    evidence.append("caller mutation")
    ambiguity.clear()

    assert assessment.evidence == ("destination matches candidate",)
    assert assessment.ambiguity_reasons == ("backup owner is unknown",)
    with pytest.raises((AttributeError, TypeError, ValidationError)):
        assessment.evidence += ("mutate",)
    mutating_names = {"reconcile", "recover", "restore", "commit", "rollback", "apply"}
    assert mutating_names.isdisjoint(dir(assessment))


def test_recovery_hashes_follow_observed_file_states():
    with pytest.raises(ValidationError):
        RecoveryAssessmentV1(
            destination_state=ObservedFileState.ABSENT,
            destination_sha256=HASH_A,
            backup_state=ObservedFileState.ABSENT,
            lock_state=LockState.ABSENT,
            recommended_branch=RecoveryBranch.MARK_ROLLED_BACK,
        )
    with pytest.raises(ValidationError):
        RecoveryAssessmentV1(
            destination_state=ObservedFileState.PRESENT,
            backup_state=ObservedFileState.ABSENT,
            lock_state=LockState.ABSENT,
            recommended_branch=RecoveryBranch.FINALIZE_COMMITTED,
        )


def test_verification_and_prune_results_are_typed_bounded_contracts():
    passing = VerificationFindingV1(
        disposition=FindingDisposition.PASS, summary="verified", evidence=()
    )
    result = WorkflowVerificationResultV1(
        run_id=RUN_ID,
        ledger=passing,
        database=passing,
        artifacts=passing,
        approval=passing,
        receipt=passing,
        destination=passing,
        overall=FindingDisposition.PASS,
    )
    assert result.overall is FindingDisposition.PASS

    with pytest.raises(ValidationError):
        WorkflowVerificationResultV1(
            **{**result.model_dump(), "overall": FindingDisposition.FAIL}
        )

    prune = WorkflowPruneResultV1(
        cutoff=UTC_1,
        affected_run_ids=(RUN_ID,),
        artifact_hashes=(HASH_A,),
        intent_event_hashes=(HASH_B,),
        reclaimed_bytes=0,
        disposition=PruneDisposition.COMPLETED,
    )
    assert prune.affected_run_ids == (RUN_ID,)
    with pytest.raises(ValidationError):
        WorkflowPruneResultV1(
            **{
                **prune.model_dump(),
                "reclaimed_bytes": -1,
            }
        )


def _contains_empty_schema(value: object) -> bool:
    if value == {}:
        return True
    if isinstance(value, dict):
        return any(_contains_empty_schema(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_empty_schema(item) for item in value)
    return False


@pytest.mark.parametrize(
    "model",
    [
        WorkflowPlanV1,
        WorkflowPrepareRequestV1,
        WorkflowPreviewV1,
        WorkflowStatusV1,
        WorkflowCommitReceiptV1,
        WorkflowCancelResultV1,
        WorkflowVerificationResultV1,
        WorkflowPruneResultV1,
        RecoveryAssessmentV1,
    ],
)
def test_public_model_schemas_have_no_unconstrained_empty_nodes(model):
    assert not _contains_empty_schema(model.model_json_schema())


def test_existing_error_values_are_unchanged_and_workflow_errors_are_stable():
    assert [member.value for member in ErrorCode][:16] == [
        "invalid_arguments",
        "invalid_configuration",
        "invalid_path",
        "path_outside_scope",
        "source_not_found",
        "target_not_found",
        "same_file_forbidden",
        "live_control_disabled",
        "permission_denied",
        "dependency_missing",
        "command_failed",
        "output_missing",
        "validation_failed",
        "transaction_failed",
        "unsupported_contract",
        "internal_error",
    ]
    assert {member.value for member in ErrorCode} >= {
        "approval_required",
        "approval_expired",
        "workflow_state_conflict",
        "workflow_stale",
        "operation_failed",
        "artifact_corrupt",
        "ledger_unavailable",
        "recovery_required",
        "idempotency_conflict",
    }


def test_support_models_reject_unbounded_or_contradictory_payloads():
    with pytest.raises(ValidationError):
        BatchAssignRolesOperation(
            kind="batch_assign_roles",
            rules=(BatchRoleAssignmentRule(match="A", role="Dialogue"),) * 1001,
        )
    with pytest.raises(ValidationError):
        ValidationResultV1(
            valid=True,
            issues=(
                ValidationIssueV1(
                    severity=FindingDisposition.FAIL,
                    code="invalid_reference",
                    summary="missing asset",
                ),
            ),
        )
