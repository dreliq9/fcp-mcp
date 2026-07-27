"""Release-candidate smoke tests against the real stdio executable."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

from scripts.wheel_smoke import check_version, inspect_server

ROOT = Path(__file__).resolve().parents[1]


def _command() -> Path:
    executable = "fcp-mcp.exe" if os.name == "nt" else "fcp-mcp"
    command = Path(sys.executable).with_name(executable)
    assert command.is_file()
    return command


def _environment(tmp_path: Path) -> dict[str, str]:
    return {
        **os.environ,
        "FCP_MCP_OUTPUT_DIR": str(tmp_path),
        "FCP_MCP_ALLOWED_ROOTS": str(tmp_path),
        "FCP_MCP_ENABLE_LIVE_CONTROL": "0",
    }


def test_installed_command_reports_release_version():
    assert check_version(str(_command())) == "0.2.1"


def test_real_stdio_initialize_catalog_and_doctor(tmp_path: Path):
    report = asyncio.run(
        inspect_server(
            str(_command()),
            cwd=ROOT,
            env=_environment(tmp_path),
        )
    )

    assert report["package_version"] == "0.2.1"
    assert report["server_name"] == "fcp-mcp"
    assert report["tool_count"] == 89
    assert report["prompt_count"] == 5
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
    assert payload["package_version"] == "0.2.1"
    assert payload["tool_count"] == 89
    assert payload["prompt_count"] == 5
    assert not roundtrip.exists()
