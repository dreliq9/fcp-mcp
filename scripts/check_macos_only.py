"""Enforce the repository's macOS-only product architecture."""

from __future__ import annotations

import fnmatch
import json
import os
import shlex
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
UPLOAD_ACTION = (
    "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a"
)
CHECKOUT_ACTION = "actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd"
SETUP_PYTHON_ACTION = (
    "actions/setup-python@a309ff8b426b58ec0e2a45f0f869d46889d02405"
)
PUBLISH_ACTION = (
    "pypa/gh-action-pypi-publish@ed0c53931b1dc9bd32cbe73a98c7f6766f8a527e"
)
RELEASE_ARTIFACT = "fcp-mcp-v0.3.0-release-dist"
RELEASE_MANIFEST = "fcp-mcp-v0.3.0.sha256"
REGISTRY_WORKFLOW = ".github/workflows/publish-registry.yml"
REGISTRY_PUBLISHER_DIGEST = (
    "a06c9096dcb9727c13555b6be26c7effa707b01f06a4c561ba7a3635443cf2cc"
)
REGISTRY_RELEASE_SHA_PATTERN = "[0-9a-f]" * 40
MACOS_CLASSIFIER = "Operating System :: MacOS :: MacOS X"
WORKFLOW_PERMISSIONS = {"contents": "read"}


def _relative(path: Path, root: Path) -> str:
    relative = path.relative_to(root).as_posix()
    return json.dumps(relative, ensure_ascii=True)[1:-1]


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
        yaml = YAML(typ="safe")
        yaml.version = (1, 2)
        loaded = yaml.load(text)
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
    expected = {
        "name": "Enforce macOS-only architecture",
        "run": ARCHITECTURE_GATE,
    }
    invocations = [
        step
        for step in job["steps"]
        if isinstance(step, dict) and step.get("run") == ARCHITECTURE_GATE
    ]
    return invocations == [expected]


def _check_gate_job(
    job: Any, relative: str, job_name: str, findings: list[str]
) -> None:
    if isinstance(job, dict) and ({"if", "continue-on-error"} & set(job)):
        findings.append(f"{relative}: {job_name} job can mask architecture gate failure")
    if isinstance(job, dict) and ({"defaults", "env"} & set(job)):
        findings.append(f"{relative}: {job_name} job can alter architecture gate execution")
    if not _has_architecture_gate(job):
        findings.append(
            f"{relative}: {job_name} job does not run an unconditional architecture gate"
        )


def _check_workflow_inventory(root: Path, findings: list[str]) -> None:
    directory = root / ".github" / "workflows"
    if not directory.is_dir() or directory.is_symlink():
        findings.append(
            ".github/workflows: required workflow directory is missing or unsafe"
        )
        return
    expected = {"ci.yml", "publish.yml", "publish-registry.yml"}
    try:
        entries = sorted(directory.iterdir(), key=lambda path: path.name)
    except OSError:
        findings.append(".github/workflows: cannot enumerate workflow directory")
        return
    for path in entries:
        if path.suffix not in {".yml", ".yaml"}:
            continue
        if path.name not in expected:
            findings.append(f"{_relative(path, root)}: unexpected workflow file")


def _check_ci(root: Path, findings: list[str]) -> None:
    path = root / ".github" / "workflows" / "ci.yml"
    workflow = _load_yaml(path, root, findings)
    if workflow is None:
        return
    if "defaults" in workflow:
        findings.append(
            ".github/workflows/ci.yml: workflow defaults can alter architecture gate"
        )
    if "env" in workflow:
        findings.append(
            ".github/workflows/ci.yml: workflow environment can alter architecture gate"
        )
    if workflow.get("permissions") != WORKFLOW_PERMISSIONS:
        findings.append(
            ".github/workflows/ci.yml: workflow permissions are not exactly contents read"
        )
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
    else:
        _check_gate_job(
            jobs["quality"],
            ".github/workflows/ci.yml",
            "quality",
            findings,
        )
        if jobs["quality"] != _expected_quality_job():
            findings.append(
                ".github/workflows/ci.yml: quality job differs from reviewed CI trajectory"
            )


