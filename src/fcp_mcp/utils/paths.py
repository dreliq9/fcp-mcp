"""Path discovery for FCP resources."""

from __future__ import annotations

from pathlib import Path


def fcp_libraries_dir() -> Path:
    """Default FCP libraries location."""
    return Path.home() / "Movies"


def motion_templates_dir() -> Path:
    """Motion templates root."""
    return Path.home() / "Movies" / "Motion Templates.localized"


def fcp_destinations_dir() -> Path:
    """Share destinations."""
    return Path.home() / "Library" / "Application Support" / "Final Cut Pro" / "Destinations"


def compressor_settings_dir() -> Path:
    """Compressor presets."""
    return Path.home() / "Library" / "Application Support" / "Compressor" / "Settings"


def compressor_binary() -> Path:
    """Compressor CLI binary."""
    return Path("/Applications/Compressor.app/Contents/MacOS/Compressor")


def find_fcpxml_files(directory: Path, recursive: bool = True) -> list[Path]:
    """Find all .fcpxml files in a directory."""
    pattern = "**/*.fcpxml" if recursive else "*.fcpxml"
    return sorted(directory.glob(pattern))


def find_fcpbundles(directory: Path, recursive: bool = False) -> list[Path]:
    """Find FCP library bundles."""
    if recursive:
        return sorted(directory.rglob("*.fcpbundle"))
    return sorted(directory.glob("*.fcpbundle"))
