"""Evidence-bound workflow approval policy with no SDK or server dependency."""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TextIO

from fcp_mcp.contracts import ErrorCode, FCPMCPError
from fcp_mcp.profiles import ApprovalMode
from fcp_mcp.workflow.ledger import (
    DecisionMutationResult,
    LedgerRunRecord,
    WorkflowLedger,
    approval_binding_for_run,
)
from fcp_mcp.workflow.models import (
    ApprovalDecision,
    ApprovalSource,
    ApprovalStrength,
    WorkflowState,
)

_CONFIRMATION = "approve"


@dataclass(frozen=True)
class ClientApproval:
    """Immutable client approval evidence staged for the commit transaction."""

    run_id: str
    binding_sha256: str
    source: ApprovalSource
    strength: ApprovalStrength
    expires_at: str


def _coded(code: ErrorCode, message: str) -> FCPMCPError:
    return FCPMCPError(code, message)


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise _coded(ErrorCode.INVALID_ARGUMENTS, "approval clock is invalid")
    if value.tzinfo is None or value.utcoffset() is None:
        raise _coded(ErrorCode.INVALID_ARGUMENTS, "approval clock must be timezone-aware")
    return value.astimezone(timezone.utc)


def _parse_utc(value: str | None) -> datetime:
    if value is None:
        raise _coded(
            ErrorCode.WORKFLOW_STATE_CONFLICT,
            "workflow approval expiry is missing",
        )
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except (TypeError, ValueError):
        raise _coded(
            ErrorCode.WORKFLOW_STATE_CONFLICT,
            "workflow approval expiry is invalid",
        ) from None
    return parsed.astimezone(timezone.utc)


def approval_binding(
    ledger: WorkflowLedger,
    run_id: str,
    *,
    plan_schema_version: str,
) -> str:
    """Hash exactly one authoritative durable workflow projection."""
    return ledger.approval_binding(
        run_id,
        plan_schema_version=plan_schema_version,
    )


def _run(ledger: WorkflowLedger, run_id: str) -> LedgerRunRecord:
    run = ledger.get_run(run_id)
    if run is None:
        raise _coded(
            ErrorCode.WORKFLOW_STATE_CONFLICT,
            "workflow run does not exist",
        )
    return run


def effective_state(
    ledger: WorkflowLedger,
    run_id: str,
    *,
    now: datetime,
) -> WorkflowState:
    """Return effective expiry without mutating the durable projection."""
    run = _run(ledger, run_id)
    if run.state in {WorkflowState.AWAITING_APPROVAL, WorkflowState.APPROVED} and _utc(
        now
    ) >= _parse_utc(run.expires_at):
        return WorkflowState.EXPIRED
    return run.state


def _require_awaiting(run: LedgerRunRecord, mode: ApprovalMode) -> None:
    if run.state is not WorkflowState.AWAITING_APPROVAL:
        raise _coded(
            ErrorCode.WORKFLOW_STATE_CONFLICT,
            "workflow is not awaiting approval",
        )
    if run.approval_mode is not mode:
        raise _coded(
            ErrorCode.WORKFLOW_STATE_CONFLICT,
            "workflow approval mode does not match",
        )


def _terminal_status(stream: TextIO) -> bool:
    try:
        value = stream.isatty()
    except Exception:  # noqa: BLE001 - sanitize terminal implementation failures
        raise _coded(
            ErrorCode.APPROVAL_REQUIRED,
            "terminal approval boundary is unavailable",
        ) from None
    if type(value) is not bool:
        raise _coded(
            ErrorCode.APPROVAL_REQUIRED,
            "terminal approval boundary is unavailable",
        )
    return value


def _candidate_matches(actual: str | None, expected: str | None) -> bool:
    return (
        actual is not None
        and expected is not None
        and len(actual) == len(expected)
        and hmac.compare_digest(actual, expected)
    )


