"""Shared test fixtures."""

import os
from pathlib import Path

import pytest

from fcp_mcp.fcpxml.models import FCPXMLDocument
from fcp_mcp.fcpxml.parser import FCPXMLParser

os.environ.setdefault("FCP_MCP_PROFILE", "full")

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def sample_fcpxml_path() -> Path:
    return FIXTURES_DIR / "sample.fcpxml"


@pytest.fixture
def parser() -> FCPXMLParser:
    return FCPXMLParser()


@pytest.fixture
def sample_doc(parser: FCPXMLParser, sample_fcpxml_path: Path) -> FCPXMLDocument:
    return parser.parse(sample_fcpxml_path)
