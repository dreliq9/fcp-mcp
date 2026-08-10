from __future__ import annotations

import hashlib
import importlib
import json
import shutil
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from fcp_mcp import server
from fcp_mcp.config import RuntimeConfig
from fcp_mcp.contracts import ErrorCode, FCPMCPError
from fcp_mcp.fcpxml.generator import FCPXMLGenerator
from fcp_mcp.security.paths import PathPolicy

OFFLINE_WRITE_CASES = (
    ("fcpxml_create_project", "FCPXMLGenerationResult"),
    ("fcpxml_create_timeline", "FCPXMLGenerationResult"),
    ("fcpxml_auto_rough_cut", "FCPXMLGenerationResult"),
    ("fcpxml_generate_montage", "FCPXMLGenerationResult"),
    ("fcpxml_import_srt", "SubtitleImportResult"),
    ("fcpxml_import_edl", "EDLImportResult"),
    ("fcpxml_reformat", "FCPXMLMutationResult"),
    ("fcpxml_fix_flash_frames", "CleanupMutationResult"),
    ("fcpxml_fill_gaps", "CleanupMutationResult"),
    ("fcpxml_remove_silence", "CleanupMutationResult"),
    ("fcpxml_batch_rename_clips", "ClipBatchMutationResult"),
    ("fcpxml_batch_assign_roles", "RoleBatchMutationResult"),
    ("fcpxml_batch_apply_transition", "TransitionBatchMutationResult"),
    ("fcpxml_apply_template", "UnsupportedToolResult"),
    ("fcpxml_save_template", "TemplateSaveResult"),
    ("fcpxml_export_resolve", "ExportResult"),
    ("fcpxml_export_fcp7", "ExportResult"),
    ("fcpxml_export_edl", "ExportResult"),
)

RESULT_MODEL_NAMES = {
    "FCPXMLGenerationResult",
    "SubtitleImportResult",
    "EDLImportResult",
    "FCPXMLMutationResult",
    "CleanupMutationResult",
    "ClipBatchMutationResult",
    "RoleRuleRecord",
    "RoleBatchMutationResult",
    "TransitionBatchMutationResult",
    "UnsupportedToolResult",
    "TemplateSaveResult",
    "ExportResult",
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _artifact_payload(
    path: Path,
    media_type: str = "application/vnd.apple.fcpxml+xml",
) -> dict[str, object]:
    return {
        "path": str(path.resolve()),
        "media_type": media_type,
        "sha256": _sha256(path),
        "size_bytes": path.stat().st_size,
    }


def _receipt_payload(
    *,
    source: str | None = "/tmp/source.fcpxml",
) -> dict[str, object]:
    return {
        "transaction_id": "11111111-1111-4111-8111-111111111111",
        "source": source,
        "destination": "/tmp/destination.fcpxml",
        "backup_path": None,
        "input_sha256": "1" * 64 if source is not None else None,
        "prior_sha256": None,
        "output_sha256": "2" * 64,
        "validation_warnings": [],
        "elapsed_ms": 1,
        "disposition": "committed",
    }


def _reference_payload() -> dict[str, object]:
    return {
        "path": "/tmp/destination.fcpxml",
        "media_type": "application/vnd.apple.fcpxml+xml",
        "sha256": "2" * 64,
        "size_bytes": 1,
    }


def _copy_source(sample_fcpxml_path: Path, tmp_path: Path, stem: str) -> Path:
    source = tmp_path / f"{stem}-source.fcpxml"
    shutil.copy2(sample_fcpxml_path, source)
    return source


def _write_media(tmp_path: Path) -> tuple[Path, Path]:
    first = tmp_path / "first.mov"
    second = tmp_path / "second.mov"
    first.write_bytes(b"first media fixture")
    second.write_bytes(b"second media fixture")
    return first, second


def _clips_json(first: Path, second: Path) -> str:
    return json.dumps(
        [
            {
                "src": str(first),
                "name": "First",
                "duration": "120120/30000s",
            },
            {
                "src": str(second),
                "name": "Second",
                "duration": "90090/30000s",
            },
        ]
    )


def _assert_fcpxml_evidence(
    payload: dict[str, object],
    destination: Path,
    *,
    source: Path | None,
) -> None:
    assert payload["destination"] == _artifact_payload(destination)
    receipt = payload["receipt"]
    assert isinstance(receipt, dict)
    uuid.UUID(str(receipt["transaction_id"]))
    assert receipt["source"] == (
        str(source.resolve()) if source is not None else None
    )
    assert receipt["destination"] == str(destination.resolve())
    assert receipt["input_sha256"] == (
        _sha256(source) if source is not None else None
    )
    assert receipt["output_sha256"] == _sha256(destination)
    assert receipt["elapsed_ms"] >= 0
    assert receipt["disposition"] == "committed"
    assert ET.parse(destination).getroot().tag == "fcpxml"


@pytest.fixture(autouse=True)
def offline_write_path_policy(
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
    OFFLINE_WRITE_CASES,
    ids=[case[0] for case in OFFLINE_WRITE_CASES],
)
async def test_exact_offline_write_group_advertises_named_nonlegacy_schemas(
    tool_name: str,
    model_name: str,
) -> None:
    definition = server.TOOLS.definitions[tool_name]
    assert definition.result_model.__name__ == model_name

    tools = {tool.name: tool for tool in await server.mcp.list_tools()}
    assert set(tools[tool_name].output_schema["properties"]) != {"result"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "project", "selected_count", "target_seconds"),
    (
        ("fcpxml_create_project", "Contract Project", 0, 0.0),
        ("fcpxml_create_timeline", "Contract Timeline", 2, 7.007),
        ("fcpxml_auto_rough_cut", "Contract Rough", 2, 5.0),
        ("fcpxml_generate_montage", "Contract Montage", 2, 6.0),
    ),
)
async def test_generation_results_are_reparsed_from_committed_fcpxml(
    tool_name: str,
    project: str,
    selected_count: int,
    target_seconds: float,
    tmp_path: Path,
) -> None:
    first, second = _write_media(tmp_path)
    destination = tmp_path / f"{tool_name}.fcpxml"
    clips_json = _clips_json(first, second)
    arguments = {
        "fcpxml_create_project": {
            "name": project,
            "output_path": str(destination),
        },
        "fcpxml_create_timeline": {
            "clips_json": clips_json,
            "project_name": project,
            "output_path": str(destination),
        },
        "fcpxml_auto_rough_cut": {
            "clips_json": clips_json,
            "target_duration": "5s",
            "max_clip_duration": "",
            "project_name": project,
            "output_path": str(destination),
        },
        "fcpxml_generate_montage": {
            "clips_json": clips_json,
            "clip_duration": "3s",
            "project_name": project,
            "output_path": str(destination),
        },
    }[tool_name]

    result = await server.mcp.call_tool(tool_name, arguments)

    expected_text = {
        "fcpxml_create_project": f"Project created: {destination}",
        "fcpxml_create_timeline": (
            f"Timeline created with 2 clips: {destination}"
        ),
        "fcpxml_auto_rough_cut": (
            f"Rough cut created (5.0s): {destination}"
        ),
        "fcpxml_generate_montage": (
            f"Montage created (2 shots): {destination}"
        ),
    }[tool_name]
    assert result.is_error is False
    assert result.content[0].text == expected_text
    payload = result.structured_content
    assert payload["project"] == project
    assert payload["selected_clip_count"] == selected_count
    assert payload["target_duration_seconds"] == target_seconds
    _assert_fcpxml_evidence(payload, destination, source=None)

    root = ET.parse(destination).getroot()
    spine = root.find(".//spine")
    assert spine is not None
    committed = [child for child in spine if child.tag != "transition"]
    assert len(committed) == selected_count
    actual_seconds = sum(
        server.RationalTime.from_fcpxml(child.get("duration", "0s")).to_seconds()
        for child in committed
    )
    assert actual_seconds == pytest.approx(target_seconds)


