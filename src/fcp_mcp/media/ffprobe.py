"""FFprobe wrapper for media file analysis."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from subprocess import CompletedProcess
from typing import Any

from fcp_mcp.contracts import ErrorCode, FCPMCPError


def _run_checked(
    command: Sequence[str],
    *,
    timeout: float,
    expected_outputs: Sequence[str | Path] = (),
    runner: Callable[..., CompletedProcess[str]] = subprocess.run,
) -> CompletedProcess[str]:
    try:
        result = runner(
            list(command),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise FCPMCPError(
            ErrorCode.COMMAND_FAILED,
            f"{command[0]} timed out after {timeout:g} seconds",
        ) from error
    except FileNotFoundError as error:
        raise FCPMCPError(
            ErrorCode.DEPENDENCY_MISSING,
            f"Executable not found: {command[0]}",
        ) from error
    except PermissionError as error:
        raise FCPMCPError(
            ErrorCode.PERMISSION_DENIED,
            f"Cannot execute {command[0]}: {error}",
        ) from error
    except OSError as error:
        raise FCPMCPError(
            ErrorCode.COMMAND_FAILED,
            f"Cannot execute {command[0]}: {error}",
        ) from error

    if result.returncode:
        detail = ((result.stderr or result.stdout) or "no command output")[-500:].strip()
        raise FCPMCPError(
            ErrorCode.COMMAND_FAILED,
            f"{command[0]} exited with status {result.returncode}: {detail}",
        )

    missing = [
        str(Path(output))
        for output in expected_outputs
        if not Path(output).is_file() or Path(output).stat().st_size == 0
    ]
    if missing:
        raise FCPMCPError(
            ErrorCode.OUTPUT_MISSING,
            f"Command did not create nonempty output: {', '.join(missing)}",
        )
    return result


def _source_path(path: str | Path) -> Path:
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise FCPMCPError(ErrorCode.SOURCE_NOT_FOUND, f"Media file not found: {source}")
    return source


def _find_binary(name: str) -> str:
    candidates = (
        f"/opt/homebrew/bin/{name}",
        f"/usr/local/bin/{name}",
        name,
    )
    for path in candidates:
        try:
            _run_checked(
                [path, "-version"],
                timeout=5,
                runner=subprocess.run,
            )
        except FCPMCPError:
            continue
        return path
    raise FCPMCPError(
        ErrorCode.DEPENDENCY_MISSING,
        f"{name} not found or unusable. Install FFmpeg: brew install ffmpeg",
    )


def _find_ffprobe() -> str:
    """Find ffprobe binary."""
    return _find_binary("ffprobe")


def _find_ffmpeg() -> str:
    """Find ffmpeg binary."""
    return _find_binary("ffmpeg")


def probe_file(path: str) -> dict[str, Any]:
    """Get comprehensive media info via ffprobe."""
    ffprobe = _find_ffprobe()
    source = _source_path(path)
    result = _run_checked(
        [
            ffprobe, "-v", "quiet",
            "-print_format", "json",
            "-show_format", "-show_streams",
            str(source),
        ],
        timeout=30,
    )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise FCPMCPError(
            ErrorCode.COMMAND_FAILED,
            f"ffprobe returned invalid JSON: {error}",
        ) from error


def get_duration(path: str) -> float:
    """Get media duration in seconds."""
    info = probe_file(path)
    return float(info.get("format", {}).get("duration", 0))


def get_streams(path: str) -> list[dict]:
    """List all streams in a media file."""
    info = probe_file(path)
    return info.get("streams", [])


def detect_silence(
    path: str,
    noise_threshold: str = "-30dB",
    min_duration: float = 0.5,
) -> list[dict]:
    """Detect silent sections in audio using FFmpeg's silencedetect filter.

    Returns list of {start, end, duration} for each silent section.
    """
    ffmpeg = _find_ffmpeg()
    source = _source_path(path)
    result = _run_checked(
        [
            ffmpeg, "-i", str(source),
            "-af", f"silencedetect=noise={noise_threshold}:d={min_duration}",
            "-f", "null", "-",
        ],
        timeout=120,
    )

    # Parse silencedetect output from stderr
    silences = []
    current = {}
    for line in (result.stderr or "").splitlines():
        if "silence_start:" in line:
            parts = line.split("silence_start:")
            current["start"] = float(parts[1].strip())
        elif "silence_end:" in line:
            parts = line.split("silence_end:")
            end_parts = parts[1].strip().split("|")
            current["end"] = float(end_parts[0].strip())
            if len(end_parts) > 1:
                dur_part = end_parts[1].strip()
                if "silence_duration:" in dur_part:
                    current["duration"] = float(dur_part.split(":")[1].strip())
            silences.append(current)
            current = {}

    return silences


def detect_beats(
    path: str,
    threshold: float = 0.1,
) -> list[float]:
    """Detect beat positions in audio using onset detection.

    Returns list of beat times in seconds.
    Uses FFmpeg's ebur128 and astats filters for onset detection.
    """
    ffmpeg = _find_ffmpeg()
    source = _source_path(path)

    # Use showinfo + astats for amplitude peaks
    result = _run_checked(
        [
            ffmpeg, "-i", str(source),
            "-af", "astats=metadata=1:reset=1,ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-",
            "-f", "null", "-",
        ],
        timeout=120,
    )

    # Parse RMS levels and find significant onsets
    beats = []
    prev_rms = -100
    frame_time = 0.0

    for line in (result.stderr or "").splitlines():
        if "lavfi.astats.Overall.RMS_level" in line:
            try:
                parts = line.split("=")
                rms = float(parts[-1].strip())
                # Detect onset: significant jump in RMS
                if rms - prev_rms > threshold * 50 and rms > -30:
                    beats.append(round(frame_time, 3))
                prev_rms = rms
                frame_time += 1024 / 44100  # approximate frame advance
            except (ValueError, IndexError):
                continue

    return beats


def analyze_loudness(path: str) -> dict[str, Any]:
    """Analyze audio loudness (EBU R128 / LUFS).

    Returns integrated loudness, loudness range, true peak.
    """
    ffmpeg = _find_ffmpeg()
    source = _source_path(path)
    result = _run_checked(
        [
            ffmpeg, "-i", str(source),
            "-af", "ebur128=peak=true",
            "-f", "null", "-",
        ],
        timeout=120,
    )

    loudness = {}
    for line in (result.stderr or "").splitlines():
        line = line.strip()
        if "I:" in line and "LUFS" in line:
            try:
                loudness["integrated_lufs"] = float(line.split("I:")[1].split("LUFS")[0].strip())
            except (ValueError, IndexError):
                pass
        elif "LRA:" in line and "LU" in line:
            try:
                loudness["loudness_range_lu"] = float(line.split("LRA:")[1].split("LU")[0].strip())
            except (ValueError, IndexError):
                pass
        elif "Peak:" in line and "dBFS" in line:
            try:
                loudness["true_peak_dbfs"] = float(line.split("Peak:")[1].split("dBFS")[0].strip())
            except (ValueError, IndexError):
                pass

    return loudness


def extract_thumbnail(
    path: str,
    time: float = 0.0,
    output_path: str | None = None,
    width: int = 320,
) -> str:
    """Extract a single frame thumbnail from a video.

    Returns path to the saved thumbnail.
    """
    ffmpeg = _find_ffmpeg()
    source = _source_path(path)
    if output_path is None:
        output = source.parent / f"{source.stem}_thumb_{time:.0f}s.jpg"
    else:
        output = Path(output_path).expanduser().resolve()

    _run_checked(
        [
            ffmpeg, "-y",
            "-ss", str(time),
            "-i", str(source),
            "-vframes", "1",
            "-vf", f"scale={width}:-1",
            str(output),
        ],
        timeout=30,
        expected_outputs=[output],
    )
    return str(output)


def extract_thumbnails(
    path: str,
    interval: float = 5.0,
    output_dir: str | None = None,
    width: int = 320,
) -> list[str]:
    """Extract thumbnails at regular intervals.

    Returns list of paths to saved thumbnails.
    """
    ffmpeg = _find_ffmpeg()
    source = _source_path(path)
    if output_dir is None:
        directory = source.parent / f"{source.stem}_thumbs"
    else:
        directory = Path(output_dir).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)

    output_pattern = str(directory / "thumb_%04d.jpg")
    _run_checked(
        [
            ffmpeg, "-y",
            "-i", str(source),
            "-vf", f"fps=1/{interval},scale={width}:-1",
            output_pattern,
        ],
        timeout=300,
    )

    outputs = sorted(
        path
        for path in directory.glob("thumb_*.jpg")
        if path.is_file() and path.stat().st_size > 0
    )
    if not outputs:
        raise FCPMCPError(
            ErrorCode.OUTPUT_MISSING,
            f"FFmpeg created no nonempty thumbnails in {directory}",
        )
    return [str(output) for output in outputs]


def detect_scenes(
    path: str,
    threshold: float = 0.3,
) -> list[dict]:
    """Detect scene changes in video.

    Returns list of {time, score} for each detected scene change.
    """
    ffmpeg = _find_ffmpeg()
    source = _source_path(path)
    result = _run_checked(
        [
            ffmpeg, "-i", str(source),
            "-vf", f"select='gt(scene,{threshold})',showinfo",
            "-f", "null", "-",
        ],
        timeout=300,
    )

    scenes = []
    for line in (result.stderr or "").splitlines():
        if "pts_time:" in line:
            try:
                time_part = line.split("pts_time:")[1].split()[0]
                scenes.append({
                    "time": float(time_part),
                })
            except (ValueError, IndexError):
                continue

    return scenes
