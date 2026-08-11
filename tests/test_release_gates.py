"""Unit tests for release-only coverage gate helpers."""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

from ruamel.yaml import YAML

from scripts.check_new_module_coverage import coverage_failures

DOWNLOAD_ACTION = (
    "actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c"
)
RELEASE_ARTIFACT = "fcp-mcp-v0.3.0-release-dist"
RELEASE_MANIFEST = "fcp-mcp-v0.3.0.sha256"


def _load_workflow(relative: str) -> dict[str, object]:
    workflow = YAML(typ="safe").load(Path(relative).read_text(encoding="utf-8"))
    assert isinstance(workflow, dict)
    return workflow


def _named_step(job: dict[str, object], name: str) -> dict[str, object]:
    steps = job["steps"]
    assert isinstance(steps, list)
    matches = [step for step in steps if step.get("name") == name]
    assert len(matches) == 1
    return matches[0]


def _assert_exact_release_binding(
    publish_workflow: dict[str, object],
    registry_workflow: dict[str, object],
) -> None:
    publish_jobs = publish_workflow["jobs"]
    assert isinstance(publish_jobs, dict)
    verify = publish_jobs["verify"]
    pypi = publish_jobs["publish"]
    verify_steps = verify["steps"]
    assert isinstance(verify_steps, list)
    verify_names = [step.get("name") for step in verify_steps]
    assert verify_names[-2:] == [
        "Create release checksum manifest",
        "Upload verified distributions",
    ]
    manifest = _named_step(verify, "Create release checksum manifest")["run"]
    assert "shasum -a 256" in manifest
    assert "dist/fcp_mcp-0.3.0-py3-none-any.whl" in manifest
    assert "dist/fcp_mcp-0.3.0.tar.gz" in manifest
    assert RELEASE_MANIFEST in manifest
    assert _named_step(verify, "Upload verified distributions")["with"] == {
        "name": RELEASE_ARTIFACT,
        "path": (
            "dist/fcp_mcp-0.3.0-py3-none-any.whl\n"
            "dist/fcp_mcp-0.3.0.tar.gz\n"
            f"{RELEASE_MANIFEST}\n"
        ),
        "if-no-files-found": "error",
        "retention-days": 7,
    }
    assert pypi["permissions"] == {"id-token": "write"}
    assert pypi["steps"][0]["with"] == {
        "name": RELEASE_ARTIFACT,
        "path": ".",
    }
    assert len(pypi["steps"]) == 2

    trigger = registry_workflow["on"]["workflow_dispatch"]["inputs"]
    assert trigger["publish_run_id"] == {
        "description": "Decimal workflow run ID that built and published v0.3.0",
        "required": True,
        "type": "string",
    }
    registry_jobs = registry_workflow["jobs"]
    assert isinstance(registry_jobs, dict)
    validate = registry_jobs["validate"]
    registry = registry_jobs["registry"]
    assert validate["permissions"] == {}
    assert validate["steps"][0]["env"] == {
        "PUBLISH_RUN_ID": "${{ inputs.publish_run_id }}",
        "RELEASE_SHA": "${{ inputs.release_sha }}",
    }
    validation = validate["steps"][0]["run"]
    assert 'case "$PUBLISH_RUN_ID" in' in validation
    assert "*[!0-9]*" in validation
    assert registry["permissions"] == {
        "contents": "read",
        "actions": "read",
        "id-token": "write",
    }
    registry_names = [step.get("name") for step in registry["steps"]]
    assert registry_names == [
        "Verify exact publish workflow run",
        "Download exact release artifact",
        "Verify release artifact contents",
        "Checkout immutable release SHA",
        "Prove immutable release identity",
        "Verify immutable release metadata",
        "Verify public PyPI files and provenance",
        "Download verified mcp-publisher",
        "Publish through GitHub OIDC",
        "Verify Registry response",
    ]

    run_step = _named_step(registry, "Verify exact publish workflow run")
    assert run_step["env"] == {
        "GH_TOKEN": "${{ github.token }}",
        "PUBLISH_RUN_ID": "${{ inputs.publish_run_id }}",
        "RELEASE_SHA": "${{ inputs.release_sha }}",
    }
    run_check = run_step["run"]
    for field_check in (
        ".id == $run_id",
        '.repository.full_name == "dreliq9/fcp-mcp"',
        '.path == ".github/workflows/publish.yml"',
        '.event == "push"',
        '.status == "completed"',
        '.conclusion == "success"',
        ".head_sha == $release_sha",
        ".run_attempt == 1",
    ):
        assert field_check in run_check

    assert _named_step(registry, "Download exact release artifact") == {
        "name": "Download exact release artifact",
        "uses": DOWNLOAD_ACTION,
        "with": {
            "name": RELEASE_ARTIFACT,
            "path": "${{ runner.temp }}/fcp-mcp-v0.3.0-release-dist",
            "github-token": "${{ github.token }}",
            "repository": "dreliq9/fcp-mcp",
            "run-id": "${{ inputs.publish_run_id }}",
            "digest-mismatch": "error",
        },
    }
    artifact_check = _named_step(registry, "Verify release artifact contents")["run"]
    assert "sha256sum -c" in artifact_check
    assert "find . -mindepth 1 -print" in artifact_check
    assert RELEASE_MANIFEST in artifact_check

    pypi_check = _named_step(registry, "Verify public PyPI files and provenance")[
        "run"
    ]
    assert ".urls | length == 2" in pypi_check
    assert pypi_check.count(".digests.sha256") == 2
    assert pypi_check.count(".yanked == false") == 2
    assert pypi_check.count("/provenance") == 2
    assert pypi_check.count('.publisher.kind == "GitHub"') == 1
    assert pypi_check.count('.publisher.repository == "dreliq9/fcp-mcp"') == 1
    assert pypi_check.count('.publisher.workflow == "publish.yml"') == 1
    assert "(.attestations | length) >= 1" in pypi_check

    oidc_index = registry_names.index("Publish through GitHub OIDC")
    assert oidc_index > registry_names.index("Verify public PyPI files and provenance")
    for step in registry["steps"]:
        if "run" in step:
            assert "${{" not in step["run"]
    for step in registry["steps"][oidc_index:]:
        assert "GH_TOKEN" not in step.get("env", {})
        assert "api.github.com" not in step.get("run", "")


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
        "PUBLISH_RUN_ID": "${{ inputs.publish_run_id }}",
        "RELEASE_SHA": "${{ inputs.release_sha }}"
    }
    assert 'if [ "$GITHUB_REF" != "refs/heads/main" ]; then' in validate["steps"][0]["run"]
    assert registry["needs"] == "validate"
    assert "if" not in registry
    checkout = _named_step(registry, "Checkout immutable release SHA")
    assert checkout["with"] == {
        "ref": "${{ inputs.release_sha }}",
        "fetch-depth": 0,
        "persist-credentials": False,
    }
    identity = _named_step(registry, "Prove immutable release identity")
    assert identity["name"] == "Prove immutable release identity"
    assert identity["env"] == {"RELEASE_SHA": "${{ inputs.release_sha }}"}
    assert 'test "$(git rev-parse HEAD)" = "$RELEASE_SHA"' in identity["run"]
    assert 'test "$(git rev-parse refs/tags/v0.3.0^{})" = "$RELEASE_SHA"' in identity["run"]
    for job in workflow["jobs"].values():
        for step in job["steps"]:
            if "run" in step:
                assert "${{" not in step["run"]