@pytest.mark.asyncio
async def test_timeline_tool_preserves_explicit_full_source_duration(
    tmp_path: Path,
) -> None:
    media = tmp_path / "source.mov"
    media.write_bytes(b"media")
    destination = tmp_path / "explicit-source-duration.fcpxml"
    result = await server.mcp.call_tool(
        "fcpxml_create_timeline",
        {
            "clips_json": json.dumps(
                [
                    {
                        "src": str(media),
                        "name": "Source range",
                        "start": "900900/30000s",
                        "duration": "150150/30000s",
                        "asset_duration": "5735730/30000s",
                    }
                ]
            ),
            "project_name": "Explicit Source Duration",
            "output_path": str(destination),
        },
    )

    assert result.is_error is False
    assert result.structured_content["selected_clip_count"] == 1
    _assert_fcpxml_evidence(
        result.structured_content,
        destination,
        source=None,
    )
    root = ET.parse(destination).getroot()
    asset = root.find("./resources/asset")
    clip = root.find(".//asset-clip")
    assert asset is not None
    assert clip is not None
    assert asset.get("duration") == "5735730/30000s"
    assert clip.get("start") == "900900/30000s"
    assert clip.get("duration") == "150150/30000s"


@pytest.mark.asyncio
async def test_import_results_report_actual_cues_and_events(
    sample_fcpxml_path: Path,
    tmp_path: Path,
) -> None:
    subtitle_source = _copy_source(
        sample_fcpxml_path,
        tmp_path,
        "subtitle",
    )
    srt = tmp_path / "captions.srt"
    srt.write_text(
        "1\n00:00:00,000 --> 00:00:01,500\nFirst cue\n\n"
        "2\n00:00:02,000 --> 00:00:03,000\nSecond cue\n",
        encoding="utf-8",
    )
    subtitle_destination = tmp_path / "subtitles.fcpxml"

    subtitle_result = await server.mcp.call_tool(
        "fcpxml_import_srt",
        {
            "path": str(subtitle_source),
            "srt_path": str(srt),
            "output_path": str(subtitle_destination),
        },
    )

    assert subtitle_result.content[0].text == (
        f"2 subtitles added. Saved to: {subtitle_destination}"
    )
    assert subtitle_result.structured_content["cue_count"] == 2
    _assert_fcpxml_evidence(
        subtitle_result.structured_content,
        subtitle_destination,
        source=subtitle_source,
    )
    assert len(
        ET.parse(subtitle_destination).getroot().findall(
            ".//title[@role='Titles.Subtitle']"
        )
    ) == 2

    media_dir = tmp_path / "media"
    media_dir.mkdir()
    (media_dir / "AX.mov").write_bytes(b"edl media fixture")
    edl = tmp_path / "source.edl"
    edl.write_text(
        "TITLE: Contract EDL\n"
        "001  AX       V     C        "
        "00:00:00:00 00:00:03:00 00:00:00:00 00:00:03:00\n",
        encoding="utf-8",
    )
    edl_destination = tmp_path / "edl-import.fcpxml"

    edl_result = await server.mcp.call_tool(
        "fcpxml_import_edl",
        {
            "edl_path": str(edl),
            "media_dir": str(media_dir),
            "project_name": "Imported EDL",
            "output_path": str(edl_destination),
        },
    )

    assert edl_result.content[0].text == (
        f"EDL imported (1 clips): {edl_destination}"
    )
    assert edl_result.structured_content["event_count"] == 1
    _assert_fcpxml_evidence(
        edl_result.structured_content,
        edl_destination,
        source=None,
    )
    assert len(
        ET.parse(edl_destination).getroot().findall(".//spine/asset-clip")
    ) == 1


