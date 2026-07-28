from __future__ import annotations

import hashlib
import importlib
import json
import os
import shutil
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Callable
from pathlib import Path

import pytest

from fcp_mcp import server
from fcp_mcp.config import RuntimeConfig
from fcp_mcp.contracts import ErrorCode, FCPMCPError
from fcp_mcp.fcpxml.writer import FCPXMLModifier
from fcp_mcp.security.paths import PathPolicy

EDIT_CASES = (
    ("fcpxml_add_marker", "MarkerMutationResult"),
    ("fcpxml_batch_add_markers", "MarkerBatchMutationResult"),
    ("fcpxml_add_keyword", "KeywordMutationResult"),
    ("fcpxml_trim_clip", "ClipMutationResult"),
    ("fcpxml_split_clip", "ClipMutationResult"),
    ("fcpxml_delete_clips", "ClipBatchMutationResult"),
    ("fcpxml_reorder_clips", "ClipBatchMutationResult"),
    ("fcpxml_add_transition", "TransitionMutationResult"),
    ("fcpxml_change_speed", "ClipMutationResult"),
    ("fcpxml_assign_role", "RoleMutationResult"),
    ("fcpxml_add_title", "TimelineElementMutationResult"),
    ("fcpxml_add_audio", "TimelineElementMutationResult"),
)

