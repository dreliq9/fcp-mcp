"""Enforce the repository's macOS-only product architecture."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised by the Python 3.10 gate
    import tomli as tomllib


FORBIDDEN_SOURCE_MARKERS = (
    "WinDLL",
    "msvcrt",
    "_windows_",
    "FILE_SHARE_",
    "_fallback_",
    "reparse",
    "os.name",
)
MACOS_RUNNER = "macos-latest"
PUBLISH_RUNNER = "ubuntu-latest"
ARCHITECTURE_GATE = "python scripts/check_macos_only.py"
DOWNLOAD_ACTION = (
    "actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c"
)
PUBLISH_ACTION = (
    "pypa/gh-action-pypi-publish@ed0c53931b1dc9bd32cbe73a98c7f6766f8a527e"
)
MACOS_CLASSIFIER = "Operating System :: MacOS :: MacOS X"


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _read_text(path: Path, root: Path, findings: list[str]) -> str | None:
    relative = _relative(path, root)
    if not path.exists() and not path.is_symlink():
        findings.append(f"{relative}: required file is missing")
        return None
    if path.is_symlink():
        findings.append(f"{relative}: unsafe symlink")
        return None
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError):
        findings.append(f"{relative}: unsafe path")
        return None
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        findings.append(f"{relative}: cannot read UTF-8 text")
        return None


def _scan_source(root: Path, findings: list[str]) -> None:
    source_root = root / "src" / "fcp_mcp"
    if not source_root.is_dir() or source_root.is_symlink():
        findings.append("src/fcp_mcp: required source directory is missing or unsafe")
        return

    source_paths: list[Path] = []
    for directory, directory_names, file_names in os.walk(source_root, followlinks=False):
        base = Path(directory)
        for name in list(directory_names):
            candidate = base / name
            if candidate.is_symlink():
                findings.append(f"{_relative(candidate, root)}: unsafe symlink")
                directory_names.remove(name)
        source_paths.extend(base / name for name in file_names if name.endswith(".py"))

    for path in sorted(source_paths, key=lambda candidate: _relative(candidate, root)):
        text = _read_text(path, root, findings)
        if text is None:
            continue
        relative = _relative(path, root)
        for marker in FORBIDDEN_SOURCE_MARKERS:
            if marker in text:
                findings.append(f"{relative}: forbidden source marker {marker}")
        if relative != "src/fcp_mcp/platform_support.py" and "sys.platform" in text:
            findings.append(f"{relative}: forbidden source marker sys.platform")


def _load_yaml(path: Path, root: Path, findings: list[str]) -> dict[str, Any] | None:
    text = _read_text(path, root, findings)
    if text is None:
        return None
    try:
        loaded = YAML(typ="safe").load(text)
    except YAMLError:
        findings.append(f"{_relative(path, root)}: invalid YAML")
        return None
    if not isinstance(loaded, dict):
        findings.append(f"{_relative(path, root)}: YAML root is not a mapping")
        return None
    return loaded


def _jobs(
    workflow: dict[str, Any], relative: str, findings: list[str]
) -> dict[str, Any] | None:
    jobs = workflow.get("jobs")
    if not isinstance(jobs, dict) or not jobs:
        findings.append(f"{relative}: jobs is not a nonempty mapping")
        return None
    return jobs


def _has_architecture_gate(job: Any) -> bool:
    if not isinstance(job, dict) or not isinstance(job.get("steps"), list):
        return False
    return any(
        isinstance(step, dict) and step.get("run") == ARCHITECTURE_GATE
        for step in job["steps"]
    )


def _check_ci(root: Path, findings: list[str]) -> None:
    path = root / ".github" / "workflows" / "ci.yml"
    workflow = _load_yaml(path, root, findings)
    if workflow is None:
        return
    jobs = _jobs(workflow, ".github/workflows/ci.yml", findings)
    if jobs is None:
        return
    for job_name, job in jobs.items():
        if not isinstance(job, dict) or job.get("runs-on") != MACOS_RUNNER:
            findings.append(
                f".github/workflows/ci.yml: job {job_name} product job is not macOS"
            )
    if "quality" not in jobs:
        findings.append(".github/workflows/ci.yml: quality job is missing")
    elif not _has_architecture_gate(jobs["quality"]):
        findings.append(
            ".github/workflows/ci.yml: quality job does not run architecture gate"
        )


def _check_publish_job(job: Any, findings: list[str]) -> None:
    prefix = ".github/workflows/publish.yml: publisher"
    if not isinstance(job, dict):
        findings.append(f"{prefix} job is not a mapping")
        return
    allowed_job_keys = {
        "name",
        "needs",
        "runs-on",
        "environment",
        "permissions",
        "steps",
    }
    if set(job) - allowed_job_keys:
        findings.append(f"{prefix} job contains forbidden keys")
    if job.get("runs-on") != PUBLISH_RUNNER:
        findings.append(f"{prefix} job is not the isolated Ubuntu exception")
    if job.get("needs") != "verify":
        findings.append(f"{prefix} job does not depend only on verify")
    if job.get("permissions") != {"id-token": "write"}:
        findings.append(f"{prefix} permissions are not isolated")

    environment = job.get("environment")
    expected_environment = {"name": "pypi", "url": "https://pypi.org/p/fcp-mcp"}
    if environment != expected_environment:
        findings.append(f"{prefix} environment is not the protected PyPI environment")

    steps = job.get("steps")
    if not isinstance(steps, list) or len(steps) != 2:
        findings.append(f"{prefix} must contain exactly two steps")
        return

    download, publish = steps
    allowed_download_keys = {"name", "uses", "with"}
    if not isinstance(download, dict):
        findings.append(f"{prefix} step 1 is not a mapping")
    else:
        if set(download) - allowed_download_keys:
            findings.append(f"{prefix} step 1 contains forbidden keys")
        if download.get("uses") != DOWNLOAD_ACTION:
            findings.append(f"{prefix} step 1 is not the pinned artifact download")
        inputs = download.get("with")
        if (
            not isinstance(inputs, dict)
            or set(inputs) != {"name", "path"}
            or not isinstance(inputs.get("name"), str)
            or not inputs["name"]
            or "${{" in inputs["name"]
            or inputs.get("path") != "dist/"
        ):
            findings.append(f"{prefix} artifact download inputs are not isolated")

    allowed_publish_keys = {"name", "uses"}
    if not isinstance(publish, dict):
        findings.append(f"{prefix} step 2 is not a mapping")
    else:
        if set(publish) - allowed_publish_keys:
            findings.append(f"{prefix} step 2 contains forbidden keys")
        if publish.get("uses") != PUBLISH_ACTION:
            findings.append(f"{prefix} step 2 is not the pinned PyPA publisher")


def _check_publish(root: Path, findings: list[str]) -> None:
    path = root / ".github" / "workflows" / "publish.yml"
    workflow = _load_yaml(path, root, findings)
    if workflow is None:
        return
    permissions = workflow.get("permissions")
    if isinstance(permissions, dict) and permissions.get("id-token") == "write":
        findings.append(
            ".github/workflows/publish.yml: publisher permissions are not isolated"
        )
    jobs = _jobs(workflow, ".github/workflows/publish.yml", findings)
    if jobs is None:
        return

    verify = jobs.get("verify")
    if not isinstance(verify, dict) or verify.get("runs-on") != MACOS_RUNNER:
        findings.append(".github/workflows/publish.yml: tagged verify job is not macOS")
    if verify is None:
        findings.append(".github/workflows/publish.yml: verify job is missing")
    elif not _has_architecture_gate(verify):
        findings.append(
            ".github/workflows/publish.yml: verify job does not run architecture gate"
        )

    for job_name, job in jobs.items():
        if job_name == "publish":
            continue
        if not isinstance(job, dict) or job.get("runs-on") != MACOS_RUNNER:
            findings.append(
                f".github/workflows/publish.yml: job {job_name} product job is not macOS"
            )

    if "publish" not in jobs:
        findings.append(".github/workflows/publish.yml: publisher job is missing")
    else:
        _check_publish_job(jobs["publish"], findings)


def _check_pyproject(root: Path, findings: list[str]) -> None:
    path = root / "pyproject.toml"
    text = _read_text(path, root, findings)
    if text is None:
        return
    try:
        project = tomllib.loads(text).get("project")
    except (tomllib.TOMLDecodeError, UnicodeError):
        findings.append("pyproject.toml: invalid TOML")
        return
    if not isinstance(project, dict):
        findings.append("pyproject.toml: project table is missing")
        return
    classifiers = project.get("classifiers")
    os_classifiers = (
        [item for item in classifiers if isinstance(item, str) and item.startswith("Operating System ::")]
        if isinstance(classifiers, list)
        else []
    )
    if os_classifiers != [MACOS_CLASSIFIER]:
        findings.append("pyproject.toml: OS classifiers are not macOS-only")
    if project.get("requires-python") != ">=3.10":
        findings.append("pyproject.toml: Requires-Python is not >=3.10")
    scripts = project.get("scripts")
    if not isinstance(scripts, dict) or scripts.get("fcp-mcp") != "fcp_mcp.cli:main":
        findings.append("pyproject.toml: console script is not fcp_mcp.cli:main")


def _require_text(
    root: Path, relative: str, marker: str, finding: str, findings: list[str]
) -> None:
    text = _read_text(root / relative, root, findings)
    if text is not None and marker not in text:
        findings.append(f"{relative}: {finding}")


def _check_support_surfaces(root: Path, findings: list[str]) -> None:
    _check_pyproject(root, findings)
    _require_text(
        root,
        "README.md",
        "macOS 15.6 or later with Final Cut Pro",
        "README lacks macOS Final Cut Pro prerequisite",
        findings,
    )
    _require_text(
        root,
        "CONTRIBUTING.md",
        "macOS",
        "contributing guide lacks macOS prerequisite",
        findings,
    )
    _require_text(
        root,
        "ROADMAP.md",
        "CI on macOS",
        "roadmap lacks macOS CI contract",
        findings,
    )

    manifest_path = root / "MANIFEST.in"
    manifest = _read_text(manifest_path, root, findings)
    if manifest is not None:
        required = ("README.md", "ROADMAP.md", "WORKFLOWS.md", "server.json", "examples", "docs", "scripts")
        for marker in required:
            if marker not in manifest:
                findings.append(f"MANIFEST.in: manifest lacks {marker}")
        if "smithery.yaml" in manifest:
            findings.append("MANIFEST.in: manifest names smithery.yaml")

    smithery = root / "smithery.yaml"
    if smithery.exists() or smithery.is_symlink():
        findings.append("smithery.yaml: smithery.yaml must be absent")


def check(root: Path) -> list[str]:
    """Return deterministic macOS-only architecture findings for *root*."""

    try:
        resolved_root = Path(root).resolve(strict=True)
    except OSError:
        return [".: repository root is missing or inaccessible"]
    if not resolved_root.is_dir():
        return [".: repository root is not a directory"]

    findings: list[str] = []
    _scan_source(resolved_root, findings)
    _check_ci(resolved_root, findings)
    _check_publish(resolved_root, findings)
    _check_support_surfaces(resolved_root, findings)
    return sorted(set(findings))


def main(argv: list[str] | None = None) -> int:
    """Run the architecture gate for the repository or an explicit root."""

    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) > 1:
        print("usage: check_macos_only.py [repository-root]", file=sys.stderr)
        return 2
    root = Path(arguments[0]) if arguments else Path(__file__).resolve().parents[1]
    findings = check(root)
    for finding in findings:
        print(finding)
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
