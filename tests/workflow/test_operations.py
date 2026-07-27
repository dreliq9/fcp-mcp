from __future__ import annotations

import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from defusedxml.common import DefusedXmlException

import fcp_mcp.workflow.operations as operations_module
from fcp_mcp.contracts import ErrorCode
from fcp_mcp.fcpxml.writer import FCPXMLModifier
from fcp_mcp.workflow.models import (
    AddKeywordOperation,
    AddMarkerOperation,
    AddTransitionOperation,
    AssignRoleOperation,
    BatchApplyTransitionOperation,
    BatchAssignRolesOperation,
    BatchRenameClipsOperation,
    BatchRoleAssignmentRule,
    ChangeSpeedOperation,
    DeleteClipsOperation,
    FillGapsOperation,
    FixFlashFramesOperation,
    OperationDisposition,
    ReorderClipsOperation,
    SplitClipOperation,
    TrimClipOperation,
    WorkflowPlanV1,
)
from fcp_mcp.workflow.operations import (
    OPERATION_EXECUTORS,
    CandidateDisposition,
    build_source_inventory,
    execute_plan,
    normalize_plan,
)

EXPECTED_KINDS = {
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
}


def _plan(operation) -> WorkflowPlanV1:
    return WorkflowPlanV1(operations=[operation])


def _candidate_root(execution) -> ET.Element:
    assert execution.disposition is CandidateDisposition.SUCCEEDED
    assert execution.candidate_bytes is not None
    return ET.fromstring(execution.candidate_bytes)


def _first_named(root: ET.Element, name: str) -> ET.Element:
    return next(
        element
        for element in root.iter()
        if element.get("name") == name
        and element.tag
        in {
            "asset-clip",
            "clip",
            "gap",
            "title",
            "generator",
            "compound-clip",
            "mc-clip",
            "sync-clip",
            "audition",
            "video",
            "audio",
            "ref-clip",
        }
    )


def _transition_attributes(root: ET.Element, duration: str) -> list[dict[str, str]]:
    return [
        dict(transition.attrib)
        for transition in root.findall(".//transition")
        if transition.get("duration") == duration
    ]


@pytest.mark.parametrize(
    ("operation", "expected_count", "expected_entities", "assert_effect"),
    [
        (
            AddMarkerOperation(
                kind="add_marker",
                clip_name="Interview_A",
                start="0s",
                value="Start",
                note="operator note",
                marker_type="chapter",
                duration="1001/30000s",
            ),
            1,
            ("Interview_A",),
            lambda root: (
                _first_named(root, "Interview_A")
                .find("chapter-marker")
                .attrib
                == {
                    "start": "0s",
                    "duration": "1001/30000s",
                    "value": "Start",
                    "note": "operator note",
                }
            ),
        ),
        (
            AddKeywordOperation(
                kind="add_keyword",
                clip_name="Interview_A",
                value="favorite",
                start="0s",
                duration="1001/30000s",
            ),
            1,
            ("Interview_A",),
            lambda root: any(
                keyword.attrib
                == {
                    "value": "favorite",
                    "start": "0s",
                    "duration": "1001/30000s",
                }
                for keyword in _first_named(root, "Interview_A").findall("keyword")
            ),
        ),
        (
            TrimClipOperation(
                kind="trim_clip",
                clip_name="Interview_A",
                new_start="0s",
                new_duration="120120/30000s",
            ),
            1,
            ("Interview_A",),
            lambda root: (
                _first_named(root, "Interview_A").get("start") == "0s"
                and _first_named(root, "Interview_A").get("duration")
                == "120120/30000s"
            ),
        ),
        (
            SplitClipOperation(
                kind="split_clip",
                clip_name="Interview_A",
                split_at="60060/30000s",
            ),
            2,
            ("Interview_A", "Interview_A_split"),
                lambda root: (
                    _first_named(root, "Interview_A").get("duration") == "60060/30000s"
                    and _first_named(root, "Interview_A_split").get("duration")
                    == "3003/1000s"
                    and _first_named(root, "Interview_A_split").find("marker") is None
                ),
        ),
        (
            DeleteClipsOperation(
                kind="delete_clips",
                clip_names=["Broll_Beach_Flash"],
            ),
            1,
            ("Broll_Beach_Flash",),
            lambda root: not any(
                element.get("name") == "Broll_Beach_Flash" for element in root.iter()
            ),
        ),
        (
            ReorderClipsOperation(
                kind="reorder_clips",
                clip_names=["Broll_City", "Interview_A"],
            ),
            7,
            (
                "Broll_City",
                "Interview_A",
                "Cross Dissolve",
                "Broll_Beach",
                "Gap",
                "Broll_Beach_Flash",
                "Interview_A_Outro",
            ),
            lambda root: [
                child.get("name")
                for child in root.find(".//spine")
                if child.get("name") in {"Broll_City", "Interview_A"}
            ]
            == ["Broll_City", "Interview_A"],
        ),
        (
            AddTransitionOperation(
                kind="add_transition",
                after_clip_name="Broll_City",
                duration="1001/30000s",
                name="Cross Dissolve",
                ref="r7",
            ),
            1,
            ("Broll_City",),
            lambda root: any(
                transition.attrib.get("duration") == "1001/30000s"
                and transition.attrib.get("name") == "Cross Dissolve"
                and transition.attrib.get("ref") == "r7"
                for transition in root.findall(".//transition")
            ),
        ),
        (
            ChangeSpeedOperation(
                kind="change_speed",
                clip_name="Broll_Beach",
                speed_factor=2.0,
            ),
            1,
            ("Broll_Beach",),
            lambda root: (
                _first_named(root, "Broll_Beach").get("duration") == "60060/30000s"
                and _first_named(root, "Broll_Beach").find("timeMap") is not None
            ),
        ),
        (
            AssignRoleOperation(
                kind="assign_role",
                clip_name="Interview_A",
                role="Voice",
            ),
            1,
            ("Interview_A",),
            lambda root: _first_named(root, "Interview_A").get("role") == "Voice",
        ),
        (
            BatchAssignRolesOperation(
                kind="batch_assign_roles",
                rules=[
                    BatchRoleAssignmentRule(match="Interview", role="Voice"),
                    BatchRoleAssignmentRule(match="Broll", role="B-Roll"),
                ],
            ),
            5,
            (
                "Interview_A",
                "Broll_Beach",
                "Broll_City",
                "Broll_Beach_Flash",
                "Interview_A_Outro",
            ),
            lambda root: (
                _first_named(root, "Interview_A").get("role") == "Voice"
                and _first_named(root, "Broll_Beach").get("role") == "B-Roll"
            ),
        ),
        (
            BatchRenameClipsOperation(
                kind="batch_rename_clips",
                pattern="Broll_",
                replacement="B-Roll_",
            ),
            5,
            (
                "B-Roll_Beach",
                "B-Roll_City",
                "B-Roll_Beach",
                "B-Roll_City",
                "B-Roll_Beach_Flash",
            ),
            lambda root: sum(
                1
                for element in root.iter()
                if (element.get("name") or "").startswith("B-Roll_")
            )
            == 5,
        ),
        (
            FillGapsOperation(
                kind="fill_gaps",
                fill_ref="r3",
                fill_name="Gap Fill",
            ),
            1,
            ("Gap Fill",),
            lambda root: (
                root.find(".//gap") is None
                and _first_named(root, "Gap Fill").get("ref") == "r3"
            ),
        ),
        (
            FixFlashFramesOperation(
                kind="fix_flash_frames",
                min_frames=3,
                frame_duration="1001/30000s",
            ),
            1,
            ("Broll_Beach_Flash",),
            lambda root: (
                _first_named(root, "Broll_Beach_Flash").get("duration")
                == "1001/10000s"
            ),
        ),
        (
            BatchApplyTransitionOperation(
                kind="batch_apply_transition",
                duration="1001/30000s",
                name="Cross Dissolve",
                ref="r7",
            ),
            3,
            ("Interview_A", "Broll_City", "Broll_Beach_Flash"),
            lambda root: _transition_attributes(root, "1001/30000s")
            == [
                {
                    "offset": "1001/200s",
                    "duration": "1001/30000s",
                    "name": "Cross Dissolve",
                    "ref": "r7",
                },
                {
                    "offset": "121121/10000s",
                    "duration": "1001/30000s",
                    "name": "Cross Dissolve",
                    "ref": "r7",
                },
                {
                    "offset": "73073/6000s",
                    "duration": "1001/30000s",
                    "name": "Cross Dissolve",
                    "ref": "r7",
                },
            ],
        ),
    ],
    ids=[
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
    ],
)
def test_each_operation_executes_with_truthful_receipt(
    sample_fcpxml_path,
    operation,
    expected_count,
    expected_entities,
    assert_effect,
):
    execution = execute_plan(sample_fcpxml_path, _plan(operation))

    root = _candidate_root(execution)
    assert len(execution.receipts) == 1
    receipt = execution.receipts[0]
    assert receipt.operation_id == "op-001"
    assert receipt.kind == operation.kind
    assert receipt.disposition is OperationDisposition.SUCCEEDED
    assert receipt.affected_count == expected_count
    assert receipt.affected_entities == expected_entities
    assert receipt.error_code is None
    assert receipt.error_summary is None
    assert assert_effect(root)