def approve_cli(
    ledger: WorkflowLedger,
    run_id: str,
    *,
    plan_schema_version: str,
    input_stream: TextIO,
    output_stream: TextIO,
    yes: bool = False,
    expect_candidate_sha256: str | None = None,
    operator: str | None = None,
    host: str | None = None,
) -> DecisionMutationResult:
    """Approve one exact CLI-mode run using terminal or explicit batch policy."""
    run = _run(ledger, run_id)
    _require_awaiting(run, ApprovalMode.CLI)
    terminal_present = _terminal_status(input_stream) and _terminal_status(
        output_stream
    )
    if terminal_present:
        try:
            output_stream.write(
                f"Candidate SHA-256: {run.candidate_sha256}\n"
                f"Type {_CONFIRMATION!r} to approve this exact candidate: "
            )
            output_stream.flush()
            confirmed = input_stream.readline()
        except Exception:  # noqa: BLE001 - sanitize terminal implementation failures
            raise _coded(
                ErrorCode.APPROVAL_REQUIRED,
                "terminal approval boundary is unavailable",
            ) from None
        if not isinstance(confirmed, str):
            raise _coded(
                ErrorCode.APPROVAL_REQUIRED,
                "terminal approval boundary is unavailable",
            )
        if confirmed.rstrip("\r\n") != _CONFIRMATION:
            raise _coded(
                ErrorCode.APPROVAL_REQUIRED,
                "interactive workflow approval was not confirmed",
            )
    else:
        if not yes or expect_candidate_sha256 is None:
            raise _coded(
                ErrorCode.APPROVAL_REQUIRED,
                "noninteractive approval requires yes and expected candidate hash",
            )
        if not _candidate_matches(run.candidate_sha256, expect_candidate_sha256):
            raise _coded(
                ErrorCode.WORKFLOW_STALE,
                "candidate hash precondition failed",
            )
    result = ledger.record_approval_or_expire(
        run.run_id,
        expected_revision=run.revision,
        operator=operator,
        host=host,
        terminal_present=terminal_present,
        plan_schema_version=plan_schema_version,
    )
    if result.expired:
        raise _coded(ErrorCode.APPROVAL_EXPIRED, "workflow approval expired")
    if result.approval is None:
        raise _coded(
            ErrorCode.INTERNAL_ERROR,
            "workflow approval result is incomplete",
        )
    return DecisionMutationResult(
        run=result.run,
        approval=result.approval,
        event=result.event,
    )


def approve_client(
    ledger: WorkflowLedger,
    run_id: str,
    *,
    plan_schema_version: str,
    expect_candidate_sha256: str,
) -> ClientApproval:
    """Stage exact client evidence; Task 17 persists it in the commit CAS."""
    run = _run(ledger, run_id)
    _require_awaiting(run, ApprovalMode.CLIENT)
    if not _candidate_matches(run.candidate_sha256, expect_candidate_sha256):
        raise _coded(
            ErrorCode.WORKFLOW_STALE,
            "candidate hash precondition failed",
        )
    return ClientApproval(
        run_id=run.run_id,
        binding_sha256=approval_binding_for_run(
            run,
            plan_schema_version=plan_schema_version,
        ),
        source=ApprovalSource.CLIENT,
        strength=ApprovalStrength.CLIENT_UNVERIFIED_HUMAN,
        expires_at=run.expires_at,
    )


def reject(
    ledger: WorkflowLedger,
    run_id: str,
    *,
    plan_schema_version: str,
    reason: str | None = None,
    operator: str | None = None,
    host: str | None = None,
    input_stream: TextIO | None = None,
    output_stream: TextIO | None = None,
) -> DecisionMutationResult:
    """Persist one immutable rejection without leaking caller text to events."""
    run = _run(ledger, run_id)
    if run.state is not WorkflowState.AWAITING_APPROVAL:
        raise _coded(
            ErrorCode.WORKFLOW_STATE_CONFLICT,
            "workflow is not awaiting approval",
        )
    if reason is not None and (not isinstance(reason, str) or len(reason) > 4096):
        raise _coded(ErrorCode.INVALID_ARGUMENTS, "rejection reason is invalid")
    if (input_stream is None) != (output_stream is None):
        raise _coded(
            ErrorCode.INVALID_ARGUMENTS,
            "rejection terminal streams must be provided together",
        )
    terminal_present = (
        False
        if input_stream is None or output_stream is None
        else _terminal_status(input_stream) and _terminal_status(output_stream)
    )
    source = ApprovalSource(run.approval_mode.value)
    binding = approval_binding_for_run(
        run,
        plan_schema_version=plan_schema_version,
    )
    return ledger.record_decision(
        run.run_id,
        expected_state=WorkflowState.AWAITING_APPROVAL,
        expected_revision=run.revision,
        decision=ApprovalDecision.REJECTED,
        source=source,
        operator=operator,
        host=host,
        terminal_present=terminal_present,
        binding_sha256=binding,
        expires_at=run.expires_at,
        approval_summary="Workflow evidence rejected",
        event_type="approval_rejected",
        event_payload={
            "decision": ApprovalDecision.REJECTED.value,
            "source": source.value,
            "reason_present": bool(reason),
            "binding_sha256": binding,
        },
    )


def cancel(
    ledger: WorkflowLedger,
    run_id: str,
    *,
    reason: str | None = None,
) -> LedgerRunRecord:
    """Cancel a legal non-committing run with a bounded path-free event."""
    run = _run(ledger, run_id)
    if reason is not None and (not isinstance(reason, str) or len(reason) > 4096):
        raise _coded(ErrorCode.INVALID_ARGUMENTS, "cancellation reason is invalid")
    result = ledger.transition(
        run.run_id,
        expected_state=run.state,
        expected_revision=run.revision,
        target_state=WorkflowState.CANCELLED,
        event_type="cancelled",
        payload={"reason_present": bool(reason)},
    )
    return result.run


__all__ = [
    "ClientApproval",
    "approval_binding",
    "approve_cli",
    "approve_client",
    "cancel",
    "effective_state",
    "reject",
]
