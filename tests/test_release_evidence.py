from __future__ import annotations

import hashlib
import json
import re
import struct
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROVENANCE_PATH = ROOT / "docs/releases/v0.3.0-provenance.json"
NOTE_PATH = ROOT / "docs/releases/v0.3.0.md"
SOURCE_COMMIT = "9bcdaad157c1000b51c7c6b08df5ed012bc33e25"
SHA256_RE = re.compile(r"[0-9a-f]{64}")


def _provenance() -> dict[str, object]:
    return json.loads(PROVENANCE_PATH.read_text(encoding="utf-8"))


def _assert_sha256(value: object) -> str:
    assert isinstance(value, str)
    assert SHA256_RE.fullmatch(value)
    return value


def test_release_provenance_binds_exact_head_installed_artifacts() -> None:
    provenance = _provenance()
    note = NOTE_PATH.read_text(encoding="utf-8")
    raw = PROVENANCE_PATH.read_text(encoding="utf-8")

    assert provenance["schema_version"] == "4"
    identity = provenance["evidence_identity"]
    assert identity["source_commit"] == SOURCE_COMMIT
    assert SOURCE_COMMIT in note
    assert "a15fc4683c2b2bf89f140321f74a89b3bcde93cc" not in note
    assert "a15fc4683c2b2bf89f140321f74a89b3bcde93cc" not in raw

    runtime = provenance["installed_execution"]
    assert runtime["source_commit"] == SOURCE_COMMIT
    for field in (
        "python_executable",
        "python_executable_resolved",
        "console_script",
        "package_import",
        "direct_url_record",
        "source_manifest_path",
        "cwd",
        "installed_wheel_path",
    ):
        path = Path(runtime[field])
        assert path.is_absolute(), field
        assert not path.is_relative_to(ROOT), field
    for field in (
        "python_executable_sha256",
        "console_script_sha256",
        "package_import_sha256",
        "direct_url_record_sha256",
        "source_manifest_sha256",
        "installed_wheel_sha256",
    ):
        _assert_sha256(runtime[field])
    assert runtime["direct_url"]["archive_info"]["hashes"]["sha256"] == runtime[
        "installed_wheel_sha256"
    ]
    assert runtime["stdio_argv"] == [runtime["console_script"]]

    installed = provenance["builds"]["exact_head_installed"]
    assert installed["source_commit"] == SOURCE_COMMIT
    assert installed["installed"] is True
    assert installed["published"] is False
    assert {artifact["kind"] for artifact in installed["artifacts"]} == {
        "wheel",
        "sdist",
    }
    wheel = next(item for item in installed["artifacts"] if item["kind"] == "wheel")
    assert wheel["sha256"] == runtime["installed_wheel_sha256"]


def test_release_provenance_binds_fresh_sample_trajectory() -> None:
    provenance = _provenance()
    note = NOTE_PATH.read_text(encoding="utf-8")
    trajectory = provenance["trajectory"]

    assert trajectory["source_sha256"] == provenance["sample_materialization"][
        "sha256"
    ]
    assert trajectory["candidate_sha256"] == trajectory["output_sha256"]
    assert trajectory["terminal_state"] == "committed"
    assert trajectory["terminal_event_sequence"] == 10
    assert trajectory["ledger_integrity_check"] == "ok"
    assert trajectory["ledger_diagnostics"]["integrity_valid"] is True
    assert trajectory["ledger_diagnostics"]["checked_runs"] == 1
    assert trajectory["ledger_diagnostics"]["checked_events"] == 10
    for field in (
        "source_sha256",
        "plan_sha256",
        "candidate_sha256",
        "output_sha256",
        "diff_sha256",
        "ledger_sha256",
    ):
        value = _assert_sha256(trajectory[field])
        assert value in note
    assert trajectory["run_id"] in note

    receipt = trajectory["receipt"]
    logical = _assert_sha256(receipt["logical_sha256"])
    artifact = _assert_sha256(receipt["artifact_sha256"])
    ledger = _assert_sha256(receipt["ledger_receipt_sha256"])
    assert logical != artifact
    assert artifact == ledger
    assert logical in note
    assert artifact in note