@pytest.mark.asyncio
async def test_subtitle_import_reports_delta_with_preexisting_subtitle(
    sample_fcpxml_path: Path,
    tmp_path: Path,
) -> None:
    source = _copy_source(sample_fcpxml_path, tmp_path, "existing-subtitle")
    tree = ET.parse(source)
    first_clip = tree.getroot().find(".//spine/asset-clip")
    assert first_clip is not None
    existing = ET.SubElement(
        first_clip,
        "title",
        {
            "ref": "r1",
            "name": "Existing subtitle",
            "offset": "0s",
            "duration": "1s",
            "lane": "1",
            "role": "Titles.Subtitle",
        },
    )
    ET.SubElement(
        existing,
        "param",
        {"name": "Text", "key": "Text", "value": "Existing"},
    )
    tree.write(source, encoding="utf-8", xml_declaration=True)
    srt = tmp_path / "delta.srt"
    srt.write_text(
        "1\n00:00:01,000 --> 00:00:02,000\nAdded one\n\n"
        "2\n00:00:03,000 --> 00:00:04,000\nAdded two\n",
        encoding="utf-8",
    )
    destination = tmp_path / "subtitle-delta.fcpxml"

    result = await server.mcp.call_tool(
        "fcpxml_import_srt",
        {
            "path": str(source),
            "srt_path": str(srt),
            "output_path": str(destination),
        },
    )

    assert result.content[0].text == (
        f"2 subtitles added. Saved to: {destination}"
    )
    assert result.structured_content["cue_count"] == 2
    assert len(
        ET.parse(destination).getroot().findall(
            ".//title[@role='Titles.Subtitle']"
        )
    ) == 3


@pytest.mark.asyncio
async def test_reformat_and_cleanup_report_committed_changes_including_zero(
    sample_fcpxml_path: Path,
    tmp_path: Path,
) -> None:
    reformat_source = _copy_source(sample_fcpxml_path, tmp_path, "reformat")
    reformat_destination = tmp_path / "reformatted.fcpxml"
    reformatted = await server.mcp.call_tool(
        "fcpxml_reformat",
        {
            "path": str(reformat_source),
            "target_width": 1080,
            "target_height": 1920,
            "target_format_name": "Vertical Contract",
            "output_path": str(reformat_destination),
        },
    )

    assert reformatted.content[0].text == (
        f"Reformatted to 1080x1920. Saved to: {reformat_destination}"
    )
    assert {
        key: reformatted.structured_content[key]
        for key in (
            "source_version",
            "target_version",
            "target_width",
            "target_height",
            "target_format_name",
        )
    } == {
        "source_version": "1.11",
        "target_version": "1.11",
        "target_width": 1080,
        "target_height": 1920,
        "target_format_name": "Vertical Contract",
    }
    _assert_fcpxml_evidence(
        reformatted.structured_content,
        reformat_destination,
        source=reformat_source,
    )

    cases = (
        ("fcpxml_fix_flash_frames", {"min_frames": 3}, "fix_flash_frames", 1),
        (
            "fcpxml_fill_gaps",
            {"fill_asset_ref": "r3", "fill_name": "Verified Fill"},
            "fill_gaps",
            1,
        ),
        (
            "fcpxml_remove_silence",
            {"silence_threshold_seconds": 99.0},
            "remove_silence",
            0,
        ),
    )
    for tool_name, extra, action, changed_count in cases:
        source = _copy_source(sample_fcpxml_path, tmp_path, tool_name)
        destination = tmp_path / f"{tool_name}.fcpxml"
        result = await server.mcp.call_tool(
            tool_name,
            {
                "path": str(source),
                "output_path": str(destination),
                **extra,
            },
        )

        expected_text = {
            "fcpxml_fix_flash_frames": (
                f"1 flash frames fixed. Saved to: {destination}"
            ),
            "fcpxml_fill_gaps": (
                f"1 gaps filled. Saved to: {destination}"
            ),
            "fcpxml_remove_silence": (
                f"0 gaps removed. Saved to: {destination}"
            ),
        }[tool_name]
        assert result.content[0].text == expected_text
        assert result.structured_content["action"] == action
        assert result.structured_content["changed_count"] == changed_count
        _assert_fcpxml_evidence(
            result.structured_content,
            destination,
            source=source,
        )


