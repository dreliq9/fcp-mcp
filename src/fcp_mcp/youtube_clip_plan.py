"""Import youtube-mcp materialized clip plans into native FCPXML timelines.

The adapter deliberately consumes the editor-neutral handoff contract rather than
calling youtube-mcp or downloading media itself. Source acquisition remains owned
by youtube-mcp; FCP-MCP owns local path policy, FCPXML generation, and edit review.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
import uuid
from pathlib import Path
from typing import Any

from .contracts import ErrorCode, FCPMCPError
from .fcpxml.time_utils import RationalTime
from .fcpxml.transaction import FCPXMLTransactionReceipt
from .fcpxml.validator import FCPXMLValidator, ValidationResult
from .observability import emit_event
from .profiles import ToolClass
from .result_models.common import ArtifactReference
from .result_models.fcpxml import TransactionReceiptResult
from .result_models.handoff import YouTubeClipPlanGenerationResult
from .tool_metadata import OFFLINE_WRITE
from .utils.atomic_write import AtomicBundleItem, atomic_replace_bundle

MATERIALIZED_SCHEMA = "youtube-mcp.materialized-clip-plan/v1"
PROVENANCE_SCHEMA = "fcp-mcp.youtube-mcp-provenance/v1"
_ALLOWED_MEDIA_SUFFIXES = {".mp4", ".mov", ".m4v"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_manifest(
    runtime: Any,
    manifest_path: str,
) -> tuple[Path, dict[str, Any], str]:
    path = runtime._resolve_input(
        manifest_path,
        suffixes={".json"},
        kind="youtube-mcp materialized clip-plan manifest",
    )
    try:
        manifest_bytes = path.read_bytes()
        data = json.loads(manifest_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FCPMCPError(
            ErrorCode.VALIDATION_FAILED,
            "youtube-mcp materialized clip-plan manifest is unreadable",
        ) from exc
    if not isinstance(data, dict):
        raise FCPMCPError(
            ErrorCode.VALIDATION_FAILED,
            "youtube-mcp materialized clip-plan manifest root must be an object",
        )
    if data.get("schema") != MATERIALIZED_SCHEMA:
        raise FCPMCPError(
            ErrorCode.UNSUPPORTED_CONTRACT,
            f"Expected {MATERIALIZED_SCHEMA!r}, got {data.get('schema')!r}",
        )
    assets = data.get("assets")
    if not isinstance(assets, list) or not assets:
        raise FCPMCPError(
            ErrorCode.VALIDATION_FAILED,
            "youtube-mcp materialized clip-plan contains no assets",
        )
    return path, data, hashlib.sha256(manifest_bytes).hexdigest()


def _validation_error(message: str, **details: Any) -> FCPMCPError:
    return FCPMCPError(ErrorCode.VALIDATION_FAILED, message, details or None)


def _validate_fcpxml_candidate(
    path: Path,
    validator: FCPXMLValidator,
) -> ValidationResult:
    result = validator.validate_file(path)
    if not result.valid:
        raise _validation_error(
            f"Generated FCPXML did not pass structural validation: {result.summary()}",
            issues=[
                {
                    "severity": issue.severity,
                    "message": issue.message,
                    "location": issue.location,
                }
                for issue in result.issues
            ],
        )
    return result


def _encode_provenance(provenance: dict[str, Any]) -> bytes:
    return (json.dumps(provenance, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _validate_provenance_candidate(
    path: Path,
    expected: dict[str, Any],
) -> None:
    try:
        candidate = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise _validation_error("Generated provenance sidecar is not valid JSON") from error
    if candidate != expected:
        raise _validation_error(
            "Generated provenance sidecar does not match its bound FCPXML attempt"
        )


def _emit_event_best_effort(event: dict[str, Any], *, format: str) -> None:
    try:
        emit_event(event, format=format)
    except Exception:  # noqa: BLE001 - logging cannot invalidate a committed pair
        # Observability must not turn a committed artifact pair into an error.
        return


def _inspect_existing_bundle(
    destination: Path,
    provenance_path: Path,
    fcpxml_sha256: str,
    provenance_without_transaction: dict[str, Any],
) -> tuple[str | None, tuple[str | None, str | None]]:
    destination_sha256 = None
    provenance_sha256 = None
    existing_bytes: bytes | None = None
    try:
        if not destination.is_symlink() and destination.is_file():
            destination_sha256 = _sha256(destination)
        if not provenance_path.is_symlink() and provenance_path.is_file():
            existing_bytes = provenance_path.read_bytes()
            provenance_sha256 = hashlib.sha256(existing_bytes).hexdigest()
    except OSError:
        destination_sha256 = None
        provenance_sha256 = None
        existing_bytes = None
    snapshot = (destination_sha256, provenance_sha256)
    if destination_sha256 != fcpxml_sha256 or existing_bytes is None:
        return None, snapshot
    try:
        existing = json.loads(existing_bytes.decode("utf-8"))
        transaction_id = str(existing.pop("transaction_id"))
        canonical_transaction_id = str(uuid.UUID(transaction_id))
    except (
        AttributeError,
        KeyError,
        OSError,
        TypeError,
        UnicodeDecodeError,
        ValueError,
        json.JSONDecodeError,
    ):
        return None, snapshot
    if transaction_id != canonical_transaction_id or existing != provenance_without_transaction:
        return None, snapshot
    expected = dict(provenance_without_transaction)
    expected["transaction_id"] = canonical_transaction_id
    if existing_bytes != _encode_provenance(expected):
        return None, snapshot
    return canonical_transaction_id, snapshot


def _validated_assets(
    runtime: Any,
    manifest: dict[str, Any],
) -> list[dict[str, Any]]:
    assets: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(manifest.get("assets") or [], start=1):
        if not isinstance(raw, dict):
            raise FCPMCPError(
                ErrorCode.VALIDATION_FAILED,
                f"Materialized asset {index} is not an object",
            )
        clip_id = str(raw.get("clip_id") or "")
        if not clip_id:
            raise FCPMCPError(
                ErrorCode.VALIDATION_FAILED,
                f"Materialized asset {index} is missing clip_id",
            )
        if clip_id in seen:
            raise FCPMCPError(
                ErrorCode.VALIDATION_FAILED,
                f"Duplicate materialized clip_id: {clip_id}",
            )
        seen.add(clip_id)

        media_path = runtime._resolve_input(
            str(raw.get("path") or ""),
            suffixes=_ALLOWED_MEDIA_SUFFIXES,
            kind=f"materialized clip {clip_id}",
        )
        try:
            duration_s = float(raw.get("duration_s"))
        except (TypeError, ValueError) as exc:
            raise FCPMCPError(
                ErrorCode.VALIDATION_FAILED,
                f"Materialized clip {clip_id} has invalid duration_s",
            ) from exc
        if not math.isfinite(duration_s) or duration_s <= 0:
            raise FCPMCPError(
                ErrorCode.VALIDATION_FAILED,
                f"Materialized clip {clip_id} duration_s must be finite and positive",
            )

        expected_sha = str(raw.get("sha256") or "")
        if len(expected_sha) != 64 or any(
            char not in "0123456789abcdefABCDEF" for char in expected_sha
        ):
            raise FCPMCPError(
                ErrorCode.ARTIFACT_CORRUPT,
                f"Materialized clip {clip_id} has no valid SHA-256",
            )
        actual_sha = _sha256(media_path)
        if actual_sha != expected_sha.lower():
            raise FCPMCPError(
                ErrorCode.ARTIFACT_CORRUPT,
                f"Materialized clip {clip_id} SHA-256 does not match its handoff manifest",
                details={
                    "clip_id": clip_id,
                    "expected_sha256": expected_sha,
                    "actual_sha256": actual_sha,
                },
            )

        item = dict(raw)
        item["clip_id"] = clip_id
        item["path"] = str(media_path)
        item["duration_s"] = duration_s
        assets.append(item)
    return assets


def register_youtube_clip_plan_tool(runtime: Any) -> None:
    """Register the handoff tool onto an already-created runtime ToolRegistry."""
    if "fcpxml_generate_from_clip_plan" in runtime.TOOLS.definitions:
        return

    @runtime.TOOLS.tool(
        tool_class=ToolClass.OFFLINE_WRITE,
        safety_hints=OFFLINE_WRITE,
        result_model=YouTubeClipPlanGenerationResult,
    )
    def fcpxml_generate_from_clip_plan(
        manifest_path: str,
        project_name: str = "YouTube Remix",
        output_path: str = "",
    ) -> YouTubeClipPlanGenerationResult:
        """Generate FCPXML from a youtube-mcp materialized clip-plan manifest.

        The source clips must already have been materialized by youtube-mcp. FCP-MCP
        revalidates every local path through its normal path policy and verifies
        SHA-256 identity. Clips are placed in manifest order and remain
        full-duration local assets because youtube-mcp already applied source trims.
        A `.sources.json` sidecar preserves original YouTube URLs/time ranges.
        """
        started = time.perf_counter()
        source_manifest, manifest, source_manifest_sha256 = _load_manifest(
            runtime,
            manifest_path,
        )
        assets = _validated_assets(runtime, manifest)

        normalized_project = project_name.strip()
        if not normalized_project:
            raise FCPMCPError(
                ErrorCode.INVALID_ARGUMENTS,
                "project_name must not be empty",
            )

        destination = runtime._resolve_output(
            output_path,
            default_name="youtube_remix.fcpxml",
            suffixes={".fcpxml"},
        )

        clips: list[dict[str, Any]] = []
        total_duration = 0.0
        for asset in assets:
            duration = RationalTime.from_seconds(float(asset["duration_s"])).to_fcpxml()
            total_duration += float(asset["duration_s"])
            clips.append(
                {
                    "src": asset["path"],
                    "name": str(asset.get("title") or asset["clip_id"]),
                    "start": "0s",
                    "duration": duration,
                    "has_video": True,
                    "has_audio": True,
                }
            )

        generator = runtime.FCPXMLGenerator()
        generator.build_timeline_from_clips(
            clips,
            project_name=normalized_project,
            event_name="YouTube MCP Handoff",
        )
        fcpxml_bytes = f"{generator.to_string()}\n".encode()
        fcpxml_sha256 = hashlib.sha256(fcpxml_bytes).hexdigest()
        provenance_path = destination.with_name(destination.stem + ".sources.json")
        provenance_without_transaction = {
            "schema": PROVENANCE_SCHEMA,
            "project": normalized_project,
            "fcpxml_path": str(destination),
            "fcpxml_sha256": fcpxml_sha256,
            "source_manifest_path": str(source_manifest),
            "source_manifest_sha256": source_manifest_sha256,
            "source_manifest_schema": manifest.get("schema"),
            "plan_revision": manifest.get("plan_revision"),
            "materialization_revision": manifest.get("materialization_revision"),
            "hashes_verified": True,
            "timeline_order": [asset["clip_id"] for asset in assets],
            "sources": [
                {
                    key: asset.get(key)
                    for key in (
                        "clip_id",
                        "video_id",
                        "title",
                        "channel",
                        "source_url",
                        "source_start_s",
                        "source_end_s",
                        "duration_s",
                        "sha256",
                        "path",
                    )
                }
                for asset in assets
            ],
        }
        existing_transaction_id, expected_prior_sha256 = _inspect_existing_bundle(
            destination,
            provenance_path,
            fcpxml_sha256,
            provenance_without_transaction,
        )
        transaction_id = existing_transaction_id or str(uuid.uuid4())
        provenance = dict(provenance_without_transaction)
        provenance["transaction_id"] = transaction_id
        provenance_bytes = _encode_provenance(provenance)
        validator = FCPXMLValidator()
        latest_validation: ValidationResult | None = None

        def validate_fcpxml(path: Path) -> None:
            nonlocal latest_validation
            latest_validation = _validate_fcpxml_candidate(path, validator)

        def validate_provenance(path: Path) -> None:
            _validate_provenance_candidate(path, provenance)

        try:
            bundle_receipt = atomic_replace_bundle(
                (
                    AtomicBundleItem(destination, fcpxml_bytes, validate_fcpxml),
                    AtomicBundleItem(
                        provenance_path,
                        provenance_bytes,
                        validate_provenance,
                    ),
                ),
                event_format=runtime.CONFIG.log_format,
                transaction_id=transaction_id,
                expected_prior_sha256=expected_prior_sha256,
            )
        except FCPMCPError as error:
            _emit_event_best_effort(
                {
                    "event": "fcpxml_transaction",
                    "operation": "youtube_clip_plan_bundle",
                    "transaction_id": transaction_id,
                    "input_path": str(source_manifest),
                    "output_path": str(destination),
                    "input_sha256": source_manifest_sha256,
                    "validation_outcome": "failed",
                    "elapsed_ms": round((time.perf_counter() - started) * 1000),
                    "disposition": "failed",
                    "error_code": error.code.value,
                },
                format=runtime.CONFIG.log_format,
            )
            raise
        primary = bundle_receipt.items[0]
        sidecar = bundle_receipt.items[1]
        receipt = FCPXMLTransactionReceipt(
            transaction_id=bundle_receipt.transaction_id,
            source=None,
            destination=primary.destination,
            backup_path=primary.backup_path,
            input_sha256=None,
            prior_sha256=primary.prior_sha256,
            output_sha256=primary.output_sha256,
            validation_warnings=tuple(
                issue.message
                for issue in (latest_validation.issues if latest_validation else ())
                if issue.severity == "warning"
            ),
            elapsed_ms=round((time.perf_counter() - started) * 1000),
        )
        _emit_event_best_effort(
            {
                "event": "fcpxml_transaction",
                "operation": "youtube_clip_plan_bundle",
                "transaction_id": receipt.transaction_id,
                "input_path": str(source_manifest),
                "output_path": str(receipt.destination),
                "input_sha256": source_manifest_sha256,
                "prior_sha256": receipt.prior_sha256,
                "output_sha256": receipt.output_sha256,
                "backup_path": str(receipt.backup_path) if receipt.backup_path else None,
                "validation_outcome": "valid",
                "elapsed_ms": receipt.elapsed_ms,
                "disposition": receipt.disposition,
                "bundle_replayed": bundle_receipt.replayed,
            },
            format=runtime.CONFIG.log_format,
        )

        return YouTubeClipPlanGenerationResult(
            project=normalized_project,
            selected_clip_count=len(assets),
            target_duration_seconds=total_duration,
            plan_revision=(
                str(manifest.get("plan_revision"))
                if manifest.get("plan_revision") is not None
                else None
            ),
            materialization_revision=(
                str(manifest.get("materialization_revision"))
                if manifest.get("materialization_revision") is not None
                else None
            ),
            hashes_verified=True,
            destination=ArtifactReference(
                path=str(primary.destination),
                media_type="application/vnd.apple.fcpxml+xml",
                sha256=primary.output_sha256,
                size_bytes=primary.output_size_bytes,
            ),
            provenance=ArtifactReference(
                path=str(sidecar.destination),
                media_type="application/json",
                sha256=sidecar.output_sha256,
                size_bytes=sidecar.output_size_bytes,
            ),
            receipt=TransactionReceiptResult(
                transaction_id=receipt.transaction_id,
                source=None,
                destination=str(receipt.destination),
                backup_path=(str(receipt.backup_path) if receipt.backup_path else None),
                input_sha256=None,
                prior_sha256=receipt.prior_sha256,
                output_sha256=receipt.output_sha256,
                validation_warnings=list(receipt.validation_warnings),
                elapsed_ms=receipt.elapsed_ms,
                disposition=receipt.disposition,
            ),
        )
