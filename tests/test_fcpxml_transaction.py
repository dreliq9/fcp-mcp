from pathlib import Path

import pytest

from fcp_mcp.contracts import FCPMCPError
from fcp_mcp.fcpxml.transaction import commit_fcpxml
from fcp_mcp.fcpxml.validator import FCPXMLValidator, parse_fcpxml_version


def test_version_1_11_is_newer_than_1_6(sample_fcpxml_path: Path):
    assert parse_fcpxml_version("1.11") > parse_fcpxml_version("1.6")
    result = FCPXMLValidator().validate_file(sample_fcpxml_path)
    assert all("Old FCPXML version" not in issue.message for issue in result.issues)


def test_commit_rejects_same_source_and_destination(
    sample_fcpxml_path: Path,
    tmp_path: Path,
):
    source = tmp_path / "source.fcpxml"
    source.write_bytes(sample_fcpxml_path.read_bytes())

    with pytest.raises(FCPMCPError, match="same_file_forbidden"):
        commit_fcpxml(
            source=source,
            destination=source,
            xml_text=source.read_text(),
        )


def test_invalid_xml_never_replaces_existing_destination(
    sample_fcpxml_path: Path,
    tmp_path: Path,
):
    destination = tmp_path / "output.fcpxml"
    destination.write_text("ORIGINAL", encoding="utf-8")

    with pytest.raises(FCPMCPError, match="validation_failed"):
        commit_fcpxml(
            source=sample_fcpxml_path,
            destination=destination,
            xml_text="<fcpxml>",
        )

    assert destination.read_text(encoding="utf-8") == "ORIGINAL"
    assert list(tmp_path.glob("*.tmp")) == []
