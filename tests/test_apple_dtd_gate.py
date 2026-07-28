"""Tests for the local Apple FCPXML DTD release gate."""

from pathlib import Path

import pytest

from scripts.apple_dtd_gate import (
    DTDValidationError,
    bundled_dtd_path,
    fcpxml_version,
    validate_fcpxml,
)


def test_bundled_dtd_path_maps_fcpxml_version(tmp_path: Path):
    resources = tmp_path / "Resources"
    resources.mkdir()
    expected = resources / "FCPXMLv1_11.dtd"
    expected.write_text("<!ELEMENT fcpxml EMPTY>", encoding="utf-8")

    assert bundled_dtd_path("1.11", resources_dir=resources) == expected


def test_fcpxml_version_rejects_missing_version(tmp_path: Path):
    candidate = tmp_path / "candidate.fcpxml"
    candidate.write_text("<fcpxml/>", encoding="utf-8")

    with pytest.raises(DTDValidationError, match="missing version"):
        fcpxml_version(candidate)


def test_validate_fcpxml_uses_real_lxml_dtd_validation(tmp_path: Path):
    dtd = tmp_path / "FCPXMLv1_11.dtd"
    dtd.write_text(
        """<!ELEMENT fcpxml (event)>
<!ATTLIST fcpxml version CDATA #REQUIRED>
<!ELEMENT event EMPTY>
<!ATTLIST event name CDATA #REQUIRED>
""",
        encoding="utf-8",
    )
    valid = tmp_path / "valid.fcpxml"
    valid.write_text(
        '<fcpxml version="1.11"><event name="Canary"/></fcpxml>',
        encoding="utf-8",
    )
    invalid = tmp_path / "invalid.fcpxml"
    invalid.write_text(
        '<fcpxml version="1.11"><event/></fcpxml>',
        encoding="utf-8",
    )

    report = validate_fcpxml(valid, dtd_path=dtd)
    assert report.candidate == valid.resolve()
    assert report.dtd == dtd.resolve()
    assert report.version == "1.11"

    with pytest.raises(DTDValidationError, match="attribute name"):
        validate_fcpxml(invalid, dtd_path=dtd)
