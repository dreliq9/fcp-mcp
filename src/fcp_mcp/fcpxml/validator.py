"""FCPXML validator — checks structure and integrity."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Union
from urllib.parse import unquote as url_unquote

from .parser import FCPXMLParser
from .models import FCPXMLDocument, ClipType


@dataclass
class ValidationIssue:
    severity: str  # "error", "warning", "info"
    message: str
    location: str = ""  # e.g. "Project 'foo' > Clip 'bar'"


@dataclass
class ValidationResult:
    valid: bool = True
    issues: list[ValidationIssue] = field(default_factory=list)

    def add(self, severity: str, message: str, location: str = ""):
        self.issues.append(ValidationIssue(severity, message, location))
        if severity == "error":
            self.valid = False

    @property
    def errors(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == "warning"]

    def summary(self) -> str:
        lines = []
        if self.valid:
            lines.append("VALID")
        else:
            lines.append("INVALID")
        lines.append(f"  {len(self.errors)} errors, {len(self.warnings)} warnings, "
                      f"{len(self.issues) - len(self.errors) - len(self.warnings)} info")
        for issue in self.issues:
            prefix = {"error": "ERROR", "warning": "WARN", "info": "INFO"}[issue.severity]
            loc = f" [{issue.location}]" if issue.location else ""
            lines.append(f"  {prefix}: {issue.message}{loc}")
        return "\n".join(lines)


class FCPXMLValidator:
    """Validate FCPXML documents for structural and logical issues."""

    def validate_file(self, path: Union[str, Path]) -> ValidationResult:
        """Validate an FCPXML file."""
        result = ValidationResult()

        # Check file exists
        path = Path(path)
        if not path.exists():
            result.add("error", f"File not found: {path}")
            return result

        # Parse
        try:
            parser = FCPXMLParser()
            doc = parser.parse(path)
        except Exception as e:
            result.add("error", f"Parse error: {e}")
            return result

        return self.validate_document(doc)

    def validate_document(self, doc: FCPXMLDocument) -> ValidationResult:
        """Validate a parsed FCPXML document."""
        result = ValidationResult()

        # Check version
        try:
            version = float(doc.version)
            if version < 1.6:
                result.add("warning", f"Old FCPXML version {doc.version} — some features may not be supported")
        except ValueError:
            result.add("error", f"Invalid version: {doc.version}")

        # Check resources
        self._validate_resources(doc, result)

        # Check projects
        for project in doc.all_projects:
            self._validate_project(project, doc, result)

        if not doc.all_projects:
            result.add("warning", "No projects found in document")

        return result

    def _validate_resources(self, doc: FCPXMLDocument, result: ValidationResult):
        """Validate resource references."""
        if not doc.formats:
            result.add("warning", "No format resources defined")

        for asset_id, asset in doc.assets.items():
            if not asset.src:
                result.add("warning", f"Asset '{asset.name}' has no source path", f"Asset {asset_id}")

    def _validate_project(self, project, doc: FCPXMLDocument, result: ValidationResult):
        """Validate a project and its sequence."""
        loc = f"Project '{project.name}'"

        if not project.sequence:
            result.add("error", "Project has no sequence", loc)
            return

        seq = project.sequence
        if seq.format_ref and seq.format_ref not in doc.formats:
            result.add("error", f"Sequence references unknown format '{seq.format_ref}'", loc)

        if not seq.spine or not seq.spine.clips:
            result.add("warning", "Empty timeline (no clips in spine)", loc)
            return

        self._validate_spine(seq.spine, doc, result, loc)

    def _validate_spine(self, spine, doc: FCPXMLDocument, result: ValidationResult, loc: str):
        """Validate clips in a spine."""
        for i, clip in enumerate(spine.clips):
            clip_loc = f"{loc} > Clip #{i+1} '{clip.name}'"

            # Check asset reference exists
            if clip.ref and clip.clip_type not in (ClipType.GAP,):
                if clip.ref not in doc.resources:
                    result.add("error", f"References unknown resource '{clip.ref}'", clip_loc)

            # Check duration is positive
            if clip.duration.is_zero and clip.clip_type != ClipType.GAP:
                result.add("warning", "Zero-duration clip", clip_loc)

            # Check for flash frames (< 3 frames at 29.97)
            if clip.duration.to_seconds() < 0.1 and not clip.is_gap and not clip.duration.is_zero:
                result.add("warning",
                           f"Very short clip ({clip.duration.to_seconds():.3f}s) — possible flash frame",
                           clip_loc)

    def check_media_links(self, doc: FCPXMLDocument) -> ValidationResult:
        """Check if all referenced media files exist on disk."""
        result = ValidationResult()
        for asset_id, asset in doc.assets.items():
            if not asset.src:
                continue
            src = asset.src
            if src.startswith("file://"):
                file_path = Path(url_unquote(src[7:]))
                if not file_path.exists():
                    result.add("warning",
                               f"Media file not found: {file_path}",
                               f"Asset '{asset.name}' ({asset_id})")
        return result