def test_registry_contains_all_and_only_model_operation_kinds():
    assert set(OPERATION_EXECUTORS) == EXPECTED_KINDS


def test_registry_is_immutable():
    assert not hasattr(operations_module, "_OPERATION_EXECUTORS")
    assert set(OPERATION_EXECUTORS) == EXPECTED_KINDS
    with pytest.raises(TypeError):
        OPERATION_EXECUTORS["unsupported"] = OPERATION_EXECUTORS["add_marker"]


def test_normalize_plan_assigns_ids_and_preserves_caller_plan(sample_fcpxml_path):
    plan = WorkflowPlanV1(
        operations=[
            AddMarkerOperation(
                kind="add_marker",
                clip_name="Interview_A",
                start="0s",
                value="Start",
            ),
            AssignRoleOperation(
                kind="assign_role",
                clip_name="Interview_A",
                role="Voice",
            ),
        ]
    )
    before = plan.model_dump(mode="json")
    inventory = build_source_inventory(FCPXMLModifier(sample_fcpxml_path))

    normalized = normalize_plan(plan, inventory)

    assert normalized.schema_version == "1"
    assert [item.operation_id for item in normalized.operations] == [
        "op-001",
        "op-002",
    ]
    assert [item.operation for item in normalized.operations] == list(plan.operations)
    assert plan.model_dump(mode="json") == before
    with pytest.raises((AttributeError, TypeError)):
        normalized.operations += ()


