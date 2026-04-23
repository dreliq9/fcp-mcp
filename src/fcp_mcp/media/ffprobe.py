"""FFprobe wrapper for media file analysis."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Optional


def _find_ffprobe() -> str:
    """Find ffprobe binary."""
    for path in ("/opt/homebrew/bin/ffprobe", "/usr/local/bin/ffprobe", "ffprobe"):
        try:
            subprocess.run([path, "-version"], capture_output=True, timeout=5)
            return path
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    raise FileNotFoundError(
        "ffprobe not found. Install FFmpeg: brew install ffmpeg"
    )


def _find_ffmpeg() -> str:
    """Find ffmpeg binary."""
    for path in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "ffmpeg"):
        try:
            subprocess.run([path, "-version"], capture_output=True, timeout=5)
            return path
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    raise FileNotFoundError(
        "ffmpeg not found. Install FFmpeg: brew install ffmpeg"
    )


def probe_file(path: str) -> dict[str, Any]:
    """Get comprehensive media info via ffprobe."""
    ffprobe = _find_ffprobe()
    result = subprocess.run(
        [
            ffprobe, "-v", "quiet",
            "-print_format", "json",
            "-show_format", "-show_streams",
            str(Path(path).resolve()),
        ],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe error: {result.stderr}")
    return json.loads(result.stdout)


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
    result = subprocess.run(
        [
            ffmpeg, "-i", str(Path(path).resolve()),
            "-af", f"silencedetect=noise={noise_threshold}:d={min_duration}",
            "-f", "null", "-",
        ],
        capture_output=True, text=True, timeout=120,
    )

    # Parse silencedetect output from stderr
    silences = []
    current = {}
    for line in result.stderr.splitlines():
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

    # Use showinfo + astats for amplitude peaks
    result = subprocess.run(
        [
            ffmpeg, "-i", str(Path(path).resolve()),
            "-af", "astats=metadata=1:reset=1,ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-",
            "-f", "null", "-",
        ],
        capture_output=True, text=True, timeout=120,
    )

    # Parse RMS levels and find significant onsets
    beats = []
    prev_rms = -100
    frame_time = 0.0

    for line in result.stderr.splitlines():
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
    result = subprocess.run(
        [
            ffmpeg, "-i", str(Path(path).resolve()),
            "-af", "ebur128=peak=true",
            "-f", "null", "-",
        ],
        capture_output=True, text=True, timeout=120,
    )

    loudness = {}
    for line in result.stderr.splitlines():
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
    output_path: Optional[str] = None,
    width: int = 320,
) -> str:
    """Extract a single frame thumbnail from a video.

    Returns path to the saved thumbnail.
    """
    ffmpeg = _find_ffmpeg()
    if output_path is None:
        stem = Path(path).stem
        output_path = str(Path(path).parent / f"{stem}_thumb_{time:.0f}s.jpg")

    subprocess.run(
        [
            ffmpeg, "-y",
            "-ss", str(time),
            "-i", str(Path(path).resolve()),
            "-vframes", "1",
            "-vf", f"scale={width}:-1",
            output_path,
        ],
        capture_output=True, timeout=30,
    )
    return output_path


def extract_thumbnails(
    path: str,
    interval: float = 5.0,
    output_dir: Optional[str] = None,
    width: int = 320,
) -> list[str]:
    """Extract thumbnails at regular intervals.

    Returns list of paths to saved thumbnails.
    """
    ffmpeg = _find_ffmpeg()
    if output_dir is None:
        output_dir = str(Path(path).parent / f"{Path(path).stem}_thumbs")
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    output_pattern = str(Path(output_dir) / "thumb_%04d.jpg")
    subprocess.run(
        [
            ffmpeg, "-y",
            "-i", str(Path(path).resolve()),
            "-vf", f"fps=1/{interval},scale={width}:-1",
            output_pattern,
        ],
        capture_output=True, timeout=300,
    )

    return sorted(str(p) for p in Path(output_dir).glob("thumb_*.jpg"))


def detect_scenes(
    path: str,
    threshold: float = 0.3,
) -> list[dict]:
    """Detect scene changes in video.

    Returns list of {time, score} for each detected scene change.
    """
    ffmpeg = _find_ffmpeg()
    result = subprocess.run(
        [
            ffmpeg, "-i", str(Path(path).resolve()),
            "-vf", f"select='gt(scene,{threshold})',showinfo",
            "-f", "null", "-",
        ],
        capture_output=True, text=True, timeout=300,
    )

    scenes = []
    for line in result.stderr.splitlines():
        if "pts_time:" in line:
            try:
                time_part = line.split("pts_time:")[1].split()[0]
                scenes.append({
                    "time": float(time_part),
                })
            except (ValueError, IndexError):
                continue

    return scenes
