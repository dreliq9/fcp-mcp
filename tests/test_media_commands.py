from pathlib import Path
from subprocess import CompletedProcess, TimeoutExpired

import pytest

from fcp_mcp.contracts import FCPMCPError
from fcp_mcp.media import ffprobe
from fcp_mcp.media.ffprobe import _run_checked


def test_nonzero_media_command_is_failure():
    def runner(command, **kwargs):
        return CompletedProcess(command, 2, "", "bad input")

    with pytest.raises(FCPMCPError, match="command_failed"):
        _run_checked(["ffmpeg", "-i", "missing"], timeout=1, runner=runner)


def test_missing_promised_output_is_failure(tmp_path: Path):
    output = tmp_path / "thumb.jpg"

    def runner(command, **kwargs):
        return CompletedProcess(command, 0, "", "")

    with pytest.raises(FCPMCPError, match="output_missing"):
        _run_checked(
            ["ffmpeg", "-version"],
            timeout=1,
            expected_outputs=[output],
            runner=runner,
        )


def test_zero_byte_promised_output_is_failure(tmp_path: Path):
    output = tmp_path / "thumb.jpg"

    def runner(command, **kwargs):
        output.touch()
        return CompletedProcess(command, 0, "", "")

    with pytest.raises(FCPMCPError, match="output_missing"):
        _run_checked(
            ["ffmpeg", "-version"],
            timeout=1,
            expected_outputs=[output],
            runner=runner,
        )


def test_nonempty_promised_output_passes(tmp_path: Path):
    output = tmp_path / "thumb.jpg"

    def runner(command, **kwargs):
        output.write_bytes(b"jpeg")
        return CompletedProcess(command, 0, "", "")

    result = _run_checked(
        ["ffmpeg", "-version"],
        timeout=1,
        expected_outputs=[output],
        runner=runner,
    )
    assert result.returncode == 0


def test_timeout_is_command_failure():
    def runner(command, **kwargs):
        raise TimeoutExpired(command, kwargs["timeout"])

    with pytest.raises(FCPMCPError, match="command_failed"):
        _run_checked(["ffmpeg", "-version"], timeout=0.01, runner=runner)


def test_binary_discovery_skips_nonzero_candidate(monkeypatch):
    def runner(command, **kwargs):
        return CompletedProcess(
            command,
            0 if command[0] == "/usr/local/bin/ffmpeg" else 1,
            "",
            "bad binary",
        )

    monkeypatch.setattr(ffprobe.subprocess, "run", runner)
    assert ffprobe._find_ffmpeg() == "/usr/local/bin/ffmpeg"