@pytest.mark.parametrize(
    ("operation", "code"),
    [
        (
            AddMarkerOperation(
                kind="add_marker",
                clip_name="missing",
                start="0s",
                value="x",
            ),
            ErrorCode.TARGET_NOT_FOUND,
        ),
        (
            FillGapsOperation(
                kind="fill_gaps",
                fill_ref="missing",
                fill_name="Fill",
            ),
            ErrorCode.TARGET_NOT_FOUND,
        ),
        (
            AddTransitionOperation(
                kind="add_transition",
                after_clip_name="Broll_City",
                duration="1s",
                name="Bad Ref",
                ref="r3",
            ),
            ErrorCode.TARGET_NOT_FOUND,
        ),
        (
            AddMarkerOperation(
                kind="add_marker",
                clip_name="Interview_A",
                start="1/0s",
                value="x",
            ),
            ErrorCode.INVALID_ARGUMENTS,
        ),
        (
            SplitClipOperation(
                kind="split_clip",
                clip_name="Interview_A",
                split_at="0s",
            ),
            ErrorCode.INVALID_ARGUMENTS,
        ),
        (
            ChangeSpeedOperation(
                kind="change_speed",
                clip_name="Broll_Beach_Flash",
                speed_factor=1e100,
            ),
            ErrorCode.INVALID_ARGUMENTS,
        ),
    ],
)
def test_preflight_failures_are_coded_without_candidate(
    sample_fcpxml_path, operation, code
):
    execution = execute_plan(sample_fcpxml_path, _plan(operation))

    assert execution.disposition is CandidateDisposition.FAILED
    assert execution.candidate_bytes is None
    assert execution.error_code is code
    assert len(execution.receipts) == 1
    assert execution.receipts[0].operation_id == "op-001"
    assert execution.receipts[0].disposition is OperationDisposition.FAILED
    assert execution.receipts[0].error_code is code
    assert len(execution.receipts[0].error_summary) <= 4096


def test_underlying_success_without_requested_tree_effect_is_operation_failed(
    sample_fcpxml_path, monkeypatch
):
    monkeypatch.setattr(FCPXMLModifier, "add_marker", lambda *args, **kwargs: True)
    execution = execute_plan(
        sample_fcpxml_path,
        _plan(
            AddMarkerOperation(
                kind="add_marker",
                clip_name="Interview_A",
                start="0s",
                value="missing effect",
            )
        ),
    )

    assert execution.error_code is ErrorCode.OPERATION_FAILED
    assert execution.receipts[0].affected_count == 0
    assert execution.candidate_bytes is None


@pytest.mark.parametrize(
    "operation",
    [
        FillGapsOperation(
            kind="fill_gaps",
            fill_ref="r5",
            fill_name="Audio Is Not A Visual Fill",
        ),
        AddTransitionOperation(
            kind="add_transition",
            after_clip_name="Broll_City",
            duration="1001/30000s",
            name="Title Is Not A Transition",
            ref="r6",
        ),
        BatchApplyTransitionOperation(
            kind="batch_apply_transition",
            duration="1001/30000s",
            name="Title Is Not A Transition",
            ref="r6",
        ),
    ],
)
def test_incompatible_resource_references_are_target_not_found(
    sample_fcpxml_path, operation
):
    execution = execute_plan(sample_fcpxml_path, _plan(operation))

    assert execution.disposition is CandidateDisposition.FAILED
    assert execution.error_code is ErrorCode.TARGET_NOT_FOUND
    assert execution.candidate_bytes is None


def test_ordered_split_result_can_be_targeted_by_later_operation(
    sample_fcpxml_path,
):
    execution = execute_plan(
        sample_fcpxml_path,
        WorkflowPlanV1(
            operations=[
                SplitClipOperation(
                    kind="split_clip",
                    clip_name="Interview_A",
                    split_at="60060/30000s",
                ),
                AssignRoleOperation(
                    kind="assign_role",
                    clip_name="Interview_A_split",
                    role="Split",
                ),
            ]
        ),
    )

    root = _candidate_root(execution)
    assert [receipt.operation_id for receipt in execution.receipts] == [
        "op-001",
        "op-002",
    ]
    assert _first_named(root, "Interview_A_split").get("role") == "Split"


def test_ordered_rename_result_can_be_targeted_by_later_operation(
    sample_fcpxml_path,
):
    execution = execute_plan(
        sample_fcpxml_path,
        WorkflowPlanV1(
            operations=[
                BatchRenameClipsOperation(
                    kind="batch_rename_clips",
                    pattern="Broll_",
                    replacement="B-Roll_",
                ),
                AssignRoleOperation(
                    kind="assign_role",
                    clip_name="B-Roll_City",
                    role="Renamed",
                ),
            ]
        ),
    )

    root = _candidate_root(execution)
    assert _first_named(root, "B-Roll_City").get("role") == "Renamed"


def test_ordered_delete_makes_later_target_not_found(sample_fcpxml_path):
    execution = execute_plan(
        sample_fcpxml_path,
        WorkflowPlanV1(
            operations=[
                DeleteClipsOperation(
                    kind="delete_clips",
                    clip_names=["Broll_City"],
                ),
                AssignRoleOperation(
                    kind="assign_role",
                    clip_name="Broll_City",
                    role="Impossible",
                ),
            ]
        ),
    )

    assert execution.disposition is CandidateDisposition.FAILED
    assert execution.error_code is ErrorCode.TARGET_NOT_FOUND
    assert execution.candidate_bytes is None
    assert execution.receipts[-1].operation_id == "op-002"
    assert execution.receipts[-1].error_code is ErrorCode.TARGET_NOT_FOUND


def test_delete_rejects_unrelated_collateral_removal(
    sample_fcpxml_path, monkeypatch
):
    real_delete = FCPXMLModifier.delete_clips

    def delete_with_collateral(modifier, clip_names):
        result = real_delete(modifier, clip_names)
        real_delete(modifier, ["Broll_City"])
        return result

    monkeypatch.setattr(FCPXMLModifier, "delete_clips", delete_with_collateral)

    execution = execute_plan(
        sample_fcpxml_path,
        _plan(
            DeleteClipsOperation(
                kind="delete_clips",
                clip_names=["Broll_Beach_Flash"],
            )
        ),
    )

    assert execution.disposition is CandidateDisposition.FAILED
    assert execution.error_code is ErrorCode.OPERATION_FAILED
    assert execution.candidate_bytes is None


