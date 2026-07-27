"""Unit tests for release-only coverage gate helpers."""

from __future__ import annotations

from pathlib import Path

from scripts.check_new_module_coverage import coverage_failures


def test_coverage_gate_normalizes_repository_prefixes():
    report = {
        "files": {
            "/runner/work/fcp-mcp/src/fcp_mcp/one.py": {
                "summary": {"percent_covered": 95.0}
            },
            "src\\fcp_mcp\\two.py": {
                "summary": {"percent_covered": 89.5}
            },
        }
    }

    assert coverage_failures(
        report,
        required_paths=(
            "src/fcp_mcp/one.py",
            "src/fcp_mcp/two.py",
            "src/fcp_mcp/missing.py",
        ),
        minimum=90.0,
    ) == {
        "src/fcp_mcp/two.py": 89.5,
        "src/fcp_mcp/missing.py": None,
    }


def test_coverage_gate_accepts_every_file_at_threshold():
    report = {
        "files": {
            "src/fcp_mcp/one.py": {
                "summary": {"percent_covered": 90.0}
            }
        }
    }

    assert coverage_failures(
        report,
        required_paths=("src/fcp_mcp/one.py",),
        minimum=90.0,
    ) == {}


def test_publish_workflow_isolates_trusted_publishing():
    workflow = Path(".github/workflows/publish.yml").read_text()

    assert "PYPI_API_TOKEN" not in workflow
    assert "id-token: write" in workflow
    assert "needs: verify" in workflow
    assert "actions/download-artifact@" in workflow
    assert "environment:" in workflow
    assert "name: pypi" in workflow
