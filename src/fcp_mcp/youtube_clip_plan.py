"""Import youtube-mcp materialized clip plans into native FCPXML timelines.

The adapter deliberately consumes the editor-neutral handoff contract rather than
calling youtube-mcp or downloading media itself. Source acquisition remains owned
by youtube-mcp; FCP-MCP owns local path policy, FCPXML generation, and edit review.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .contracts import ErrorCode, FCPMCPError
from .fcpxml.time_utils import RationalTime
from .profiles import ToolClass
from .result_models.handoff import YouTubeClipPlanGenerationResult
from .tool_metadata import OFFLINE_WRITE

MATERIALIZED_SCHEMA = "youtube-mcp.materialized-clip-plan/v1"
PROVENANCE_SCHEMA = "fcp-mcp.youtube-mcp-provenance/v1"
_ALLOWED_MEDIA_SUFFIXES = {".mp4", ".mov", ".m4v"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_manifest(runtime: Any, manifest_path: str) -> tuple[Path, dict[str, Any]]:
    path = runtime._resolve_input(
        manifest_path,
        suffixes={".json"},
        kind="youtube-mcp materialized clip-plan manifest",
    )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FCPMCPError(
            ErrorCode.VALIDATION_FAILED,
            "youtube-mcp materialized clip-plan manifest is unreadable",
        ) from exc
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
    return path, data


def _validated_assets(
    runtime: Any,
    manifest: dict[str, Any],
    *,
    verify_hashes: bool,
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
        if duration_s <= 0:
            raise FCPMCPError(
                ErrorCode.VALIDATION_FAILED,
                f"Materialized clip {clip_id} duration_s must be positive",
            )

        expected_sha = str(raw.get("sha256") or "")
        if verify_hashes:
            if len(expected_sha) != 64:
                raise FCPMCPError(
                    ErrorCode.ARTIFACT_CORRUPT,
                    f"Materialized clip {clip_id} has no valid SHA-256",
                )
            actual_sha = _sha256(media_path)
            if actual_sha != expected_sha:
                raise FCPMCPError(
                    ErrorCode.ARTIFACT_CORRUPT,
                    f"Materialized clip {clip_id} SHA-256 does not match its handoff manifest",
                    details={"clip_id": clip_id, "expected_sha256": expected_sha, "actual_sha256": actual_sha},
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
        verify_hashes: bool = True,
    ) -> YouTubeClipPlanGenerationResult:
        """Generate FCPXML from a youtube-mcp materialized clip-plan manifest.

        The source clips must already have been materialized by youtube-mcp. FCP-MCP
        revalidates every local path through its normal path policy and verifies
        SHA-256 identity by default. Clips are placed in manifest order and remain
        full-duration local assets because youtube-mcp already applied source trims.
        A `.sources.json` sidecar preserves original YouTube URLs/time ranges.
        """
        source_manifest, manifest = _load_manifest(runtime, manifest_path)
        assets = _validated_assets(runtime, manifest, verify_hashes=verify_hashes)

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
        receipt = generator.save_with_receipt(
            destination,
            event_format=runtime.CONFIG.log_format,
        )

        provenance_path = receipt.destination.with_name(
            receipt.destination.stem + ".sources.json"
        )
        provenance = {
            "schema": PROVENANCE_SCHEMA,
            "project": normalized_project,
            "fcpxml_path": str(receipt.destination),
            "source_manifest_path": str(source_manifest),
            "source_manifest_schema": manifest.get("schema"),
            "plan_revision": manifest.get("plan_revision"),
            "materialization_revision": manifest.get("materialization_revision"),
            "verify_hashes": verify_hashes,
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
        runtime.atomic_replace_bytes(
            provenance_path,
            (json.dumps(provenance, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
            event_format=runtime.CONFIG.log_format,
        )

        return YouTubeClipPlanGenerationResult(
            project=normalized_project,
            selected_clip_count=len(assets),
            target_duration_seconds=total_duration,
            plan_revision=(str(manifest.get("plan_revision")) if manifest.get("plan_revision") is not None else None),
            materialization_revision=(str(manifest.get("materialization_revision")) if manifest.get("materialization_revision") is not None else None),
            hashes_verified=verify_hashes,
            destination=runtime._artifact_reference(receipt),
            provenance=runtime._artifact_reference_for_path(
                provenance_path,
                media_type="application/json",
            ),
            receipt=runtime._receipt_result(receipt),
        )
