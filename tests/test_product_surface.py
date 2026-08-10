from pathlib import Path

import tomllib

ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_readme_leads_with_bounded_outcome_and_frozen_catalog():
    text = read("README.md")
    first_screen = "\n".join(text.splitlines()[:45]).lower()
    assert "trusted, local" in first_screen
    assert "human-approved" in first_screen
    assert "most capable" not in first_screen
    assert "94 tools" in text
    assert "93 tools" not in text
    assert "v0.3 release candidate" not in text.lower()


def test_package_metadata_has_a_human_maintainer():
    project = tomllib.loads(read("pyproject.toml"))["project"]
    expected = [{"name": "Adam Steen", "email": "dreliq9@gmail.com"}]
    assert project["authors"] == expected
    assert project["maintainers"] == expected


def test_public_navigation_links_launch_surfaces():
    text = read("README.md")
    for target in (
        "docs/FIRST_RUN.md",
        "docs/COMPATIBILITY.md",
        "SUPPORT.md",
        "SECURITY.md",
        "docs/releases/v0.3.0.md",
    ):
        assert target in text


def test_first_run_documents_the_supported_transactional_path():
    text = read("docs/FIRST_RUN.md")

    for required in (
        "pipx install fcp-mcp",
        "fcp-mcp --version",
        "fcp-mcp doctor --json",
        "fcp-mcp sample --output",
        "fcpxml_parse",
        "fcpxml_workflow_prepare",
        "add_marker",
        "Demo Clip",
        "fcpxml_workflow_commit",
        "fcpxml_validate",
        "candidate_sha256",
        "offline by design",
    ):
        assert required in text


def test_public_compatibility_security_and_support_contracts_are_concrete():
    compatibility = read("docs/COMPATIBILITY.md")
    security = read("SECURITY.md")
    support = read("SUPPORT.md")

    for required in (
        "macOS 15.6",
        "Final Cut Pro 12.2",
        "FCPXML 1.11",
        "Python 3.10",
        "Python 3.13",
        "FCP_MCP_ALLOWED_ROOTS",
        "FCP_MCP_ENABLE_LIVE_CONTROL",
        "DTD",
        "import",
    ):
        assert required in compatibility

    for required in ("redact", "private", "FCP_MCP_ENABLE_LIVE_CONTROL", "XML"):
        assert required in security

    for required in ("doctor --json", "version", "client", "profile", "reproduction"):
        assert required in support


def test_issue_forms_follow_the_public_intake_contract():
    config = read(".github/ISSUE_TEMPLATE/config.yml")
    bug = read(".github/ISSUE_TEMPLATE/bug_report.yml")
    feature = read(".github/ISSUE_TEMPLATE/feature_request.yml")

    assert "blank_issues_enabled: false" in config
    assert "SECURITY.md" in config
    for required in ("version", "client", "profile", "doctor", "reproduction"):
        assert required in bug
    assert "problem" in feature
