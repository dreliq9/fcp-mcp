from __future__ import annotations

import hashlib
import inspect
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from defusedxml import ElementTree

from fcp_mcp.contracts import ErrorCode, FCPMCPError
from fcp_mcp.fcpxml.generator import FCPXMLGenerator
from fcp_mcp.registry import ToolRegistry
from fcp_mcp.result_models.common import ArtifactReference
from fcp_mcp.result_models.fcpxml import TransactionReceiptResult
from fcp_mcp.utils.atomic_write import atomic_replace_bytes
from fcp_mcp.youtube_clip_plan import (
    MATERIALIZED_SCHEMA,
    PROVENANCE_SCHEMA,
    register_youtube_clip_plan_tool,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _runtime(tmp_path: Path):
    def resolve_input(path: str, *, suffixes=(), kind="file") -> Path:
        resolved = Path(path).resolve()
        if not resolved.exists() or not resolved.is_file():
            raise FCPMCPError(ErrorCode.SOURCE_NOT_FOUND, f"{kind} not found")
        if suffixes and resolved.suffix.lower() not in suffixes:
            raise FCPMCPError(ErrorCode.INVALID_PATH, "bad suffix")
        return resolved

    def resolve_output(
        output_path: str,
        *,
        input_path=None,
        default_name=None,
        suffixes=(),
    ) -> Path:
        destination = (
            Path(output_path) if output_path else tmp_path / str(default_name)
        ).resolve()
        if suffixes and destination.suffix.lower() not in suffixes:
            raise FCPMCPError(ErrorCode.INVALID_PATH, "bad output suffix")
        destination.parent.mkdir(parents=True, exist_ok=True)
        return destination

    def artifact_for_path(path: Path, *, media_type: str) -> ArtifactReference:
        resolved = Path(path).resolve()
        return ArtifactReference(
            path=str(resolved),
            media_type=media_type,
            sha256=_sha256(resolved),
            size_bytes=resolved.stat().st_size,
        )

    def artifact_for_receipt(receipt) -> ArtifactReference:
        return artifact_for_path(
            receipt.destination,
            media_type="application/vnd.apple.fcpxml+xml",
        )

    def receipt_result(receipt) -> TransactionReceiptResult:
        return TransactionReceiptResult(
            transaction_id=receipt.transaction_id,
            source=None,
            destination=str(receipt.destination),
            backup_path=str(receipt.backup_path) if receipt.backup_path else None,
            input_sha256=None,
            prior_sha256=receipt.prior_sha256,
            output_sha256=receipt.output_sha256,
            validation_warnings=list(receipt.validation_warnings),
            elapsed_ms=receipt.elapsed_ms,
            disposition="committed",
        )

    runtime = SimpleNamespace(
        TOOLS=ToolRegistry(),
        CONFIG=SimpleNamespace(log_format="text"),
        FCPXMLGenerator=FCPXMLGenerator,
        atomic_replace_bytes=atomic_replace_bytes,
        _resolve_input=resolve_input,
        _resolve_output=resolve_output,
        _artifact_reference=artifact_for_receipt,
        _artifact_reference_for_path=artifact_for_path,
        _receipt_result=receipt_result,
    )
    register_youtube_clip_plan_tool(runtime)
    return runtime


def _manifest(tmp_path: Path) -> Path:
    first = tmp_path / "clip-a.mp4"
    second = tmp_path / "clip-b.mp4"
    first.write_bytes(b"first-video")
    second.write_bytes(b"second-video")
    payload = {
        "schema": MATERIALIZED_SCHEMA,
        "plan_revision": "cp-aaaaaaaaaaaaaaaaaaaaaaaa",
        "materialization_revision": "cm-bbbbbbbbbbbbbbbbbbbbbbbb",
        "assets": [
            {
                "clip_id": "clip-001",
                "video_id": "jNQXAC9IVRw",
                "title": "History",
                "channel": "Historian",
                "source_url": "https://www.youtube.com/watch?v=jNQXAC9IVRw",
                "source_start_s": 98.0,
                "source_end_s": 108.0,
                "duration_s": 10.0,
                "path": str(first),
                "sha256": _sha256(first),
            },
            {
                "clip_id": "clip-002",
                "video_id": "dQw4w9WgXcQ",
                "title": "Artist",
                "channel": "Studio",
                "source_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                "source_start_s": 40.0,
                "source_end_s": 46.0,
                "duration_s": 6.0,
                "path": str(second),
                "sha256": _sha256(second),
            },
        ],
    }
    manifest = tmp_path / "handoff.json"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    return manifest


def test_handoff_generates_native_fcpxml_and_provenance(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    handler = runtime.TOOLS.definitions["fcpxml_generate_from_clip_plan"].handler
    result = handler(
        str(_manifest(tmp_path)),
        project_name="Frontier Montage",
        output_path=str(tmp_path / "frontier.fcpxml"),
    )
    assert result.selected_clip_count == 2
    assert result.target_duration_seconds == 16.0
    assert result.hashes_verified is True

    root = ElementTree.parse(result.destination.path).getroot()
    project = root.find(".//project")
    assert project is not None
    assert project.attrib["name"] == "Frontier Montage"
    clips = root.findall(".//asset-clip")
    assert [clip.attrib["name"] for clip in clips] == ["History", "Artist"]

    provenance = json.loads(Path(result.provenance.path).read_text())
    assert provenance["schema"] == PROVENANCE_SCHEMA
    assert provenance["hashes_verified"] is True
    assert "verify_hashes" not in provenance
    assert provenance["timeline_order"] == ["clip-001", "clip-002"]
    assert provenance["sources"][0]["source_start_s"] == 98.0


def test_handoff_rejects_tampered_materialized_media(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    handler = runtime.TOOLS.definitions["fcpxml_generate_from_clip_plan"].handler
    manifest = _manifest(tmp_path)
    payload = json.loads(manifest.read_text())
    Path(payload["assets"][0]["path"]).write_bytes(b"tampered")
    with pytest.raises(FCPMCPError) as exc:
        handler(str(manifest), output_path=str(tmp_path / "bad.fcpxml"))
    assert exc.value.code == ErrorCode.ARTIFACT_CORRUPT


def test_handoff_rejects_unknown_schema(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    handler = runtime.TOOLS.definitions["fcpxml_generate_from_clip_plan"].handler
    manifest = _manifest(tmp_path)
    payload = json.loads(manifest.read_text())
    payload["schema"] = "something-else/v1"
    manifest.write_text(json.dumps(payload))
    with pytest.raises(FCPMCPError) as exc:
        handler(str(manifest), output_path=str(tmp_path / "bad.fcpxml"))
    assert exc.value.code == ErrorCode.UNSUPPORTED_CONTRACT


@pytest.mark.parametrize("root", [[], None, "not-a-manifest", 3])
def test_handoff_rejects_non_object_manifest_roots(tmp_path: Path, root: object) -> None:
    runtime = _runtime(tmp_path)
    handler = runtime.TOOLS.definitions["fcpxml_generate_from_clip_plan"].handler
    manifest = tmp_path / "invalid-root.json"
    manifest.write_text(json.dumps(root), encoding="utf-8")

    with pytest.raises(FCPMCPError) as exc:
        handler(str(manifest), output_path=str(tmp_path / "bad.fcpxml"))

    assert exc.value.code == ErrorCode.VALIDATION_FAILED


def test_handoff_requires_a_hex_sha256_and_accepts_uppercase_digest(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    handler = runtime.TOOLS.definitions["fcpxml_generate_from_clip_plan"].handler
    manifest = _manifest(tmp_path)
    payload = json.loads(manifest.read_text())
    payload["assets"][0]["sha256"] = "g" * 64
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(FCPMCPError) as exc:
        handler(str(manifest), output_path=str(tmp_path / "bad.fcpxml"))

    assert exc.value.code == ErrorCode.ARTIFACT_CORRUPT
    assert "no valid SHA-256" in str(exc.value)

    payload["assets"][0]["sha256"] = _sha256(
        Path(payload["assets"][0]["path"])
    ).upper()
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    result = handler(str(manifest), output_path=str(tmp_path / "uppercase.fcpxml"))
    assert result.hashes_verified is True


def test_handoff_has_no_public_hash_verification_opt_out(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    handler = runtime.TOOLS.definitions["fcpxml_generate_from_clip_plan"].handler

    assert "verify_hashes" not in inspect.signature(handler).parameters


def test_registration_is_idempotent(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    register_youtube_clip_plan_tool(runtime)
    assert list(runtime.TOOLS.definitions).count("fcpxml_generate_from_clip_plan") == 1
