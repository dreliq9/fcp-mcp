"""SDK-neutral registration and lazy runtime for the public workflow surface."""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable
from datetime import datetime
from typing import Literal
from uuid import UUID

from fcp_mcp.config import RuntimeConfig
from fcp_mcp.contracts import ErrorCode, FCPMCPError
from fcp_mcp.profiles import ApprovalMode, Profile, ToolClass
from fcp_mcp.registry import ResourceRegistry, ToolRegistry
from fcp_mcp.result_models.common import ToolOutcome
from fcp_mcp.security.paths import PathPolicy
from fcp_mcp.tool_metadata import OFFLINE_READ, OFFLINE_WRITE, STATEFUL_WRITE
from fcp_mcp.version import package_version
from fcp_mcp.workflow.artifacts import (
    ArtifactKind,
    ArtifactMetadataV1,
    ArtifactStore,
    StatePaths,
)
from fcp_mcp.workflow.engine import WorkflowEngine
from fcp_mcp.workflow.ledger import ArtifactRecord, LedgerRunRecord, WorkflowLedger
from fcp_mcp.workflow.models import (
    ArtifactAvailabilityV1,
    ArtifactState,
    WorkflowCancelResultV1,
    WorkflowCommitReceiptV1,
    WorkflowOperation,
    WorkflowPrepareRequestV1,
    WorkflowPreviewV1,
    WorkflowState,
    WorkflowStatusV1,
    WorkflowTerminalErrorV1,
)

_RUN_ID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_RESOURCE_PROFILES = frozenset({Profile.WORKFLOW, Profile.EDIT, Profile.FULL})
_EVENT_LIMIT = 100


def _coded(code: ErrorCode, message: str) -> FCPMCPError:
    return FCPMCPError(code, message)


def _canonical_run_id(value: object) -> str:
    if not isinstance(value, str) or not _RUN_ID_PATTERN.fullmatch(value):
        raise _coded(
            ErrorCode.INVALID_ARGUMENTS,
            "run_id must use canonical lowercase UUID spelling",
        )
    try:
        parsed = UUID(value)
    except ValueError as error:
        coded = _coded(ErrorCode.INVALID_ARGUMENTS, "run_id is not a UUID")
        coded.__cause__ = error
        raise coded
    if parsed.int == 0 or str(parsed) != value:
        raise _coded(
            ErrorCode.INVALID_ARGUMENTS,
            "run_id must be a non-nil canonical lowercase UUID",
        )
    return value


def _metadata(record: ArtifactRecord) -> ArtifactMetadataV1:
    return ArtifactMetadataV1(
        run_id=record.run_id,
        kind=record.kind,
        relative_path=record.relative_path,
        sha256=record.sha256,
        byte_size=record.byte_size,
        created_at=datetime.fromisoformat(record.created_at.replace("Z", "+00:00")),
    )


