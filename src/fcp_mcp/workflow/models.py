from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import Annotated, Literal, TypeAlias
from uuid import UUID

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationInfo,
    field_validator,
    model_validator,
)

from fcp_mcp.contracts import ErrorCode
from fcp_mcp.profiles import ApprovalMode

MAX_OPERATIONS = 1000
MAX_CLIP_NAMES = 1000
MAX_ROLE_RULES = 1000
MAX_RECEIPTS = 1000
MAX_WARNINGS = 100
MAX_EVIDENCE_ITEMS = 100
MAX_PRUNE_ITEMS = 1000

_UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_UTC_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$"
)
_OPERATION_ID_PATTERN = re.compile(r"^op-[0-9]{3,6}$")


def _canonical_uuid(value: str) -> str:
    if not _UUID_PATTERN.fullmatch(value):
        raise ValueError("identifier must use canonical lowercase UUID spelling")
    parsed = UUID(value)
    if parsed.int == 0 or str(parsed) != value:
        raise ValueError("identifier must be a non-nil canonical lowercase UUID")
    return value


def _canonical_utc_timestamp(value: str) -> str:
    if not _UTC_PATTERN.fullmatch(value):
        raise ValueError("timestamp must use canonical UTC ISO-8601 spelling")
    parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError("timestamp must represent a UTC instant")
    canonical = parsed.isoformat().replace("+00:00", "Z")
    if canonical != value:
        raise ValueError("timestamp must use canonical UTC ISO-8601 spelling")
    return value


def _operation_id(value: str) -> str:
    if not _OPERATION_ID_PATTERN.fullmatch(value):
        raise ValueError("operation_id must use op-NNN spelling")
    return value


StrictNonEmptyStr: TypeAlias = Annotated[
    str, StringConstraints(strict=True, min_length=1, max_length=255)
]
BoundedText: TypeAlias = Annotated[
    str, StringConstraints(strict=True, min_length=1, max_length=4096)
]
BoundedOptionalText: TypeAlias = Annotated[
    str, StringConstraints(strict=True, min_length=1, max_length=4096)
]
BoundedEmptyText: TypeAlias = Annotated[
    str, StringConstraints(strict=True, min_length=0, max_length=4096)
]
PathText: TypeAlias = Annotated[
    str, StringConstraints(strict=True, min_length=1, max_length=4096)
]
VersionText: TypeAlias = Annotated[
    str, StringConstraints(strict=True, min_length=1, max_length=64)
]
Sha256: TypeAlias = Annotated[
    str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$")
]
CanonicalUUID: TypeAlias = Annotated[
    str,
    StringConstraints(strict=True),
    AfterValidator(_canonical_uuid),
]
UtcTimestamp: TypeAlias = Annotated[
    str,
    StringConstraints(strict=True),
    AfterValidator(_canonical_utc_timestamp),
]
OperationId: TypeAlias = Annotated[
    str,
    StringConstraints(strict=True),
    AfterValidator(_operation_id),
]
NonNegativeInt: TypeAlias = Annotated[int, Field(strict=True, ge=0)]
PositiveInt: TypeAlias = Annotated[int, Field(strict=True, gt=0)]
PositiveFiniteFloat: TypeAlias = Annotated[
    float, Field(strict=True, gt=0, allow_inf_nan=False)
]


class _FrozenStrictModel(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        strict=True,
        allow_inf_nan=False,
    )


def _tuple_input(value: object) -> object:
    if isinstance(value, (list, tuple)):
        return tuple(value)
    return value


class WorkflowState(str, Enum):
    PREPARING = "preparing"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    COMMITTING = "committing"
    COMMITTED = "committed"
    FAILED = "failed"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    STALE = "stale"
    ROLLED_BACK = "rolled_back"
    RECOVERY_REQUIRED = "recovery_required"


