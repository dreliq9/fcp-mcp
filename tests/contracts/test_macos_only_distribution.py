from __future__ import annotations

import ast
from pathlib import Path

import pytest
from ruamel.yaml import YAML

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[2]
FULL_PROFILE_CHECK = "FCP_MCP_PROFILE=full python scripts/check_contracts.py"
PUBLISHER_ENVIRONMENT = {
    "name": "pypi",
    "url": "https://pypi.org/p/fcp-mcp",
}
PUBLISH_STEPS = [
    {
        "name": "Download verified distributions",
        "uses": (
            "actions/download-artifact@"
            "3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c"
        ),
        "with": {
            "name": "fcp-mcp-v0.2.1-release-dist",
            "path": "dist/",
        },
    },
    {
        "name": "Publish verified distributions to PyPI",
        "uses": (
            "pypa/gh-action-pypi-publish@"
            "ed0c53931b1dc9bd32cbe73a98c7f6766f8a527e"
        ),
    },
]


def _normalize_requirement_name(requirement: str) -> str:
    name = requirement.split(";", maxsplit=1)[0]
    for separator in ("[", "<", ">", "=", "!", "~", " "):
        name = name.split(separator, maxsplit=1)[0]
    return name.lower().replace("_", "-").replace(".", "-")


def _assert_workflow_parser_contract(
    project: dict[str, object],
    module: ast.Module,
) -> None:
    dev_dependencies = project["project"]["optional-dependencies"]["dev"]
    assert "ruamel.yaml>=0.18.6,<0.20" in dev_dependencies
    assert "tomli>=2.0.1,<3; python_version < '3.11'" in dev_dependencies
    assert all(
        _normalize_requirement_name(requirement) not in {"ruamel-yaml", "tomli"}
        for requirement in project["project"]["dependencies"]
    )

    imported_modules = {
        alias.name
        for node in ast.walk(module)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported_modules.update(
        node.module
        for node in ast.walk(module)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    )
    assert "subprocess" not in imported_modules


def test_workflow_parser_is_declared_dev_only_and_does_not_shell_out():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    module = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    _assert_workflow_parser_contract(project, module)


@pytest.mark.parametrize("dependency", ("ruamel.yaml>=0.18.6,<0.20", "tomli>=2.0.1,<3"))
def test_workflow_parser_contract_rejects_production_promotion(dependency: str):
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    project["project"]["dependencies"].append(dependency)

    with pytest.raises(AssertionError):
        _assert_workflow_parser_contract(project, ast.parse(""))


@pytest.mark.parametrize("import_statement", ("import subprocess", "from subprocess import run"))
def test_workflow_parser_contract_rejects_subprocess_import(import_statement: str):
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())

    with pytest.raises(AssertionError):
        _assert_workflow_parser_contract(
            project,
            ast.parse(import_statement),
        )


def test_runtime_self_compatibility_dependency_is_direct_and_bounded():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())

    assert "typing_extensions>=4.5,<5" in project["project"]["dependencies"]


def _load_workflow(relative: str) -> dict[str, object]:
    parser = YAML(typ="safe", pure=True)
    workflow = parser.load((ROOT / relative).read_text(encoding="utf-8"))
    assert isinstance(workflow, dict)
    return workflow


def _assert_isolated_publish_job(publish: dict[str, object]) -> None:
    assert publish["needs"] == "verify"
    assert publish["permissions"] == {"id-token": "write"}
    assert publish["environment"] == PUBLISHER_ENVIRONMENT
    assert publish["steps"] == PUBLISH_STEPS


def _step_named(steps: list[dict[str, object]], name: str) -> dict[str, object]:
    return next(step for step in steps if step.get("name") == name)


def test_package_and_active_docs_claim_only_macos():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    classifiers = project["project"]["classifiers"]
    assert "Operating System :: MacOS :: MacOS X" in classifiers
    assert "Operating System :: OS Independent" not in classifiers

    for relative in ("README.md", "CONTRIBUTING.md", "ROADMAP.md"):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert "Windows" not in text
        assert "Linux" not in text
        assert "work anywhere" not in text

    assert not (ROOT / "smithery.yaml").exists()
    assert "smithery.yaml" not in (ROOT / "MANIFEST.in").read_text()


def test_only_macos_jobs_execute_product():
    ci = _load_workflow(".github/workflows/ci.yml")
    ci_jobs = ci["jobs"]
    assert isinstance(ci_jobs, dict)
    assert {job["runs-on"] for job in ci_jobs.values()} == {"macos-latest"}

    workflow = _load_workflow(".github/workflows/publish.yml")
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    ubuntu_jobs = {
        name: job
        for name, job in jobs.items()
        if job["runs-on"] == "ubuntu-latest"
    }
    assert ubuntu_jobs.keys() == {"publish"}
    assert jobs["verify"]["runs-on"] == "macos-latest"
    _assert_isolated_publish_job(ubuntu_jobs["publish"])


def test_isolated_publish_contract_rejects_an_extra_action():
    workflow = _load_workflow(".github/workflows/publish.yml")
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    publish = jobs["publish"]
    publish["steps"].append(
        {
            "uses": (
                "attacker/example-action@"
                "0123456789012345678901234567890123456789"
            ),
        }
    )

    with pytest.raises(AssertionError):
        _assert_isolated_publish_job(publish)


def test_active_contract_checks_use_the_full_documentation_profile():
    ci = _load_workflow(".github/workflows/ci.yml")
    ci_jobs = ci["jobs"]
    assert isinstance(ci_jobs, dict)
    quality_steps = ci_jobs["quality"]["steps"]
    assert isinstance(quality_steps, list)
    assert _step_named(quality_steps, "Validate documented calls")["run"] == (
        FULL_PROFILE_CHECK
    )

    publish = _load_workflow(".github/workflows/publish.yml")
    publish_jobs = publish["jobs"]
    assert isinstance(publish_jobs, dict)
    verify_steps = publish_jobs["verify"]["steps"]
    assert isinstance(verify_steps, list)
    assert FULL_PROFILE_CHECK in _step_named(
        verify_steps,
        "Re-run release gates",
    )["run"]

    contributing = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    assert FULL_PROFILE_CHECK in contributing
