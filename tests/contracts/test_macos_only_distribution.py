from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import tomllib

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


def _load_workflow(relative: str) -> dict[str, object]:
    rendered = subprocess.run(
        [
            "ruby",
            "-rjson",
            "-ryaml",
            "-e",
            (
                "puts JSON.generate("
                "Psych.safe_load(File.read(ARGV.fetch(0)), aliases: true)"
                ")"
            ),
            str(ROOT / relative),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(rendered.stdout)


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