LEGAL_TRANSITIONS: Mapping[WorkflowState, frozenset[WorkflowState]] = MappingProxyType(
    {
        WorkflowState.PREPARING: frozenset(
            {
                WorkflowState.AWAITING_APPROVAL,
                WorkflowState.FAILED,
                WorkflowState.CANCELLED,
            }
        ),
        WorkflowState.AWAITING_APPROVAL: frozenset(
            {
                WorkflowState.APPROVED,
                WorkflowState.REJECTED,
                WorkflowState.CANCELLED,
                WorkflowState.EXPIRED,
            }
        ),
        WorkflowState.APPROVED: frozenset(
            {
                WorkflowState.COMMITTING,
                WorkflowState.STALE,
                WorkflowState.CANCELLED,
                WorkflowState.EXPIRED,
            }
        ),
        WorkflowState.COMMITTING: frozenset(
            {
                WorkflowState.COMMITTED,
                WorkflowState.ROLLED_BACK,
                WorkflowState.RECOVERY_REQUIRED,
            }
        ),
        WorkflowState.COMMITTED: frozenset(),
        WorkflowState.FAILED: frozenset(),
        WorkflowState.REJECTED: frozenset(),
        WorkflowState.CANCELLED: frozenset(),
        WorkflowState.EXPIRED: frozenset(),
        WorkflowState.STALE: frozenset(),
        WorkflowState.ROLLED_BACK: frozenset(),
        WorkflowState.RECOVERY_REQUIRED: frozenset(),
    }
)


def transition_allowed(source: object, target: object) -> bool:
    """Return whether two already-validated states form a declared edge."""
    if not isinstance(source, WorkflowState) or not isinstance(target, WorkflowState):
        return False
    return target in LEGAL_TRANSITIONS[source]


class _OperationModel(_FrozenStrictModel):
    pass


class AddMarkerOperation(_OperationModel):
    kind: Literal["add_marker"]
    clip_name: StrictNonEmptyStr
    start: StrictNonEmptyStr
    value: StrictNonEmptyStr
    note: BoundedEmptyText = ""
    marker_type: StrictNonEmptyStr = "standard"
    duration: StrictNonEmptyStr = "1/1s"


class AddKeywordOperation(_OperationModel):
    kind: Literal["add_keyword"]
    clip_name: StrictNonEmptyStr
    value: StrictNonEmptyStr
    start: StrictNonEmptyStr = "0s"
    duration: StrictNonEmptyStr | None = None


class TrimClipOperation(_OperationModel):
    kind: Literal["trim_clip"]
    clip_name: StrictNonEmptyStr
    new_start: StrictNonEmptyStr | None = None
    new_duration: StrictNonEmptyStr | None = None

    @model_validator(mode="after")
    def _has_a_trim_value(self) -> TrimClipOperation:
        if self.new_start is None and self.new_duration is None:
            raise ValueError("trim_clip requires new_start or new_duration")
        return self


class SplitClipOperation(_OperationModel):
    kind: Literal["split_clip"]
    clip_name: StrictNonEmptyStr
    split_at: StrictNonEmptyStr


class _ClipCollectionOperation(_OperationModel):
    clip_names: Annotated[tuple[StrictNonEmptyStr, ...], Field(min_length=1, max_length=1000)]

    @field_validator("clip_names", mode="before")
    @classmethod
    def _freeze_clip_names(cls, value: object) -> object:
        return _tuple_input(value)

    @field_validator("clip_names")
    @classmethod
    def _unique_clip_names(
        cls, value: tuple[StrictNonEmptyStr, ...]
    ) -> tuple[StrictNonEmptyStr, ...]:
        if len(set(value)) != len(value):
            raise ValueError("clip_names must not contain duplicates")
        return value


class DeleteClipsOperation(_ClipCollectionOperation):
    kind: Literal["delete_clips"]


class ReorderClipsOperation(_ClipCollectionOperation):
    kind: Literal["reorder_clips"]


class AddTransitionOperation(_OperationModel):
    kind: Literal["add_transition"]
    after_clip_name: StrictNonEmptyStr
    duration: StrictNonEmptyStr = "30030/30000s"
    name: StrictNonEmptyStr = "Cross Dissolve"
    ref: BoundedEmptyText = ""


