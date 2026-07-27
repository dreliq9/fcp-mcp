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
    assert set(tools[tool_name].outputSchema["properties"]) != {"result"}


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
    assert result.isError is False
    assert result.content[0].text == expected_text
    payload = result.structuredContent
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
    assert subtitle_result.structuredContent["cue_count"] == 2
    _assert_fcpxml_evidence(
        subtitle_result.structuredContent,
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
    assert edl_result.structuredContent["event_count"] == 1
    _assert_fcpxml_evidence(
        edl_result.structuredContent,
        edl_destination,
        source=None,
    )
    assert len(
        ET.parse(edl_destination).getroot().findall(".//spine/asset-clip")
    ) == 1


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
        key: reformatted.structuredContent[key]
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
        reformatted.structuredContent,
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
        assert result.structuredContent["action"] == action
        assert result.structuredContent["changed_count"] == changed_count
        _assert_fcpxml_evidence(
            result.structuredContent,
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
        "pattern": renamed.structuredContent["pattern"],
        "replacement": renamed.structuredContent["replacement"],
        "changed_count": renamed.structuredContent["changed_count"],
    } == {
        "pattern": "Broll",
        "replacement": "Scenic",
        "changed_count": 5,
    }
    _assert_fcpxml_evidence(
        renamed.structuredContent,
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
    assert assigned.structuredContent["rules"] == rules
    assert assigned.structuredContent["changed_count"] == 5
    _assert_fcpxml_evidence(
        assigned.structuredContent,
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
        "name": transitioned.structuredContent["name"],
        "duration": transitioned.structuredContent["duration"],
        "changed_count": transitioned.structuredContent["changed_count"],
    } == {
        "name": "Verified Dissolve",
        "duration": "6006/30000s",
        "changed_count": 3,
    }
    _assert_fcpxml_evidence(
        transitioned.structuredContent,
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
    assert result.structuredContent["template"] == _artifact_payload(destination)
    assert result.structuredContent["source_path"] == str(source.resolve())
    assert result.structuredContent["source_sha256"] == _sha256(source)
    assert destination.read_bytes() == source.read_bytes()
    assert result.structuredContent["source_sha256"] == _sha256(destination)
    backup_path = Path(result.structuredContent["backup_path"])
    assert backup_path.is_absolute()
    assert backup_path.read_bytes() == prior_bytes
    receipt = result.structuredContent["receipt"]
    assert receipt["source"] == str(source.resolve())
    assert receipt["destination"] == str(destination.resolve())
    assert receipt["input_sha256"] == _sha256(source)
    assert receipt["output_sha256"] == _sha256(destination)
    assert receipt["backup_path"] == str(backup_path)


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
    assert result.structuredContent["format"] == export_format
    assert result.structuredContent["source"] == _artifact_payload(source)
    assert result.structuredContent["artifact"] == _artifact_payload(
        destination,
        media_type,
    )
    if export_format == "resolve":
        assert result.structuredContent["receipt"] is not None
        assert result.structuredContent["receipt"]["source"] == str(
            source.resolve()
        )
        assert ET.parse(destination).getroot().get("version") == "1.9"
    else:
        assert result.structuredContent["receipt"] is None
        if export_format == "fcp7":
            assert ET.parse(destination).getroot().tag == "xmeml"
        else:
            assert destination.read_text(encoding="utf-8").startswith(
                "TITLE: Travel Vlog v1\nFCM: NON-DROP FRAME\n"
            )


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