def test_reorder_rejects_unrelated_collateral_removal(
    sample_fcpxml_path, monkeypatch
):
    real_reorder = FCPXMLModifier.reorder_clips
    real_delete = FCPXMLModifier.delete_clips

    def reorder_with_collateral(modifier, clip_names):
        result = real_reorder(modifier, clip_names)
        real_delete(modifier, ["Broll_Beach"])
        return result

    monkeypatch.setattr(FCPXMLModifier, "reorder_clips", reorder_with_collateral)

    execution = execute_plan(
        sample_fcpxml_path,
        _plan(
            ReorderClipsOperation(
                kind="reorder_clips",
                clip_names=["Broll_City", "Interview_A"],
            )
        ),
    )

    assert execution.disposition is CandidateDisposition.FAILED
    assert execution.error_code is ErrorCode.OPERATION_FAILED
    assert execution.candidate_bytes is None


def test_delete_rejects_collateral_nested_deletion(
    sample_fcpxml_path, monkeypatch
):
    real_delete = FCPXMLModifier.delete_clips

    def delete_with_nested_collateral(modifier, clip_names):
        result = real_delete(modifier, clip_names)
        beach = modifier._find_clip_by_name("Broll_Beach")
        title = beach.find("title")
        beach.remove(title)
        return result

    monkeypatch.setattr(
        FCPXMLModifier,
        "delete_clips",
        delete_with_nested_collateral,
    )

    execution = execute_plan(
        sample_fcpxml_path,
        _plan(
            DeleteClipsOperation(
                kind="delete_clips",
                clip_names=["Broll_Beach_Flash"],
            )
        ),
    )

    assert execution.disposition is CandidateDisposition.FAILED
    assert execution.error_code is ErrorCode.OPERATION_FAILED
    assert execution.candidate_bytes is None


def test_reorder_rejects_collateral_nested_deletion(
    sample_fcpxml_path, monkeypatch
):
    real_reorder = FCPXMLModifier.reorder_clips

    def reorder_with_nested_collateral(modifier, clip_names):
        result = real_reorder(modifier, clip_names)
        beach = modifier._find_clip_by_name("Broll_Beach")
        title = beach.find("title")
        beach.remove(title)
        return result

    monkeypatch.setattr(
        FCPXMLModifier,
        "reorder_clips",
        reorder_with_nested_collateral,
    )

    execution = execute_plan(
        sample_fcpxml_path,
        _plan(
            ReorderClipsOperation(
                kind="reorder_clips",
                clip_names=["Broll_City", "Interview_A"],
            )
        ),
    )

    assert execution.disposition is CandidateDisposition.FAILED
    assert execution.error_code is ErrorCode.OPERATION_FAILED
    assert execution.candidate_bytes is None


def test_reorder_rejects_missing_expected_offset_after_modifier(
    sample_fcpxml_path, monkeypatch
):
    real_reorder = FCPXMLModifier.reorder_clips

    def reorder_with_missing_offset(modifier, clip_names):
        result = real_reorder(modifier, clip_names)
        del modifier.root.find(".//spine")[0].attrib["offset"]
        return result

    monkeypatch.setattr(
        FCPXMLModifier,
        "reorder_clips",
        reorder_with_missing_offset,
    )

    execution = execute_plan(
        sample_fcpxml_path,
        _plan(
            ReorderClipsOperation(
                kind="reorder_clips",
                clip_names=["Broll_City"],
            )
        ),
    )

    assert execution.disposition is CandidateDisposition.FAILED
    assert execution.error_code is ErrorCode.OPERATION_FAILED
    assert execution.candidate_bytes is None


def test_single_transition_rejects_correct_attributes_at_wrong_position(
    sample_fcpxml_path, monkeypatch
):
    def append_transition(modifier, after_clip_name, duration, name, ref):
        transition = ET.Element(
            "transition",
            {
                "offset": "121121/10000s",
                "duration": duration,
                "name": name,
                "ref": ref,
            },
        )
        modifier.root.find(".//spine").append(transition)
        return True

    monkeypatch.setattr(FCPXMLModifier, "add_transition", append_transition)

    execution = execute_plan(
        sample_fcpxml_path,
        _plan(
            AddTransitionOperation(
                kind="add_transition",
                after_clip_name="Broll_City",
                duration="1001/30000s",
                name="Cross Dissolve",
                ref="r7",
            )
        ),
    )

    assert execution.disposition is CandidateDisposition.FAILED
    assert execution.error_code is ErrorCode.OPERATION_FAILED


def test_batch_transition_rejects_correct_attributes_at_wrong_positions(
    sample_fcpxml_path, monkeypatch
):
    real_batch = FCPXMLModifier.batch_apply_transition

    def append_added_transitions(modifier, duration, name, ref):
        before = {id(element) for element in modifier.root.iter("transition")}
        result = real_batch(modifier, duration, name, ref)
        spine = modifier.root.find(".//spine")
        added = [
            element
            for element in spine.findall("transition")
            if id(element) not in before
        ]
        for element in added:
            spine.remove(element)
            spine.append(element)
        return result

    monkeypatch.setattr(
        FCPXMLModifier,
        "batch_apply_transition",
        append_added_transitions,
    )

    execution = execute_plan(
        sample_fcpxml_path,
        _plan(
            BatchApplyTransitionOperation(
                kind="batch_apply_transition",
                duration="1001/30000s",
                name="Cross Dissolve",
                ref="r7",
            )
        ),
    )

    assert execution.disposition is CandidateDisposition.FAILED
    assert execution.error_code is ErrorCode.OPERATION_FAILED