RESULT_MODEL_NAMES = {
    "ArtifactReference",
    "TransactionReceiptResult",
    "MarkerMutationRecord",
    "MarkerMutationResult",
    "MarkerBatchMutationResult",
    "KeywordMutationRecord",
    "KeywordMutationResult",
    "ClipMutationRecord",
    "ClipFieldMutationRecord",
    "ClipMutationResult",
    "ClipBatchMutationResult",
    "TransitionMutationRecord",
    "TransitionMutationResult",
    "RoleMutationRecord",
    "RoleMutationResult",
    "TimelineElementMutationRecord",
    "TimelineElementMutationResult",
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _find_named(root: ET.Element, tag: str, name: str) -> ET.Element:
    found = next(
        (
            element
            for element in root.iter(tag)
            if element.get("name") == name
        ),
        None,
    )
    assert found is not None
    return found


def _marker_payload(
    clip_name: str,
    marker_type: str,
    start: str,
    value: str,
    note: str,
) -> dict[str, object]:
    return {
        "clip_name": clip_name,
        "marker_type": marker_type,
        "start": start,
        "duration": "1/1s",
        "value": value,
        "note": note,
    }


def _clip_payload(
    *,
    name: str,
    offset: str,
    start: str,
    duration: str,
    role: str,
    ref: str,
) -> dict[str, str]:
    return {
        "name": name,
        "element_type": "asset-clip",
        "offset": offset,
        "start": start,
        "duration": duration,
        "role": role,
        "ref": ref,
    }


def _arguments_for(
    tool_name: str,
    source: Path,
    destination: Path,
    audio: Path,
) -> dict[str, object]:
    common: dict[str, object] = {"path": str(source)}
    cases: dict[str, dict[str, object]] = {
        "fcpxml_add_marker": {
            **common,
            "clip_name": "Interview_A",
            "start": "0s",
            "value": "Review",
            "note": "Check this",
        },
        "fcpxml_batch_add_markers": {
            **common,
            "markers_json": json.dumps(
                [
                    {
                        "clip_name": "Interview_A",
                        "start": "0s",
                        "value": "One",
                    },
                    {
                        "clip_name": "Broll_City",
                        "start": "1001/30000s",
                        "value": "Two",
                        "note": "Chapter note",
                        "type": "chapter",
                    },
                ]
            ),
            "output_path": str(destination),
        },
        "fcpxml_add_keyword": {
            **common,
            "clip_name": "Broll_Beach",
            "value": "golden-hour",
            "start": "1001/30000s",
            "duration": "30030/30000s",
            "output_path": str(destination),
        },
        "fcpxml_trim_clip": {
            **common,
            "clip_name": "Interview_A",
            "new_start": "60060/30000s",
            "new_duration": "120120/30000s",
            "output_path": str(destination),
        },
        "fcpxml_split_clip": {
            **common,
            "clip_name": "Interview_A",
            "split_at": "60060/30000s",
            "output_path": str(destination),
        },
        "fcpxml_delete_clips": {
            **common,
            "clip_names_json": json.dumps(
                ["Broll_Beach_Flash", "Interview_A_Outro"]
            ),
            "output_path": str(destination),
        },
        "fcpxml_reorder_clips": {
            **common,
            "clip_names_json": json.dumps(["Broll_City", "Interview_A"]),
            "output_path": str(destination),
        },
        "fcpxml_add_transition": {
            **common,
            "after_clip_name": "Interview_A",
            "duration": "6006/30000s",
            "name": "Short Dissolve",
            "output_path": str(destination),
        },
        "fcpxml_change_speed": {
            **common,
            "clip_name": "Broll_Beach",
            "speed_factor": 2.0,
            "output_path": str(destination),
        },
        "fcpxml_assign_role": {
            **common,
            "clip_name": "Interview_A",
            "role": "Narration",
            "output_path": str(destination),
        },
        "fcpxml_add_title": {
            **common,
            "text": "Act Two",
            "duration": "60060/30000s",
            "position": "Broll_City",
            "output_path": str(destination),
        },
        "fcpxml_add_audio": {
            **common,
            "audio_src": str(audio),
            "name": "Voiceover",
            "duration": "30030/30000s",
            "position": "start",
            "output_path": str(destination),
        },
    }
    return cases[tool_name]


def _expected_text(tool_name: str, destination: Path) -> str:
    return {
        "fcpxml_add_marker": f"Marker added. Saved to: {destination}",
        "fcpxml_batch_add_markers": (
            f"2/2 markers added. Saved to: {destination}"
        ),
        "fcpxml_add_keyword": (
            f"Keyword 'golden-hour' added. Saved to: {destination}"
        ),
        "fcpxml_trim_clip": f"Clip trimmed. Saved to: {destination}",
        "fcpxml_split_clip": f"Clip split. Saved to: {destination}",
        "fcpxml_delete_clips": (
            f"2/2 clips deleted. Saved to: {destination}"
        ),
        "fcpxml_reorder_clips": f"Clips reordered. Saved to: {destination}",
        "fcpxml_add_transition": (
            f"Transition added. Saved to: {destination}"
        ),
        "fcpxml_change_speed": (
            f"Speed changed to 2.0x. Saved to: {destination}"
        ),
        "fcpxml_assign_role": (
            f"Role 'Narration' assigned. Saved to: {destination}"
        ),
        "fcpxml_add_title": (
            f"Title 'Act Two' added. Saved to: {destination}"
        ),
        "fcpxml_add_audio": (
            f"Audio 'Voiceover' added. Saved to: {destination}"
        ),
    }[tool_name]


def _expected_domain(
    tool_name: str,
    audio: Path,
) -> dict[str, object]:
    return {
        "fcpxml_add_marker": {
            "marker": _marker_payload(
                "Interview_A",
                "standard",
                "0s",
                "Review",
                "Check this",
            ),
        },
        "fcpxml_batch_add_markers": {
            "requested_count": 2,
            "changed_count": 2,
            "markers": [
                _marker_payload(
                    "Interview_A",
                    "standard",
                    "0s",
                    "One",
                    "",
                ),
                _marker_payload(
                    "Broll_City",
                    "chapter",
                    "1001/30000s",
                    "Two",
                    "Chapter note",
                ),
            ],
        },
        "fcpxml_add_keyword": {
            "keyword": {
                "clip_name": "Broll_Beach",
                "value": "golden-hour",
                "start": "1001/30000s",
                "duration": "30030/30000s",
            },
        },
        "fcpxml_trim_clip": {
            "operation": "trim",
            "clip": _clip_payload(
                name="Interview_A",
                offset="0s",
                start="60060/30000s",
                duration="120120/30000s",
                role="Dialogue",
                ref="r2",
            ),
            "created_clip": None,
            "changed_fields": [
                {
                    "field": "start",
                    "before": "30030/30000s",
                    "after": "60060/30000s",
                },
                {
                    "field": "duration",
                    "before": "150150/30000s",
                    "after": "120120/30000s",
                },
            ],
            "speed_factor": None,
        },
        "fcpxml_split_clip": {
            "operation": "split",
            "clip": _clip_payload(
                name="Interview_A",
                offset="0s",
                start="30030/30000s",
                duration="60060/30000s",
                role="Dialogue",
                ref="r2",
            ),
            "created_clip": _clip_payload(
                name="Interview_A_split",
                offset="1001/500s",
                start="3003/1000s",
                duration="3003/1000s",
                role="Dialogue",
                ref="r2",
            ),
            "changed_fields": [
                {
                    "field": "duration",
                    "before": "150150/30000s",
                    "after": "60060/30000s",
                },
            ],
            "speed_factor": None,
        },
        "fcpxml_delete_clips": {
            "operation": "delete",
            "requested_count": 2,
            "changed_count": 2,
            "requested_names": [
                "Broll_Beach_Flash",
                "Interview_A_Outro",
            ],
            "deleted_names": [
                "Broll_Beach_Flash",
                "Interview_A_Outro",
            ],
            "requested_order": [],
            "resulting_order": [
                "Interview_A",
                "Broll_Beach",
                "Gap",
                "Broll_City",
            ],
        },
        "fcpxml_reorder_clips": {
            "operation": "reorder",
            "requested_count": 2,
            "changed_count": 2,
            "requested_names": ["Broll_City", "Interview_A"],
            "deleted_names": [],
            "requested_order": ["Broll_City", "Interview_A"],
            "resulting_order": [
                "Broll_City",
                "Interview_A",
                "Broll_Beach",
                "Gap",
                "Broll_Beach_Flash",
                "Interview_A_Outro",
            ],
        },
        "fcpxml_add_transition": {
            "transition": {
                "after_clip_name": "Interview_A",
                "name": "Short Dissolve",
                "duration": "6006/30000s",
                "offset": "1001/200s",
                "ref": "",
            },
        },
        "fcpxml_change_speed": {
            "operation": "change_speed",
            "clip": _clip_payload(
                name="Broll_Beach",
                offset="150150/30000s",
                start="0s",
                duration="60060/30000s",
                role="Video",
                ref="r3",
            ),
            "created_clip": None,
            "changed_fields": [
                {
                    "field": "duration",
                    "before": "120120/30000s",
                    "after": "60060/30000s",
                },
            ],
            "speed_factor": 2.0,
        },
        "fcpxml_assign_role": {
            "assignment": {
                "clip_name": "Interview_A",
                "role": "Narration",
            },
        },
        "fcpxml_add_title": {
            "element": {
                "kind": "title",
                "name": "Act Two",
                "ref": "r6",
                "position": "Broll_City",
                "source": None,
                "lane": 0,
                "offset": "121121/10000s",
                "start": None,
                "duration": "60060/30000s",
                "role": "Titles",
            },
        },
        "fcpxml_add_audio": {
            "element": {
                "kind": "audio",
                "name": "Voiceover",
                "ref": "r_audio_Voiceover",
                "position": "start",
                "source": str(audio.resolve()),
                "lane": 0,
                "offset": "0s",
                "start": "0s",
                "duration": "30030/30000s",
                "role": "Music",
            },
        },
    }[tool_name]


def _assert_committed_entity(
    tool_name: str,
    root: ET.Element,
    audio: Path,
) -> None:
    if tool_name == "fcpxml_add_marker":
        clip = _find_named(root, "asset-clip", "Interview_A")
        marker = next(
            element
            for element in clip.findall("marker")
            if element.get("value") == "Review"
        )
        assert marker.attrib == {
            "start": "0s",
            "duration": "1/1s",
            "value": "Review",
            "note": "Check this",
        }
    elif tool_name == "fcpxml_batch_add_markers":
        interview = _find_named(root, "asset-clip", "Interview_A")
        city = _find_named(root, "asset-clip", "Broll_City")
        assert any(
            marker.get("value") == "One"
            and marker.get("start") == "0s"
            for marker in interview.findall("marker")
        )
        assert any(
            marker.get("value") == "Two"
            and marker.get("start") == "1001/30000s"
            and marker.get("note") == "Chapter note"
            for marker in city.findall("chapter-marker")
        )
    elif tool_name == "fcpxml_add_keyword":
        clip = _find_named(root, "asset-clip", "Broll_Beach")
        keyword = next(
            element
            for element in clip.findall("keyword")
            if element.get("value") == "golden-hour"
        )
        assert keyword.attrib == {
            "value": "golden-hour",
            "start": "1001/30000s",
            "duration": "30030/30000s",
        }
    elif tool_name == "fcpxml_trim_clip":
        clip = _find_named(root, "asset-clip", "Interview_A")
        assert clip.get("start") == "60060/30000s"
        assert clip.get("duration") == "120120/30000s"
    elif tool_name == "fcpxml_split_clip":
        original = _find_named(root, "asset-clip", "Interview_A")
        created = _find_named(root, "asset-clip", "Interview_A_split")
        assert original.get("duration") == "60060/30000s"
        assert created.get("offset") == "1001/500s"
        assert created.get("start") == "3003/1000s"
        assert created.get("duration") == "3003/1000s"
    elif tool_name == "fcpxml_delete_clips":
        spine = root.find(".//spine")
        assert spine is not None
        names = {
            element.get("name")
            for element in spine
            if element.tag != "transition"
        }
        assert "Broll_Beach_Flash" not in names
        assert "Interview_A_Outro" not in names
    elif tool_name == "fcpxml_reorder_clips":
        spine = root.find(".//spine")
        assert spine is not None
        names = [
            element.get("name")
            for element in spine
            if element.tag != "transition"
        ]
        assert names == [
            "Broll_City",
            "Interview_A",
            "Broll_Beach",
            "Gap",
            "Broll_Beach_Flash",
            "Interview_A_Outro",
        ]
    elif tool_name == "fcpxml_add_transition":
        transition = next(
            element
            for element in root.iter("transition")
            if element.get("name") == "Short Dissolve"
        )
        assert transition.attrib == {
            "offset": "1001/200s",
            "duration": "6006/30000s",
            "name": "Short Dissolve",
        }
    elif tool_name == "fcpxml_change_speed":
        clip = _find_named(root, "asset-clip", "Broll_Beach")
        assert clip.get("duration") == "60060/30000s"
        time_points = clip.findall("./timeMap/timept")
        assert [point.attrib for point in time_points] == [
            {
                "time": "0s",
                "value": "0s",
                "interp": "smooth2",
            },
            {
                "time": "60060/30000s",
                "value": "120120/30000s",
                "interp": "smooth2",
            },
        ]
    elif tool_name == "fcpxml_assign_role":
        clip = _find_named(root, "asset-clip", "Interview_A")
        assert clip.get("role") == "Narration"
    elif tool_name == "fcpxml_add_title":
        title = _find_named(root, "title", "Act Two")
        assert title.get("offset") == "121121/10000s"
        assert title.get("duration") == "60060/30000s"
        assert title.find("param").get("value") == "Act Two"  # type: ignore[union-attr]
    else:
        clip = _find_named(root, "asset-clip", "Voiceover")
        asset = next(
            element
            for element in root.iter("asset")
            if element.get("id") == "r_audio_Voiceover"
        )
        assert clip.get("offset") == "0s"
        assert clip.get("duration") == "30030/30000s"
        assert asset.get("src") == f"file://{audio.resolve()}"


@pytest.fixture
def edit_path_policy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = RuntimeConfig.from_env(
        {
            "FCP_MCP_OUTPUT_DIR": str(tmp_path),
            "FCP_MCP_ALLOWED_ROOTS": str(tmp_path),
            "FCP_MCP_PROFILE": "full",
        },
        home=tmp_path,
    )
    monkeypatch.setattr(server, "CONFIG", config)
    monkeypatch.setattr(server, "PATHS", PathPolicy(config))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "model_name"),
    EDIT_CASES,
    ids=[case[0] for case in EDIT_CASES],
)
async def test_core_edits_publish_exact_text_real_receipts_and_verified_entities(
    tool_name: str,
    model_name: str,
    sample_fcpxml_path: Path,
    tmp_path: Path,
    edit_path_policy: None,
) -> None:
    source = tmp_path / f"{tool_name}-source.fcpxml"
    shutil.copy2(sample_fcpxml_path, source)
    input_sha256 = _sha256(source)
    audio = tmp_path / f"{tool_name}-audio.wav"
    audio.write_bytes(b"real fixture audio bytes")
    destination = (
        source.with_name(f"{source.stem}_modified.fcpxml")
        if tool_name == "fcpxml_add_marker"
        else tmp_path / f"{tool_name}-output.fcpxml"
    )
    prior_bytes: bytes | None = None
    if tool_name == "fcpxml_add_keyword":
        prior_bytes = sample_fcpxml_path.read_bytes().replace(
            b'Travel Vlog v1',
            b'Prior Destination',
        )
        assert prior_bytes != source.read_bytes()
        assert ET.fromstring(prior_bytes).tag == "fcpxml"
        destination.write_bytes(prior_bytes)

    result = await server.mcp.call_tool(
        tool_name,
        _arguments_for(tool_name, source, destination, audio),
    )

    assert result.is_error is False
    assert result.content[0].text == _expected_text(tool_name, destination)
    definitions = {tool.name: tool for tool in await server.mcp.list_tools()}
    assert set(definitions[tool_name].output_schema["properties"]) != {"result"}

    models = importlib.import_module("fcp_mcp.result_models.fcpxml")
    result_model = getattr(models, model_name)
    model = result_model.model_validate(result.structured_content)
    assert model.schema_version == "1"

    payload = model.model_dump(mode="json")
    assert payload["destination"] == {
        "path": str(destination.resolve()),
        "media_type": "application/vnd.apple.fcpxml+xml",
        "sha256": _sha256(destination),
        "size_bytes": destination.stat().st_size,
    }
    receipt = payload["receipt"]
    uuid.UUID(receipt["transaction_id"])
    assert receipt["source"] == str(source.resolve())
    assert receipt["destination"] == str(destination.resolve())
    assert receipt["input_sha256"] == input_sha256
    assert receipt["output_sha256"] == _sha256(destination)
    expected_warnings = (
        []
        if tool_name == "fcpxml_delete_clips"
        else ["Very short clip (0.067s) — possible flash frame"]
    )
    assert receipt["validation_warnings"] == expected_warnings
    assert receipt["elapsed_ms"] >= 0
    assert receipt["disposition"] == "committed"
    if prior_bytes is None:
        assert receipt["prior_sha256"] is None
        assert receipt["backup_path"] is None
    else:
        prior_sha256 = hashlib.sha256(prior_bytes).hexdigest()
        assert input_sha256 != prior_sha256
        assert receipt["output_sha256"] not in {
            input_sha256,
            prior_sha256,
        }
        assert receipt["prior_sha256"] == prior_sha256
        backup = Path(receipt["backup_path"])
        assert backup.resolve() not in {
            source.resolve(),
            destination.resolve(),
        }
        assert backup.resolve().parent == destination.resolve().parent
        assert receipt["transaction_id"] in backup.name
        assert backup.read_bytes() == prior_bytes
        assert _sha256(backup) == prior_sha256

    common_fields = {"schema_version", "destination", "receipt"}
    domain = {
        key: value
        for key, value in payload.items()
        if key not in common_fields
    }
    assert domain == _expected_domain(tool_name, audio)

    root = ET.parse(destination).getroot()
    assert root.tag == "fcpxml"
    _assert_committed_entity(tool_name, root, audio)