def _expected_quality_job() -> dict[str, Any]:
    return {
        "name": "Static and documentation contracts",
        "runs-on": MACOS_RUNNER,
        "steps": [
            {
                "uses": CHECKOUT_ACTION,
                "with": {"persist-credentials": False},
            },
            {
                "uses": SETUP_PYTHON_ACTION,
                "with": {
                    "python-version": "3.12",
                    "cache": "pip",
                    "cache-dependency-path": "pyproject.toml",
                },
            },
            {
                "name": "Install quality dependencies",
                "run": (
                    "python -m pip install --upgrade pip\n"
                    'python -m pip install -e ".[dev]"\n'
                ),
            },
            {
                "name": "Ruff",
                "run": "ruff check src tests scripts",
            },
            {
                "name": "Enforce macOS-only architecture",
                "run": ARCHITECTURE_GATE,
            },
            {
                "name": "Validate documented calls",
                "run": "FCP_MCP_PROFILE=full python scripts/check_contracts.py",
            },
            {
                "name": "Validate structured tool results",
                "run": "FCP_MCP_PROFILE=full python scripts/check_tool_results.py",
            },
        ],
    }


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
            or inputs.get("name") != RELEASE_ARTIFACT
            or inputs.get("path") != "."
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


def _check_verify_producer(verify: Any, findings: list[str]) -> None:
    prefix = ".github/workflows/publish.yml: verify"
    if not isinstance(verify, dict) or not isinstance(verify.get("steps"), list):
        findings.append(f"{prefix} job lacks exact verified artifact upload")
        return
    steps = verify["steps"]
    producers = [
        step
        for step in steps
        if isinstance(step, dict)
        and isinstance(step.get("uses"), str)
        and step["uses"].startswith("actions/upload-artifact@")
    ]
    if len(producers) != 1:
        findings.append(f"{prefix} job must contain exactly one artifact producer")
    expected = {
        "name": "Upload verified distributions",
        "uses": UPLOAD_ACTION,
        "with": {
            "name": RELEASE_ARTIFACT,
            "path": (
                "dist/fcp_mcp-0.3.0-py3-none-any.whl\n"
                "dist/fcp_mcp-0.3.0.tar.gz\n"
                f"{RELEASE_MANIFEST}\n"
            ),
            "if-no-files-found": "error",
            "retention-days": 7,
        },
    }
    allowed_actions = {CHECKOUT_ACTION, SETUP_PYTHON_ACTION, UPLOAD_ACTION}
    if any(
        isinstance(step, dict)
        and "uses" in step
        and step.get("uses") not in allowed_actions
        for step in steps
    ):
        findings.append(f"{prefix} job contains unreviewed action")
    if not steps or steps[-1] != expected:
        findings.append(f"{prefix} job lacks exact verified artifact upload")