def test_batch_role_all_noop_reports_zero_affected(sample_fcpxml_path):
    execution = execute_plan(
        sample_fcpxml_path,
        _plan(
            BatchAssignRolesOperation(
                kind="batch_assign_roles",
                rules=[
                    BatchRoleAssignmentRule(match="Interview", role="Dialogue"),
                ],
            )
        ),
    )

    assert execution.disposition is CandidateDisposition.SUCCEEDED
    assert execution.receipts[0].affected_count == 0
    assert execution.receipts[0].affected_entities == ()
    assert "already" in execution.receipts[0].warnings[0]


def test_batch_role_rejects_unmatched_collateral_role_mutation(
    sample_fcpxml_path, monkeypatch
):
    real_batch = FCPXMLModifier.batch_assign_roles

    def assign_with_collateral(modifier, rules):
        result = real_batch(modifier, rules)
        modifier._find_clip_by_name("Beach Scene").set("role", "Collateral")
        return result

    monkeypatch.setattr(
        FCPXMLModifier,
        "batch_assign_roles",
        assign_with_collateral,
    )

    execution = execute_plan(
        sample_fcpxml_path,
        _plan(
            BatchAssignRolesOperation(
                kind="batch_assign_roles",
                rules=[
                    BatchRoleAssignmentRule(match="Interview", role="Voice"),
                ],
            )
        ),
    )

    assert execution.disposition is CandidateDisposition.FAILED
    assert execution.error_code is ErrorCode.OPERATION_FAILED
    assert execution.candidate_bytes is None


def test_execution_stops_after_first_failure_and_does_not_serialize(
    sample_fcpxml_path, monkeypatch
):
    monkeypatch.setattr(FCPXMLModifier, "add_marker", lambda *args, **kwargs: False)

    def must_not_run(*args, **kwargs):
        raise AssertionError("later operation executed")

    monkeypatch.setattr(FCPXMLModifier, "assign_role", must_not_run)
    monkeypatch.setattr(FCPXMLModifier, "serialize", must_not_run)
    plan = WorkflowPlanV1(
        operations=[
            AddMarkerOperation(
                kind="add_marker",
                clip_name="Interview_A",
                start="0s",
                value="Start",
            ),
            AssignRoleOperation(
                kind="assign_role",
                clip_name="Interview_A",
                role="Voice",
            ),
        ]
    )

    execution = execute_plan(sample_fcpxml_path, plan)

    assert execution.disposition is CandidateDisposition.FAILED
    assert [receipt.operation_id for receipt in execution.receipts] == ["op-001"]
    assert execution.receipts[0].error_code is ErrorCode.OPERATION_FAILED
    assert execution.candidate_bytes is None


def test_execution_retains_completed_receipts_before_later_failure(
    sample_fcpxml_path, monkeypatch
):
    monkeypatch.setattr(FCPXMLModifier, "assign_role", lambda *args, **kwargs: False)
    plan = WorkflowPlanV1(
        operations=[
            AddMarkerOperation(
                kind="add_marker",
                clip_name="Interview_A",
                start="0s",
                value="Start",
            ),
            AssignRoleOperation(
                kind="assign_role",
                clip_name="Interview_A",
                role="Voice",
            ),
        ]
    )

    execution = execute_plan(sample_fcpxml_path, plan)

    assert [receipt.operation_id for receipt in execution.receipts] == [
        "op-001",
        "op-002",
    ]
    assert [receipt.disposition for receipt in execution.receipts] == [
        OperationDisposition.SUCCEEDED,
        OperationDisposition.FAILED,
    ]
    assert execution.error_code is ErrorCode.OPERATION_FAILED
    assert execution.candidate_bytes is None


def test_success_serializes_exactly_once(sample_fcpxml_path, monkeypatch):
    calls = 0
    real_serialize = FCPXMLModifier.serialize

    def counted_serialize(modifier):
        nonlocal calls
        calls += 1
        return real_serialize(modifier)

    monkeypatch.setattr(FCPXMLModifier, "serialize", counted_serialize)

    execution = execute_plan(
        sample_fcpxml_path,
        _plan(
            AssignRoleOperation(
                kind="assign_role",
                clip_name="Interview_A",
                role="Voice",
            )
        ),
    )

    assert execution.disposition is CandidateDisposition.SUCCEEDED
    assert calls == 1


def test_execute_plan_never_writes_a_destination(sample_fcpxml_path, tmp_path):
    source = tmp_path / "source.fcpxml"
    source.write_bytes(sample_fcpxml_path.read_bytes())
    before = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }

    execution = execute_plan(
        source,
        _plan(
            AssignRoleOperation(
                kind="assign_role",
                clip_name="Interview_A",
                role="Voice",
            )
        ),
    )

    assert execution.disposition is CandidateDisposition.SUCCEEDED
    after = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_duplicate_names_use_deterministic_first_match_without_unique_claim(
    sample_fcpxml_path, tmp_path
):
    source = tmp_path / "duplicates.fcpxml"
    xml = sample_fcpxml_path.read_text().replace(
        'ref="r4" name="Broll_City"',
        'ref="r4" name="Interview_A"',
        1,
    )
    source.write_text(xml)

    execution = execute_plan(
        source,
        _plan(
            AssignRoleOperation(
                kind="assign_role",
                clip_name="Interview_A",
                role="Chosen",
            )
        ),
    )

    root = _candidate_root(execution)
    matches = [
        element
        for element in root.iter()
        if element.tag == "asset-clip" and element.get("name") == "Interview_A"
    ]
    assert [element.get("role") for element in matches] == ["Chosen", "Video"]
    assert execution.receipts[0].affected_entities == ("Interview_A",)


