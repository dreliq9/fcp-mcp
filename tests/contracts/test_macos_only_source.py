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
RELEASE_ARTIFACT = "fcp-mcp-v0.3.0-release-dist"


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
permissions:
  contents: read
jobs:
  quality:
    name: Static and documentation contracts
    runs-on: macos-latest
    steps:
      - uses: actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd
        with:
          persist-credentials: false
      - uses: actions/setup-python@a309ff8b426b58ec0e2a45f0f869d46889d02405
        with:
          python-version: "3.12"
          cache: pip
          cache-dependency-path: pyproject.toml
      - name: Install quality dependencies
        run: |
          python -m pip install --upgrade pip
          python -m pip install -e ".[dev]"
      - name: Ruff
        run: ruff check src tests scripts
      - name: Enforce macOS-only architecture
        run: python scripts/check_macos_only.py
      - name: Validate documented calls
        run: FCP_MCP_PROFILE=full python scripts/check_contracts.py
      - name: Validate structured tool results
        run: FCP_MCP_PROFILE=full python scripts/check_tool_results.py
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
    name: Reverify tagged release candidate
    runs-on: macos-latest
    steps:
      - uses: actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd
        with:
          persist-credentials: false
      - uses: actions/setup-python@a309ff8b426b58ec0e2a45f0f869d46889d02405
        with:
          python-version: "3.12"
          cache: pip
          cache-dependency-path: pyproject.toml
      - name: Install release dependencies
        run: |
          python -m pip install --upgrade pip
          python -m pip install -e ".[dev]"
      - name: Verify tag matches package
        run: |
          test "${{GITHUB_REF_NAME}}" = "v$(python -c 'from fcp_mcp.version import package_version; print(package_version())')"
      - name: Re-run release gates
        run: |
          ruff check src tests scripts
          FCP_MCP_PROFILE=full python scripts/check_contracts.py
          FCP_MCP_PROFILE=full python scripts/check_tool_results.py
          python -m pytest -q
          pip-audit --local
      - name: Enforce macOS-only architecture
        run: python scripts/check_macos_only.py
      - name: Rebuild distributions from tag
        run: |
          rm -rf build dist
          python -m build
          python -m twine check dist/*
      - name: Smoke-test installed wheel
        env:
          FCP_MCP_PROFILE: full
        run: |
          python -m venv /tmp/fcp-mcp-wheel-smoke
          /tmp/fcp-mcp-wheel-smoke/bin/python -m pip install --upgrade pip
          /tmp/fcp-mcp-wheel-smoke/bin/python -m pip install dist/fcp_mcp-0.3.0-py3-none-any.whl
          mkdir -p "${{RUNNER_TEMP}}/fcp-mcp-output"
          mkdir -p "${{RUNNER_TEMP}}/fcp-mcp-state"
          chmod 700 "${{RUNNER_TEMP}}/fcp-mcp-state"
          FCP_MCP_OUTPUT_DIR="${{RUNNER_TEMP}}/fcp-mcp-output" \\
          FCP_MCP_ALLOWED_ROOTS="${{RUNNER_TEMP}}/fcp-mcp-output" \\
          FCP_MCP_STATE_DIR="${{RUNNER_TEMP}}/fcp-mcp-state" \\
          FCP_MCP_ENABLE_LIVE_CONTROL=0 \\
          /tmp/fcp-mcp-wheel-smoke/bin/python scripts/wheel_smoke.py \\
            --command /tmp/fcp-mcp-wheel-smoke/bin/fcp-mcp
      - name: Verify installed wheel on Python 3.10
        uses: actions/setup-python@a309ff8b426b58ec0e2a45f0f869d46889d02405
        with:
          python-version: "3.10"
      - name: Import installed workflow package on Python 3.10
        run: |
          python -m venv /tmp/fcp-mcp-wheel-py310
          /tmp/fcp-mcp-wheel-py310/bin/python -m pip install dist/fcp_mcp-0.3.0-py3-none-any.whl
          /tmp/fcp-mcp-wheel-py310/bin/python -c "import fcp_mcp.workflow.locking"
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
    (root / "WORKFLOWS.md").write_text("Workflow guide.\n", encoding="utf-8")
    (root / "server.json").write_text("{}\n", encoding="utf-8")
    required_tree_files = {
        "examples/quickstart.py": "print('example')\n",
        "examples/README.md": "Examples.\n",
        "docs/guide.md": "Guide.\n",
        "docs/research/architecture.md": "Research.\n",
        "scripts/check_contracts.py": "print('contracts')\n",
        "scripts/check_macos_only.py": "print('architecture')\n",
    }
    for relative, contents in required_tree_files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
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


@pytest.mark.parametrize("workflow_name", ["ci.yml", "publish.yml"])
@pytest.mark.parametrize(
    "replacement",
    [
        "",
        "permissions: read-all\n",
        "permissions: write-all\n",
        "permissions: {}\n",
        "permissions:\n  id-token: write\n",
        "permissions:\n  contents: read\n  actions: read\n",
        'permissions:\n  contents: "${{ github.token }}"\n',
        (
            "x-permissions: &extra-permissions\n"
            "  actions: write\n"
            "permissions:\n"
            "  <<: *extra-permissions\n"
            "  contents: read\n"
        ),
    ],
)
def test_checker_requires_exact_workflow_permissions(
    tmp_path, workflow_name, replacement
):
    _write_valid_repository(tmp_path)
    workflow = tmp_path / ".github" / "workflows" / workflow_name
    text = workflow.read_text(encoding="utf-8")
    workflow.write_text(
        text.replace("permissions:\n  contents: read\n", replacement, 1),
        encoding="utf-8",
    )

    assert any(
        "workflow permissions are not exactly contents read" in item
        for item in check(tmp_path)
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "overwrite_before_gate",
        "overwrite_after_gate",
        "checkout_repository",
        "checkout_ref",
        "checkout_path",
        "checkout_credentials",
        "checkout_sha",
        "duplicate_checkout",
        "setup_sha",
        "setup_extra_input",
        "extra_action",
        "extra_run",
        "reordered_steps",
        "install_command",
        "ruff_command",
        "gate_command",
        "contracts_command",
        "step_env",
        "job_env",
        "job_defaults",
        "yaml_merge_job",
    ],
)
def test_checker_requires_exact_ordered_ci_quality_trajectory(tmp_path, mutation):
    _write_valid_repository(tmp_path)
    ci = tmp_path / ".github" / "workflows" / "ci.yml"
    text = ci.read_text(encoding="utf-8")
    checkout = (
        "      - uses: actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd\n"
        "        with:\n"
        "          persist-credentials: false\n"
    )
    setup = (
        "      - uses: actions/setup-python@a309ff8b426b58ec0e2a45f0f869d46889d02405\n"
        "        with:\n"
        '          python-version: "3.12"\n'
        "          cache: pip\n"
        "          cache-dependency-path: pyproject.toml\n"
    )
    install = (
        "      - name: Install quality dependencies\n"
        "        run: |\n"
        "          python -m pip install --upgrade pip\n"
        '          python -m pip install -e ".[dev]"\n'
    )
    ruff = (
        "      - name: Ruff\n"
        "        run: ruff check src tests scripts\n"
    )
    gate = (
        "      - name: Enforce macOS-only architecture\n"
        "        run: python scripts/check_macos_only.py\n"
    )

    if mutation == "overwrite_before_gate":
        text = text.replace(
            gate,
            "      - name: Replace architecture checker\n"
            "        run: printf pass > scripts/check_macos_only.py\n"
            + gate,
        )
    elif mutation == "overwrite_after_gate":
        text = text.replace(
            gate,
            gate
            + "      - name: Replace architecture checker after validation\n"
            "        run: printf pass > scripts/check_macos_only.py\n",
        )
    elif mutation == "checkout_repository":
        text = text.replace(
            "          persist-credentials: false\n",
            "          persist-credentials: false\n"
            "          repository: attacker/repository\n",
            1,
        )
    elif mutation == "checkout_ref":
        text = text.replace(
            "          persist-credentials: false\n",
            "          persist-credentials: false\n"
            "          ref: attacker-ref\n",
            1,
        )
    elif mutation == "checkout_path":
        text = text.replace(
            "          persist-credentials: false\n",
            "          persist-credentials: false\n"
            "          path: alternate\n",
            1,
        )
    elif mutation == "checkout_credentials":
        text = text.replace(
            "persist-credentials: false",
            "persist-credentials: true",
            1,
        )
    elif mutation == "checkout_sha":
        text = text.replace(
            "actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd",
            "actions/checkout@0123456789012345678901234567890123456789",
            1,
        )
    elif mutation == "duplicate_checkout":
        text = text.replace(setup, checkout + setup, 1)
    elif mutation == "setup_sha":
        text = text.replace(
            "actions/setup-python@a309ff8b426b58ec0e2a45f0f869d46889d02405",
            "actions/setup-python@0123456789012345678901234567890123456789",
            1,
        )
    elif mutation == "setup_extra_input":
        text = text.replace(
            "          cache-dependency-path: pyproject.toml\n",
            "          cache-dependency-path: pyproject.toml\n"
            "          check-latest: true\n",
            1,
        )
    elif mutation == "extra_action":
        text = text.replace(
            install,
            "      - uses: attacker/action@0123456789012345678901234567890123456789\n"
            + install,
        )
    elif mutation == "extra_run":
        text = text.replace(
            gate,
            "      - name: Extra command\n"
            "        run: echo extra\n"
            + gate,
        )
    elif mutation == "reordered_steps":
        text = text.replace(ruff + gate, gate + ruff)
    elif mutation == "install_command":
        text = text.replace(
            'python -m pip install -e ".[dev]"',
            "python -m pip install attacker-package",
            1,
        )
    elif mutation == "ruff_command":
        text = text.replace(
            "ruff check src tests scripts",
            "ruff check src",
            1,
        )
    elif mutation == "gate_command":
        text = text.replace(
            "python scripts/check_macos_only.py",
            "python -O scripts/check_macos_only.py",
            1,
        )
    elif mutation == "contracts_command":
        text = text.replace(
            "FCP_MCP_PROFILE=full python scripts/check_contracts.py",
            "python scripts/check_contracts.py",
            1,
        )
    elif mutation == "step_env":
        text = text.replace(
            "        run: ruff check src tests scripts\n",
            "        env:\n"
            "          BASH_ENV: /tmp/poison\n"
            "        run: ruff check src tests scripts\n",
            1,
        )
    elif mutation == "job_env":
        text = text.replace(
            "  quality:\n",
            "  quality:\n"
            "    env:\n"
            "      BASH_ENV: /tmp/poison\n",
            1,
        )
    elif mutation == "job_defaults":
        text = text.replace(
            "  quality:\n",
            "  quality:\n"
            "    defaults:\n"
            "      run:\n"
            "        shell: true {0}\n",
            1,
        )
    elif mutation == "yaml_merge_job":
        text = text.replace(
            "jobs:\n",
            "x-quality-poison: &quality-poison\n"
            "  env:\n"
            "    BASH_ENV: /tmp/poison\n"
            "jobs:\n",
            1,
        )
        text = text.replace(
            "  quality:\n",
            "  quality:\n"
            "    <<: *quality-poison\n",
            1,
        )

    ci.write_text(text, encoding="utf-8")

    assert (
        ".github/workflows/ci.yml: quality job differs from reviewed CI trajectory"
        in check(tmp_path)
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "checkout_missing_inputs",
        "setup_cache_dependency",
        "run_shell",
        "run_continue_on_error",
        "quality_permissions",
        "yaml_merge_step",
    ],
)
def test_checker_rejects_equivalent_ci_quality_execution_mutations(
    tmp_path, mutation
):
    _write_valid_repository(tmp_path)
    ci = tmp_path / ".github" / "workflows" / "ci.yml"
    text = ci.read_text(encoding="utf-8")

    if mutation == "checkout_missing_inputs":
        text = text.replace(
            "        with:\n"
            "          persist-credentials: false\n",
            "",
            1,
        )
    elif mutation == "setup_cache_dependency":
        text = text.replace(
            "cache-dependency-path: pyproject.toml",
            "cache-dependency-path: requirements.txt",
            1,
        )
    elif mutation == "run_shell":
        text = text.replace(
            "      - name: Ruff\n",
            "      - name: Ruff\n"
            "        shell: bash -c 'exit 0; # {0}'\n",
            1,
        )
    elif mutation == "run_continue_on_error":
        text = text.replace(
            "      - name: Ruff\n",
            "      - name: Ruff\n"
            "        continue-on-error: true\n",
            1,
        )
    elif mutation == "quality_permissions":
        text = text.replace(
            "  quality:\n",
            "  quality:\n"
            "    permissions:\n"
            "      contents: write\n",
            1,
        )
    elif mutation == "yaml_merge_step":
        text = text.replace(
            "permissions:\n",
            "x-step-poison: &step-poison\n"
            "  env:\n"
            "    BASH_ENV: /tmp/poison\n"
            "permissions:\n",
            1,
        )
        text = text.replace(
            "      - name: Ruff\n",
            "      - name: Ruff\n"
            "        <<: *step-poison\n",
            1,
        )

    ci.write_text(text, encoding="utf-8")

    assert (
        ".github/workflows/ci.yml: quality job differs from reviewed CI trajectory"
        in check(tmp_path)
    )


@pytest.mark.parametrize(
    "mutation,expected_marker",
    [
        (
            "verify_contents_write",
            "verify job differs from reviewed release trajectory",
        ),
        (
            "publisher_contents_read",
            "publisher permissions are not isolated",
        ),
        (
            "publisher_oidc_expression",
            "publisher permissions are not isolated",
        ),
        (
            "publisher_read_all",
            "publisher permissions are not isolated",
        ),
    ],
)
def test_checker_preserves_exact_job_permission_isolation(
    tmp_path, mutation, expected_marker
):
    _write_valid_repository(tmp_path)
    publish = tmp_path / ".github" / "workflows" / "publish.yml"
    text = publish.read_text(encoding="utf-8")

    if mutation == "verify_contents_write":
        text = text.replace(
            "  verify:\n",
            "  verify:\n"
            "    permissions:\n"
            "      contents: write\n",
            1,
        )
    elif mutation == "publisher_contents_read":
        text = text.replace(
            "    permissions:\n"
            "      id-token: write\n",
            "    permissions:\n"
            "      id-token: write\n"
            "      contents: read\n",
            1,
        )
    elif mutation == "publisher_oidc_expression":
        text = text.replace(
            "      id-token: write\n",
            '      id-token: "${{ inputs.oidc }}"\n',
            1,
        )
    elif mutation == "publisher_read_all":
        text = text.replace(
            "    permissions:\n"
            "      id-token: write\n",
            "    permissions: read-all\n",
            1,
        )

    publish.write_text(text, encoding="utf-8")

    assert any(expected_marker in item for item in check(tmp_path))


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


@pytest.mark.parametrize("workflow_name", ["ci.yml", "publish.yml"])
@pytest.mark.parametrize(
    "defaults",
    [
        "defaults:\n  run:\n    shell: true {0}\n",
        "defaults:\n  run:\n    working-directory: /tmp\n",
    ],
)
def test_checker_rejects_workflow_level_run_defaults(
    tmp_path, workflow_name, defaults
):
    _write_valid_repository(tmp_path)
    workflow = tmp_path / ".github" / "workflows" / workflow_name
    text = workflow.read_text(encoding="utf-8")
    workflow.write_text(text.replace("jobs:\n", defaults + "jobs:\n", 1), encoding="utf-8")

    assert any("workflow defaults can alter architecture gate" in item for item in check(tmp_path))


@pytest.mark.parametrize("workflow_name", ["ci.yml", "publish.yml"])
@pytest.mark.parametrize(
    "defaults",
    [
        "    defaults:\n      run:\n        shell: true {0}\n",
        "    defaults:\n      run:\n        working-directory: /tmp\n",
        (
            "    defaults: &poison\n"
            "      run:\n"
            "        shell: true {0}\n"
        ),
    ],
)
def test_checker_rejects_gate_job_run_defaults(
    tmp_path, workflow_name, defaults
):
    _write_valid_repository(tmp_path)
    workflow = tmp_path / ".github" / "workflows" / workflow_name
    text = workflow.read_text(encoding="utf-8")
    job_name = "quality" if workflow_name == "ci.yml" else "verify"
    needle = f"  {job_name}:\n"
    workflow.write_text(text.replace(needle, needle + defaults, 1), encoding="utf-8")

    assert any(
        f"{job_name} job can alter architecture gate execution" in item
        for item in check(tmp_path)
    )


@pytest.mark.parametrize("workflow_name", ["ci.yml", "publish.yml"])
def test_checker_rejects_aliased_gate_job_defaults(tmp_path, workflow_name):
    _write_valid_repository(tmp_path)
    workflow = tmp_path / ".github" / "workflows" / workflow_name
    text = workflow.read_text(encoding="utf-8")
    text = text.replace(
        "jobs:\n",
        "x-poison: &poison\n"
        "  run:\n"
        "    working-directory: /tmp\n"
        "jobs:\n",
        1,
    )
    job_name = "quality" if workflow_name == "ci.yml" else "verify"
    text = text.replace(f"  {job_name}:\n", f"  {job_name}:\n    defaults: *poison\n", 1)
    workflow.write_text(text, encoding="utf-8")

    assert any(
        f"{job_name} job can alter architecture gate execution" in item
        for item in check(tmp_path)
    )


@pytest.mark.parametrize("workflow_name", ["ci.yml", "publish.yml"])
@pytest.mark.parametrize(
    "scope,needle,replacement",
    [
        (
            "workflow",
            "jobs:\n",
            "env:\n  BASH_ENV: /tmp/neutralize-gate\njobs:\n",
        ),
        (
            "job",
            "JOB_NEEDLE",
            "JOB_NEEDLE    env:\n      BASH_ENV: /tmp/neutralize-gate\n",
        ),
    ],
)
def test_checker_rejects_inherited_gate_environment(
    tmp_path, workflow_name, scope, needle, replacement
):
    _write_valid_repository(tmp_path)
    workflow = tmp_path / ".github" / "workflows" / workflow_name
    text = workflow.read_text(encoding="utf-8")
    job_name = "quality" if workflow_name == "ci.yml" else "verify"
    if scope == "job":
        needle = f"  {job_name}:\n"
        replacement = replacement.replace("JOB_NEEDLE", needle)
    workflow.write_text(text.replace(needle, replacement, 1), encoding="utf-8")

    assert any("can alter architecture gate" in item for item in check(tmp_path))


@pytest.mark.parametrize("workflow_name", ["ci.yml", "publish.yml"])
def test_checker_rejects_yaml_merge_inherited_job_defaults(tmp_path, workflow_name):
    _write_valid_repository(tmp_path)
    workflow = tmp_path / ".github" / "workflows" / workflow_name
    text = workflow.read_text(encoding="utf-8")
    text = text.replace(
        "jobs:\n",
        "x-job-poison: &job-poison\n"
        "  defaults:\n"
        "    run:\n"
        "      shell: true {0}\n"
        "jobs:\n",
        1,
    )
    job_name = "quality" if workflow_name == "ci.yml" else "verify"
    text = text.replace(
        f"  {job_name}:\n",
        f"  {job_name}:\n    <<: *job-poison\n",
        1,
    )
    workflow.write_text(text, encoding="utf-8")

    assert any(
        f"{job_name} job can alter architecture gate execution" in item
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
            "        run: echo unsafe > dist/fcp_mcp-0.3.0.tar.gz\n"
            "  publish:\n",
        )

    publish.write_text(text, encoding="utf-8")

    assert any(expected_marker in item for item in check(tmp_path))


@pytest.mark.parametrize(
    "mutation",
    [
        "checkout_repository",
        "checkout_ref",
        "checkout_path",
        "duplicate_checkout",
        "duplicate_setup",
        "setup_version",
        "setup_cache",
        "poison_run",
        "renamed_run",
        "reordered_run",
        "install_command",
        "gate_command",
        "build_command",
        "smoke_command",
        "extra_run",
    ],
)
def test_checker_requires_exact_ordered_verify_trajectory(tmp_path, mutation):
    _write_valid_repository(tmp_path)
    publish = tmp_path / ".github" / "workflows" / "publish.yml"
    text = publish.read_text(encoding="utf-8")
    checkout = (
        "      - uses: actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd\n"
        "        with:\n"
        "          persist-credentials: false\n"
    )
    setup = (
        "      - uses: actions/setup-python@a309ff8b426b58ec0e2a45f0f869d46889d02405\n"
        "        with:\n"
        '          python-version: "3.12"\n'
        "          cache: pip\n"
        "          cache-dependency-path: pyproject.toml\n"
    )
    install = (
        "      - name: Install release dependencies\n"
        "        run: |\n"
        "          python -m pip install --upgrade pip\n"
        '          python -m pip install -e ".[dev]"\n'
    )
    tag_check = (
        "      - name: Verify tag matches package\n"
        "        run: |\n"
        "          test \"${GITHUB_REF_NAME}\" = \"v$(python -c 'from fcp_mcp.version import package_version; print(package_version())')\"\n"
    )

    if mutation == "checkout_repository":
        text = text.replace(
            "          persist-credentials: false\n",
            "          persist-credentials: false\n          repository: attacker/repo\n",
            1,
        )
    elif mutation == "checkout_ref":
        text = text.replace(
            "          persist-credentials: false\n",
            "          persist-credentials: false\n          ref: main\n",
            1,
        )
    elif mutation == "checkout_path":
        text = text.replace(
            "          persist-credentials: false\n",
            "          persist-credentials: false\n          path: alternate\n",
            1,
        )
    elif mutation == "duplicate_checkout":
        text = text.replace(setup, checkout + setup, 1)
    elif mutation == "duplicate_setup":
        text = text.replace(install, setup + install, 1)
    elif mutation == "setup_version":
        text = text.replace('python-version: "3.12"', 'python-version: "3.13"', 1)
    elif mutation == "setup_cache":
        text = text.replace("cache: pip", "cache: poetry", 1)
    elif mutation == "poison_run":
        text = text.replace(
            "      - name: Upload verified distributions\n",
            "      - name: Replace verified distributions\n"
            "        run: printf malicious > dist/release.whl\n"
            "      - name: Upload verified distributions\n",
        )
    elif mutation == "renamed_run":
        text = text.replace(
            "name: Install release dependencies",
            "name: Maybe install release dependencies",
            1,
        )
    elif mutation == "reordered_run":
        text = text.replace(install + tag_check, tag_check + install, 1)
    elif mutation == "install_command":
        text = text.replace(
            'python -m pip install -e ".[dev]"',
            "python -m pip install attacker-package",
            1,
        )
    elif mutation == "gate_command":
        text = text.replace(
            "FCP_MCP_PROFILE=full python scripts/check_contracts.py",
            "python scripts/check_contracts.py",
            1,
        )
    elif mutation == "build_command":
        text = text.replace("python -m build", "python setup.py bdist_wheel", 1)
    elif mutation == "smoke_command":
        text = text.replace(
            "FCP_MCP_ENABLE_LIVE_CONTROL=0",
            "FCP_MCP_ENABLE_LIVE_CONTROL=1",
            1,
        )
    elif mutation == "extra_run":
        text = text.replace(
            "      - name: Rebuild distributions from tag\n",
            "      - name: Extra command\n"
            "        run: echo extra\n"
            "      - name: Rebuild distributions from tag\n",
        )
    publish.write_text(text, encoding="utf-8")

    assert (
        ".github/workflows/publish.yml: verify job differs from reviewed release trajectory"
        in check(tmp_path)
    )


@pytest.mark.parametrize(
    "mutation",
    ["checkout_credentials", "checkout_missing_inputs", "setup_extra_input"],
)
def test_checker_rejects_exact_action_with_unsafe_input_variant(tmp_path, mutation):
    _write_valid_repository(tmp_path)
    publish = tmp_path / ".github" / "workflows" / "publish.yml"
    text = publish.read_text(encoding="utf-8")
    if mutation == "checkout_credentials":
        text = text.replace("persist-credentials: false", "persist-credentials: true", 1)
    elif mutation == "checkout_missing_inputs":
        text = text.replace(
            "        with:\n          persist-credentials: false\n",
            "",
            1,
        )
    elif mutation == "setup_extra_input":
        text = text.replace(
            "          cache-dependency-path: pyproject.toml\n",
            "          cache-dependency-path: pyproject.toml\n"
            "          check-latest: true\n",
            1,
        )
    publish.write_text(text, encoding="utf-8")

    assert (
        ".github/workflows/publish.yml: verify job differs from reviewed release trajectory"
        in check(tmp_path)
    )


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


@pytest.mark.parametrize(
    "directive,missing_file,missing_docs",
    [
        ("exclude *.md\n", True, False),
        ("recursive-exclude docs *\n", False, True),
        ("recursive-exclude docs *.m?\n", False, True),
        ("recursive-exclude docs [ag]*.md\n", False, True),
        ("recursive-exclude docs **/*.md\n", False, True),
        ("prune docs/research\n", False, True),
        ("global-exclude *.md\n", True, True),
    ],
)
def test_checker_applies_manifest_globs_to_real_required_paths(
    tmp_path, directive, missing_file, missing_docs
):
    _write_valid_repository(tmp_path)
    manifest = tmp_path / "MANIFEST.in"
    manifest.write_text(
        manifest.read_text(encoding="utf-8") + directive,
        encoding="utf-8",
    )

    findings = check(tmp_path)

    assert any("manifest lacks README.md" in item for item in findings) is missing_file
    assert any("manifest lacks recursive tree docs" in item for item in findings) is missing_docs


