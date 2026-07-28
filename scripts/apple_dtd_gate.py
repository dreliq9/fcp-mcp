"""Validate release-candidate FCPXML with Final Cut Pro's bundled DTD."""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from lxml import etree

DEFAULT_RESOURCES_DIR = Path(
    "/Applications/Final Cut Pro.app/Contents/Frameworks/"
    "Interchange.framework/Versions/A/Resources"
)
VERSION_PATTERN = re.compile(r"^\d+\.\d+$")


class DTDValidationError(RuntimeError):
    """The candidate or local Apple DTD could not satisfy the release gate."""


@dataclass(frozen=True)
class DTDValidationReport:
    candidate: Path
    dtd: Path
    version: str


def fcpxml_version(candidate: Path) -> str:
    """Read and validate a candidate's declared FCPXML version."""
    candidate = candidate.expanduser().resolve()
    try:
        tree = etree.parse(
            str(candidate),
            etree.XMLParser(resolve_entities=False, no_network=True),
        )
    except (OSError, etree.XMLSyntaxError) as error:
        raise DTDValidationError(f"cannot parse {candidate}: {error}") from error
    version = tree.getroot().get("version")
    if not version:
        raise DTDValidationError(f"{candidate}: fcpxml root is missing version")
    if VERSION_PATTERN.fullmatch(version) is None:
        raise DTDValidationError(f"{candidate}: unsupported version {version!r}")
    return version


def bundled_dtd_path(
    version: str,
    *,
    resources_dir: Path = DEFAULT_RESOURCES_DIR,
) -> Path:
    """Resolve the matching DTD from the installed Final Cut Pro bundle."""
    if VERSION_PATTERN.fullmatch(version) is None:
        raise DTDValidationError(f"unsupported FCPXML version {version!r}")
    candidate = resources_dir.expanduser().resolve() / f"FCPXMLv{version.replace('.', '_')}.dtd"
    if not candidate.is_file():
        raise DTDValidationError(
            f"Final Cut Pro does not bundle an FCPXML {version} DTD at {candidate}"
        )
    return candidate


def validate_fcpxml(
    candidate: Path,
    *,
    dtd_path: Path | None = None,
    resources_dir: Path = DEFAULT_RESOURCES_DIR,
) -> DTDValidationReport:
    """Validate one FCPXML document and return its release-gate evidence."""
    candidate = candidate.expanduser().resolve()
    version = fcpxml_version(candidate)
    dtd = (
        dtd_path.expanduser().resolve()
        if dtd_path is not None
        else bundled_dtd_path(version, resources_dir=resources_dir)
    )
    try:
        with dtd.open("rb") as handle:
            validator = etree.DTD(handle)
        tree = etree.parse(
            str(candidate),
            etree.XMLParser(resolve_entities=False, no_network=True),
        )
    except (OSError, etree.DTDParseError, etree.XMLSyntaxError) as error:
        raise DTDValidationError(f"cannot load DTD gate inputs: {error}") from error
    if not validator.validate(tree):
        details = "; ".join(str(entry) for entry in validator.error_log)
        raise DTDValidationError(f"{candidate} failed Apple DTD validation: {details}")
    return DTDValidationReport(candidate=candidate, dtd=dtd, version=version)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("candidates", nargs="+", type=Path)
    args = parser.parse_args(argv)
    try:
        reports = [validate_fcpxml(candidate) for candidate in args.candidates]
    except DTDValidationError as error:
        parser.exit(1, f"Apple DTD gate failed: {error}\n")
    print(
        json.dumps(
            [
                {
                    **asdict(report),
                    "candidate": str(report.candidate),
                    "dtd": str(report.dtd),
                }
                for report in reports
            ],
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
