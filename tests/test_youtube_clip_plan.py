from __future__ import annotations

import hashlib
import inspect
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from defusedxml import ElementTree

from fcp_mcp.contracts import ErrorCode, FCPMCPError
from fcp_mcp.fcpxml.generator import FCPXMLGenerator
from fcp_mcp.registry import ToolRegistry
from fcp_mcp.result_models.common import ArtifactReference
from fcp_mcp.result_models.fcpxml import TransactionReceiptResult
from fcp_mcp.result_models.handoff import YouTubeClipPlanGenerationResult
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
        destination = (Path(output_path) if output_path else tmp_path / str(default_name)).resolve()
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
    assert provenance["transaction_id"] == result.receipt.transaction_id
    assert provenance["fcpxml_path"] == result.destination.path
    assert provenance["fcpxml_sha256"] == result.destination.sha256
    assert provenance["source_manifest_path"] == str(
        Path(result.provenance.path).with_name("handoff.json").resolve()
    )
    assert provenance["source_manifest_sha256"] == _sha256(Path(provenance["source_manifest_path"]))
    assert result.receipt.output_sha256 == result.destination.sha256


def test_handoff_overwrite_and_identical_retry_keep_one_bound_logical_effect(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    handler = runtime.TOOLS.definitions["fcpxml_generate_from_clip_plan"].handler
    manifest = _manifest(tmp_path)
    destination = tmp_path / "frontier.fcpxml"
    provenance_path = tmp_path / "frontier.sources.json"
    destination.write_bytes(b"old timeline")
    provenance_path.write_bytes(b'{"old": true}\n')

    first = handler(
        str(manifest),
        project_name="Frontier Montage",
        output_path=str(destination),
    )
    first_backups = sorted(tmp_path.glob("*.bak.*"))
    second = handler(
        str(manifest),
        project_name="Frontier Montage",
        output_path=str(destination),
    )

    assert first.receipt.transaction_id == second.receipt.transaction_id
    assert first.destination == second.destination
    assert first.provenance == second.provenance
    assert len(first_backups) == 2
    assert sorted(tmp_path.glob("*.bak.*")) == first_backups
    assert first.receipt.backup_path is not None
    assert Path(first.receipt.backup_path).read_bytes() == b"old timeline"
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    assert provenance["transaction_id"] == second.receipt.transaction_id
    assert provenance["fcpxml_sha256"] == _sha256(destination)


def test_staggered_identical_handoffs_commit_only_one_logical_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import fcp_mcp.youtube_clip_plan as clip_plan_module

    runtime = _runtime(tmp_path)
    handler = runtime.TOOLS.definitions["fcpxml_generate_from_clip_plan"].handler
    manifest = _manifest(tmp_path)
    destination = tmp_path / "frontier.fcpxml"
    real_replace = clip_plan_module.atomic_replace_bundle
    first_ready = threading.Event()
    release_first = threading.Event()
    call_lock = threading.Lock()
    call_count = 0

    def staggered_replace(*args, **kwargs):
        nonlocal call_count
        with call_lock:
            call_count += 1
            is_first = call_count == 1
        if is_first:
            first_ready.set()
            assert release_first.wait(timeout=5)
        return real_replace(*args, **kwargs)

    monkeypatch.setattr(clip_plan_module, "atomic_replace_bundle", staggered_replace)
    with ThreadPoolExecutor(max_workers=1) as executor:
        delayed = executor.submit(
            handler,
            str(manifest),
            project_name="Frontier Montage",
            output_path=str(destination),
        )
        assert first_ready.wait(timeout=5)
        winner = handler(
            str(manifest),
            project_name="Frontier Montage",
            output_path=str(destination),
        )
        release_first.set()
        with pytest.raises(FCPMCPError) as caught:
            delayed.result(timeout=5)

    assert caught.value.code is ErrorCode.IDEMPOTENCY_CONFLICT
    provenance = json.loads((tmp_path / "frontier.sources.json").read_text())
    assert provenance["transaction_id"] == winner.receipt.transaction_id
    assert len(list(tmp_path.glob("*.bak.*"))) == 0


def test_delayed_stale_handoff_cannot_overwrite_a_newer_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import fcp_mcp.youtube_clip_plan as clip_plan_module

    runtime = _runtime(tmp_path)
    handler = runtime.TOOLS.definitions["fcpxml_generate_from_clip_plan"].handler
    manifest = _manifest(tmp_path)
    destination = tmp_path / "frontier.fcpxml"
    real_replace = clip_plan_module.atomic_replace_bundle
    first_ready = threading.Event()
    release_first = threading.Event()
    call_lock = threading.Lock()
    call_count = 0

    def staggered_replace(*args, **kwargs):
        nonlocal call_count
        with call_lock:
            call_count += 1
            is_first = call_count == 1
        if is_first:
            first_ready.set()
            assert release_first.wait(timeout=5)
        return real_replace(*args, **kwargs)

    monkeypatch.setattr(clip_plan_module, "atomic_replace_bundle", staggered_replace)
    with ThreadPoolExecutor(max_workers=1) as executor:
        stale = executor.submit(
            handler,
            str(manifest),
            project_name="Stale Project",
            output_path=str(destination),
        )
        assert first_ready.wait(timeout=5)
        winner = handler(
            str(manifest),
            project_name="Winning Project",
            output_path=str(destination),
        )
        release_first.set()
        with pytest.raises(FCPMCPError) as caught:
            stale.result(timeout=5)

    assert caught.value.code is ErrorCode.IDEMPOTENCY_CONFLICT
    project = ElementTree.parse(destination).getroot().find(".//project")
    assert project is not None
    assert project.attrib["name"] == "Winning Project"
    provenance = json.loads((tmp_path / "frontier.sources.json").read_text())
    assert provenance["transaction_id"] == winner.receipt.transaction_id


def test_result_evidence_cannot_mix_with_a_later_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import fcp_mcp.youtube_clip_plan as clip_plan_module

    runtime = _runtime(tmp_path)
    handler = runtime.TOOLS.definitions["fcpxml_generate_from_clip_plan"].handler
    manifest = _manifest(tmp_path)
    destination = tmp_path / "frontier.fcpxml"
    real_emit = clip_plan_module.emit_event
    first_event = threading.Event()
    release_first = threading.Event()
    event_lock = threading.Lock()
    blocked = False

    def staggered_emit(event, *, format):
        nonlocal blocked
        should_block = False
        if event.get("validation_outcome") == "valid":
            with event_lock:
                if not blocked:
                    blocked = True
                    should_block = True
        if should_block:
            first_event.set()
            assert release_first.wait(timeout=5)
        return real_emit(event, format=format)

    monkeypatch.setattr(clip_plan_module, "emit_event", staggered_emit)
    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(
            handler,
            str(manifest),
            project_name="First Project",
            output_path=str(destination),
        )
        assert first_event.wait(timeout=5)
        second = handler(
            str(manifest),
            project_name="Second Project",
            output_path=str(destination),
        )
        release_first.set()
        first_result = first.result(timeout=5)

    assert first_result.receipt.transaction_id != second.receipt.transaction_id
    assert first_result.destination.sha256 == first_result.receipt.output_sha256
    assert first_result.destination.sha256 != second.destination.sha256


def test_success_event_failure_does_not_turn_a_committed_bundle_into_an_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import fcp_mcp.youtube_clip_plan as clip_plan_module

    runtime = _runtime(tmp_path)
    handler = runtime.TOOLS.definitions["fcpxml_generate_from_clip_plan"].handler
    destination = tmp_path / "frontier.fcpxml"

    def fail_event(*_args, **_kwargs) -> None:
        raise OSError("event sink unavailable")

    monkeypatch.setattr(clip_plan_module, "emit_event", fail_event)
    result = handler(
        str(_manifest(tmp_path)),
        project_name="Frontier Montage",
        output_path=str(destination),
    )

    assert result.destination.sha256 == result.receipt.output_sha256
    assert destination.exists()
    assert (tmp_path / "frontier.sources.json").exists()


def test_invalid_sidecar_candidate_preserves_existing_output_pair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import fcp_mcp.youtube_clip_plan as clip_plan_module

    runtime = _runtime(tmp_path)
    handler = runtime.TOOLS.definitions["fcpxml_generate_from_clip_plan"].handler
    destination = tmp_path / "frontier.fcpxml"
    provenance_path = tmp_path / "frontier.sources.json"
    destination.write_bytes(b"old timeline")
    provenance_path.write_bytes(b'{"old": true}\n')
    monkeypatch.setattr(
        clip_plan_module,
        "_encode_provenance",
        lambda _provenance: b"not-json",
    )

    with pytest.raises(FCPMCPError) as caught:
        handler(
            str(_manifest(tmp_path)),
            output_path=str(destination),
        )

    assert caught.value.code is ErrorCode.VALIDATION_FAILED
    assert destination.read_bytes() == b"old timeline"
    assert provenance_path.read_bytes() == b'{"old": true}\n'


def test_handoff_failure_emits_the_standard_transaction_failure_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import fcp_mcp.youtube_clip_plan as clip_plan_module

    runtime = _runtime(tmp_path)
    runtime.CONFIG.log_format = "json"
    handler = runtime.TOOLS.definitions["fcpxml_generate_from_clip_plan"].handler
    monkeypatch.setattr(
        clip_plan_module,
        "_encode_provenance",
        lambda _provenance: b"not-json",
    )

    with pytest.raises(FCPMCPError):
        handler(
            str(_manifest(tmp_path)),
            output_path=str(tmp_path / "frontier.fcpxml"),
        )

    events = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    transaction_events = [event for event in events if event["event"] == "fcpxml_transaction"]
    assert len(transaction_events) == 1
    assert transaction_events[0]["operation"] == "youtube_clip_plan_bundle"
    assert transaction_events[0]["validation_outcome"] == "failed"
    assert transaction_events[0]["disposition"] == "failed"
    assert transaction_events[0]["error_code"] == ErrorCode.VALIDATION_FAILED.value


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

    payload["assets"][0]["sha256"] = _sha256(Path(payload["assets"][0]["path"])).upper()
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    result = handler(str(manifest), output_path=str(tmp_path / "uppercase.fcpxml"))
    assert result.hashes_verified is True


def test_handoff_has_no_public_hash_verification_opt_out(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    handler = runtime.TOOLS.definitions["fcpxml_generate_from_clip_plan"].handler

    assert "verify_hashes" not in inspect.signature(handler).parameters


def test_handoff_preserves_public_signature_and_result_schema(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    handler = runtime.TOOLS.definitions["fcpxml_generate_from_clip_plan"].handler
    signature = inspect.signature(handler)

    assert list(signature.parameters) == [
        "manifest_path",
        "project_name",
        "output_path",
    ]
    assert signature.parameters["manifest_path"].default is inspect.Parameter.empty
    assert signature.parameters["project_name"].default == "YouTube Remix"
    assert signature.parameters["output_path"].default == ""
    assert set(YouTubeClipPlanGenerationResult.model_fields) == {
        "schema_version",
        "project",
        "selected_clip_count",
        "target_duration_seconds",
        "plan_revision",
        "materialization_revision",
        "hashes_verified",
        "destination",
        "provenance",
        "receipt",
    }


def test_registration_is_idempotent(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    register_youtube_clip_plan_tool(runtime)
    assert list(runtime.TOOLS.definitions).count("fcpxml_generate_from_clip_plan") == 1