@pytest.mark.parametrize(
    "tail,missing_readme,missing_docs",
    [
        ("exclude README.md\ninclude README.md\n", False, False),
        ("include README.md\nexclude README.md\n", True, False),
        ("prune docs\ngraft docs\n", False, False),
        ("graft docs\nprune docs\n", False, True),
        (
            "recursive-exclude docs *.md\nrecursive-include docs *.md\n",
            False,
            False,
        ),
        (
            "recursive-include docs *.md\nrecursive-exclude docs *.md\n",
            False,
            True,
        ),
        ("global-exclude *.md\nglobal-include *.md\n", False, False),
        ("global-include *.md\nglobal-exclude *.md\n", True, True),
    ],
)
def test_checker_applies_manifest_add_remove_directives_in_order(
    tmp_path, tail, missing_readme, missing_docs
):
    _write_valid_repository(tmp_path)
    manifest = tmp_path / "MANIFEST.in"
    manifest.write_text(
        manifest.read_text(encoding="utf-8") + tail,
        encoding="utf-8",
    )

    findings = check(tmp_path)

    assert any("manifest lacks README.md" in item for item in findings) is missing_readme
    assert any("manifest lacks recursive tree docs" in item for item in findings) is missing_docs


@pytest.mark.parametrize(
    "tail,missing_docs",
    [
        ("prune docs\ngraft .\n", False),
        ("prune docs\nrecursive-include . **/*.md\n", False),
        ("prune docs/researc?\n", True),
        ("prune docs/[a-z]*\n", True),
        ("prune docs/**\n", True),
    ],
)
def test_checker_handles_root_and_wildcard_manifest_directories(
    tmp_path, tail, missing_docs
):
    _write_valid_repository(tmp_path)
    manifest = tmp_path / "MANIFEST.in"
    manifest.write_text(
        manifest.read_text(encoding="utf-8") + tail,
        encoding="utf-8",
    )

    findings = check(tmp_path)

    assert any("manifest lacks recursive tree docs" in item for item in findings) is missing_docs