class WorkflowRuntime:
    """One process-local workflow runtime initialized on first actual use."""

    def __init__(self, config: RuntimeConfig) -> None:
        self.config = config
        paths = StatePaths.from_config(config)
        self.ledger = WorkflowLedger(paths, package_version=package_version())
        self.ledger.initialize()
        self.artifacts = ArtifactStore(
            paths,
            max_artifact_bytes=config.max_artifact_bytes,
        )
        self.engine = WorkflowEngine(
            config,
            PathPolicy(config),
            self.ledger,
            self.artifacts,
        )

    def _run(self, run_id: str) -> LedgerRunRecord:
        run = self.ledger.get_run(_canonical_run_id(run_id))
        if run is None:
            raise _coded(
                ErrorCode.WORKFLOW_STATE_CONFLICT,
                "workflow run does not exist",
            )
        return run

    def _artifact(
        self,
        run: LedgerRunRecord,
        kind: ArtifactKind,
        expected_sha256: str | None,
        expected_size: int | None,
    ) -> ArtifactAvailabilityV1:
        record = self.ledger.get_artifact(run.run_id, kind)
        if expected_sha256 is None:
            if record is not None:
                raise _coded(
                    ErrorCode.ARTIFACT_CORRUPT,
                    f"{kind.value} artifact exists without a durable milestone",
                )
            return ArtifactAvailabilityV1(state=ArtifactState.ABSENT)
        if expected_size is None:
            raise _coded(
                ErrorCode.ARTIFACT_CORRUPT,
                f"{kind.value} artifact size is missing",
            )
        if record is None:
            return ArtifactAvailabilityV1(
                state=ArtifactState.CORRUPT,
                sha256=expected_sha256,
                size_bytes=expected_size,
            )
        state = ArtifactState.PRESENT
        try:
            self.artifacts.read(_metadata(record))
        except FCPMCPError:
            state = ArtifactState.CORRUPT
        return ArtifactAvailabilityV1(
            state=state,
            sha256=expected_sha256,
            size_bytes=expected_size,
        )

    def status(self, run_id: str) -> WorkflowStatusV1:
        run = self._run(run_id)
        effective_state = self.engine.effective_state(run.run_id)
        effective_updated_at = run.updated_at
        if effective_state is WorkflowState.EXPIRED and run.state is not effective_state:
            if run.expires_at is None:
                raise _coded(
                    ErrorCode.ARTIFACT_CORRUPT,
                    "expired workflow is missing its expiry timestamp",
                )
            effective_updated_at = run.expires_at
        terminal_error = (
            WorkflowTerminalErrorV1(
                code=run.terminal_error_code,
                summary=run.terminal_error_summary,
            )
            if run.terminal_error_code is not None
            and run.terminal_error_summary is not None
            else None
        )
        recovery = (
            self.engine.assess(run.run_id)
            if run.state is WorkflowState.COMMITTING
            else None
        )
        return WorkflowStatusV1(
            graph_version=run.graph_version,
            package_version=run.package_version,
            run_version=run.run_version,
            run_id=run.run_id,
            state=effective_state,
            source_path=run.source_path,
            destination_path=run.destination_path,
            source_sha256=run.source_sha256,
            prior_destination_state=run.prior_destination_state,
            prior_destination_sha256=run.prior_destination_sha256,
            plan_sha256=run.plan_sha256,
            candidate_sha256=run.candidate_sha256,
            diff_sha256=run.diff_sha256,
            revision=run.revision,
            approval_decision=run.approval_decision,
            approval_source=run.approval_source,
            created_at=run.created_at,
            updated_at=effective_updated_at,
            approved_at=run.approved_at,
            committed_at=run.committed_at,
            expires_at=run.expires_at,
            terminal_error=terminal_error,
            candidate_artifact=self._artifact(
                run,
                ArtifactKind.CANDIDATE,
                run.candidate_sha256,
                run.candidate_size_bytes,
            ),
            diff_artifact=self._artifact(
                run,
                ArtifactKind.DIFF,
                run.diff_sha256,
                run.diff_size_bytes,
            ),
            receipt_artifact=self._artifact(
                run,
                ArtifactKind.RECEIPT,
                run.receipt_sha256,
                run.receipt_size_bytes,
            ),
            recovery=recovery,
            run_uri=f"fcp-workflow://runs/{run.run_id}",
            events_uri=f"fcp-workflow://runs/{run.run_id}/events",
            diff_uri=f"fcp-workflow://runs/{run.run_id}/diff",
        )

    def events_json(self, run_id: str) -> str:
        run = self._run(run_id)
        events = self.ledger.list_events(run.run_id, limit=_EVENT_LIMIT)
        payload = {
            "schema_version": "1",
            "run_id": run.run_id,
            "events": [
                {
                    "sequence": event.sequence,
                    "event_type": event.event_type,
                    "payload": event.payload,
                    "timestamp": event.timestamp,
                    "elapsed_ms": event.elapsed_ms,
                    "previous_hash": event.previous_hash,
                    "event_hash": event.event_hash,
                }
                for event in events
            ],
            "truncated": bool(events and events[-1].sequence < run.revision),
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    def diff_json(self, run_id: str) -> str:
        run = self._run(run_id)
        record = self.ledger.get_artifact(run.run_id, ArtifactKind.DIFF)
        if record is None:
            raise _coded(ErrorCode.ARTIFACT_CORRUPT, "workflow diff is unavailable")
        try:
            return self.artifacts.read(_metadata(record)).decode("utf-8")
        except UnicodeDecodeError as error:
            coded = _coded(ErrorCode.ARTIFACT_CORRUPT, "workflow diff is not UTF-8 JSON")
            coded.__cause__ = error
            raise coded

    def commit(
        self,
        run_id: str,
        *,
        expected_candidate_sha256: str | None = None,
    ) -> WorkflowCommitReceiptV1:
        selected = _canonical_run_id(run_id)
        approval = None
        if self.config.workflow_approval is ApprovalMode.CLIENT:
            run = self._run(selected)
            if run.candidate_sha256 is None:
                raise _coded(
                    ErrorCode.WORKFLOW_STATE_CONFLICT,
                    "workflow candidate evidence is incomplete",
                )
            if expected_candidate_sha256 is None:
                raise _coded(
                    ErrorCode.APPROVAL_REQUIRED,
                    "client commit requires the reviewed candidate hash",
                )
            approval = self.engine.approve_client(
                selected,
                expect_candidate_sha256=expected_candidate_sha256,
            )
        elif expected_candidate_sha256 is not None:
            raise _coded(
                ErrorCode.WORKFLOW_STATE_CONFLICT,
                "client candidate hash is not accepted in CLI approval mode",
            )
        return self.engine.commit(selected, client_approval=approval)


class LazyWorkflowRuntime:
    """Thread-safe holder that does not touch user state until requested."""

    def __init__(self, config: RuntimeConfig) -> None:
        self._config = config
        self._runtime: WorkflowRuntime | None = None
        self._lock = threading.Lock()

    def get(self) -> WorkflowRuntime:
        current = self._runtime
        if current is not None:
            return current
        with self._lock:
            if self._runtime is None:
                self._runtime = WorkflowRuntime(self._config)
            return self._runtime


def _outcome(text: str, structured):
    if not text or len(text) > 4096:
        raise RuntimeError("workflow tool text must contain 1..4096 characters")
    return ToolOutcome(text=text, structured=structured)


def register_workflow_surface(
    config: RuntimeConfig,
    tools: ToolRegistry,
    resources: ResourceRegistry,
    *,
    runtime_provider: Callable[[], WorkflowRuntime] | None = None,
) -> LazyWorkflowRuntime:
    """Register exactly four tools and three resources without initializing state."""
    lazy = LazyWorkflowRuntime(config)
    runtime = runtime_provider or lazy.get

    @tools.tool(
        name="fcpxml_workflow_prepare",
        tool_class=ToolClass.WORKFLOW,
        safety_hints=STATEFUL_WRITE,
        result_model=WorkflowPreviewV1,
        description="Prepare and validate a private transactional FCPXML preview.",
    )
    def workflow_prepare(
        source_path: str,
        destination_path: str,
        operations: list[WorkflowOperation],
        schema_version: Literal["1"] = "1",
        expected_source_sha256: str | None = None,
        idempotency_key: str | None = None,
    ) -> ToolOutcome[WorkflowPreviewV1]:
        request = WorkflowPrepareRequestV1.model_validate(
            {
                "schema_version": schema_version,
                "source_path": source_path,
                "destination_path": destination_path,
                "operations": operations,
                "expected_source_sha256": expected_source_sha256,
                "idempotency_key": idempotency_key,
            },
            context={"max_operations": config.max_operations},
        )
        preview = runtime().engine.prepare(request)
        return _outcome(
            f"Prepared workflow {preview.run_id}: {preview.state.value}. "
            f"Review {preview.diff_uri} before approval and commit.",
            preview,
        )

    @tools.tool(
        name="fcpxml_workflow_status",
        tool_class=ToolClass.WORKFLOW,
        safety_hints=OFFLINE_READ,
        result_model=WorkflowStatusV1,
        description="Read one workflow's durable state and evidence assessment.",
    )
    def workflow_status(run_id: str) -> ToolOutcome[WorkflowStatusV1]:
        status = runtime().status(_canonical_run_id(run_id))
        return _outcome(
            f"Workflow {status.run_id}: {status.state.value} at revision "
            f"{status.revision}.",
            status,
        )

    @tools.tool(
        name="fcpxml_workflow_commit",
        tool_class=ToolClass.WORKFLOW,
        safety_hints=OFFLINE_WRITE,
        result_model=WorkflowCommitReceiptV1,
        description="Commit one exactly approved transactional FCPXML candidate.",
    )
    def workflow_commit(
        run_id: str,
        expected_candidate_sha256: str | None = None,
    ) -> ToolOutcome[WorkflowCommitReceiptV1]:
        selected = _canonical_run_id(run_id)
        receipt = runtime().commit(
            selected,
            expected_candidate_sha256=expected_candidate_sha256,
        )
        return _outcome(
            f"Committed workflow {receipt.run_id}: output "
            f"{receipt.output_sha256}.",
            receipt,
        )

    @tools.tool(
        name="fcpxml_workflow_cancel",
        tool_class=ToolClass.WORKFLOW,
        safety_hints=STATEFUL_WRITE,
        result_model=WorkflowCancelResultV1,
        description="Cancel a legal non-committing workflow while preserving evidence.",
    )
    def workflow_cancel(
        run_id: str,
        reason: str | None = None,
    ) -> ToolOutcome[WorkflowCancelResultV1]:
        selected = _canonical_run_id(run_id)
        cancelled = runtime().engine.cancel(selected, reason=reason)
        result = WorkflowCancelResultV1(
            run_id=cancelled.run_id,
            state=WorkflowState.CANCELLED,
            reason=reason or "Cancelled by client",
            cancelled_at=cancelled.updated_at,
        )
        return _outcome(
            f"Cancelled workflow {result.run_id}; durable evidence was preserved.",
            result,
        )

    @resources.resource(
        "fcp-workflow://runs/{run_id}",
        name="workflow-run",
        mime_type="application/json",
        profiles=_RESOURCE_PROFILES,
        description="Read one durable workflow status projection.",
    )
    def workflow_run_resource(run_id: str) -> str:
        selected = _canonical_run_id(run_id)
        return runtime().status(selected).model_dump_json(indent=2)

    @resources.resource(
        "fcp-workflow://runs/{run_id}/events",
        name="workflow-events",
        mime_type="application/json",
        profiles=_RESOURCE_PROFILES,
        description="Read a bounded prefix of one workflow's durable event chain.",
    )
    def workflow_events_resource(run_id: str) -> str:
        selected = _canonical_run_id(run_id)
        return runtime().events_json(selected)

    @resources.resource(
        "fcp-workflow://runs/{run_id}/diff",
        name="workflow-diff",
        mime_type="application/json",
        profiles=_RESOURCE_PROFILES,
        description="Read the complete bounded JSON diff for one workflow.",
    )
    def workflow_diff_resource(run_id: str) -> str:
        selected = _canonical_run_id(run_id)
        return runtime().diff_json(selected)

    return lazy


__all__ = [
    "LazyWorkflowRuntime",
    "WorkflowRuntime",
    "register_workflow_surface",
]