def test_duplicate_delete_removes_deterministic_first_match(
    sample_fcpxml_path, tmp_path
):
    source = tmp_path / "duplicate-delete.fcpxml"
    xml = sample_fcpxml_path.read_text().replace(
        'ref="r4" name="Broll_City"',
        'ref="r4" name="Broll_Beach_Flash"',
        1,
    )
    source.write_text(xml)

    execution = execute_plan(
        source,
        _plan(
            DeleteClipsOperation(
                kind="delete_clips",
                clip_names=["Broll_Beach_Flash"],
            )
        ),
    )

    root = _candidate_root(execution)
    matches = [
        element
        for element in root.iter()
        if element.tag == "asset-clip"
        and element.get("name") == "Broll_Beach_Flash"
    ]
    assert len(matches) == 1
    assert matches[0].get("ref") == "r3"
    assert execution.receipts[0].affected_entities == ("Broll_Beach_Flash",)


def test_transition_preflight_uses_clip_adjacency_not_raw_child_index(
    sample_fcpxml_path,
):
    execution = execute_plan(
        sample_fcpxml_path,
        _plan(
            AddTransitionOperation(
                kind="add_transition",
                after_clip_name="Broll_Beach_Flash",
                duration="1001/30000s",
                name="Cross Dissolve",
                ref="r7",
            )
        ),
    )

    assert execution.disposition is CandidateDisposition.SUCCEEDED
    assert execution.receipts[0].affected_count == 1


def test_transition_preflight_rejects_gap_separated_clips(sample_fcpxml_path):
    execution = execute_plan(
        sample_fcpxml_path,
        _plan(
            AddTransitionOperation(
                kind="add_transition",
                after_clip_name="Broll_Beach",
                duration="1001/30000s",
                name="Cross Dissolve",
                ref="r7",
            )
        ),
    )

    assert execution.disposition is CandidateDisposition.FAILED
    assert execution.error_code is ErrorCode.TARGET_NOT_FOUND
    assert execution.candidate_bytes is None


def test_transition_rejects_gap_as_after_clip_target(sample_fcpxml_path):
    execution = execute_plan(
        sample_fcpxml_path,
        _plan(
            AddTransitionOperation(
                kind="add_transition",
                after_clip_name="Gap",
                duration="1001/30000s",
                name="Cross Dissolve",
                ref="r7",
            )
        ),
    )

    assert execution.disposition is CandidateDisposition.FAILED
    assert execution.error_code is ErrorCode.TARGET_NOT_FOUND
    assert execution.candidate_bytes is None


def test_batch_transition_consistently_skips_gap_adjacencies(sample_fcpxml_path):
    execution = execute_plan(
        sample_fcpxml_path,
        _plan(
            BatchApplyTransitionOperation(
                kind="batch_apply_transition",
                duration="1001/30000s",
                name="Cross Dissolve",
                ref="r7",
            )
        ),
    )

    root = _candidate_root(execution)
    gap = _first_named(root, "Gap")
    parent = next(parent for parent in root.iter() if gap in list(parent))
    gap_index = list(parent).index(gap)
    assert list(parent)[gap_index - 1].tag != "transition"
    assert list(parent)[gap_index + 1].tag != "transition"


def test_reorder_receipt_reports_offset_only_changes(sample_fcpxml_path):
    execution = execute_plan(
        sample_fcpxml_path,
        _plan(
            ReorderClipsOperation(
                kind="reorder_clips",
                clip_names=["Interview_A"],
            )
        ),
    )

    root = _candidate_root(execution)
    receipt = execution.receipts[0]
    assert receipt.affected_count == 6
    assert receipt.affected_entities == (
        "Cross Dissolve",
        "Broll_Beach",
        "Gap",
        "Broll_City",
        "Broll_Beach_Flash",
        "Interview_A_Outro",
    )
    offset_changes = [
        change for change in receipt.changes if change.field.startswith("offset:")
    ]
    assert len(offset_changes) == 6
    assert receipt.warnings == ()
    assert _first_named(root, "Broll_City").get("offset") == "91091/10000s"


def test_reorder_receipt_reports_missing_offset_normalization(
    sample_fcpxml_path, tmp_path
):
    source = tmp_path / "missing-offset.fcpxml"
    source.write_text(
        sample_fcpxml_path.read_text().replace(
            'name="Interview_A" offset="0s"',
            'name="Interview_A"',
        )
    )

    execution = execute_plan(
        source,
        _plan(
            ReorderClipsOperation(
                kind="reorder_clips",
                clip_names=["Interview_A"],
            )
        ),
    )

    receipt = execution.receipts[0]
    interview_change = next(
        change
        for change in receipt.changes
        if change.field == "offset:Interview_A"
    )
    assert interview_change.before is None
    assert interview_change.after == "0s"
    assert receipt.affected_count == 7
    assert receipt.warnings == ()


def test_reorder_receipt_disambiguates_duplicate_affected_names(
    sample_fcpxml_path, tmp_path
):
    source = tmp_path / "duplicate-names.fcpxml"
    source.write_text(
        sample_fcpxml_path.read_text()
        .replace('name="Broll_Beach" offset=', 'name="Duplicate" offset=')
        .replace('name="Broll_City" offset=', 'name="Duplicate" offset=')
    )

    execution = execute_plan(
        source,
        _plan(
            ReorderClipsOperation(
                kind="reorder_clips",
                clip_names=["Interview_A"],
            )
        ),
    )

    receipt = execution.receipts[0]
    duplicate_entities = [
        entity
        for entity in receipt.affected_entities
        if entity.startswith("Duplicate")
    ]
    duplicate_changes = [
        change
        for change in receipt.changes
        if change.field.startswith("offset:Duplicate")
    ]
    assert receipt.affected_count == 6
    assert duplicate_entities == ["Duplicate [3]", "Duplicate [5]"]
    assert [change.field for change in duplicate_changes] == [
        "offset:Duplicate [3]",
        "offset:Duplicate [5]",
    ]