@pytest.mark.parametrize(
    "tail",
    [
        "exclude **/*.md\n",
        "global-exclude *.[mM][dD]\n",
    ],
)
def test_checker_handles_double_star_and_class_manifest_excludes(tmp_path, tail):
    _write_valid_repository(tmp_path)
    manifest = tmp_path / "MANIFEST.in"
    manifest.write_text(
        manifest.read_text(encoding="utf-8") + tail,
        encoding="utf-8",
    )

    findings = check(tmp_path)

    assert any("manifest lacks README.md" in item for item in findings)
    assert any("manifest lacks recursive tree docs" in item for item in findings)


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


@pytest.mark.parametrize(
    "control,escaped", [("\n", r"\n"), ("\t", r"\t"), ("\r", r"\r")]
)
def test_checker_escapes_control_characters_in_finding_paths(
    tmp_path, control, escaped
):
    _write_valid_repository(tmp_path)
    filename = f"evil{control}FORGED.yml"
    (tmp_path / ".github" / "workflows" / filename).write_text(
        "jobs:\n  product:\n    runs-on: ubuntu-latest\n", encoding="utf-8"
    )

    findings = check(tmp_path)

    expected = f".github/workflows/evil{escaped}FORGED.yml: unexpected workflow file"
    assert expected in findings
    assert all(control not in finding for finding in findings)


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
