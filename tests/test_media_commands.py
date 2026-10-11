from pathlib import Path
from subprocess import CompletedProcess, TimeoutExpired

import pytest

from fcp_mcp.contracts import ErrorCode, FCPMCPError
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


@pytest.mark.parametrize("threshold", [
    "-30dB,anull", "-30dB;anull", "-30dB:d=1", "[in]-30dB[out]",
    "'-30dB'", r"-30dB\,anull", "/noise=/tmp/input", "-30dB\n",
    "NaN", "inf", "1e309", "1e309dB", "7000dB", "-0.1", "٣dB",
    "", "pi", "0.01+0.02", "0x1", "1k", None, 0.01,
])
def test_silence_rejects_invalid_threshold_before_any_command(monkeypatch, threshold):
    calls = []
    def unexpected(*args, **kwargs):
        calls.append(args)
        raise AssertionError("invalid input reached a side effect")
    for name in ("_find_ffmpeg", "_source_path", "_run_checked"):
        monkeypatch.setattr(ffprobe, name, unexpected)
    with pytest.raises(FCPMCPError) as caught:
        ffprobe.detect_silence("unused.wav", threshold)
    assert caught.value.code is ErrorCode.INVALID_ARGUMENTS
    assert calls == []


@pytest.mark.parametrize("duration", [
    "0.5,anull", "0.5", None, True, -0.01, 86400.1,
    float("nan"), float("inf"), float("-inf"), 10 ** 400,
])
def test_silence_rejects_invalid_duration_before_any_command(monkeypatch, duration):
    calls = []
    def unexpected(*args, **kwargs):
        calls.append(args)
        raise AssertionError("invalid input reached a side effect")
    for name in ("_find_ffmpeg", "_source_path", "_run_checked"):
        monkeypatch.setattr(ffprobe, name, unexpected)
    with pytest.raises(FCPMCPError) as caught:
        ffprobe.detect_silence("unused.wav", min_duration=duration)
    assert caught.value.code is ErrorCode.INVALID_ARGUMENTS
    assert calls == []


@pytest.mark.parametrize("threshold", [
    "-30dB", "-30.5dB", "0dB", "+3dB", "0", "0.001", ".001", "1e-3", "2",
])
@pytest.mark.parametrize("duration, serialized", [(0, "0.0"), (0.5, "0.5"),
                                                     (1e-7, "0.0000001"), (86400, "86400.0")])
def test_silence_accepts_scalar_formats_and_parses_output(
    monkeypatch, tmp_path, threshold, duration, serialized,
):
    source = tmp_path / "media.wav"
    source.write_bytes(b"fixture")
    monkeypatch.setattr(ffprobe, "_find_ffmpeg", lambda: "fixture-ffmpeg")
    commands = []
    def run(command, **kwargs):
        commands.append(command)
        return CompletedProcess(command, 0, "", "silence_start: 1\nsilence_end: 2 | silence_duration: 1\n")
    monkeypatch.setattr(ffprobe, "_run_checked", run)
    assert ffprobe.detect_silence(str(source), threshold, duration) == [
        {"start": 1.0, "end": 2.0, "duration": 1.0},
    ]
    assert commands == [["fixture-ffmpeg", "-i", str(source), "-af",
                         f"silencedetect=noise={threshold}:d={serialized}", "-f", "null", "-"]]


def test_silence_normalizes_float_subclass_before_filter_interpolation(monkeypatch, tmp_path):
    class Duration(float):
        def __str__(self):
            return "0.5,anull"
    source = tmp_path / "media.wav"
    source.write_bytes(b"fixture")
    monkeypatch.setattr(ffprobe, "_find_ffmpeg", lambda: "fixture-ffmpeg")
    commands = []
    def run(command, **kwargs):
        commands.append(command)
        return CompletedProcess(command, 0, "", "")
    monkeypatch.setattr(ffprobe, "_run_checked", run)
    ffprobe.detect_silence(str(source), min_duration=Duration(0.5))
    assert commands[0][4] == "silencedetect=noise=-30dB:d=0.5"
