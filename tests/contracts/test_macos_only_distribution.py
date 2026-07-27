from __future__ import annotations

import re
from pathlib import Path

import tomllib

ROOT = Path(__file__).resolve().parents[2]


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
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    assert "ubuntu-latest" not in ci
    assert "windows-latest" not in ci
    assert set(re.findall(r"runs-on:\s*([^\s]+)", ci)) == {
        "macos-latest"
    }

    publish = (ROOT / ".github/workflows/publish.yml").read_text()
    assert publish.count("runs-on: ubuntu-latest") == 1
    verify, isolated_publish = publish.split("  publish:", maxsplit=1)
    assert "runs-on: macos-latest" in verify
    assert "runs-on: ubuntu-latest" in isolated_publish
    for forbidden in (
        "actions/checkout",
        "actions/setup-python",
        "pip install",
        "python -m",
        "run:",
    ):
        assert forbidden not in isolated_publish