@pytest.mark.asyncio
async def test_batch_results_use_service_counts_and_committed_values(
    sample_fcpxml_path: Path,
    tmp_path: Path,
) -> None:
    rename_source = _copy_source(sample_fcpxml_path, tmp_path, "rename")
    rename_destination = tmp_path / "renamed.fcpxml"
    renamed = await server.mcp.call_tool(
        "fcpxml_batch_rename_clips",
        {
            "path": str(rename_source),
            "pattern": "Broll",
            "replacement": "Scenic",
            "output_path": str(rename_destination),
        },
    )

    assert renamed.content[0].text == (
        f"5 clips renamed. Saved to: {rename_destination}"
    )
    assert {
        "pattern": renamed.structured_content["pattern"],
        "replacement": renamed.structured_content["replacement"],
        "changed_count": renamed.structured_content["changed_count"],
    } == {
        "pattern": "Broll",
        "replacement": "Scenic",
        "changed_count": 5,
    }
    _assert_fcpxml_evidence(
        renamed.structured_content,
        rename_destination,
        source=rename_source,
    )
    assert sorted(
        element.get("name")
        for element in ET.parse(rename_destination).getroot().iter("asset-clip")
        if element.get("name", "").startswith("Scenic")
    ) == ["Scenic_Beach", "Scenic_Beach_Flash", "Scenic_City"]

    role_source = _copy_source(sample_fcpxml_path, tmp_path, "roles")
    role_destination = tmp_path / "roles.fcpxml"
    rules = [
        {"match": "Interview", "role": "Narration"},
        {"match": "Broll", "role": "B-Roll"},
    ]
    assigned = await server.mcp.call_tool(
        "fcpxml_batch_assign_roles",
        {
            "path": str(role_source),
            "rules_json": json.dumps(rules),
            "output_path": str(role_destination),
        },
    )

    assert assigned.content[0].text == (
        f"5 roles assigned. Saved to: {role_destination}"
    )
    assert assigned.structured_content["rules"] == rules
    assert assigned.structured_content["changed_count"] == 5
    _assert_fcpxml_evidence(
        assigned.structured_content,
        role_destination,
        source=role_source,
    )

    transition_source = _copy_source(
        sample_fcpxml_path,
        tmp_path,
        "transitions",
    )
    transition_destination = tmp_path / "transitions.fcpxml"
    transitioned = await server.mcp.call_tool(
        "fcpxml_batch_apply_transition",
        {
            "path": str(transition_source),
            "duration": "6006/30000s",
            "name": "Verified Dissolve",
            "output_path": str(transition_destination),
        },
    )

    assert transitioned.content[0].text == (
        f"3 transitions added. Saved to: {transition_destination}"
    )
    assert {
        "name": transitioned.structured_content["name"],
        "duration": transitioned.structured_content["duration"],
        "changed_count": transitioned.structured_content["changed_count"],
    } == {
        "name": "Verified Dissolve",
        "duration": "6006/30000s",
        "changed_count": 3,
    }
    _assert_fcpxml_evidence(
        transitioned.structured_content,
        transition_destination,
        source=transition_source,
    )
    assert len(
        ET.parse(transition_destination).getroot().findall(
            ".//transition[@name='Verified Dissolve']"
        )
    ) == 3