def test_release_workflows_bind_registry_to_exact_tag_built_pypi_files():
    _assert_exact_release_binding(
        _load_workflow(".github/workflows/publish.yml"),
        _load_workflow(".github/workflows/publish-registry.yml"),
    )


def test_exact_manifest_and_artifact_shell_trajectory_rejects_extra_file(tmp_path):
    publish = _load_workflow(".github/workflows/publish.yml")
    registry = _load_workflow(".github/workflows/publish-registry.yml")
    dist = tmp_path / "dist"
    dist.mkdir()
    wheel = b"wheel-bytes"
    sdist = b"sdist-bytes"
    (dist / "fcp_mcp-0.3.0-py3-none-any.whl").write_bytes(wheel)
    (dist / "fcp_mcp-0.3.0.tar.gz").write_bytes(sdist)

    manifest_step = _named_step(
        publish["jobs"]["verify"], "Create release checksum manifest"
    )
    subprocess.run(
        ["/bin/bash", "-c", manifest_step["run"]],
        cwd=tmp_path,
        check=True,
    )
    assert (tmp_path / RELEASE_MANIFEST).read_text(encoding="utf-8") == (
        f"{hashlib.sha256(wheel).hexdigest()}  "
        "dist/fcp_mcp-0.3.0-py3-none-any.whl\n"
        f"{hashlib.sha256(sdist).hexdigest()}  dist/fcp_mcp-0.3.0.tar.gz\n"
    )

    artifact_dir = tmp_path / RELEASE_ARTIFACT
    artifact_dir.mkdir()
    dist.rename(artifact_dir / "dist")
    (tmp_path / RELEASE_MANIFEST).rename(artifact_dir / RELEASE_MANIFEST)
    artifact_step = _named_step(
        registry["jobs"]["registry"], "Verify release artifact contents"
    )
    environment = {**os.environ, "RUNNER_TEMP": str(tmp_path)}
    subprocess.run(
        ["/bin/bash", "-c", artifact_step["run"]],
        cwd=tmp_path,
        env=environment,
        check=True,
    )

    (artifact_dir / "unexpected.txt").write_text("extra", encoding="utf-8")
    rejected = subprocess.run(
        ["/bin/bash", "-c", artifact_step["run"]],
        cwd=tmp_path,
        env=environment,
        check=False,
    )
    assert rejected.returncode != 0


