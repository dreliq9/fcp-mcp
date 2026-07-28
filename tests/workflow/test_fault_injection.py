from __future__ import annotations

from pathlib import Path

import pytest
from test_recovery import RUN_ID, interrupt_commit, make_engine

from fcp_mcp.workflow.models import RecoveryBranch, WorkflowState


@pytest.mark.parametrize(
    ("boundary", "branch", "state"),
    (
        (
            "commit_started",
            RecoveryBranch.MARK_ROLLED_BACK,
            WorkflowState.ROLLED_BACK,
        ),
        (
            "destination_replaced",
            RecoveryBranch.FINALIZE_COMMITTED,
            WorkflowState.COMMITTED,
        ),
        (
            "receipt_body_written",
            RecoveryBranch.FINALIZE_COMMITTED,
            WorkflowState.COMMITTED,
        ),
    ),
)
def test_restart_reconciles_each_durable_commit_outcome(
    tmp_path: Path,
    boundary: str,
    branch: RecoveryBranch,
    state: WorkflowState,
) -> None:
    engine, _, _ = make_engine(tmp_path)
    interrupt_commit(engine, boundary)
    restarted, ledger, _ = make_engine(tmp_path, prepare=False)
    receipt_path = (
        restarted.artifacts.paths.artifacts / RUN_ID / "receipt.json"
    )
    receipt_before = (
        (receipt_path.stat().st_ino, receipt_path.read_bytes())
        if receipt_path.exists()
        else None
    )

    assert restarted.assess(RUN_ID).recommended_branch is branch
    result = restarted.reconcile(RUN_ID)

    assert result.state is state
    assert ledger.get_run(RUN_ID).state is state
    if receipt_before is not None:
        assert (receipt_path.stat().st_ino, receipt_path.read_bytes()) == receipt_before


def test_ambiguous_candidate_never_becomes_committed_or_rolled_back(
    tmp_path: Path,
) -> None:
    engine, ledger, destination = make_engine(tmp_path)
    interrupt_commit(engine, "destination_replaced")
    destination.write_bytes(b"not the candidate or prior state")
    restarted, restarted_ledger, _ = make_engine(tmp_path, prepare=False)

    assessment = restarted.assess(RUN_ID)
    result = restarted.reconcile(RUN_ID)

    assert assessment.recommended_branch is RecoveryBranch.MARK_RECOVERY_REQUIRED
    assert result.state is WorkflowState.RECOVERY_REQUIRED
    assert restarted_ledger.get_run(RUN_ID).state is WorkflowState.RECOVERY_REQUIRED
    assert [event.event_type for event in ledger.list_events(RUN_ID, limit=100)][
        -1
    ] == "commit_recovery_required"


def test_corrupt_unrecorded_receipt_is_ambiguous(tmp_path: Path) -> None:
    engine, _, _ = make_engine(tmp_path)
    interrupt_commit(engine, "receipt_body_written")
    restarted, _, _ = make_engine(tmp_path, prepare=False)
    receipt_path = restarted.artifacts.paths.artifacts / RUN_ID / "receipt.json"
    receipt_path.write_bytes(b"corrupt receipt")

    assessment = restarted.assess(RUN_ID)

    assert assessment.recommended_branch is RecoveryBranch.MARK_RECOVERY_REQUIRED
    assert "receipt" in " ".join(assessment.ambiguity_reasons)


def test_valid_receipt_cannot_mask_destination_rollback(tmp_path: Path) -> None:
    engine, _, destination = make_engine(tmp_path)
    interrupt_commit(engine, "receipt_body_written")
    destination.unlink()
    restarted, _, _ = make_engine(tmp_path, prepare=False)

    assessment = restarted.assess(RUN_ID)

    assert assessment.recommended_branch is RecoveryBranch.MARK_RECOVERY_REQUIRED
    assert "receipt claims commit" in " ".join(assessment.ambiguity_reasons)