def test_release_provenance_binds_installed_generation_and_live_fcp() -> None:
    provenance = _provenance()
    note = NOTE_PATH.read_text(encoding="utf-8")
    generation = provenance["installed_generation"]
    runtime = provenance["installed_execution"]

    assert generation["source_commit"] == SOURCE_COMMIT
    assert generation["tool"] == "fcpxml_create_timeline"
    assert generation["stdio_argv"] == [runtime["console_script"]]
    clips = json.loads(generation["arguments"]["clips_json"])
    assert clips == [
        {
            "src": "/Volumes/Extreme SSD/GHG/P1010413.MP4",
            "name": "GHG P1010413 final-head five-second evidence",
            "start": "900900/30000s",
            "duration": "150150/30000s",
            "asset_duration": "5735730/30000s",
            "role": "Dialogue",
        }
    ]

    live_import = provenance["live_import"]
    generated = live_import["installed_generated_fcpxml"]
    generated_hash = _assert_sha256(generated["sha256"])
    assert generated_hash == generation["output_sha256"]
    assert generated_hash in note
    assert generated["post_generation_manual_edits"] is False

    dtd = live_import["dtd_validation"]
    assert dtd["result"] == "passed"
    assert dtd["version"] == "1.11"
    assert dtd["authoritative_gate"] == "scripts/apple_dtd_gate.py (lxml)"
    assert dtd["xml_sha256_before"] == dtd["xml_sha256_after"] == generated_hash

    observed = live_import["operator_observation"]
    assert observed["library"] == "v0.3-final-head-evidence.fcpbundle"
    assert observed["event"] == "v0.3 Final Head Evidence"
    assert observed["project"] == "v0.3 Final Head Installed Evidence"
    assert observed["route"] == "Final Cut Pro > File > Import > XML"
    assert observed["import_warning"] == "none observed"
    assert observed["visible_clip_duration"] == "00:00:05:00"
    assert observed["visible_video"] == "filmstrip"
    assert observed["visible_audio"] == "waveform"
    assert observed["visible_format"] == "1080p HD 29.97p, Stereo"
    assert observed["visible_role"] == "Dialogue-1"

    source = provenance["media"]
    managed = live_import["managed_media_copy"]
    assert managed["size_bytes"] == source["size_bytes"]
    assert managed["sha256"] == source["sha256"]
    assert managed["matches_authorized_original"] is True
    assert "managed library copy" in note

    image = provenance["screenshot"]
    image_path = ROOT / image["path"]
    image_bytes = image_path.read_bytes()
    assert image_bytes[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", image_bytes[16:24])
    assert [width, height] == image["dimensions_px"] == [972, 768]
    assert hashlib.sha256(image_bytes).hexdigest() == image["sha256"]
    assert image["capture"] == "genuine Final Cut Pro window capture, cropped only"


def test_release_provenance_separates_inventory_snapshot() -> None:
    provenance = _provenance()
    inventory = provenance["builds"]["final_inventory_only"]
    installed = provenance["builds"]["exact_head_installed"]

    assert inventory["source_commit"] == SOURCE_COMMIT
    assert inventory["purpose"] == "inventory-only"
    assert inventory["installed"] is False
    assert inventory["published"] is False
    assert inventory["archive_was_installed"] is False
    assert inventory["artifacts"] != installed["artifacts"]
    assert set(inventory["required_members"]) == {
        "docs/assets/v0.3-live-import.png",
        "docs/releases/v0.3.0-provenance.json",
        "docs/releases/v0.3.0.md",
    }
    assert set(inventory["snapshot_inputs"]) == set(inventory["required_members"])
    for digest in inventory["snapshot_inputs"].values():
        _assert_sha256(digest)
    assert inventory["member_hashes"] == inventory["snapshot_inputs"]
    assert inventory["required_member_archive"] == "sdist"
    for artifact in inventory["artifacts"]:
        assert Path(artifact["path"]).is_absolute()
        _assert_sha256(artifact["sha256"])
    assert inventory["member_hashes_match_snapshot_inputs"] is True
    assert "pre-attestation snapshot" in inventory["attestation_boundary"]

    manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    assert "recursive-include docs/assets *.png" in manifest
    assert "recursive-include docs/releases *.json" in manifest


def test_release_evidence_has_no_placeholders_or_publication_claim() -> None:
    note = NOTE_PATH.read_text(encoding="utf-8")
    provenance = PROVENANCE_PATH.read_text(encoding="utf-8")
    combined = f"{note}\n{provenance}".lower()
    assert "todo" not in combined
    assert "placeholder" not in combined
    assert "not published, tagged, pushed, or submitted" in combined