@pytest.mark.asyncio
async def test_trim_with_no_requested_changes_reports_no_changed_fields(
    sample_fcpxml_path: Path,
    tmp_path: Path,
    edit_path_policy: None,
) -> None:
    source = tmp_path / "zero-change-source.fcpxml"
    shutil.copy2(sample_fcpxml_path, source)
    destination = tmp_path / "zero-change-output.fcpxml"

    result = await server.mcp.call_tool(
        "fcpxml_trim_clip",
        {
            "path": str(source),
            "clip_name": "Interview_A",
            "output_path": str(destination),
        },
    )

    assert result.is_error is False
    assert result.content[0].text == (
        f"Clip trimmed. Saved to: {destination}"
    )
    assert result.structured_content["changed_fields"] == []
    assert result.structured_content["clip"]["start"] == "30030/30000s"
    assert result.structured_content["clip"]["duration"] == "150150/30000s"
    assert _sha256(destination) == result.structured_content["receipt"]["output_sha256"]


@pytest.mark.asyncio
async def test_one_x_speed_has_no_duration_change_and_verified_identity_map(
    sample_fcpxml_path: Path,
    tmp_path: Path,
    edit_path_policy: None,
) -> None:
    source = tmp_path / "one-x-source.fcpxml"
    shutil.copy2(sample_fcpxml_path, source)
    destination = tmp_path / "one-x-output.fcpxml"

    result = await server.mcp.call_tool(
        "fcpxml_change_speed",
        {
            "path": str(source),
            "clip_name": "Broll_Beach",
            "speed_factor": 1.0,
            "output_path": str(destination),
        },
    )

    assert result.is_error is False
    assert result.content[0].text == (
        f"Speed changed to 1.0x. Saved to: {destination}"
    )
    assert result.structured_content["changed_fields"] == []
    assert result.structured_content["clip"]["duration"] == "120120/30000s"
    assert result.structured_content["speed_factor"] == 1.0
    clip = _find_named(
        ET.parse(destination).getroot(),
        "asset-clip",
        "Broll_Beach",
    )
    assert [point.attrib for point in clip.findall("./timeMap/timept")] == [
        {
            "time": "0s",
            "value": "0s",
            "interp": "smooth2",
        },
        {
            "time": "120120/30000s",
            "value": "120120/30000s",
            "interp": "smooth2",
        },
    ]