def test_apply_template_advertises_stable_unsupported_contract_and_writes_nothing(
    sample_fcpxml_path: Path,
    tmp_path: Path,
) -> None:
    destination = tmp_path / "must-not-exist.fcpxml"
    definition = server.TOOLS.definitions["fcpxml_apply_template"]
    assert definition.result_model.__name__ == "UnsupportedToolResult"
    schema = definition.result_model.model_json_schema()
    assert schema["properties"]["supported"]["const"] is False
    assert schema["properties"]["reason_code"]["const"] == (
        "unsupported_contract"
    )
    assert schema["properties"]["reason"]["const"] == (
        "Template clip replacement has no stable clip substitution schema "
        "in v0.2.1"
    )

    with pytest.raises(FCPMCPError) as caught:
        server.fcpxml_apply_template(
            str(sample_fcpxml_path),
            "[]",
            output_path=str(destination),
        )

    assert caught.value.code is ErrorCode.UNSUPPORTED_CONTRACT
    assert str(caught.value) == (
        "unsupported_contract: Template clip replacement has no stable clip "
        "substitution schema in v0.2.1"
    )
    assert destination.exists() is False


@pytest.mark.asyncio
async def test_template_result_binds_copy_to_source_hash_and_absolute_backup(
    sample_fcpxml_path: Path,
    tmp_path: Path,
) -> None:
    source = _copy_source(sample_fcpxml_path, tmp_path, "template")
    destination = tmp_path / "template_contract.fcpxml"
    prior_bytes = b"prior template bytes"
    destination.write_bytes(prior_bytes)

    result = await server.mcp.call_tool(
        "fcpxml_save_template",
        {
            "path": str(source),
            "template_name": "contract",
            "output_dir": str(tmp_path),
        },
    )

    assert result.content[0].text == f"Template saved: {destination}"
    assert result.structured_content["template"] == _artifact_payload(destination)
    assert result.structured_content["source_path"] == str(source.resolve())
    assert result.structured_content["source_sha256"] == _sha256(source)
    assert destination.read_bytes() == source.read_bytes()
    assert result.structured_content["source_sha256"] == _sha256(destination)
    backup_path = Path(result.structured_content["backup_path"])
    assert backup_path.is_absolute()
    assert backup_path.read_bytes() == prior_bytes
    receipt = result.structured_content["receipt"]
    assert receipt["source"] == str(source.resolve())
    assert receipt["destination"] == str(destination.resolve())
    assert receipt["input_sha256"] == _sha256(source)
    assert receipt["output_sha256"] == _sha256(destination)
    assert receipt["backup_path"] == str(backup_path)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("declared_encoding", "line_ending", "project_bytes"),
    (
        ("UTF-8", b"\r\n", b"CRLF Project"),
        ("ISO-8859-1", b"\n", b"Caf\xe9 Project"),
    ),
)
async def test_template_save_preserves_declared_encoding_and_exact_bytes(
    declared_encoding: str,
    line_ending: bytes,
    project_bytes: bytes,
    tmp_path: Path,
) -> None:
    source = tmp_path / f"encoded-{declared_encoding}.fcpxml"
    source_bytes = line_ending.join(
        (
            (
                f'<?xml version="1.0" encoding="{declared_encoding}"?>'
            ).encode("ascii"),
            b"<!DOCTYPE fcpxml>",
            b'<fcpxml version="1.11">',
            b"<resources />",
            b'<event name="Encoded"><project name="'
            + project_bytes
            + b'"><sequence duration="0s"><spine /></sequence></project></event>',
            b"</fcpxml>",
            b"",
        )
    )
    source.write_bytes(source_bytes)
    template_name = f"bytes-{declared_encoding.lower()}"
    destination = tmp_path / f"template_{template_name}.fcpxml"

    result = await server.mcp.call_tool(
        "fcpxml_save_template",
        {
            "path": str(source),
            "template_name": template_name,
            "output_dir": str(tmp_path),
        },
    )

    assert result.content[0].text == f"Template saved: {destination}"
    assert destination.read_bytes() == source_bytes
    assert result.structured_content["source_sha256"] == hashlib.sha256(
        source_bytes
    ).hexdigest()
    assert result.structured_content["template"]["sha256"] == hashlib.sha256(
        source_bytes
    ).hexdigest()
    assert result.structured_content["receipt"]["input_sha256"] == hashlib.sha256(
        source_bytes
    ).hexdigest()
    assert result.structured_content["receipt"]["output_sha256"] == hashlib.sha256(
        source_bytes
    ).hexdigest()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "export_format", "suffix", "media_type"),
    (
        (
            "fcpxml_export_resolve",
            "resolve",
            ".fcpxml",
            "application/vnd.apple.fcpxml+xml",
        ),
        ("fcpxml_export_fcp7", "fcp7", ".xml", "application/xml"),
        ("fcpxml_export_edl", "edl", ".edl", "text/x-cmx3600"),
    ),
)
async def test_exports_bind_source_and_each_actual_output_format(
    tool_name: str,
    export_format: str,
    suffix: str,
    media_type: str,
    sample_fcpxml_path: Path,
    tmp_path: Path,
) -> None:
    source = _copy_source(sample_fcpxml_path, tmp_path, export_format)
    destination = tmp_path / f"export-{export_format}{suffix}"
    result = await server.mcp.call_tool(
        tool_name,
        {"path": str(source), "output_path": str(destination)},
    )

    expected_text = {
        "resolve": f"Resolve-compatible FCPXML saved: {destination}",
        "fcp7": f"FCP7 XML saved: {destination}",
        "edl": f"EDL exported (5 edits): {destination}",
    }[export_format]
    assert result.content[0].text == expected_text
    assert result.structured_content["format"] == export_format
    assert result.structured_content["source"] == _artifact_payload(source)
    assert result.structured_content["artifact"] == _artifact_payload(
        destination,
        media_type,
    )
    receipt = result.structured_content["receipt"]
    assert receipt is not None
    uuid.UUID(receipt["transaction_id"])
    assert receipt["source"] == str(source.resolve())
    assert receipt["destination"] == str(destination.resolve())
    assert receipt["input_sha256"] == _sha256(source)
    assert receipt["prior_sha256"] is None
    assert receipt["backup_path"] is None
    assert receipt["output_sha256"] == _sha256(destination)
    assert receipt["elapsed_ms"] >= 0
    assert receipt["disposition"] == "committed"
    if export_format == "resolve":
        assert ET.parse(destination).getroot().get("version") == "1.9"
    else:
        assert receipt["validation_warnings"] == []
        if export_format == "fcp7":
            assert ET.parse(destination).getroot().tag == "xmeml"
        else:
            assert destination.read_text(encoding="utf-8").startswith(
                "TITLE: Travel Vlog v1\nFCM: NON-DROP FRAME\n"
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "suffix"),
    (
        ("fcpxml_export_fcp7", ".xml"),
        ("fcpxml_export_edl", ".edl"),
    ),
)
async def test_non_fcpxml_export_receipt_captures_overwrite_backup(
    tool_name: str,
    suffix: str,
    sample_fcpxml_path: Path,
    tmp_path: Path,
) -> None:
    source = _copy_source(sample_fcpxml_path, tmp_path, "export-overwrite")
    destination = tmp_path / f"overwrite{suffix}"
    prior_bytes = b"prior export bytes"
    destination.write_bytes(prior_bytes)

    result = await server.mcp.call_tool(
        tool_name,
        {"path": str(source), "output_path": str(destination)},
    )

    receipt = result.structured_content["receipt"]
    assert receipt is not None
    assert receipt["source"] == str(source.resolve())
    assert receipt["destination"] == str(destination.resolve())
    assert receipt["input_sha256"] == _sha256(source)
    assert receipt["prior_sha256"] == hashlib.sha256(prior_bytes).hexdigest()
    assert receipt["output_sha256"] == _sha256(destination)
    backup = Path(receipt["backup_path"])
    assert backup.is_absolute()
    assert backup.read_bytes() == prior_bytes


