"""Unit tests for release-only coverage gate helpers."""

from __future__ import annotations

from pathlib import Path

from ruamel.yaml import YAML

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


def test_registry_workflow_is_manual_oidc_only_and_verifies_public_metadata():
    workflow = Path(".github/workflows/publish-registry.yml").read_text(
        encoding="utf-8"
    )

    assert "workflow_dispatch:" in workflow
    assert "id-token: write" in workflow
    assert "contents: read" in workflow
    assert "persist-credentials: false" in workflow
    assert "refs/tags/v0.3.0^{}" in workflow
    assert "mcp-publisher_linux_amd64.tar.gz" in workflow
    assert "v1.8.1" in workflow
    assert (
        "a06c9096dcb9727c13555b6be26c7effa707b01f06a4c561ba7a3635443cf2cc"
        in workflow
    )
    assert "https://pypi.org/pypi/fcp-mcp/0.3.0/json" in workflow
    assert "registry.modelcontextprotocol.io/v0.1/servers/" in workflow
    assert "io.github.dreliq9/fcp-mcp" in workflow
    assert '"fcp-mcp"' in workflow
    assert '"0.3.0"' in workflow
    assert "login github-oidc" in workflow
    assert '"$publisher" publish' in workflow
    assert "MCP_GITHUB_TOKEN" not in workflow
    assert "github --token" not in workflow


def test_registry_workflow_guards_dispatch_before_oidc_and_proves_tag_identity():
    workflow = YAML(typ="safe").load(
        Path(".github/workflows/publish-registry.yml").read_text(encoding="utf-8")
    )

    validate = workflow["jobs"]["validate"]
    registry = workflow["jobs"]["registry"]
    assert validate["runs-on"] == "ubuntu-latest"
    assert validate["permissions"] == {}
    assert validate["steps"][0]["env"] == {
        "RELEASE_SHA": "${{ inputs.release_sha }}"
    }
    assert 'if [ "$GITHUB_REF" != "refs/heads/main" ]; then' in validate["steps"][0]["run"]
    assert registry["needs"] == "validate"
    assert "if" not in registry
    assert registry["steps"][0]["with"] == {
        "ref": "${{ inputs.release_sha }}",
        "fetch-depth": 0,
        "persist-credentials": False,
    }
    identity = registry["steps"][1]
    assert identity["name"] == "Prove immutable release identity"
    assert identity["env"] == {"RELEASE_SHA": "${{ inputs.release_sha }}"}
    assert 'test "$(git rev-parse HEAD)" = "$RELEASE_SHA"' in identity["run"]
    assert 'test "$(git rev-parse refs/tags/v0.3.0^{})" = "$RELEASE_SHA"' in identity["run"]
    for job in workflow["jobs"].values():
        for step in job["steps"]:
            if "run" in step:
                assert "${{" not in step["run"]
