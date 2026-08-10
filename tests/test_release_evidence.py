from __future__ import annotations

import hashlib
import json
import re
import struct
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROVENANCE_PATH = ROOT / "docs/releases/v0.3.0-provenance.json"
NOTE_PATH = ROOT / "docs/releases/v0.3.0.md"
SHA256_RE = re.compile(r"[0-9a-f]{64}")


def _provenance() -> dict[str, object]:
    return json.loads(PROVENANCE_PATH.read_text(encoding="utf-8"))


def _assert_sha256(value: object) -> str:
    assert isinstance(value, str)
    assert SHA256_RE.fullmatch(value)
    return value


def test_release_provenance_binds_one_installed_execution() -> None:
    provenance = _provenance()
    note = NOTE_PATH.read_text(encoding="utf-8")

    runtime = provenance["installed_executions"]["prior_workflow_smoke"]
    assert isinstance(runtime, dict)
    for field in (
        "python_executable",
        "console_script",
        "package_import",
        "cwd",
        "installed_wheel_path",
    ):
        path = Path(runtime[field])
        assert path.is_absolute(), field
        assert not path.is_relative_to(ROOT), field
    _assert_sha256(runtime["installed_wheel_sha256"])

    trajectory = provenance["trajectory"]
    assert isinstance(trajectory, dict)
    assert trajectory["candidate_sha256"] == trajectory["output_sha256"]
    for field in (
        "source_sha256",
        "plan_sha256",
        "candidate_sha256",
        "output_sha256",
        "diff_sha256",
    ):
        value = _assert_sha256(trajectory[field])
        assert value in note
    assert trajectory["run_id"] in note

    receipt = trajectory["receipt"]
    assert isinstance(receipt, dict)
    logical = _assert_sha256(receipt["logical_sha256"])
    artifact = _assert_sha256(receipt["artifact_sha256"])
    ledger = _assert_sha256(receipt["ledger_receipt_sha256"])
    assert logical != artifact
    assert artifact == ledger
    assert logical in note
    assert artifact in note


def test_release_provenance_binds_installed_generated_live_import_and_image() -> None:
    provenance = _provenance()
    note = NOTE_PATH.read_text(encoding="utf-8")
    generation = provenance["installed_generation"]
    assert isinstance(generation, dict)
    assert generation["source_commit"] == provenance["evidence_identity"][
        "fixed_head_source_commit"
    ]
    assert generation["tool"] == "fcpxml_create_timeline"
    assert generation["stdio_argv"] == [
        provenance["installed_executions"]["fixed_head_generation"][
            "console_script"
        ]
    ]
    clips = json.loads(generation["arguments"]["clips_json"])
    assert clips == [
        {
            "src": "/Volumes/Extreme SSD/GHG/P1010413.MP4",
            "name": "GHG P1010413 five-second evidence",
            "start": "900900/30000s",
            "duration": "150150/30000s",
            "asset_duration": "5735730/30000s",
            "role": "Dialogue",
        }
    ]

    live_import = provenance["live_import"]
    assert isinstance(live_import, dict)
    assert live_import["asset_duration"] == "5735730/30000s"
    assert live_import["source_start"] == "900900/30000s"
    assert live_import["clip_duration"] == "150150/30000s"

    generated_xml = live_import["installed_generated_fcpxml"]
    assert isinstance(generated_xml, dict)
    assert Path(generated_xml["path"]).is_absolute()
    generated_hash = _assert_sha256(generated_xml["sha256"])
    assert generated_hash == generation["output_sha256"]
    assert generated_hash in note

    dtd = live_import["dtd_validation"]
    assert isinstance(dtd, dict)
    assert Path(dtd["dtd_path"]).is_absolute()
    assert dtd["result"] == "passed"
    assert dtd["authoritative_gate"] == "scripts/apple_dtd_gate.py (lxml)"

    observed = live_import["operator_observation"]
    assert isinstance(observed, dict)
    assert observed["library"] == "v0.3-installed-generated-evidence.fcpbundle"
    assert observed["event"] == "v0.3 Fixed Head Evidence"
    assert observed["project"] == "v0.3 Installed Generated Evidence"
    assert observed["import_warning"] == "none observed"
    assert observed["visible_clip_duration"] == "00:00:05:00"
    assert observed["visible_role"] == "Dialogue-1"

    managed_copy = live_import["managed_media_copy"]
    source = provenance["media"]
    assert managed_copy["size_bytes"] == source["size_bytes"]
    assert managed_copy["sha256"] == source["sha256"]
    assert "not copied source" not in note
    assert "managed library copy" in note

    image = provenance["screenshot"]
    assert isinstance(image, dict)
    image_path = ROOT / image["path"]
    image_bytes = image_path.read_bytes()
    assert image_bytes[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", image_bytes[16:24])
    assert [width, height] == image["dimensions_px"]
    assert hashlib.sha256(image_bytes).hexdigest() == image["sha256"]


def test_release_provenance_separates_installed_and_inventory_builds() -> None:
    provenance = _provenance()
    builds = provenance["builds"]
    assert isinstance(builds, dict)
    assert set(builds) == {
        "prior_workflow_installed",
        "fixed_head_generation_installed",
        "final_inventory_only",
    }

    prior = builds["prior_workflow_installed"]
    fixed = builds["fixed_head_generation_installed"]
    inventory = builds["final_inventory_only"]
    assert prior["installed"] is True
    assert fixed["installed"] is True
    assert inventory["installed"] is False
    assert inventory["published"] is False
    assert inventory["purpose"] == "inventory-only"

    assert provenance["installed_generation"]["wheel_sha256"] == next(
        artifact["sha256"]
        for artifact in fixed["artifacts"]
        if artifact["kind"] == "wheel"
    )

    for build in (prior, fixed, inventory):
        artifacts = build["artifacts"]
        assert {artifact["kind"] for artifact in artifacts} == {"wheel", "sdist"}
        for artifact in artifacts:
            assert Path(artifact["path"]).is_absolute()
            _assert_sha256(artifact["sha256"])

    assert set(inventory["required_members"]) == {
        "docs/assets/v0.3-live-import.png",
        "docs/releases/v0.3.0-provenance.json",
        "docs/releases/v0.3.0.md",
    }
    manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    assert "recursive-include docs/assets *.png" in manifest
    assert "recursive-include docs/releases *.json" in manifest