class ChangeSpeedOperation(_OperationModel):
    kind: Literal["change_speed"]
    clip_name: StrictNonEmptyStr
    speed_factor: PositiveFiniteFloat


class AssignRoleOperation(_OperationModel):
    kind: Literal["assign_role"]
    clip_name: StrictNonEmptyStr
    role: StrictNonEmptyStr


class BatchRoleAssignmentRule(_FrozenStrictModel):
    match: StrictNonEmptyStr
    role: StrictNonEmptyStr


class BatchAssignRolesOperation(_OperationModel):
    kind: Literal["batch_assign_roles"]
    rules: Annotated[
        tuple[BatchRoleAssignmentRule, ...], Field(min_length=1, max_length=MAX_ROLE_RULES)
    ]

    @field_validator("rules", mode="before")
    @classmethod
    def _freeze_rules(cls, value: object) -> object:
        return _tuple_input(value)


class BatchRenameClipsOperation(_OperationModel):
    kind: Literal["batch_rename_clips"]
    pattern: StrictNonEmptyStr
    replacement: BoundedEmptyText


class FillGapsOperation(_OperationModel):
    kind: Literal["fill_gaps"]
    fill_ref: StrictNonEmptyStr
    fill_name: StrictNonEmptyStr = "Fill"


class FixFlashFramesOperation(_OperationModel):
    kind: Literal["fix_flash_frames"]
    min_frames: Annotated[int, Field(strict=True, ge=1)] = 3
    frame_duration: StrictNonEmptyStr = "1001/30000s"


class BatchApplyTransitionOperation(_OperationModel):
    kind: Literal["batch_apply_transition"]
    duration: StrictNonEmptyStr
    name: StrictNonEmptyStr
    ref: BoundedEmptyText


WorkflowOperation: TypeAlias = Annotated[
    AddMarkerOperation
    | AddKeywordOperation
    | TrimClipOperation
    | SplitClipOperation
    | DeleteClipsOperation
    | ReorderClipsOperation
    | AddTransitionOperation
    | ChangeSpeedOperation
    | AssignRoleOperation
    | BatchAssignRolesOperation
    | BatchRenameClipsOperation
    | FillGapsOperation
    | FixFlashFramesOperation
    | BatchApplyTransitionOperation,
    Field(discriminator="kind"),
]

OperationKindValue: TypeAlias = Literal[
    "add_marker",
    "add_keyword",
    "trim_clip",
    "split_clip",
    "delete_clips",
    "reorder_clips",
    "add_transition",
    "change_speed",
    "assign_role",
    "batch_assign_roles",
    "batch_rename_clips",
    "fill_gaps",
    "fix_flash_frames",
    "batch_apply_transition",
]


class WorkflowPlanV1(_FrozenStrictModel):
    schema_version: Literal["1"] = "1"
    operations: Annotated[
        tuple[WorkflowOperation, ...], Field(min_length=1, max_length=MAX_OPERATIONS)
    ]

    @field_validator("operations", mode="before")
    @classmethod
    def _freeze_operations(cls, value: object) -> object:
        return _tuple_input(value)


class WorkflowPrepareRequestV1(_FrozenStrictModel):
    schema_version: Literal["1"] = "1"
    source_path: PathText
    destination_path: PathText
    operations: Annotated[
        tuple[WorkflowOperation, ...], Field(min_length=1, max_length=MAX_OPERATIONS)
    ]
    expected_source_sha256: Sha256 | None = None
    idempotency_key: Annotated[
        str | None,
        StringConstraints(strict=True, min_length=1, max_length=128),
    ] = None

    @field_validator("operations", mode="before")
    @classmethod
    def _freeze_operations(cls, value: object) -> object:
        return _tuple_input(value)

    @model_validator(mode="after")
    def _configured_operation_limit(self, info: ValidationInfo) -> WorkflowPrepareRequestV1:
        if info.context is None or "max_operations" not in info.context:
            return self
        maximum = info.context["max_operations"]
        if isinstance(maximum, bool) or not isinstance(maximum, int):
            raise TypeError("configured operation maximum must be an integer")
        self.enforce_operation_limit(maximum)
        return self

    def enforce_operation_limit(self, max_operations: int) -> WorkflowPrepareRequestV1:
        """Apply an explicit configured limit without consulting process state."""
        if isinstance(max_operations, bool) or not isinstance(max_operations, int):
            raise TypeError("configured operation maximum must be an integer")
        if not 1 <= max_operations <= MAX_OPERATIONS:
            raise ValueError(f"configured operation maximum must be within 1..{MAX_OPERATIONS}")
        if len(self.operations) > max_operations:
            raise ValueError(
                f"operation count exceeds configured maximum of {max_operations}"
            )
        return self