def test_parsed_release_contract_rejects_missing_run_id_and_extra_permission():
    publish = _load_workflow(".github/workflows/publish.yml")
    registry = _load_workflow(".github/workflows/publish-registry.yml")
    del registry["on"]["workflow_dispatch"]["inputs"]["publish_run_id"]
    registry["jobs"]["registry"]["permissions"]["packages"] = "write"

    try:
        _assert_exact_release_binding(publish, registry)
    except (AssertionError, KeyError):
        pass
    else:  # pragma: no cover - proves the adversarial fixture is rejected
        raise AssertionError("unsafe parsed workflow mutation was accepted")


def test_parsed_release_contract_rejects_oidc_before_pypi_provenance():
    publish = _load_workflow(".github/workflows/publish.yml")
    registry = _load_workflow(".github/workflows/publish-registry.yml")
    steps = registry["jobs"]["registry"]["steps"]
    oidc = next(step for step in steps if step.get("name") == "Publish through GitHub OIDC")
    provenance = next(
        step
        for step in steps
        if step.get("name") == "Verify public PyPI files and provenance"
    )
    oidc_index = steps.index(oidc)
    provenance_index = steps.index(provenance)
    steps[oidc_index], steps[provenance_index] = provenance, oidc

    try:
        _assert_exact_release_binding(publish, registry)
    except AssertionError:
        pass
    else:  # pragma: no cover - proves the adversarial fixture is rejected
        raise AssertionError("OIDC-before-provenance mutation was accepted")


def test_parsed_release_contract_rejects_moving_cross_run_download():
    publish = _load_workflow(".github/workflows/publish.yml")
    registry = _load_workflow(".github/workflows/publish-registry.yml")
    download = _named_step(registry["jobs"]["registry"], "Download exact release artifact")
    download["uses"] = "actions/download-artifact@v8"
    download["with"].pop("digest-mismatch")

    try:
        _assert_exact_release_binding(publish, registry)
    except AssertionError:
        pass
    else:  # pragma: no cover - proves the adversarial fixture is rejected
        raise AssertionError("moving cross-run download mutation was accepted")