@pytest.mark.parametrize(
    ("operation", "expected_code"),
    [
        (
            lambda source, destination: server.fcpxml_batch_add_markers(
                str(source),
                "[]",
                output_path=str(destination),
            ),
            ErrorCode.INVALID_ARGUMENTS,
        ),
        (
            lambda source, destination: server.fcpxml_add_marker(
                str(source),
                "missing",
                "0s",
                "Marker",
                output_path=str(destination),
            ),
            ErrorCode.TARGET_NOT_FOUND,
        ),
        (
            lambda source, destination: server.fcpxml_change_speed(
                str(source),
                "Interview_A",
                0,
                output_path=str(destination),
            ),
            ErrorCode.INVALID_ARGUMENTS,
        ),
    ],
)
def test_core_edit_failures_keep_their_codes_and_commit_nothing(
    operation: Callable[[Path, Path], object],
    expected_code: ErrorCode,
    sample_fcpxml_path: Path,
    tmp_path: Path,
    edit_path_policy: None,
) -> None:
    source = tmp_path / f"{expected_code.value}-source.fcpxml"
    shutil.copy2(sample_fcpxml_path, source)
    destination = tmp_path / f"{expected_code.value}-output.fcpxml"

    with pytest.raises(FCPMCPError) as caught:
        operation(source, destination)

    assert caught.value.code is expected_code
    assert destination.exists() is False