def test_reorder_shadow_projects_normalized_offsets_for_later_split(
    sample_fcpxml_path, tmp_path
):
    source = tmp_path / "stale-offset.fcpxml"
    source.write_text(
        sample_fcpxml_path.read_text().replace(
            'name="Broll_City" offset="273273/30000s"',
            'name="Broll_City" offset="9223372036854775807s"',
        )
    )
    execution = execute_plan(
        source,
        WorkflowPlanV1(
            operations=[
                ReorderClipsOperation(
                    kind="reorder_clips",
                    clip_names=["Interview_A"],
                ),
                SplitClipOperation(
                    kind="split_clip",
                    clip_name="Broll_City",
                    split_at="1s",
                ),
            ]
        ),
    )

    root = _candidate_root(execution)
    assert [receipt.operation_id for receipt in execution.receipts] == [
        "op-001",
        "op-002",
    ]
    assert _first_named(root, "Broll_City_split").get("offset") == "101091/10000s"


def test_reorder_shadow_retains_and_normalizes_interleaved_transition(
    sample_fcpxml_path, monkeypatch
):
    observed_inventories = []
    real_preflight = operations_module._preflight_operation

    def capture_second_preflight(operation, inventory):
        if operation.kind == "assign_role":
            observed_inventories.append(inventory)
        return real_preflight(operation, inventory)

    monkeypatch.setattr(
        operations_module,
        "_preflight_operation",
        capture_second_preflight,
    )
    modifier = FCPXMLModifier(sample_fcpxml_path)
    initial_inventory = build_source_inventory(modifier)
    plan = WorkflowPlanV1(
        operations=[
            ReorderClipsOperation(
                kind="reorder_clips",
                clip_names=["Interview_A"],
            ),
            AssignRoleOperation(
                kind="assign_role",
                clip_name="Interview_A",
                role="Voice",
            ),
        ]
    )

    normalize_plan(plan, initial_inventory)

    projected = observed_inventories[0]
    transition = next(
        item for item in projected.spines[0].items if item.kind == "transition"
    )
    assert transition.offset == "1001/200s"
    assert all(clip.kind != "transition" for clip in projected.clips)


def test_fix_flash_noop_shadow_preserves_interleaved_transition_offset(
    sample_fcpxml_path, monkeypatch
):
    observed_inventories = []
    real_preflight = operations_module._preflight_operation

    def capture_second_preflight(operation, inventory):
        if operation.kind == "assign_role":
            observed_inventories.append(inventory)
        return real_preflight(operation, inventory)

    monkeypatch.setattr(
        operations_module,
        "_preflight_operation",
        capture_second_preflight,
    )
    initial_inventory = build_source_inventory(FCPXMLModifier(sample_fcpxml_path))
    plan = WorkflowPlanV1(
        operations=[
            FixFlashFramesOperation(
                kind="fix_flash_frames",
                min_frames=1,
                frame_duration="1001/30000s",
            ),
            AssignRoleOperation(
                kind="assign_role",
                clip_name="Interview_A",
                role="Voice",
            ),
        ]
    )

    normalize_plan(plan, initial_inventory)

    projected = observed_inventories[0]
    transition = next(
        item for item in projected.spines[0].items if item.kind == "transition"
    )
    assert transition.offset == "150150/30000s"


@pytest.mark.parametrize(
    "operation",
    [
        ReorderClipsOperation(
            kind="reorder_clips",
            clip_names=["Cross Dissolve"],
        ),
        AddTransitionOperation(
            kind="add_transition",
            after_clip_name="Cross Dissolve",
            duration="1001/30000s",
            name="Cross Dissolve",
            ref="r7",
        ),
    ],
)
def test_existing_transition_cannot_be_named_operation_target(
    sample_fcpxml_path, operation
):
    execution = execute_plan(sample_fcpxml_path, _plan(operation))

    assert execution.disposition is CandidateDisposition.FAILED
    assert execution.error_code is ErrorCode.TARGET_NOT_FOUND
    assert execution.candidate_bytes is None


def test_reorder_does_not_select_same_named_transition(
    sample_fcpxml_path, tmp_path
):
    source = tmp_path / "same-named-transition.fcpxml"
    source.write_text(
        sample_fcpxml_path.read_text().replace(
            'name="Cross Dissolve" ref="r7"',
            'name="Broll_City" ref="r7"',
        )
    )

    execution = execute_plan(
        source,
        _plan(
            ReorderClipsOperation(
                kind="reorder_clips",
                clip_names=["Broll_City"],
            )
        ),
    )

    root = _candidate_root(execution)
    spine = root.find(".//spine")
    assert len(spine.findall("transition")) == 1
    assert len(
        [
            child
            for child in spine
            if child.tag != "transition" and child.get("name") == "Broll_City"
        ]
    ) == 1


def test_transition_rejects_out_of_domain_result_time(
    sample_fcpxml_path, tmp_path
):
    source = tmp_path / "overflow.fcpxml"
    source.write_text(
        sample_fcpxml_path.read_text().replace(
            'name="Broll_City" offset="273273/30000s"',
            'name="Broll_City" offset="9223372036854775807s"',
        )
    )

    execution = execute_plan(
        source,
        _plan(
            AddTransitionOperation(
                kind="add_transition",
                after_clip_name="Broll_City",
                duration="1s",
                name="Cross Dissolve",
                ref="r7",
            )
        ),
    )

    assert execution.disposition is CandidateDisposition.FAILED
    assert execution.error_code is ErrorCode.INVALID_ARGUMENTS
    assert execution.candidate_bytes is None


