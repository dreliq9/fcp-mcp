from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from fcp_mcp.workflow.models import (
    LEGAL_TRANSITIONS,
    WorkflowState,
    transition_allowed,
)


@given(st.sampled_from(list(WorkflowState)), st.sampled_from(list(WorkflowState)))
def test_only_declared_transitions_are_legal(source, target):
    assert transition_allowed(source, target) is (target in LEGAL_TRANSITIONS[source])


def test_exact_transition_graph_is_closed_and_immutable():
    assert set(LEGAL_TRANSITIONS) == set(WorkflowState)
    assert LEGAL_TRANSITIONS == {
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


def test_terminal_states_have_no_outgoing_edges():
    terminal = {
        WorkflowState.FAILED,
        WorkflowState.REJECTED,
        WorkflowState.CANCELLED,
        WorkflowState.EXPIRED,
        WorkflowState.STALE,
        WorkflowState.ROLLED_BACK,
        WorkflowState.RECOVERY_REQUIRED,
        WorkflowState.COMMITTED,
    }
    assert all(LEGAL_TRANSITIONS[state] == frozenset() for state in terminal)


@given(
    st.one_of(st.text(), st.integers(), st.none()),
    st.one_of(st.text(), st.integers(), st.none()),
)
def test_transition_function_does_not_coerce_arbitrary_values(source, target):
    assert transition_allowed(source, target) is False