@pytest.mark.parametrize(
    ("tool_name", "suffix", "has_prior"),
    (
        ("fcpxml_export_resolve", ".fcpxml", True),
        ("fcpxml_export_fcp7", ".xml", False),
        ("fcpxml_export_edl", ".edl", True),
    ),
)
def test_malformed_export_candidate_preserves_destination_state(
    tool_name: str,
    suffix: str,
    has_prior: bool,
    sample_fcpxml_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fcp_mcp.fcpxml import transaction

    source = _copy_source(sample_fcpxml_path, tmp_path, "malformed-export")
    destination = tmp_path / f"malformed{suffix}"
    prior_bytes = b"prior destination bytes"
    if has_prior:
        destination.write_bytes(prior_bytes)

    if tool_name == "fcpxml_export_resolve":
        real_atomic = transaction.atomic_replace_bytes

        def corrupt_resolve(
            output: str | Path,
            payload: bytes,
            **kwargs: object,
        ) -> object:
            corrupted = payload.replace(b'version="1.9"', b'version="1.8"')
            return real_atomic(output, corrupted, **kwargs)

        monkeypatch.setattr(
            transaction,
            "atomic_replace_bytes",
            corrupt_resolve,
        )
    else:
        real_atomic = server.atomic_replace_bytes

        def corrupt_export(
            output: str | Path,
            payload: bytes,
            **kwargs: object,
        ) -> object:
            corrupted = (
                b"<malformed"
                if tool_name == "fcpxml_export_fcp7"
                else b"TITLE: Broken\nFCM: NON-DROP FRAME\n"
            )
            return real_atomic(output, corrupted, **kwargs)

        monkeypatch.setattr(server, "atomic_replace_bytes", corrupt_export)

    with pytest.raises(Exception) as caught:
        getattr(server, tool_name)(
            str(source),
            output_path=str(destination),
        )

    if has_prior:
        assert destination.read_bytes() == prior_bytes
    else:
        assert destination.exists() is False
    assert list(tmp_path.glob(f"{destination.name}.bak.*")) == []
    assert isinstance(caught.value, FCPMCPError)
    assert caught.value.code is ErrorCode.VALIDATION_FAILED


@pytest.mark.parametrize(
    ("tool_name", "arguments", "before", "after", "destination_name"),
    (
        (
            "fcpxml_reformat",
            {
                "target_width": 1080,
                "target_height": 1920,
                "target_format_name": "Vertical Candidate",
            },
            b'name="Vertical Candidate"',
            b'name="Corrupt Candidate"',
            "semantic-reformat.fcpxml",
        ),
        (
            "fcpxml_batch_rename_clips",
            {"pattern": "Broll", "replacement": "Scenic"},
            b"Scenic",
            b"Corrupt",
            "semantic-rename.fcpxml",
        ),
        (
            "fcpxml_save_template",
            {"template_name": "atomic"},
            b"Travel Vlog v1",
            b"Travel Vlog v2",
            "template_atomic.fcpxml",
        ),
    ),
)
def test_semantic_candidate_failure_preserves_prior_destination(
    tool_name: str,
    arguments: dict[str, object],
    before: bytes,
    after: bytes,
    destination_name: str,
    sample_fcpxml_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fcp_mcp.fcpxml import transaction

    source = _copy_source(sample_fcpxml_path, tmp_path, "semantic-candidate")
    destination = tmp_path / destination_name
    prior_bytes = b"prior destination bytes"
    destination.write_bytes(prior_bytes)
    real_atomic = transaction.atomic_replace_bytes

    def corrupt_candidate(
        output: str | Path,
        payload: bytes,
        **kwargs: object,
    ) -> object:
        assert before in payload
        return real_atomic(output, payload.replace(before, after), **kwargs)

    monkeypatch.setattr(
        transaction,
        "atomic_replace_bytes",
        corrupt_candidate,
    )
    call_arguments = dict(arguments)
    if tool_name == "fcpxml_save_template":
        call_arguments["output_dir"] = str(tmp_path)
        positional = (str(source),)
    else:
        call_arguments["output_path"] = str(destination)
        positional = (str(source),)

    with pytest.raises(FCPMCPError) as caught:
        getattr(server, tool_name)(*positional, **call_arguments)

    assert caught.value.code is ErrorCode.VALIDATION_FAILED
    assert destination.read_bytes() == prior_bytes
    assert list(tmp_path.glob(f"{destination.name}.bak.*")) == []


def test_generator_save_compatibility_and_overwrite_receipt(
    tmp_path: Path,
) -> None:
    generator = FCPXMLGenerator()
    generator.create_project(name="Generator Contract")
    destination = tmp_path / "generator.fcpxml"
    prior_bytes = (
        b'<?xml version="1.0" encoding="UTF-8"?>\n'
        b'<!DOCTYPE fcpxml>\n<fcpxml version="1.11"><resources />'
        b'<event name="Old" /></fcpxml>\n'
    )
    destination.write_bytes(prior_bytes)

    receipt = generator.save_with_receipt(destination)
    compatibility_path = generator.save(tmp_path / "compatibility.fcpxml")

    assert receipt.source is None
    assert receipt.destination == destination.resolve()
    assert receipt.prior_sha256 == hashlib.sha256(prior_bytes).hexdigest()
    assert receipt.output_sha256 == _sha256(destination)
    assert receipt.backup_path is not None
    assert receipt.backup_path.is_absolute()
    assert receipt.backup_path.read_bytes() == prior_bytes
    assert isinstance(compatibility_path, Path)
    assert compatibility_path == (tmp_path / "compatibility.fcpxml").resolve()


def test_offline_write_models_are_frozen_strict_and_opaque_free() -> None:
    models = importlib.import_module("fcp_mcp.result_models.fcpxml")
    for name in RESULT_MODEL_NAMES:
        model = getattr(models, name)
        assert model.model_config["frozen"] is True, name
        assert model.model_config["extra"] == "forbid", name
        assert model.model_config["strict"] is True, name
        schema = model.model_json_schema()
        pending: list[object] = [schema]
        while pending:
            node = pending.pop()
            if isinstance(node, dict):
                if "additionalProperties" in node:
                    assert node["additionalProperties"] is False, name
                assert node != {}, name
                pending.extend(node.values())
            elif isinstance(node, list):
                pending.extend(node)


def test_task8_result_models_reject_wrong_scalar_types() -> None:
    common = importlib.import_module("fcp_mcp.result_models.common")
    fcpxml = importlib.import_module("fcp_mcp.result_models.fcpxml")
    reference = _reference_payload()
    receipt = _receipt_payload()
    cases = (
        (
            fcpxml.FCPXMLGenerationResult,
            {
                "project": "Project",
                "selected_clip_count": "2",
                "target_duration_seconds": 2.0,
                "destination": reference,
                "receipt": receipt,
            },
        ),
        (
            fcpxml.SubtitleImportResult,
            {
                "cue_count": "2",
                "destination": reference,
                "receipt": receipt,
            },
        ),
        (
            fcpxml.EDLImportResult,
            {
                "event_count": "2",
                "destination": reference,
                "receipt": receipt,
            },
        ),
        (
            fcpxml.FCPXMLMutationResult,
            {
                "source_version": "1.11",
                "target_version": "1.11",
                "target_width": "1080",
                "target_height": 1920,
                "target_format_name": "Vertical",
                "destination": reference,
                "receipt": receipt,
            },
        ),
        (
            fcpxml.CleanupMutationResult,
            {
                "action": "fill_gaps",
                "changed_count": "1",
                "destination": reference,
                "receipt": receipt,
            },
        ),
        (
            fcpxml.ClipBatchMutationResult,
            {
                "operation": "rename",
                "changed_count": "1",
                "pattern": "A",
                "replacement": "B",
                "destination": reference,
                "receipt": receipt,
            },
        ),
        (
            fcpxml.RoleBatchMutationResult,
            {
                "rules": [{"match": "A", "role": "B"}],
                "changed_count": "1",
                "destination": reference,
                "receipt": receipt,
            },
        ),
        (
            fcpxml.TransitionBatchMutationResult,
            {
                "name": "Dissolve",
                "duration": "1s",
                "changed_count": "1",
                "destination": reference,
                "receipt": receipt,
            },
        ),
    )
    for model, payload in cases:
        with pytest.raises(ValueError):
            model.model_validate(payload)

    assert common.ArtifactReference.model_config["strict"] is True
    with pytest.raises(ValueError):
        common.ArtifactReference(
            path="/tmp/artifact",
            media_type="application/xml",
            sha256="0" * 64,
            size_bytes="1",
        )
    assert fcpxml.TransactionReceiptResult.model_config["strict"] is True
    with pytest.raises(ValueError):
        fcpxml.TransactionReceiptResult.model_validate(
            {**receipt, "elapsed_ms": "1"}
        )


def test_export_result_requires_receipt() -> None:
    models = importlib.import_module("fcp_mcp.result_models.fcpxml")
    with pytest.raises(ValueError):
        models.ExportResult(
            format="fcp7",
            source=_reference_payload(),
            artifact={
                **_reference_payload(),
                "media_type": "application/xml",
            },
            receipt=None,
        )


@pytest.mark.parametrize(
    "payload",
    (
        {
            "operation": "rename",
            "changed_count": 1,
            "pattern": "A",
        },
        {
            "operation": "rename",
            "changed_count": 1,
            "pattern": "A",
            "replacement": "B",
            "requested_names": ["A"],
        },
        {
            "operation": "delete",
            "changed_count": 1,
            "requested_names": ["A"],
            "deleted_names": ["A"],
            "requested_order": [],
            "resulting_order": [],
        },
        {
            "operation": "delete",
            "requested_count": 1,
            "changed_count": 1,
            "requested_names": ["A"],
            "deleted_names": ["A"],
            "requested_order": [],
            "resulting_order": [],
            "pattern": "A",
        },
        {
            "operation": "delete",
            "requested_count": 1,
            "changed_count": 1,
            "requested_names": ["A"],
            "deleted_names": ["A"],
            "requested_order": ["A"],
            "resulting_order": [],
        },
        {
            "operation": "reorder",
            "requested_count": 1,
            "changed_count": 1,
            "requested_names": ["A"],
            "deleted_names": [],
            "requested_order": ["A"],
        },
        {
            "operation": "reorder",
            "requested_count": 1,
            "changed_count": 1,
            "requested_names": ["A"],
            "deleted_names": ["A"],
            "requested_order": ["A"],
            "resulting_order": ["A"],
        },
    ),
)
def test_clip_batch_result_rejects_impossible_operation_fields(
    payload: dict[str, object],
) -> None:
    models = importlib.import_module("fcp_mcp.result_models.fcpxml")
    with pytest.raises(ValueError):
        models.ClipBatchMutationResult.model_validate(
            {
                **payload,
                "destination": _reference_payload(),
                "receipt": _receipt_payload(),
            }
        )


@pytest.mark.parametrize(
    "media_type",
    (
        "",
        "application",
        "/xml",
        "application/",
        "application /xml",
        f"application/{'x' * 128}",
    ),
)
def test_artifact_reference_rejects_invalid_or_unbounded_media_types(
    media_type: str,
) -> None:
    common = importlib.import_module("fcp_mcp.result_models.common")
    with pytest.raises(ValueError):
        common.ArtifactReference(
            path="/tmp/artifact",
            media_type=media_type,
            sha256="0" * 64,
            size_bytes=0,
        )


@pytest.mark.parametrize(
    "media_type",
    (
        "application/vnd.apple.fcpxml+xml",
        "application/xml",
        "text/x-cmx3600",
    ),
)
def test_artifact_reference_accepts_bounded_specific_mime_types(
    media_type: str,
) -> None:
    common = importlib.import_module("fcp_mcp.result_models.common")
    artifact = common.ArtifactReference(
        path="/tmp/artifact",
        media_type=media_type,
        sha256="0" * 64,
        size_bytes=0,
    )
    assert artifact.media_type == media_type
