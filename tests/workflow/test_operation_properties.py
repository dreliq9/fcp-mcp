from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from fcp_mcp.fcpxml.writer import FCPXMLModifier
from fcp_mcp.workflow.models import (
    AddMarkerOperation,
    AssignRoleOperation,
    WorkflowPlanV1,
    sha256_canonical,
)
from fcp_mcp.workflow.operations import (
    CandidateDisposition,
    build_source_inventory,
    execute_plan,
    normalize_plan,
)

safe_text = st.text(
    alphabet=st.characters(
        blacklist_categories=("Cc", "Cs"),
        blacklist_characters=("\x00",),
    ),
    min_size=1,
    max_size=24,
)


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(st.lists(safe_text, min_size=1, max_size=25))
def test_normalized_ids_are_contiguous_and_hash_survives_round_trip(
    sample_fcpxml_path, values
):
    plan = WorkflowPlanV1(
        operations=[
            AddMarkerOperation(
                kind="add_marker",
                clip_name="Interview_A",
                start=f"{index}s",
                value=value,
            )
            for index, value in enumerate(values)
        ]
    )
    before = plan.model_dump(mode="json")
    round_tripped = WorkflowPlanV1.model_validate_json(plan.model_dump_json())
    inventory = build_source_inventory(FCPXMLModifier(sample_fcpxml_path))

    normalized = normalize_plan(plan, inventory)
    round_trip_normalized = normalize_plan(round_tripped, inventory)

    assert [item.operation_id for item in normalized.operations] == [
        f"op-{index:03d}" for index in range(1, len(values) + 1)
    ]
    assert normalized.caller_plan_sha256 == sha256_canonical(plan)
    assert round_trip_normalized.caller_plan_sha256 == normalized.caller_plan_sha256
    assert plan.model_dump(mode="json") == before


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(st.lists(safe_text, min_size=1, max_size=8))
def test_candidate_bytes_are_deterministic_and_plan_is_not_mutated(
    sample_fcpxml_path, values
):
    plan = WorkflowPlanV1(
        operations=[
            AddMarkerOperation(
                kind="add_marker",
                clip_name="Interview_A",
                start=f"{index}s",
                value=value,
            )
            for index, value in enumerate(values)
        ]
    )
    before = plan.model_dump(mode="json")

    first = execute_plan(sample_fcpxml_path, plan)
    second = execute_plan(sample_fcpxml_path, plan)

    assert first.disposition is CandidateDisposition.SUCCEEDED
    assert second.disposition is CandidateDisposition.SUCCEEDED
    assert first.candidate_bytes == second.candidate_bytes
    assert first.receipts == second.receipts
    assert plan.model_dump(mode="json") == before


@given(safe_text)
def test_unsupported_discriminant_fails_model_validation_before_execution(value):
    with pytest.raises(ValidationError):
        WorkflowPlanV1.model_validate(
            {
                "operations": [
                    {
                        "kind": value + "_unsupported",
                        "clip_name": "Interview_A",
                    }
                ]
            }
        )


@given(safe_text)
def test_frozen_plan_rejects_caller_mutation(value):
    plan = WorkflowPlanV1(
        operations=[
            AssignRoleOperation(
                kind="assign_role",
                clip_name="Interview_A",
                role=value,
            )
        ]
    )

    with pytest.raises(ValidationError):
        plan.operations[0].role = "mutated"