def _expected_verify_job() -> dict[str, Any]:
    return {
        "name": "Reverify tagged release candidate",
        "runs-on": MACOS_RUNNER,
        "steps": [
            {
                "uses": CHECKOUT_ACTION,
                "with": {"persist-credentials": False},
            },
            {
                "uses": SETUP_PYTHON_ACTION,
                "with": {
                    "python-version": "3.12",
                    "cache": "pip",
                    "cache-dependency-path": "pyproject.toml",
                },
            },
            {
                "name": "Install release dependencies",
                "run": (
                    "python -m pip install --upgrade pip\n"
                    'python -m pip install -e ".[dev]"\n'
                ),
            },
            {
                "name": "Verify tag matches package",
                "run": (
                    'test "${GITHUB_REF_NAME}" = "v$(python -c \'from '
                    "fcp_mcp.version import package_version; "
                    "print(package_version())')\"\n"
                ),
            },
            {
                "name": "Re-run release gates",
                "run": (
                    "ruff check src tests scripts\n"
                    "FCP_MCP_PROFILE=full python scripts/check_contracts.py\n"
                    "FCP_MCP_PROFILE=full python scripts/check_tool_results.py\n"
                    "python -m pytest -q\n"
                    "pip-audit --local\n"
                ),
            },
            {
                "name": "Enforce macOS-only architecture",
                "run": ARCHITECTURE_GATE,
            },
            {
                "name": "Rebuild distributions from tag",
                "run": (
                    "rm -rf build dist\n"
                    "python -m build\n"
                    "python -m twine check dist/*\n"
                ),
            },
            {
                "name": "Smoke-test installed wheel",
                "env": {"FCP_MCP_PROFILE": "full"},
                "run": (
                    "python -m venv /tmp/fcp-mcp-wheel-smoke\n"
                    "/tmp/fcp-mcp-wheel-smoke/bin/python -m pip install --upgrade pip\n"
                    "/tmp/fcp-mcp-wheel-smoke/bin/python -m pip install "
                    "dist/fcp_mcp-0.3.0-py3-none-any.whl\n"
                    'mkdir -p "${RUNNER_TEMP}/fcp-mcp-output"\n'
                    'mkdir -p "${RUNNER_TEMP}/fcp-mcp-state"\n'
                    'chmod 700 "${RUNNER_TEMP}/fcp-mcp-state"\n'
                    'FCP_MCP_OUTPUT_DIR="${RUNNER_TEMP}/fcp-mcp-output" \\\n'
                    'FCP_MCP_ALLOWED_ROOTS="${RUNNER_TEMP}/fcp-mcp-output" \\\n'
                    'FCP_MCP_STATE_DIR="${RUNNER_TEMP}/fcp-mcp-state" \\\n'
                    "FCP_MCP_ENABLE_LIVE_CONTROL=0 \\\n"
                    "/tmp/fcp-mcp-wheel-smoke/bin/python scripts/wheel_smoke.py \\\n"
                    "  --command /tmp/fcp-mcp-wheel-smoke/bin/fcp-mcp\n"
                ),
            },
            {
                "name": "Verify installed wheel on Python 3.10",
                "uses": SETUP_PYTHON_ACTION,
                "with": {"python-version": "3.10"},
            },
            {
                "name": "Import installed workflow package on Python 3.10",
                "run": (
                    "python -m venv /tmp/fcp-mcp-wheel-py310\n"
                    "/tmp/fcp-mcp-wheel-py310/bin/python -m pip install "
                    "dist/fcp_mcp-0.3.0-py3-none-any.whl\n"
                    "/tmp/fcp-mcp-wheel-py310/bin/python -c "
                    '"import fcp_mcp.workflow.locking"\n'
                ),
            },
            {
                "name": "Create release checksum manifest",
                "run": (
                    "set -euo pipefail\n"
                    'actual_files="$(find dist -mindepth 1 -maxdepth 1 -type f '
                    '-print | LC_ALL=C sort)"\n'
                    'expected_files="$(printf \'%s\\n\' \\\n'
                    "  dist/fcp_mcp-0.3.0-py3-none-any.whl \\\n"
                    '  dist/fcp_mcp-0.3.0.tar.gz)"\n'
                    'test "$actual_files" = "$expected_files"\n'
                    "shasum -a 256 \\\n"
                    "  dist/fcp_mcp-0.3.0-py3-none-any.whl \\\n"
                    f"  dist/fcp_mcp-0.3.0.tar.gz > {RELEASE_MANIFEST}\n"
                ),
            },
            {
                "name": "Upload verified distributions",
                "uses": UPLOAD_ACTION,
                "with": {
                    "name": RELEASE_ARTIFACT,
                    "path": (
                        "dist/fcp_mcp-0.3.0-py3-none-any.whl\n"
                        "dist/fcp_mcp-0.3.0.tar.gz\n"
                        f"{RELEASE_MANIFEST}\n"
                    ),
                    "if-no-files-found": "error",
                    "retention-days": 7,
                },
            },
        ],
    }