def test_fill_gaps_rejects_malformed_source_gap_time(
    sample_fcpxml_path, tmp_path
):
    source = tmp_path / "bad-gap.fcpxml"
    source.write_text(
        sample_fcpxml_path.read_text().replace(
            'duration="3003/30000s" name="Gap"',
            'duration="1/0s" name="Gap"',
        )
    )

    execution = execute_plan(
        source,
        _plan(
            FillGapsOperation(
                kind="fill_gaps",
                fill_ref="r3",
                fill_name="Fill",
            )
        ),
    )

    assert execution.disposition is CandidateDisposition.FAILED
    assert execution.error_code is ErrorCode.INVALID_ARGUMENTS
    assert execution.candidate_bytes is None


def test_fix_flash_frames_rejects_out_of_domain_recalculated_offsets(
    sample_fcpxml_path,
):
    execution = execute_plan(
        sample_fcpxml_path,
        _plan(
            FixFlashFramesOperation(
                kind="fix_flash_frames",
                min_frames=1,
                frame_duration="9223372036854775807s",
            )
        ),
    )

    assert execution.disposition is CandidateDisposition.FAILED
    assert execution.error_code is ErrorCode.INVALID_ARGUMENTS
    assert execution.candidate_bytes is None


def test_fill_gaps_ignores_nested_non_spine_gaps(
    sample_fcpxml_path, tmp_path
):
    source = tmp_path / "nested-gap.fcpxml"
    nested_gap = (
        '<asset-clip ref="r3" name="Nested Parent" offset="270270/30000s" '
        'start="0s" duration="3003/30000s">'
        '<gap offset="0s" duration="3003/30000s" name="Nested Gap"/>'
        "</asset-clip>"
    )
    source.write_text(
        sample_fcpxml_path.read_text().replace(
            '<gap offset="270270/30000s" duration="3003/30000s" name="Gap"/>',
            nested_gap,
        )
    )

    execution = execute_plan(
        source,
        _plan(
            FillGapsOperation(
                kind="fill_gaps",
                fill_ref="r3",
                fill_name="Fill",
            )
        ),
    )

    root = _candidate_root(execution)
    assert execution.receipts[0].affected_count == 0
    assert "no gaps" in execution.receipts[0].warnings[0]
    assert root.find(".//gap").get("name") == "Nested Gap"


def test_nested_gap_is_not_projected_as_a_later_fill_target(
    sample_fcpxml_path, tmp_path
):
    source = tmp_path / "nested-gap-plan.fcpxml"
    nested_gap = (
        '<asset-clip ref="r3" name="Nested Parent" offset="270270/30000s" '
        'start="0s" duration="3003/30000s">'
        '<gap offset="0s" duration="3003/30000s" name="Nested Gap"/>'
        "</asset-clip>"
    )
    source.write_text(
        sample_fcpxml_path.read_text().replace(
            '<gap offset="270270/30000s" duration="3003/30000s" name="Gap"/>',
            nested_gap,
        )
    )

    execution = execute_plan(
        source,
        WorkflowPlanV1(
            operations=[
                FillGapsOperation(
                    kind="fill_gaps",
                    fill_ref="r3",
                    fill_name="Fill",
                ),
                AssignRoleOperation(
                    kind="assign_role",
                    clip_name="Fill",
                    role="Impossible",
                ),
            ]
        ),
    )

    assert execution.disposition is CandidateDisposition.FAILED
    assert execution.error_code is ErrorCode.TARGET_NOT_FOUND
    assert execution.receipts[-1].operation_id == "op-002"


def test_receipt_before_values_are_bounded(sample_fcpxml_path, tmp_path):
    source = tmp_path / "long-role.fcpxml"
    source.write_text(
        sample_fcpxml_path.read_text().replace(
            'role="Dialogue"',
            f'role="{"x" * 5000}"',
            1,
        )
    )

    execution = execute_plan(
        source,
        _plan(
            AssignRoleOperation(
                kind="assign_role",
                clip_name="Interview_A",
                role="Voice",
            )
        ),
    )

    assert execution.disposition is CandidateDisposition.SUCCEEDED
    assert len(execution.receipts[0].changes[0].before) == 4096


def test_receipt_entities_are_bounded_for_untrusted_source_names(
    sample_fcpxml_path, tmp_path
):
    source = tmp_path / "long-name.fcpxml"
    source.write_text(
        sample_fcpxml_path.read_text().replace(
            'name="Interview_A" offset="0s"',
            f'name="Interview_{"x" * 5000}" offset="0s"',
            1,
        )
    )

    execution = execute_plan(
        source,
        _plan(
            BatchAssignRolesOperation(
                kind="batch_assign_roles",
                rules=[BatchRoleAssignmentRule(match="Interview_", role="Voice")],
            )
        ),
    )

    assert execution.disposition is CandidateDisposition.SUCCEEDED
    assert len(execution.receipts[0].affected_entities[0]) == 255
    assert "bounded" in execution.receipts[0].warnings[0]


def test_malformed_source_fails_at_safe_parse_boundary(tmp_path):
    source = tmp_path / "malformed.fcpxml"
    source.write_text(
        '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
        "<fcpxml>&x;</fcpxml>"
    )
    plan = _plan(
        AddMarkerOperation(
            kind="add_marker",
            clip_name="Interview_A",
            start="0s",
            value="Start",
        )
    )

    with pytest.raises(DefusedXmlException):
        execute_plan(source, plan)

    assert list(tmp_path.iterdir()) == [source]


def test_operations_module_does_not_import_server_or_mcp_sdk():
    command = [
        sys.executable,
        "-c",
        (
            "import json,sys; import fcp_mcp.workflow.operations; "
            "print(json.dumps(sorted(name for name in sys.modules "
            "if name == 'fcp_mcp.server' or name == 'mcp' or name.startswith('mcp.'))))"
        ),
    ]
    result = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(Path.cwd() / "src")},
    )

    assert json.loads(result.stdout) == []
