from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from scripts.check_macos_only import check

DOWNLOAD_ACTION = (
    "actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c"
)
UPLOAD_ACTION = (
    "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a"
)
PUBLISH_ACTION = (
    "pypa/gh-action-pypi-publish@ed0c53931b1dc9bd32cbe73a98c7f6766f8a527e"
)
RELEASE_ARTIFACT = "fcp-mcp-v0.2.1-release-dist"


def _write_valid_repository(root: Path) -> None:
    source = root / "src" / "fcp_mcp"
    source.mkdir(parents=True)
    (source / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    (source / "platform_support.py").write_text(
        "import sys\nPLATFORM = sys.platform\n", encoding="utf-8"
    )

    workflows = root / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(
        """
jobs:
  quality:
    runs-on: macos-latest
    steps:
      - name: Enforce macOS-only architecture
        run: python scripts/check_macos_only.py
""".lstrip(),
        encoding="utf-8",
    )
    (workflows / "publish.yml").write_text(
        f"""
on:
  push:
    tags: ["v*"]
permissions:
  contents: read
jobs:
  verify:
    runs-on: macos-latest
    steps:
      - name: Enforce macOS-only architecture
        run: python scripts/check_macos_only.py
      - name: Upload verified distributions
        uses: {UPLOAD_ACTION}
        with:
          name: {RELEASE_ARTIFACT}
          path: dist/
          if-no-files-found: error
          retention-days: 1
  publish:
    name: Publish verified distributions to PyPI
    needs: verify
    runs-on: ubuntu-latest
    environment:
      name: pypi
      url: https://pypi.org/p/fcp-mcp
    permissions:
      id-token: write
    steps:
      - name: Download verified distributions
        uses: {DOWNLOAD_ACTION}
        with:
          name: {RELEASE_ARTIFACT}
          path: dist/
      - name: Publish verified distributions to PyPI
        uses: {PUBLISH_ACTION}
""".lstrip(),
        encoding="utf-8",
    )

    (root / "pyproject.toml").write_text(
        """
[project]
requires-python = ">=3.10"
classifiers = ["Operating System :: MacOS :: MacOS X"]

[project.scripts]
fcp-mcp = "fcp_mcp.cli:main"
""".lstrip(),
        encoding="utf-8",
    )
    (root / "README.md").write_text(
        "Requires macOS 15.6 or later with Final Cut Pro.\n", encoding="utf-8"
    )
    (root / "CONTRIBUTING.md").write_text(
        "Development and product verification require macOS.\n", encoding="utf-8"
    )
    (root / "ROADMAP.md").write_text(
        "Python 3.10–3.13 CI on macOS.\n", encoding="utf-8"
    )
    (root / "MANIFEST.in").write_text(
        """include README.md ROADMAP.md WORKFLOWS.md
include server.json
recursive-include examples *.py *.md
recursive-include docs *.md
recursive-include scripts *.py
""",
        encoding="utf-8",
    )


def test_repository_satisfies_macos_only_architecture_contract():
    assert check(Path(__file__).resolve().parents[2]) == []


def test_checker_reports_windows_source_and_linux_product_job(tmp_path):
    _write_valid_repository(tmp_path)
    (tmp_path / "src" / "fcp_mcp" / "bad.py").write_text(
        "kernel32 = ctypes.WinDLL('kernel32')\n", encoding="utf-8"
    )
    (tmp_path / ".github" / "workflows" / "ci.yml").write_text(
        "jobs:\n  test:\n    runs-on: ubuntu-latest\n", encoding="utf-8"
    )

    findings = check(tmp_path)

    assert any("WinDLL" in finding for finding in findings)
    assert any("product job is not macOS" in finding for finding in findings)


@pytest.mark.parametrize(
    "marker",
    ["WinDLL", "msvcrt", "_windows_", "FILE_SHARE_", "_fallback_", "reparse", "os.name"],
)
def test_checker_reports_each_forbidden_source_marker(tmp_path, marker):
    _write_valid_repository(tmp_path)
    (tmp_path / "src" / "fcp_mcp" / "bad.py").write_text(
        f"VALUE = {marker!r}\n", encoding="utf-8"
    )

    findings = check(tmp_path)

    assert any(
        finding == f"src/fcp_mcp/bad.py: forbidden source marker {marker}"
        for finding in findings
    )


def test_checker_allows_sys_platform_only_in_platform_support(tmp_path):
    _write_valid_repository(tmp_path)
    assert check(tmp_path) == []

    (tmp_path / "src" / "fcp_mcp" / "module.py").write_text(
        "import sys\nVALUE = sys.platform\n", encoding="utf-8"
    )

    assert (
        "src/fcp_mcp/module.py: forbidden source marker sys.platform"
        in check(tmp_path)
    )


def test_checker_rejects_nested_file_named_platform_support(tmp_path):
    _write_valid_repository(tmp_path)
    nested = tmp_path / "src" / "fcp_mcp" / "nested"
    nested.mkdir()
    (nested / "platform_support.py").write_text(
        "import sys\nVALUE = sys.platform\n", encoding="utf-8"
    )

    assert (
        "src/fcp_mcp/nested/platform_support.py: forbidden source marker sys.platform"
        in check(tmp_path)
    )


@pytest.mark.parametrize(
    "runs_on,strategy",
    [
        ("ubuntu-latest", ""),
        ("windows-latest", ""),
        ('"${{ matrix.os }}"', "    strategy:\n      matrix:\n        os: [macos-latest, ubuntu-latest]\n"),
        ("[macos-latest, self-hosted]", ""),
        ("\n      group: mac-runners\n      labels: macos-latest", ""),
    ],
)
def test_checker_rejects_nonliteral_ci_runner_forms(tmp_path, runs_on, strategy):
    _write_valid_repository(tmp_path)
    (tmp_path / ".github" / "workflows" / "ci.yml").write_text(
        "jobs:\n"
        "  quality:\n"
        f"    runs-on: {runs_on}\n"
        f"{strategy}"
        "    steps:\n"
        "      - run: python scripts/check_macos_only.py\n",
        encoding="utf-8",
    )

    findings = check(tmp_path)

    assert any("ci.yml: job quality product job is not macOS" in item for item in findings)


def test_checker_reads_workflows_structurally_not_by_substring(tmp_path):
    _write_valid_repository(tmp_path)
    (tmp_path / ".github" / "workflows" / "ci.yml").write_text(
        """
jobs:
  quality:
    runs-on: "${{ 'ubuntu-latest' }}"
    env:
      DECOY: macos-latest
    steps:
      - run: python scripts/check_macos_only.py
""".lstrip(),
        encoding="utf-8",
    )

    assert any("product job is not macOS" in item for item in check(tmp_path))


@pytest.mark.parametrize(
    "trigger",
    [
        "on:\n  push:\n    branches: [main]\n",
        "on:\n  push:\n",
        "on:\n  workflow_dispatch:\n",
        "on:\n  pull_request:\n",
        "on:\n  release:\n    types: [published]\n",
        "on: [push]\n",
        "on: push\n",
        "on:\n  push:\n    tags: [\"v*\"]\n  workflow_dispatch:\n",
        "on:\n  push:\n    tags: v*\n",
        'on:\n  push:\n    tags: ["${{ inputs.tags }}"]\n',
        'on:\n  push:\n    tags: ["v*", "release-*"]\n',
        'on:\n  push:\n    tags: ["v*"]\n    paths: ["src/**"]\n',
    ],
)
def test_checker_requires_exact_tag_only_publish_trigger(tmp_path, trigger):
    _write_valid_repository(tmp_path)
    publish = tmp_path / ".github" / "workflows" / "publish.yml"
    text = publish.read_text(encoding="utf-8")
    exact = 'on:\n  push:\n    tags: ["v*"]\n'
    publish.write_text(text.replace(exact, trigger), encoding="utf-8")

    assert (
        ".github/workflows/publish.yml: release trigger is not exact tag-only v*"
        in check(tmp_path)
    )


@pytest.mark.parametrize(
    "modifier",
    [
        "        if: false\n",
        "        continue-on-error: true\n",
        "        shell: bash\n",
        "        working-directory: scripts\n",
        "        env:\n          DECOY: value\n",
        "        timeout-minutes: 1\n",
        "        id: optional-gate\n",
    ],
)
@pytest.mark.parametrize("workflow_name", ["ci.yml", "publish.yml"])
def test_checker_rejects_modified_architecture_gate_step(
    tmp_path, workflow_name, modifier
):
    _write_valid_repository(tmp_path)
    workflow = tmp_path / ".github" / "workflows" / workflow_name
    text = workflow.read_text(encoding="utf-8")
    gate = "        run: python scripts/check_macos_only.py\n"
    workflow.write_text(text.replace(gate, gate + modifier, 1), encoding="utf-8")

    expected_job = "quality" if workflow_name == "ci.yml" else "verify"
    assert any(
        f"{expected_job} job does not run an unconditional architecture gate" in item
        for item in check(tmp_path)
    )


@pytest.mark.parametrize("modifier", ["    if: false\n", "    continue-on-error: true\n"])
@pytest.mark.parametrize("workflow_name", ["ci.yml", "publish.yml"])
def test_checker_rejects_gate_job_masking(tmp_path, workflow_name, modifier):
    _write_valid_repository(tmp_path)
    workflow = tmp_path / ".github" / "workflows" / workflow_name
    text = workflow.read_text(encoding="utf-8")
    job_name = "quality" if workflow_name == "ci.yml" else "verify"
    needle = f"  {job_name}:\n"
    workflow.write_text(text.replace(needle, needle + modifier, 1), encoding="utf-8")

    assert any(
        f"{job_name} job can mask architecture gate failure" in item
        for item in check(tmp_path)
    )


def test_checker_rejects_modified_duplicate_architecture_gate(tmp_path):
    _write_valid_repository(tmp_path)
    ci = tmp_path / ".github" / "workflows" / "ci.yml"
    text = ci.read_text(encoding="utf-8")
    text = text.replace(
        "      - name: Enforce macOS-only architecture\n"
        "        run: python scripts/check_macos_only.py\n",
        "      - name: Enforce macOS-only architecture\n"
        "        run: python scripts/check_macos_only.py\n"
        "      - name: Decoy architecture gate\n"
        "        run: python scripts/check_macos_only.py\n"
        "        continue-on-error: true\n",
    )
    ci.write_text(text, encoding="utf-8")

    assert any(
        "quality job does not run an unconditional architecture gate" in item
        for item in check(tmp_path)
    )


@pytest.mark.parametrize(
    "mutation,expected_marker",
    [
        ("missing_upload", "verify job lacks exact verified artifact upload"),
        ("producer_name", "verify job lacks exact verified artifact upload"),
        ("consumer_name", "publisher artifact download inputs are not isolated"),
        ("producer_path", "verify job lacks exact verified artifact upload"),
        ("producer_missing_policy", "verify job lacks exact verified artifact upload"),
        ("moving_upload_tag", "verify job lacks exact verified artifact upload"),
        ("expression_name", "verify job lacks exact verified artifact upload"),
        ("alternate_producer", "publish workflow jobs are not exactly verify and publish"),
        ("extra_upload", "verify job must contain exactly one artifact producer"),
        ("unreviewed_action", "verify job contains unreviewed action"),
        ("upload_not_last", "verify job lacks exact verified artifact upload"),
    ],
)
def test_checker_binds_published_artifact_to_exact_verify_producer(
    tmp_path, mutation, expected_marker
):
    _write_valid_repository(tmp_path)
    publish = tmp_path / ".github" / "workflows" / "publish.yml"
    text = publish.read_text(encoding="utf-8")

    if mutation == "missing_upload":
        start = text.index("      - name: Upload verified distributions\n")
        end = text.index("  publish:\n", start)
        text = text[:start] + text[end:]
    elif mutation == "producer_name":
        text = text.replace(f"name: {RELEASE_ARTIFACT}", "name: other-dist", 1)
    elif mutation == "consumer_name":
        text = text.replace(f"name: {RELEASE_ARTIFACT}", "name: other-dist", 1)
        text = text.replace(f"name: {RELEASE_ARTIFACT}", "name: other-dist", 1)
    elif mutation == "producer_path":
        text = text.replace("          path: dist/\n", "          path: build/\n", 1)
    elif mutation == "producer_missing_policy":
        text = text.replace("          if-no-files-found: error\n", "", 1)
    elif mutation == "moving_upload_tag":
        text = text.replace(UPLOAD_ACTION, "actions/upload-artifact@v7")
    elif mutation == "expression_name":
        text = text.replace(
            f"name: {RELEASE_ARTIFACT}", 'name: "${{ github.ref_name }}"', 1
        )
    elif mutation == "alternate_producer":
        text = text.replace(
            "  publish:\n",
            "  alternate:\n"
            "    runs-on: macos-latest\n"
            "    steps:\n"
            f"      - uses: {UPLOAD_ACTION}\n"
            "        with:\n"
            f"          name: {RELEASE_ARTIFACT}\n"
            "          path: dist/\n"
            "          if-no-files-found: error\n"
            "          retention-days: 1\n"
            "  publish:\n",
        )
    elif mutation == "extra_upload":
        producer = text[text.index("      - name: Upload verified distributions\n") :]
        producer = producer[: producer.index("  publish:\n")]
        text = text.replace("  publish:\n", producer + "  publish:\n")
    elif mutation == "unreviewed_action":
        text = text.replace(
            "      - name: Upload verified distributions\n",
            "      - uses: attacker/artifact-producer@0123456789012345678901234567890123456789\n"
            "      - name: Upload verified distributions\n",
        )
    elif mutation == "upload_not_last":
        text = text.replace(
            "  publish:\n",
            "      - name: Post-upload mutation\n"
            "        run: echo unsafe > dist/fcp_mcp-0.2.1.tar.gz\n"
            "  publish:\n",
        )

    publish.write_text(text, encoding="utf-8")

    assert any(expected_marker in item for item in check(tmp_path))


@pytest.mark.parametrize(
    "mutation,expected_marker",
    [
        ("publisher_run", "publisher step 2 is not the pinned PyPA publisher"),
        ("extra_action", "publisher must contain exactly two steps"),
        ("moving_tag", "publisher step 2 is not the pinned PyPA publisher"),
        ("verify_linux", "tagged verify job is not macOS"),
        ("workflow_permission", "publisher permissions are not isolated"),
        ("publisher_env", "publisher step 2 contains forbidden keys"),
        ("publisher_if", "publisher step 2 contains forbidden keys"),
        ("publisher_with", "publisher step 2 contains forbidden keys"),
        ("job_container", "publisher job contains forbidden keys"),
    ],
)
def test_checker_rejects_publish_workflow_evasions(tmp_path, mutation, expected_marker):
    _write_valid_repository(tmp_path)
    path = tmp_path / ".github" / "workflows" / "publish.yml"
    text = path.read_text(encoding="utf-8")

    if mutation == "publisher_run":
        text = text.replace(
            f"        uses: {PUBLISH_ACTION}",
            "        run: python -m twine upload dist/*",
        )
    elif mutation == "extra_action":
        text += "      - uses: attacker/arbitrary@0123456789012345678901234567890123456789\n"
    elif mutation == "moving_tag":
        text = text.replace(PUBLISH_ACTION, "pypa/gh-action-pypi-publish@release/v1")
    elif mutation == "verify_linux":
        text = text.replace("runs-on: macos-latest", "runs-on: ubuntu-latest", 1)
    elif mutation == "workflow_permission":
        text = text.replace(
            "permissions:\n  contents: read\n",
            "permissions:\n  id-token: write\n",
        ).replace(
            "    permissions:\n      id-token: write\n",
            "",
        )
    elif mutation == "publisher_env":
        text = text.replace(
            f"        uses: {PUBLISH_ACTION}",
            f"        uses: {PUBLISH_ACTION}\n        env:\n          TOKEN: exposed",
        )
    elif mutation == "publisher_if":
        text = text.replace(
            f"        uses: {PUBLISH_ACTION}",
            f"        uses: {PUBLISH_ACTION}\n        if: always()",
        )
    elif mutation == "publisher_with":
        text = text.replace(
            f"        uses: {PUBLISH_ACTION}",
            f"        uses: {PUBLISH_ACTION}\n        with:\n          repository-url: https://evil.invalid",
        )
    elif mutation == "job_container":
        text = text.replace(
            "    environment:\n",
            "    container: attacker/image\n    environment:\n",
        )

    path.write_text(text, encoding="utf-8")

    assert any(expected_marker in item for item in check(tmp_path))


def test_checker_rejects_missing_architecture_steps(tmp_path):
    _write_valid_repository(tmp_path)
    ci = tmp_path / ".github" / "workflows" / "ci.yml"
    ci.write_text(
        ci.read_text(encoding="utf-8").replace(
            "python scripts/check_macos_only.py", "python scripts/check_contracts.py"
        ),
        encoding="utf-8",
    )
    publish = tmp_path / ".github" / "workflows" / "publish.yml"
    publish.write_text(
        publish.read_text(encoding="utf-8").replace(
            "python scripts/check_macos_only.py", "python scripts/check_contracts.py"
        ),
        encoding="utf-8",
    )

    findings = check(tmp_path)

    assert any(
        "quality job does not run an unconditional architecture gate" in item
        for item in findings
    )
    assert any(
        "verify job does not run an unconditional architecture gate" in item
        for item in findings
    )


def test_checker_enforces_package_document_and_manifest_contract(tmp_path):
    _write_valid_repository(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        """
[project]
requires-python = ">=3.10"
classifiers = [
  "Operating System :: MacOS :: MacOS X",
  "Operating System :: Microsoft :: Windows",
]
[project.scripts]
fcp-mcp = "other:main"
""".lstrip(),
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text("Portable editor integration.\n", encoding="utf-8")
    (tmp_path / "CONTRIBUTING.md").write_text("Run tests anywhere.\n", encoding="utf-8")
    (tmp_path / "ROADMAP.md").write_text("CI comes later.\n", encoding="utf-8")
    (tmp_path / "MANIFEST.in").write_text("include smithery.yaml\n", encoding="utf-8")
    (tmp_path / "smithery.yaml").write_text("startCommand: unsafe\n", encoding="utf-8")

    findings = check(tmp_path)

    expected_markers = {
        "OS classifiers are not macOS-only",
        "console script is not fcp_mcp.cli:main",
        "README lacks macOS Final Cut Pro prerequisite",
        "contributing guide lacks macOS prerequisite",
        "roadmap lacks macOS CI contract",
        "manifest names smithery.yaml",
        "smithery.yaml must be absent",
    }
    assert expected_markers <= {item.split(": ", 1)[-1] for item in findings}
    assert any("manifest lacks README.md" in item for item in findings)


@pytest.mark.parametrize(
    "manifest,missing_file,missing_trees",
    [
        (
            "# README.md ROADMAP.md WORKFLOWS.md server.json examples docs scripts\n",
            True,
            {"examples", "docs", "scripts"},
        ),
        (
            (
                "exclude README.md ROADMAP.md WORKFLOWS.md server.json\n"
                "prune examples docs scripts\n"
            ),
            True,
            {"examples", "docs", "scripts"},
        ),
        (
            (
                "include xREADME.md xROADMAP.md xWORKFLOWS.md xserver.json\n"
                "recursive-include xexamples *.py\n"
                "recursive-include xdocs *.md\n"
                "recursive-include xscripts *.py\n"
            ),
            True,
            {"examples", "docs", "scripts"},
        ),
        (
            "include README.md ROADMAP.md WORKFLOWS.md server.json examples docs scripts\n",
            False,
            {"examples", "docs", "scripts"},
        ),
        (
            (
                "include README.md ROADMAP.md WORKFLOWS.md server.json\n"
                "recursive-include examples *.py *.md\n"
                "recursive-include docs *.md\n"
                "recursive-include scripts *.py\n"
                "exclude README.md\n"
                "prune docs\n"
            ),
            True,
            {"docs"},
        ),
        (
            (
                "include README.md ROADMAP.md WORKFLOWS.md server.json\n"
                "recursive-include examples *.txt\n"
                "recursive-include docs *.txt\n"
                "recursive-include scripts *.txt\n"
            ),
            False,
            {"examples", "docs", "scripts"},
        ),
    ],
)
def test_checker_parses_manifest_directives_semantically(
    tmp_path, manifest, missing_file, missing_trees
):
    _write_valid_repository(tmp_path)
    (tmp_path / "MANIFEST.in").write_text(manifest, encoding="utf-8")

    findings = check(tmp_path)

    assert any("manifest lacks README.md" in item for item in findings) is missing_file
    for tree in ("examples", "docs", "scripts"):
        assert any(
            f"manifest lacks recursive tree {tree}" in item for item in findings
        ) is (tree in missing_trees)


@pytest.mark.parametrize("suffix", [".yml", ".yaml"])
def test_checker_rejects_unexpected_workflow_files(tmp_path, suffix):
    _write_valid_repository(tmp_path)
    extra = tmp_path / ".github" / "workflows" / f"extra{suffix}"
    extra.write_text(
        "jobs:\n  product:\n    runs-on: ubuntu-latest\n", encoding="utf-8"
    )

    assert (
        f".github/workflows/extra{suffix}: unexpected workflow file"
        in check(tmp_path)
    )


def test_checker_returns_sorted_path_relative_findings_without_source_contents(tmp_path):
    _write_valid_repository(tmp_path)
    secret = "DO_NOT_LEAK_THIS_SOURCE_TEXT"
    (tmp_path / "src" / "fcp_mcp" / "z.py").write_text(
        f"{secret} = 'WinDLL'\n", encoding="utf-8"
    )
    (tmp_path / "src" / "fcp_mcp" / "a.py").write_text(
        "VALUE = 'msvcrt'\n", encoding="utf-8"
    )

    findings = check(tmp_path)

    assert findings == sorted(findings)
    assert all(not item.startswith(str(tmp_path)) for item in findings)
    assert all(secret not in item for item in findings)


def test_checker_reports_malformed_yaml_without_crashing(tmp_path):
    _write_valid_repository(tmp_path)
    (tmp_path / ".github" / "workflows" / "ci.yml").write_text(
        "jobs: [not: valid\n", encoding="utf-8"
    )

    assert any("invalid YAML" in item for item in check(tmp_path))


def test_checker_does_not_follow_source_symlink_outside_root(tmp_path):
    _write_valid_repository(tmp_path)
    outside = tmp_path.parent / f"{tmp_path.name}-outside.py"
    outside.write_text("SECRET_OUTSIDE_ROOT = 'WinDLL'\n", encoding="utf-8")
    (tmp_path / "src" / "fcp_mcp" / "linked.py").symlink_to(outside)

    findings = check(tmp_path)

    assert "src/fcp_mcp/linked.py: unsafe symlink" in findings
    assert all("SECRET_OUTSIDE_ROOT" not in item for item in findings)


def test_checker_cli_exits_nonzero_and_reports_stable_marker(tmp_path):
    _write_valid_repository(tmp_path)
    (tmp_path / "src" / "fcp_mcp" / "bad.py").write_text(
        "PRIVATE_VALUE = 'WinDLL'\n", encoding="utf-8"
    )
    script = Path(__file__).resolve().parents[2] / "scripts" / "check_macos_only.py"

    result = subprocess.run(
        [sys.executable, str(script), str(tmp_path)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "src/fcp_mcp/bad.py: forbidden source marker WinDLL" in result.stdout
    assert "PRIVATE_VALUE" not in result.stdout