class PriorDestinationState(str, Enum):
    ABSENT = "absent"
    PRESENT = "present"


class OperationDisposition(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class FindingDisposition(str, Enum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


class ApprovalDecision(str, Enum):
    APPROVED = "approved"
    REJECTED = "rejected"


class ApprovalSource(str, Enum):
    CLI = "cli"
    CLIENT = "client"


class ApprovalStrength(str, Enum):
    STRONG = "strong"
    WEAK = "weak"


class ArtifactState(str, Enum):
    ABSENT = "absent"
    PRESENT = "present"
    PRUNED = "pruned"
    CORRUPT = "corrupt"


class ObservedFileState(str, Enum):
    ABSENT = "absent"
    PRESENT = "present"
    UNREADABLE = "unreadable"


class LockState(str, Enum):
    ABSENT = "absent"
    HELD = "held"
    STALE = "stale"
    UNKNOWN = "unknown"


class RecoveryBranch(str, Enum):
    NONE = "none"
    CLEAR_ORPHAN_LOCK = "clear_orphan_lock"
    FINALIZE_COMMITTED = "finalize_committed"
    MARK_ROLLED_BACK = "mark_rolled_back"
    MARK_RECOVERY_REQUIRED = "mark_recovery_required"


class PruneDisposition(str, Enum):
    COMPLETED = "completed"
    PARTIAL = "partial"
    NOOP = "noop"


class ValidationIssueV1(_FrozenStrictModel):
    severity: FindingDisposition
    code: StrictNonEmptyStr
    summary: BoundedText


class ValidationResultV1(_FrozenStrictModel):
    valid: bool = Field(strict=True)
    issues: Annotated[
        tuple[ValidationIssueV1, ...], Field(max_length=MAX_EVIDENCE_ITEMS)
    ] = ()

    @field_validator("issues", mode="before")
    @classmethod
    def _freeze_issues(cls, value: object) -> object:
        return _tuple_input(value)

    @model_validator(mode="after")
    def _consistent_validity(self) -> ValidationResultV1:
        has_failure = any(issue.severity is FindingDisposition.FAIL for issue in self.issues)
        if self.valid == has_failure:
            raise ValueError("valid must be false exactly when a failing issue exists")
        return self


class OperationValueChangeV1(_FrozenStrictModel):
    field: StrictNonEmptyStr
    before: BoundedEmptyText | None = None
    after: BoundedEmptyText | None = None

    @model_validator(mode="after")
    def _has_a_value(self) -> OperationValueChangeV1:
        if self.before is None and self.after is None:
            raise ValueError("an operation value change requires before or after")
        return self


class OperationReceiptV1(_FrozenStrictModel):
    operation_id: OperationId
    kind: OperationKindValue
    disposition: OperationDisposition
    affected_count: NonNegativeInt
    affected_entities: Annotated[
        tuple[StrictNonEmptyStr, ...], Field(max_length=MAX_CLIP_NAMES)
    ] = ()
    changes: Annotated[
        tuple[OperationValueChangeV1, ...], Field(max_length=MAX_EVIDENCE_ITEMS)
    ] = ()
    warnings: Annotated[tuple[BoundedText, ...], Field(max_length=MAX_WARNINGS)] = ()
    error_code: ErrorCode | None = None
    error_summary: BoundedOptionalText | None = None

    @field_validator("affected_entities", "changes", "warnings", mode="before")
    @classmethod
    def _freeze_collections(cls, value: object) -> object:
        return _tuple_input(value)

    @model_validator(mode="after")
    def _consistent_disposition(self) -> OperationReceiptV1:
        if self.disposition is OperationDisposition.SUCCEEDED:
            if self.error_code is not None or self.error_summary is not None:
                raise ValueError("successful operation receipt cannot carry an error")
        elif self.error_code is None or self.error_summary is None:
            raise ValueError("failed operation receipt requires a coded error")
        return self


class ArtifactAvailabilityV1(_FrozenStrictModel):
    state: ArtifactState
    sha256: Sha256 | None = None
    size_bytes: NonNegativeInt | None = None

    @model_validator(mode="after")
    def _consistent_metadata(self) -> ArtifactAvailabilityV1:
        if self.state is ArtifactState.ABSENT:
            if self.sha256 is not None or self.size_bytes is not None:
                raise ValueError("absent artifact cannot carry hash or size")
        elif self.sha256 is None or self.size_bytes is None:
            raise ValueError("recorded artifact requires hash and byte size")
        return self


class WorkflowTerminalErrorV1(_FrozenStrictModel):
    code: ErrorCode
    summary: BoundedText


class RecoveryAssessmentV1(_FrozenStrictModel):
    destination_state: ObservedFileState
    destination_sha256: Sha256 | None = None
    backup_state: ObservedFileState
    backup_sha256: Sha256 | None = None
    lock_state: LockState
    lock_sha256: Sha256 | None = None
    recommended_branch: RecoveryBranch
    evidence: Annotated[
        tuple[BoundedText, ...], Field(max_length=MAX_EVIDENCE_ITEMS)
    ] = ()
    ambiguity_reasons: Annotated[
        tuple[BoundedText, ...], Field(max_length=MAX_EVIDENCE_ITEMS)
    ] = ()

    @field_validator("evidence", "ambiguity_reasons", mode="before")
    @classmethod
    def _freeze_collections(cls, value: object) -> object:
        return _tuple_input(value)

    @model_validator(mode="after")
    def _consistent_observations(self) -> RecoveryAssessmentV1:
        for label, state, digest in (
            ("destination", self.destination_state, self.destination_sha256),
            ("backup", self.backup_state, self.backup_sha256),
        ):
            if state is ObservedFileState.PRESENT and digest is None:
                raise ValueError(f"present {label} requires a hash")
            if state is not ObservedFileState.PRESENT and digest is not None:
                raise ValueError(f"non-present {label} cannot carry a hash")
        if self.lock_state is LockState.ABSENT and self.lock_sha256 is not None:
            raise ValueError("absent lock cannot carry a hash")
        return self


class WorkflowPreviewV1(_FrozenStrictModel):
    schema_version: Literal["1"] = "1"
    graph_version: VersionText
    package_version: VersionText
    run_version: VersionText
    run_id: CanonicalUUID
    state: WorkflowState
    source_path: PathText
    destination_path: PathText
    source_sha256: Sha256
    prior_destination_state: PriorDestinationState
    prior_destination_sha256: Sha256 | None = None
    plan_sha256: Sha256
    candidate_sha256: Sha256
    candidate_size_bytes: PositiveInt
    diff_sha256: Sha256
    diff_size_bytes: PositiveInt
    validation: ValidationResultV1
    operation_receipts: Annotated[
        tuple[OperationReceiptV1, ...], Field(min_length=1, max_length=MAX_RECEIPTS)
    ]
    summary: BoundedText
    warnings: Annotated[tuple[BoundedText, ...], Field(max_length=MAX_WARNINGS)] = ()
    approval_mode: ApprovalMode
    approval_expires_at: UtcTimestamp
    run_uri: BoundedText
    events_uri: BoundedText
    diff_uri: BoundedText

    @field_validator("operation_receipts", "warnings", mode="before")
    @classmethod
    def _freeze_collections(cls, value: object) -> object:
        return _tuple_input(value)

    @model_validator(mode="after")
    def _consistent_preview(self) -> WorkflowPreviewV1:
        if self.state is not WorkflowState.AWAITING_APPROVAL:
            raise ValueError("a workflow preview must be awaiting approval")
        if self.prior_destination_state is PriorDestinationState.ABSENT:
            if self.prior_destination_sha256 is not None:
                raise ValueError("absent prior destination cannot carry a hash")
        elif self.prior_destination_sha256 is None:
            raise ValueError("present prior destination requires a hash")
        expected = (
            f"fcp-workflow://runs/{self.run_id}",
            f"fcp-workflow://runs/{self.run_id}/events",
            f"fcp-workflow://runs/{self.run_id}/diff",
        )
        if (self.run_uri, self.events_uri, self.diff_uri) != expected:
            raise ValueError("workflow resource URIs must bind the same run ID")
        return self


class WorkflowStatusV1(_FrozenStrictModel):
    schema_version: Literal["1"] = "1"
    graph_version: VersionText
    package_version: VersionText
    run_version: VersionText
    run_id: CanonicalUUID
    state: WorkflowState
    source_path: PathText
    destination_path: PathText
    source_sha256: Sha256
    prior_destination_state: PriorDestinationState
    prior_destination_sha256: Sha256 | None = None
    plan_sha256: Sha256
    candidate_sha256: Sha256
    diff_sha256: Sha256
    revision: PositiveInt
    approval_decision: ApprovalDecision | None = None
    approval_source: ApprovalSource | None = None
    created_at: UtcTimestamp
    updated_at: UtcTimestamp
    approved_at: UtcTimestamp | None = None
    committed_at: UtcTimestamp | None = None
    expires_at: UtcTimestamp | None = None
    terminal_error: WorkflowTerminalErrorV1 | None = None
    candidate_artifact: ArtifactAvailabilityV1
    diff_artifact: ArtifactAvailabilityV1
    receipt_artifact: ArtifactAvailabilityV1
    recovery: RecoveryAssessmentV1 | None = None
    warnings: Annotated[tuple[BoundedText, ...], Field(max_length=MAX_WARNINGS)] = ()
    run_uri: BoundedText
    events_uri: BoundedText
    diff_uri: BoundedText

    @field_validator("warnings", mode="before")
    @classmethod
    def _freeze_warnings(cls, value: object) -> object:
        return _tuple_input(value)

    @model_validator(mode="after")
    def _consistent_status(self) -> WorkflowStatusV1:
        if self.prior_destination_state is PriorDestinationState.ABSENT:
            if self.prior_destination_sha256 is not None:
                raise ValueError("absent prior destination cannot carry a hash")
        elif self.prior_destination_sha256 is None:
            raise ValueError("present prior destination requires a hash")
        expected_uris = (
            f"fcp-workflow://runs/{self.run_id}",
            f"fcp-workflow://runs/{self.run_id}/events",
            f"fcp-workflow://runs/{self.run_id}/diff",
        )
        if (self.run_uri, self.events_uri, self.diff_uri) != expected_uris:
            raise ValueError("workflow resource URIs must bind the same run ID")

        approved_states = {
            WorkflowState.APPROVED,
            WorkflowState.COMMITTING,
            WorkflowState.COMMITTED,
        }
        approval_fields = (
            self.approval_decision,
            self.approval_source,
            self.approved_at,
        )
        if self.state in approved_states:
            if (
                self.approval_decision is not ApprovalDecision.APPROVED
                or self.approval_source is None
                or self.approved_at is None
            ):
                raise ValueError("approved workflow state requires approval evidence")
        elif self.state is WorkflowState.REJECTED:
            if (
                self.approval_decision is not ApprovalDecision.REJECTED
                or self.approval_source is None
                or self.approved_at is not None
            ):
                raise ValueError("rejected state requires rejection evidence")
        elif any(field is not None for field in approval_fields):
            raise ValueError("workflow state cannot carry approval evidence")

        if self.state is WorkflowState.COMMITTED:
            if self.committed_at is None:
                raise ValueError("committed state requires committed_at")
            if self.receipt_artifact.state is not ArtifactState.PRESENT:
                raise ValueError("committed state requires a present receipt")
        elif self.committed_at is not None:
            raise ValueError("non-committed state cannot carry committed_at")

        error_states = {
            WorkflowState.FAILED,
            WorkflowState.STALE,
            WorkflowState.ROLLED_BACK,
            WorkflowState.RECOVERY_REQUIRED,
        }
        if self.terminal_error is not None and self.state not in error_states:
            raise ValueError("terminal error contradicts workflow state")
        if (
            self.state in {WorkflowState.FAILED, WorkflowState.RECOVERY_REQUIRED}
            and self.terminal_error is None
        ):
            raise ValueError("failed or recovery-required state needs terminal error")

        if (
            self.state in {WorkflowState.AWAITING_APPROVAL, WorkflowState.APPROVED}
            and self.expires_at is None
        ):
            raise ValueError("approval-bearing state requires expires_at")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        if self.approved_at is not None and self.approved_at < self.created_at:
            raise ValueError("approved_at cannot precede created_at")
        if self.committed_at is not None and self.committed_at < self.created_at:
            raise ValueError("committed_at cannot precede created_at")
        return self


class WorkflowCommitReceiptV1(_FrozenStrictModel):
    schema_version: Literal["1"] = "1"
    run_id: CanonicalUUID
    commit_attempt_id: CanonicalUUID
    candidate_sha256: Sha256
    output_sha256: Sha256
    source_sha256: Sha256
    prior_destination_sha256: Sha256 | None = None
    destination_path: PathText
    backup_path: PathText | None = None
    validation_warnings: Annotated[
        tuple[BoundedText, ...], Field(max_length=MAX_WARNINGS)
    ] = ()
    approval_source: ApprovalSource
    approval_strength: ApprovalStrength
    approval_binding_sha256: Sha256
    committed_at: UtcTimestamp
    receipt_sha256: Sha256

    @field_validator("validation_warnings", mode="before")
    @classmethod
    def _freeze_warnings(cls, value: object) -> object:
        return _tuple_input(value)

    @model_validator(mode="after")
    def _consistent_commit(self) -> WorkflowCommitReceiptV1:
        if self.output_sha256 != self.candidate_sha256:
            raise ValueError("committed output hash must equal candidate hash")
        if (self.prior_destination_sha256 is None) != (self.backup_path is None):
            raise ValueError("backup path must exist exactly when a prior destination existed")
        expected_strength = (
            ApprovalStrength.STRONG
            if self.approval_source is ApprovalSource.CLI
            else ApprovalStrength.WEAK
        )
        if self.approval_strength is not expected_strength:
            raise ValueError("approval strength contradicts approval source")
        return self


class WorkflowCancelResultV1(_FrozenStrictModel):
    schema_version: Literal["1"] = "1"
    run_id: CanonicalUUID
    state: Literal[WorkflowState.CANCELLED]
    reason: BoundedText
    cancelled_at: UtcTimestamp


class VerificationFindingV1(_FrozenStrictModel):
    disposition: FindingDisposition
    summary: BoundedText
    evidence: Annotated[
        tuple[BoundedText, ...], Field(max_length=MAX_EVIDENCE_ITEMS)
    ] = ()

    @field_validator("evidence", mode="before")
    @classmethod
    def _freeze_evidence(cls, value: object) -> object:
        return _tuple_input(value)


class WorkflowVerificationResultV1(_FrozenStrictModel):
    schema_version: Literal["1"] = "1"
    run_id: CanonicalUUID | None = None
    ledger: VerificationFindingV1
    database: VerificationFindingV1
    artifacts: VerificationFindingV1
    approval: VerificationFindingV1
    receipt: VerificationFindingV1
    destination: VerificationFindingV1
    overall: FindingDisposition

    @model_validator(mode="after")
    def _consistent_overall(self) -> WorkflowVerificationResultV1:
        severity = {
            FindingDisposition.PASS: 0,
            FindingDisposition.WARN: 1,
            FindingDisposition.FAIL: 2,
        }
        findings = (
            self.ledger,
            self.database,
            self.artifacts,
            self.approval,
            self.receipt,
            self.destination,
        )
        expected = max(
            (finding.disposition for finding in findings), key=severity.__getitem__
        )
        if self.overall is not expected:
            raise ValueError("overall disposition must reflect the worst finding")
        return self


class WorkflowPruneResultV1(_FrozenStrictModel):
    schema_version: Literal["1"] = "1"
    cutoff: UtcTimestamp
    affected_run_ids: Annotated[
        tuple[CanonicalUUID, ...], Field(max_length=MAX_PRUNE_ITEMS)
    ] = ()
    artifact_hashes: Annotated[
        tuple[Sha256, ...], Field(max_length=MAX_PRUNE_ITEMS)
    ] = ()
    intent_event_hashes: Annotated[
        tuple[Sha256, ...], Field(max_length=MAX_PRUNE_ITEMS)
    ] = ()
    reclaimed_bytes: NonNegativeInt
    disposition: PruneDisposition

    @field_validator(
        "affected_run_ids", "artifact_hashes", "intent_event_hashes", mode="before"
    )
    @classmethod
    def _freeze_collections(cls, value: object) -> object:
        return _tuple_input(value)


def _json_value_copy(value: object, *, path: str) -> object:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} contains a nonfinite number")
        return value
    if isinstance(value, list):
        return [
            _json_value_copy(item, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    if isinstance(value, Mapping):
        copied: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{path} contains a non-string mapping key")
            copied[key] = _json_value_copy(item, path=f"{path}.{key}")
        return copied
    raise TypeError(f"{path} contains unsupported JSON value {type(value).__name__}")


def canonical_json(value: BaseModel | Mapping[str, object]) -> bytes:
    """Serialize a model or JSON-value mapping using the project canonical form."""
    if isinstance(value, BaseModel):
        payload: object = value.model_dump(mode="json")
    elif isinstance(value, Mapping):
        payload = value
    else:
        raise TypeError("canonical_json accepts a Pydantic model or mapping")
    copied = _json_value_copy(payload, path="$")
    return json.dumps(
        copied,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def sha256_canonical(value: BaseModel | Mapping[str, object]) -> str:
    """Return the SHA-256 digest of project-canonical JSON bytes."""
    return hashlib.sha256(canonical_json(value)).hexdigest()


__all__ = [
    "LEGAL_TRANSITIONS",
    "MAX_OPERATIONS",
    "AddKeywordOperation",
    "AddMarkerOperation",
    "AddTransitionOperation",
    "ApprovalDecision",
    "ApprovalSource",
    "ApprovalStrength",
    "ArtifactAvailabilityV1",
    "ArtifactState",
    "AssignRoleOperation",
    "BatchApplyTransitionOperation",
    "BatchAssignRolesOperation",
    "BatchRenameClipsOperation",
    "BatchRoleAssignmentRule",
    "ChangeSpeedOperation",
    "DeleteClipsOperation",
    "FillGapsOperation",
    "FindingDisposition",
    "FixFlashFramesOperation",
    "LockState",
    "ObservedFileState",
    "OperationDisposition",
    "OperationReceiptV1",
    "OperationValueChangeV1",
    "PriorDestinationState",
    "PruneDisposition",
    "RecoveryAssessmentV1",
    "RecoveryBranch",
    "ReorderClipsOperation",
    "SplitClipOperation",
    "TrimClipOperation",
    "ValidationIssueV1",
    "ValidationResultV1",
    "VerificationFindingV1",
    "WorkflowCancelResultV1",
    "WorkflowCommitReceiptV1",
    "WorkflowOperation",
    "WorkflowPlanV1",
    "WorkflowPrepareRequestV1",
    "WorkflowPreviewV1",
    "WorkflowPruneResultV1",
    "WorkflowState",
    "WorkflowStatusV1",
    "WorkflowTerminalErrorV1",
    "WorkflowVerificationResultV1",
    "canonical_json",
    "sha256_canonical",
    "transition_allowed",
]
