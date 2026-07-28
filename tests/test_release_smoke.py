"""Release-candidate smoke tests against the real stdio executable."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from scripts.wheel_smoke import (
    PROFILE_COUNTS,
    SOURCE_XML,
    check_version,
    inspect_server,
    smoke_workflow,
)

ROOT = Path(__file__).resolve().parents[1]


def _command() -> Path:
    executable = "fcp-mcp.exe" if os.name == "nt" else "fcp-mcp"
    command = Path(sys.executable).with_name(executable)
    assert command.is_file()
    return command


def _environment(tmp_path: Path) -> dict[str, str]:
    return {
        **os.environ,
        "FCP_MCP_PROFILE": "full",
        "FCP_MCP_OUTPUT_DIR": str(tmp_path),
        "FCP_MCP_ALLOWED_ROOTS": str(tmp_path),
        "FCP_MCP_STATE_DIR": str(tmp_path / "state"),
        "FCP_MCP_ENABLE_LIVE_CONTROL": "0",
    }


def test_installed_command_reports_release_version():
    assert check_version(str(_command())) == "0.3.0"


def test_real_stdio_initialize_catalog_and_doctor(tmp_path: Path):
    report = asyncio.run(
        inspect_server(
            str(_command()),
            cwd=ROOT,
            env=_environment(tmp_path),
        )
    )

    assert report["package_version"] == "0.3.0"
    assert report["server_name"] == "fcp-mcp"
    assert report["tool_count"] == 93
    assert report["prompt_count"] == 5
    assert report["resource_template_count"] == 3
    assert report["doctor_status"] in {"ready", "degraded"}
    assert report["doctor_is_error"] is False


def test_quickstart_uses_stdio_without_writing_roundtrip(tmp_path: Path):
    roundtrip = ROOT / "examples" / "_roundtrip.fcpxml"
    roundtrip.unlink(missing_ok=True)
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "examples" / "quickstart.py"),
            "--command",
            str(_command()),
        ],
        cwd=ROOT,
        env=_environment(tmp_path),
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    payload = json.loads(result.stdout)
    assert payload["package_version"] == "0.3.0"
    assert payload["tool_count"] == 93
    assert payload["prompt_count"] == 5
    assert not roundtrip.exists()


def test_profile_contract_includes_workflow_default():
    assert PROFILE_COUNTS == {
        "inspect": (30, 2, 0),
        "workflow": (34, 3, 3),
        "edit": (74, 5, 3),
        "full": (93, 5, 3),
    }


def test_release_smoke_source_has_required_asset_media_representation():
    root = ET.fromstring(SOURCE_XML)
    asset = root.find("./resources/asset")

    assert asset is not None
    media_rep = asset.find("media-rep")
    assert media_rep is not None
    assert media_rep.get("src", "").startswith("file://")


def test_real_stdio_hash_bound_workflow_and_cli_reconcile(tmp_path: Path):
    report = asyncio.run(
        smoke_workflow(
            str(_command()),
            cwd=ROOT,
            env=_environment(tmp_path),
        )
    )

    assert report["state"] == "committed"
    assert report["prepare_left_destination_untouched"] is True
    assert report["candidate_matches_destination"] is True
    assert report["resource_count"] == 3
    assert report["ledger_integrity_valid"] is True
    assert report["ledger_checked_runs"] >= 1
    assert report["reconcile_state"] == "committed"
    assert report["reconcile_recovery"] is None
