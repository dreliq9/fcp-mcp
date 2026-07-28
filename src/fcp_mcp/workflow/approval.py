"""Evidence-bound workflow approval policy with no SDK or server dependency."""

from __future__ import annotations

import hashlib
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
)
from fcp_mcp.workflow.models import (
    ApprovalDecision,
    ApprovalSource,
    ApprovalStrength,
    WorkflowState,
    canonical_json,
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


def _path_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _complete_binding_fields(
    run: LedgerRunRecord,
    *,
    plan_schema_version: str,
) -> dict[str, object]:
    if (
        not isinstance(plan_schema_version, str)
        or not plan_schema_version
        or len(plan_schema_version) > 64
    ):
        raise _coded(
            ErrorCode.INVALID_ARGUMENTS,
            "plan schema version is invalid",
        )
    required = (
        run.source_sha256,
        run.prior_destination_state,
        run.plan_sha256,
        run.candidate_sha256,
        run.diff_sha256,
        run.expires_at,
    )
    if any(value is None for value in required):
        raise _coded(
            ErrorCode.WORKFLOW_STATE_CONFLICT,
            "workflow approval evidence is incomplete",
        )
    return {
        "run_id": run.run_id,
        "graph_version": run.graph_version,
        "plan_schema_version": plan_schema_version,
        "source_path_sha256": _path_sha256(run.source_path),
        "destination_path_sha256": _path_sha256(run.destination_path),
        "source_sha256": run.source_sha256,
        "prior_destination_state": run.prior_destination_state.value,
        "prior_destination_sha256": run.prior_destination_sha256,
        "plan_sha256": run.plan_sha256,
        "candidate_sha256": run.candidate_sha256,
        "diff_sha256": run.diff_sha256,
        "approval_mode": run.approval_mode.value,
        "expires_at": run.expires_at,
    }


def approval_binding(
    run: LedgerRunRecord,
    *,
    plan_schema_version: str,
) -> str:
    """Hash exactly the durable evidence authorized by the approval design."""
    if not isinstance(run, LedgerRunRecord):
        raise _coded(ErrorCode.INVALID_ARGUMENTS, "workflow run is invalid")
    payload = _complete_binding_fields(
        run,
        plan_schema_version=plan_schema_version,
    )
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def effective_state(run: LedgerRunRecord, *, now: datetime) -> WorkflowState:
    """Return effective expiry without mutating the durable projection."""
    if not isinstance(run, LedgerRunRecord):
        raise _coded(ErrorCode.INVALID_ARGUMENTS, "workflow run is invalid")
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


def _expire_and_raise(
    ledger: WorkflowLedger,
    run: LedgerRunRecord,
    *,
    now: datetime,
) -> None:
    if effective_state(run, now=now) is not WorkflowState.EXPIRED:
        return
    ledger.transition(
        run.run_id,
        expected_state=run.state,
        expected_revision=run.revision,
        target_state=WorkflowState.EXPIRED,
        event_type="approval_expired",
        payload={"expires_at": run.expires_at},
    )
    raise _coded(ErrorCode.APPROVAL_EXPIRED, "workflow approval expired")


def _is_terminal(stream: TextIO) -> bool:
    try:
        return bool(stream.isatty())
    except (AttributeError, OSError, ValueError):
        return False


def _candidate_matches(actual: str | None, expected: str | None) -> bool:
    return (
        actual is not None
        and expected is not None
        and len(actual) == len(expected)
        and hmac.compare_digest(actual, expected)
    )


def approve_cli(
    ledger: WorkflowLedger,
    run: LedgerRunRecord,
    *,
    plan_schema_version: str,
    now: datetime,
    input_stream: TextIO,
    output_stream: TextIO,
    yes: bool = False,
    expect_candidate_sha256: str | None = None,
    operator: str | None = None,
    host: str | None = None,
) -> DecisionMutationResult:
    """Approve one exact CLI-mode run using terminal or explicit batch policy."""
    _require_awaiting(run, ApprovalMode.CLI)
    _expire_and_raise(ledger, run, now=now)
    terminal_present = _is_terminal(input_stream) and _is_terminal(output_stream)
    if terminal_present:
        output_stream.write(
            f"Candidate SHA-256: {run.candidate_sha256}\n"
            f"Type {_CONFIRMATION!r} to approve this exact candidate: "
        )
        output_stream.flush()
        try:
            confirmed = input_stream.readline()
        except (EOFError, OSError, ValueError):
            confirmed = ""
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
    binding = approval_binding(
        run,
        plan_schema_version=plan_schema_version,
    )
    return ledger.record_decision(
        run.run_id,
        expected_state=WorkflowState.AWAITING_APPROVAL,
        expected_revision=run.revision,
        decision=ApprovalDecision.APPROVED,
        source=ApprovalSource.CLI,
        operator=operator,
        host=host,
        terminal_present=terminal_present,
        binding_sha256=binding,
        expires_at=run.expires_at,
        approval_summary="Exact workflow evidence approved",
        event_type="approval_recorded",
        event_payload={
            "decision": ApprovalDecision.APPROVED.value,
            "source": ApprovalSource.CLI.value,
            "binding_sha256": binding,
        },
    )


def approve_client(
    ledger: WorkflowLedger,
    run: LedgerRunRecord,
    *,
    plan_schema_version: str,
    now: datetime,
    expect_candidate_sha256: str,
) -> ClientApproval:
    """Stage exact client evidence; Task 17 persists it in the commit CAS."""
    _require_awaiting(run, ApprovalMode.CLIENT)
    _expire_and_raise(ledger, run, now=now)
    if not _candidate_matches(run.candidate_sha256, expect_candidate_sha256):
        raise _coded(
            ErrorCode.WORKFLOW_STALE,
            "candidate hash precondition failed",
        )
    return ClientApproval(
        run_id=run.run_id,
        binding_sha256=approval_binding(
            run,
            plan_schema_version=plan_schema_version,
        ),
        source=ApprovalSource.CLIENT,
        strength=ApprovalStrength.CLIENT_UNVERIFIED_HUMAN,
        expires_at=run.expires_at,
    )


def reject(
    ledger: WorkflowLedger,
    run: LedgerRunRecord,
    *,
    plan_schema_version: str,
    reason: str | None = None,
    operator: str | None = None,
    host: str | None = None,
    terminal_present: bool = False,
) -> DecisionMutationResult:
    """Persist one immutable rejection without leaking caller text to events."""
    if run.state is not WorkflowState.AWAITING_APPROVAL:
        raise _coded(
            ErrorCode.WORKFLOW_STATE_CONFLICT,
            "workflow is not awaiting approval",
        )
    if reason is not None and (not isinstance(reason, str) or len(reason) > 4096):
        raise _coded(ErrorCode.INVALID_ARGUMENTS, "rejection reason is invalid")
    source = ApprovalSource(run.approval_mode.value)
    binding = approval_binding(
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
    run: LedgerRunRecord,
    *,
    reason: str | None = None,
) -> LedgerRunRecord:
    """Cancel a legal non-committing run with a bounded path-free event."""
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