def test_same_file_edit_keeps_the_same_file_forbidden_code(
    sample_fcpxml_path: Path,
    tmp_path: Path,
    edit_path_policy: None,
) -> None:
    source = tmp_path / "same-file.fcpxml"
    shutil.copy2(sample_fcpxml_path, source)
    before = source.read_bytes()

    with pytest.raises(FCPMCPError) as caught:
        server.fcpxml_assign_role(
            str(source),
            "Interview_A",
            "Narration",
            output_path=str(source),
        )

    assert caught.value.code is ErrorCode.SAME_FILE_FORBIDDEN
    assert source.read_bytes() == before


def test_save_still_returns_path_and_save_with_receipt_returns_atomic_receipt(
    sample_fcpxml_path: Path,
    tmp_path: Path,
) -> None:
    source = tmp_path / "save-source.fcpxml"
    shutil.copy2(sample_fcpxml_path, source)
    first = tmp_path / "save-output.fcpxml"
    second = tmp_path / "receipt-output.fcpxml"

    modifier = FCPXMLModifier(source)
    modifier.add_marker("Interview_A", "0s", "Compatibility")
    saved = modifier.save(first)
    receipt = modifier.save_with_receipt(second)

    assert saved == first.resolve()
    assert isinstance(saved, Path)
    assert receipt.source == source.resolve()
    assert receipt.destination == second.resolve()
    assert receipt.input_sha256 == _sha256(source)
    assert receipt.output_sha256 == _sha256(second)
    assert receipt.prior_sha256 is None
    assert receipt.backup_path is None
    assert receipt.elapsed_ms >= 0
    assert receipt.disposition == "committed"


def test_edit_result_models_are_frozen_strict_and_opaque_free() -> None:
    common = importlib.import_module("fcp_mcp.result_models.common")
    fcpxml = importlib.import_module("fcp_mcp.result_models.fcpxml")
    models = {
        "ArtifactReference": common.ArtifactReference,
        **{
            name: getattr(fcpxml, name)
            for name in RESULT_MODEL_NAMES - {"ArtifactReference"}
        },
    }

    for name, model in models.items():
        assert model.model_config["frozen"] is True, name
        assert model.model_config["extra"] == "forbid", name
        schema = model.model_json_schema()
        pending: list[object] = [schema]
        while pending:
            node = pending.pop()
            if isinstance(node, dict):
                if "additionalProperties" in node:
                    assert node["additionalProperties"] is False, name
                pending.extend(node.values())
            elif isinstance(node, list):
                pending.extend(node)