def _check_publish(root: Path, findings: list[str]) -> None:
    path = root / ".github" / "workflows" / "publish.yml"
    workflow = _load_yaml(path, root, findings)
    if workflow is None:
        return
    if "defaults" in workflow:
        findings.append(
            ".github/workflows/publish.yml: workflow defaults can alter architecture gate"
        )
    if "env" in workflow:
        findings.append(
            ".github/workflows/publish.yml: workflow environment can alter architecture gate"
        )
    expected_trigger = {"push": {"tags": ["v*"]}}
    if workflow.get("on") != expected_trigger:
        findings.append(
            ".github/workflows/publish.yml: release trigger is not exact tag-only v*"
        )
    if workflow.get("permissions") != WORKFLOW_PERMISSIONS:
        findings.append(
            ".github/workflows/publish.yml: workflow permissions are not exactly contents read"
        )
    jobs = _jobs(workflow, ".github/workflows/publish.yml", findings)
    if jobs is None:
        return
    if set(jobs) != {"verify", "publish"}:
        findings.append(
            ".github/workflows/publish.yml: publish workflow jobs are not exactly verify and publish"
        )

    verify = jobs.get("verify")
    if not isinstance(verify, dict) or verify.get("runs-on") != MACOS_RUNNER:
        findings.append(".github/workflows/publish.yml: tagged verify job is not macOS")
    if verify is None:
        findings.append(".github/workflows/publish.yml: verify job is missing")
    else:
        _check_gate_job(
            verify,
            ".github/workflows/publish.yml",
            "verify",
            findings,
        )
        _check_verify_producer(verify, findings)
        if verify != _expected_verify_job():
            findings.append(
                ".github/workflows/publish.yml: verify job differs from reviewed release trajectory"
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


def _normalize_run(command: str) -> str:
    return "\n".join(line.rstrip() for line in command.strip().splitlines()) + "\n"


def _expected_registry_jobs() -> dict[str, Any]:
    validation = _normalize_run(
        f'''set -euo pipefail
if [ "$GITHUB_REF" != "refs/heads/main" ]; then
  echo "Registry publication must be dispatched from refs/heads/main" >&2
  exit 1
fi
case "$RELEASE_SHA" in
  {REGISTRY_RELEASE_SHA_PATTERN}) ;;
  *) echo "release_sha must be a full lowercase 40-hex commit SHA" >&2; exit 1 ;;
esac
case "$PUBLISH_RUN_ID" in
  ''|*[!0-9]*) echo "publish_run_id must be a decimal workflow run ID" >&2; exit 1 ;;
esac
'''
    )
    run_identity = _normalize_run(
        '''set -euo pipefail
run_json="$RUNNER_TEMP/publish-run.json"
curl --fail --silent --show-error --location --max-time 20 --retry 0 \\
  --header "Accept: application/vnd.github+json" \\
  --header "Authorization: Bearer $GH_TOKEN" \\
  --header "X-GitHub-Api-Version: 2022-11-28" \\
  --output "$run_json" \\
  "https://api.github.com/repos/dreliq9/fcp-mcp/actions/runs/$PUBLISH_RUN_ID"
jq -e --argjson run_id "$PUBLISH_RUN_ID" --arg release_sha "$RELEASE_SHA" '
  .id == $run_id and
  .repository.full_name == "dreliq9/fcp-mcp" and
  .path == ".github/workflows/publish.yml" and
  .event == "push" and
  .status == "completed" and
  .conclusion == "success" and
  .head_sha == $release_sha and
  .run_attempt == 1
' "$run_json"'''
    )
    artifact = _normalize_run(
        f'''set -euo pipefail
artifact_dir="$RUNNER_TEMP/fcp-mcp-v0.3.0-release-dist"
manifest="$artifact_dir/{RELEASE_MANIFEST}"
actual_entries="$(cd "$artifact_dir" && find . -mindepth 1 -print | LC_ALL=C sort)"
expected_entries="$(printf '%s\\n' \\
  ./dist \\
  ./dist/fcp_mcp-0.3.0-py3-none-any.whl \\
  ./dist/fcp_mcp-0.3.0.tar.gz \\
  ./{RELEASE_MANIFEST})"
test "$actual_entries" = "$expected_entries"
test "$(wc -l < "$manifest" | tr -d '[:space:]')" = "2"
sed -n '1p' "$manifest" | grep -Eq '^[0-9a-f]{{64}}  dist/fcp_mcp-0[.]3[.]0-py3-none-any[.]whl$'
sed -n '2p' "$manifest" | grep -Eq '^[0-9a-f]{{64}}  dist/fcp_mcp-0[.]3[.]0[.]tar[.]gz$'
(cd "$artifact_dir" && sha256sum -c {RELEASE_MANIFEST})'''
    )
    identity = _normalize_run(
        '''set -euo pipefail
test "$(git rev-parse HEAD)" = "$RELEASE_SHA"
test "$(git rev-parse refs/tags/v0.3.0^{})" = "$RELEASE_SHA"'''
    )
    metadata = _normalize_run(
        '''set -euo pipefail
test "$(jq -r '.name' server.json)" = "io.github.dreliq9/fcp-mcp"
test "$(jq -r '.version' server.json)" = "0.3.0"
test "$(jq -r '.packages | length' server.json)" = "1"
test "$(jq -r '.packages[0].registryType' server.json)" = "pypi"
test "$(jq -r '.packages[0].identifier' server.json)" = "fcp-mcp"
test "$(jq -r '.packages[0].version' server.json)" = "0.3.0"
test "$(python3 -c 'import pathlib, tomllib; print(tomllib.loads(pathlib.Path("pyproject.toml").read_text())["project"]["version"])')" = "0.3.0"'''
    )
    pypi = _normalize_run(
        f'''set -euo pipefail
artifact_dir="$RUNNER_TEMP/fcp-mcp-v0.3.0-release-dist"
manifest="$artifact_dir/{RELEASE_MANIFEST}"
package_json="$RUNNER_TEMP/pypi-fcp-mcp-0.3.0.json"
wheel_provenance="$RUNNER_TEMP/pypi-wheel-provenance.json"
sdist_provenance="$RUNNER_TEMP/pypi-sdist-provenance.json"
wheel_sha="$(sed -n '1s/  dist\\/fcp_mcp-0[.]3[.]0-py3-none-any[.]whl$//p' "$manifest")"
sdist_sha="$(sed -n '2s/  dist\\/fcp_mcp-0[.]3[.]0[.]tar[.]gz$//p' "$manifest")"
provenance_query='
  [.attestation_bundles[] | select(
    .publisher.kind == "GitHub" and
    .publisher.repository == "dreliq9/fcp-mcp" and
    .publisher.workflow == "publish.yml" and
    (.attestations | length) >= 1
  )] | length >= 1
'
for attempt in $(seq 1 12); do
  if curl --fail --silent --show-error --location --max-time 20 --retry 0 \\
    --output "$package_json" \\
    https://pypi.org/pypi/fcp-mcp/0.3.0/json && \\
    jq -e \\
      --arg name "fcp-mcp" \\
      --arg version "0.3.0" \\
      --arg wheel "fcp_mcp-0.3.0-py3-none-any.whl" \\
      --arg sdist "fcp_mcp-0.3.0.tar.gz" \\
      --arg wheel_sha "$wheel_sha" \\
      --arg sdist_sha "$sdist_sha" '
        .info.name == $name and
        .info.version == $version and
        (.urls | length == 2) and
        ([.urls[] | select(
          .filename == $wheel and
          .packagetype == "bdist_wheel" and
          .digests.sha256 == $wheel_sha and
          .yanked == false
        )] | length == 1) and
        ([.urls[] | select(
          .filename == $sdist and
          .packagetype == "sdist" and
          .digests.sha256 == $sdist_sha and
          .yanked == false
        )] | length == 1)
      ' "$package_json" && \\
    curl --fail --silent --show-error --location --max-time 20 --retry 0 \\
      --header "Accept: application/vnd.pypi.integrity.v1+json" \\
      --output "$wheel_provenance" \\
      https://pypi.org/integrity/fcp-mcp/0.3.0/fcp_mcp-0.3.0-py3-none-any.whl/provenance && \\
    curl --fail --silent --show-error --location --max-time 20 --retry 0 \\
      --header "Accept: application/vnd.pypi.integrity.v1+json" \\
      --output "$sdist_provenance" \\
      https://pypi.org/integrity/fcp-mcp/0.3.0/fcp_mcp-0.3.0.tar.gz/provenance && \\
    jq -e "$provenance_query" "$wheel_provenance" && \\
    jq -e "$provenance_query" "$sdist_provenance"; then
    exit 0
  fi
  if [ "$attempt" -lt 12 ]; then
    sleep 10
  fi
done
echo "PyPI fcp-mcp 0.3.0 files or provenance did not match the release artifact" >&2
exit 1'''
    )
    publisher = _normalize_run(
        f'''set -euo pipefail
publisher_archive="$RUNNER_TEMP/mcp-publisher_linux_amd64.tar.gz"
publisher_dir="$RUNNER_TEMP/mcp-publisher"
curl --fail --silent --show-error --location --max-time 60 --retry 0 \\
  --output "$publisher_archive" \\
  https://github.com/modelcontextprotocol/registry/releases/download/v1.8.1/mcp-publisher_linux_amd64.tar.gz
printf '%s  %s\\n' \\
  {REGISTRY_PUBLISHER_DIGEST} \\
  "$publisher_archive" | sha256sum -c -
mkdir -p "$publisher_dir"
tar --extract --gzip --file "$publisher_archive" --directory "$publisher_dir" --no-same-owner
test -x "$publisher_dir/mcp-publisher"'''
    )
    publish = _normalize_run(
        '''set -euo pipefail
publisher="$RUNNER_TEMP/mcp-publisher/mcp-publisher"
"$publisher" login github-oidc
"$publisher" publish'''
    )
    response = _normalize_run(
        '''set -euo pipefail
registry_json="$RUNNER_TEMP/registry-fcp-mcp-0.3.0.json"
registry_url="https://registry.modelcontextprotocol.io/v0.1/servers/io.github.dreliq9%2Ffcp-mcp/versions/0.3.0"
for attempt in $(seq 1 6); do
  if curl --fail --silent --show-error --location --max-time 20 --retry 0 \\
    --output "$registry_json" "$registry_url" && \\
    jq -e --arg name "io.github.dreliq9/fcp-mcp" --arg package "fcp-mcp" --arg version "0.3.0" \\
      '.server.name == $name and .server.version == $version and ([.server.packages[] | select(.registryType == "pypi" and .identifier == $package and .version == $version)] | length == 1)' \\
      "$registry_json"; then
    exit 0
  fi
  if [ "$attempt" -lt 6 ]; then
    sleep 10
  fi
done
echo "Registry response did not confirm fcp-mcp 0.3.0" >&2
exit 1'''
    )
    return {
        "validate": {
            "name": "Reject unsafe Registry dispatch",
            "runs-on": PUBLISH_RUNNER,
            "permissions": {},
            "steps": [
                {
                    "name": "Validate source and release inputs",
                    "env": {
                        "PUBLISH_RUN_ID": "${{ inputs.publish_run_id }}",
                        "RELEASE_SHA": "${{ inputs.release_sha }}",
                    },
                    "run": validation,
                }
            ],
        },
        "registry": {
            "name": "Publish immutable v0.3.0 Registry metadata",
            "needs": "validate",
            "runs-on": PUBLISH_RUNNER,
            "permissions": {
                "contents": "read",
                "actions": "read",
                "id-token": "write",
            },
            "steps": [
                {
                    "name": "Verify exact publish workflow run",
                    "env": {
                        "GH_TOKEN": "${{ github.token }}",
                        "PUBLISH_RUN_ID": "${{ inputs.publish_run_id }}",
                        "RELEASE_SHA": "${{ inputs.release_sha }}",
                    },
                    "run": run_identity,
                },
                {
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
                },
                {"name": "Verify release artifact contents", "run": artifact},
                {
                    "name": "Checkout immutable release SHA",
                    "uses": CHECKOUT_ACTION,
                    "with": {
                        "ref": "${{ inputs.release_sha }}",
                        "fetch-depth": 0,
                        "persist-credentials": False,
                    },
                },
                {
                    "name": "Prove immutable release identity",
                    "env": {"RELEASE_SHA": "${{ inputs.release_sha }}"},
                    "run": identity,
                },
                {"name": "Verify immutable release metadata", "run": metadata},
                {"name": "Verify public PyPI files and provenance", "run": pypi},
                {"name": "Download verified mcp-publisher", "run": publisher},
                {"name": "Publish through GitHub OIDC", "run": publish},
                {"name": "Verify Registry response", "run": response},
            ],
        },
    }


def _check_registry_publish(root: Path, findings: list[str]) -> None:
    workflow = _load_yaml(root / REGISTRY_WORKFLOW, root, findings)
    if workflow is None:
        return
    expected_trigger = {
        "workflow_dispatch": {
            "inputs": {
                "release_sha": {
                    "description": "40-hex commit SHA peeled from refs/tags/v0.3.0",
                    "required": True,
                    "type": "string",
                },
                "publish_run_id": {
                    "description": (
                        "Decimal workflow run ID that built and published v0.3.0"
                    ),
                    "required": True,
                    "type": "string",
                },
            }
        }
    }
    if workflow.get("on") != expected_trigger:
        findings.append(f"{REGISTRY_WORKFLOW}: trigger is not manual-only")
    if workflow.get("permissions") != WORKFLOW_PERMISSIONS:
        findings.append(f"{REGISTRY_WORKFLOW}: workflow permissions are not exactly contents read")
    if "defaults" in workflow or "env" in workflow:
        findings.append(f"{REGISTRY_WORKFLOW}: workflow may alter registry publishing")
    jobs = _jobs(workflow, REGISTRY_WORKFLOW, findings)
    if jobs is None:
        return
    expected_jobs = _expected_registry_jobs()
    if set(jobs) != set(expected_jobs):
        findings.append(f"{REGISTRY_WORKFLOW}: jobs are not exactly the Registry exception")
        return
    for job_name, expected_job in expected_jobs.items():
        job = jobs[job_name]
        if not isinstance(job, dict):
            findings.append(f"{REGISTRY_WORKFLOW}: {job_name} job is not a mapping")
            continue
        normalized_job = dict(job)
        steps = normalized_job.get("steps")
        if isinstance(steps, list):
            normalized_job["steps"] = [
                {**step, "run": _normalize_run(step["run"])}
                if isinstance(step, dict) and isinstance(step.get("run"), str)
                else step
                for step in steps
            ]
        if normalized_job != expected_job:
            findings.append(
                f"{REGISTRY_WORKFLOW}: {job_name} job differs from reviewed trajectory"
            )


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
    if project.get("requires-python") != ">=3.10,<3.14":
        findings.append("pyproject.toml: Requires-Python is not >=3.10,<3.14")
    scripts = project.get("scripts")
    if not isinstance(scripts, dict) or scripts.get("fcp-mcp") != "fcp_mcp.cli:main":
        findings.append("pyproject.toml: console script is not fcp_mcp.cli:main")


def _require_text(
    root: Path, relative: str, marker: str, finding: str, findings: list[str]
) -> None:
    text = _read_text(root / relative, root, findings)
    if text is not None and marker not in text:
        findings.append(f"{relative}: {finding}")


def _glob_path_matches(path: str, pattern: str) -> bool:
    path_parts = tuple(part for part in path.split("/") if part not in {"", "."})
    pattern_parts = tuple(
        part for part in pattern.split("/") if part not in {"", "."}
    )

    def match(path_index: int, pattern_index: int) -> bool:
        if pattern_index == len(pattern_parts):
            return path_index == len(path_parts)
        part = pattern_parts[pattern_index]
        if part == "**":
            return match(path_index, pattern_index + 1) or (
                path_index < len(path_parts)
                and match(path_index + 1, pattern_index)
            )
        return (
            path_index < len(path_parts)
            and fnmatch.fnmatchcase(path_parts[path_index], part)
            and match(path_index + 1, pattern_index + 1)
        )

    return match(0, 0)


def _recursive_manifest_matches(
    path: str, directory_pattern: str, file_patterns: list[str]
) -> bool:
    parts = path.split("/")
    if directory_pattern.strip("./") == "":
        return any(
            _glob_path_matches(path, pattern)
            or ("/" not in pattern and fnmatch.fnmatchcase(parts[-1], pattern))
            for pattern in file_patterns
        )
    for boundary in range(1, len(parts)):
        directory = "/".join(parts[:boundary])
        if not _glob_path_matches(directory, directory_pattern):
            continue
        relative = "/".join(parts[boundary:])
        basename = parts[-1]
        if any(
            _glob_path_matches(relative, pattern)
            or ("/" not in pattern and fnmatch.fnmatchcase(basename, pattern))
            for pattern in file_patterns
        ):
            return True
    return False


def _directory_manifest_matches(path: str, directory_pattern: str) -> bool:
    if directory_pattern.strip("./") == "":
        return True
    parts = path.split("/")
    return any(
        _glob_path_matches("/".join(parts[:boundary]), directory_pattern)
        for boundary in range(1, len(parts))
    )


def _required_manifest_paths(
    root: Path, findings: list[str]
) -> dict[str, set[str]]:
    groups: dict[str, set[str]] = {
        "top": set(),
        "examples": set(),
        "docs": set(),
        "scripts": set(),
    }
    for relative in ("README.md", "ROADMAP.md", "WORKFLOWS.md", "server.json"):
        path = root / relative
        if path.is_file() and not path.is_symlink():
            groups["top"].add(relative)
        else:
            findings.append(f"MANIFEST.in: required repository path {relative} is unsafe")

    suffixes = {
        "examples": {".py", ".md"},
        "docs": {".md"},
        "scripts": {".py"},
    }
    for tree, allowed_suffixes in suffixes.items():
        directory = root / tree
        if not directory.is_dir() or directory.is_symlink():
            findings.append(f"MANIFEST.in: required repository tree {tree} is unsafe")
            continue
        for current, directory_names, file_names in os.walk(
            directory, followlinks=False
        ):
            base = Path(current)
            for name in list(directory_names):
                candidate = base / name
                if candidate.is_symlink():
                    findings.append(
                        f"{_relative(candidate, root)}: unsafe manifest tree symlink"
                    )
                    directory_names.remove(name)
            for name in file_names:
                candidate = base / name
                if candidate.suffix not in allowed_suffixes:
                    continue
                if candidate.is_symlink():
                    findings.append(
                        f"{_relative(candidate, root)}: unsafe manifest file symlink"
                    )
                    continue
                groups[tree].add(candidate.relative_to(root).as_posix())
        if not groups[tree]:
            findings.append(f"MANIFEST.in: required repository tree {tree} is empty")
    return groups


def _check_manifest(root: Path, manifest: str, findings: list[str]) -> None:
    groups = _required_manifest_paths(root, findings)
    candidates = set().union(*groups.values())
    included: set[str] = set()
    manifest_tokens: list[str] = []

    for line in manifest.splitlines():
        try:
            tokens = shlex.split(line, comments=True, posix=True)
        except ValueError:
            findings.append("MANIFEST.in: invalid manifest directive")
            continue
        if not tokens:
            continue
        manifest_tokens.extend(tokens)
        command, arguments = tokens[0], tokens[1:]
        matches: set[str] = set()
        if command in {"include", "exclude"}:
            matches = {
                path
                for path in candidates
                if any(_glob_path_matches(path, pattern) for pattern in arguments)
            }
        elif command in {"global-include", "global-exclude"}:
            matches = {
                path
                for path in candidates
                if any(
                    fnmatch.fnmatchcase(path.rsplit("/", 1)[-1], pattern)
                    for pattern in arguments
                )
            }
        elif (
            command in {"recursive-include", "recursive-exclude"}
            and len(arguments) >= 2
        ):
            directory_pattern, file_patterns = arguments[0], arguments[1:]
            matches = {
                path
                for path in candidates
                if _recursive_manifest_matches(
                    path, directory_pattern, file_patterns
                )
            }
        elif command in {"graft", "prune"}:
            matches = {
                path
                for path in candidates
                if any(
                    _directory_manifest_matches(path, pattern)
                    for pattern in arguments
                )
            }

        if command in {"include", "global-include", "recursive-include", "graft"}:
            included.update(matches)
        elif command in {
            "exclude",
            "global-exclude",
            "recursive-exclude",
            "prune",
        }:
            included.difference_update(matches)

    for required_file in ("README.md", "ROADMAP.md", "WORKFLOWS.md", "server.json"):
        if required_file not in included:
            findings.append(f"MANIFEST.in: manifest lacks {required_file}")
    for tree in ("examples", "docs", "scripts"):
        if not groups[tree] <= included:
            findings.append(f"MANIFEST.in: manifest lacks recursive tree {tree}")
    if "smithery.yaml" in manifest_tokens:
        findings.append("MANIFEST.in: manifest names smithery.yaml")


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
        _check_manifest(root, manifest, findings)

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
    _check_workflow_inventory(resolved_root, findings)
    _check_ci(resolved_root, findings)
    _check_publish(resolved_root, findings)
    _check_registry_publish(resolved_root, findings)
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
