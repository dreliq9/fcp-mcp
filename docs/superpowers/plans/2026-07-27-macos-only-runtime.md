# fcp-mcp macOS-Only Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make macOS the sole supported `fcp-mcp` product runtime, reject every
non-macOS operational start before side effects, and remove Windows/Linux
compatibility machinery without losing the platform-independent Task 14
correctness repairs.

**Architecture:** One side-effect-free platform policy guards the CLI, doctor,
server, and workflow runtime boundaries. Product filesystem code keeps only the
descriptor-relative macOS implementation; package metadata, active
documentation, CI, and release verification state the same boundary. An
isolated Ubuntu job may publish an already verified artifact through PyPA's
container action but may never execute the product.

**Tech Stack:** Python 3.10-3.13, Pydantic 2, MCP Python SDK v1, SQLite,
`platformdirs`, `fcntl`, pytest, Hypothesis, Ruff, GitHub Actions, PyPI Trusted
Publishing.

## Global Constraints

- Work in
  `/Users/adamsteen/fcp-mcp/.worktrees/v0.3-transactional-workflows` on
  `codex/v0.3-transactional-workflows`.
- Do not push, tag, publish, create a GitHub release, or submit to a registry
  without separate user authorization.
- The authoritative design is
  `docs/superpowers/specs/2026-07-27-macos-only-runtime-design.md`.
- Supported runtime means exactly `sys.platform == "darwin"`.
- `--help` and `--version` remain side-effect-free and available off macOS.
- Every operational entry point fails with `unsupported_platform` and CLI exit
  code `2` before configuration, state, SQLite, catalog, subprocess, or caller
  file activity.
- Pure-module imports remain side-effect-free. This is import tolerance, not a
  Windows/Linux compatibility promise.
- No Windows/Linux implementation, tests, active documentation, or CI runtime
  matrix remains.
- Every job that installs, imports, tests, audits, builds, or smoke-runs
  `fcp-mcp` uses macOS.
- The final artifact-only PyPI job may use Ubuntu solely for the pinned
  `pypa/gh-action-pypi-publish` container. It must not check out source, set up
  Python, or execute `fcp-mcp`.
- Preserve SQLite rollback journaling, `synchronous=FULL`,
  `foreign_keys=ON`, `busy_timeout=5000`, and explicit
  `BEGIN IMMEDIATE`.
- Preserve exact successful v0.2.1 text behavior and the current 89-tool
  legacy catalog in broader profiles.
- Preserve Task 14's platform-independent repair requirements: complete
  prepare evidence, approval consistency, exact commit evidence, terminal
  errors, resumable pruning, read nonmutation, authenticated backups, and
  sanitized cleanup failures.
- Use test-first red/green cycles. Record the exact failing and passing
  commands in the task implementer report.
- Run a controller-owned independent review after every task. Fix and re-review
  every Important or Critical finding before continuing.
- Before each task, refresh the narrow primary-source evidence relevant to that
  task and write the decisions into the ignored SDD task brief. This preserves
  the user's research-at-each-phase requirement.
- Current base commit is `1daf979`. The worktree also contains an interrupted,
  uncommitted Task 14 repair in `ledger.py` and `test_ledger.py`. It belongs to
  this effort. Do not reset, check out, or discard it wholesale; selectively
  remove only its Windows-specific portions and preserve its test-first
  platform-independent fixes.

## File map

### New focused files

- `src/fcp_mcp/platform_support.py`: the sole runtime platform policy.
- `tests/test_platform_support.py`: platform policy and side-effect-order
  tests.
- `tests/contracts/test_macos_only_distribution.py`: package, docs, CI, and
  release-runner contract.
- `scripts/check_macos_only.py`: durable active-surface/source check after
  compatibility code is removed.
- `tests/contracts/test_macos_only_source.py`: tests the durable checker.

### Existing files changed

- `src/fcp_mcp/contracts.py`: add `ErrorCode.UNSUPPORTED_PLATFORM`.
- `src/fcp_mcp/cli.py`: guard default, serve, and doctor execution.
- `src/fcp_mcp/server.py`: guard direct server execution.
- `src/fcp_mcp/diagnostics.py`: remove non-macOS diagnostic behavior.
- `src/fcp_mcp/workflow/ledger.py`: finish Task 14 repairs using only the
  macOS descriptor implementation.
- `src/fcp_mcp/workflow/artifacts.py`: delete Win32 and pathname-fallback
  implementations.
- `src/fcp_mcp/utils/atomic_write.py`: make directory durability the single
  supported macOS path.
- `tests/test_cli.py`, `tests/test_server_contracts.py`,
  `tests/test_diagnostics.py`: runtime-boundary coverage.
- `tests/workflow/test_ledger.py`,
  `tests/workflow/test_ledger_properties.py`: Task 14 and macOS-only ledger
  coverage.
- `tests/workflow/test_artifacts.py`: retain macOS descriptor tests and delete
  Windows/fallback tests.
- `tests/test_atomic_write.py`: macOS-only durability contract.
- `pyproject.toml`, `README.md`, `CONTRIBUTING.md`, `ROADMAP.md`,
  `MANIFEST.in`: truthful support and distribution metadata.
- `.github/workflows/ci.yml`, `.github/workflows/publish.yml`: macOS product
  execution and isolated artifact-only publishing.
- `docs/superpowers/plans/2026-07-26-v0.3.0-transactional-workflows.md`:
  macOS-only requirements for the remaining tasks.
- `docs/superpowers/plans/2026-07-26-v0.2.1-trust-baseline.md`: historical
  supersession note.

### Deleted file

- `smithery.yaml`: its hosted/Linux command surface cannot run a macOS-only
  local Final Cut Pro server.

---

### Task 1: Enforce the macOS Runtime Boundary

**Files:**

- Create: `src/fcp_mcp/platform_support.py`
- Create: `tests/test_platform_support.py`
- Modify: `src/fcp_mcp/contracts.py`
- Modify: `src/fcp_mcp/cli.py`
- Modify: `src/fcp_mcp/server.py`
- Modify: `src/fcp_mcp/diagnostics.py`
- Modify: `tests/test_cli.py`
- Modify: `tests/test_server_contracts.py`
- Modify: `tests/test_diagnostics.py`

**Interfaces:**

- Produces:
  `require_macos(platform: str | None = None) -> None`.
- Produces: `ErrorCode.UNSUPPORTED_PLATFORM`.
- Consumed by: CLI operational dispatch, `_doctor()`, `server.main()`, and the
  future workflow-runtime constructor in Task 19 of the v0.3 plan.

- [ ] **Step 1: Refresh and record the runtime-boundary research**

Read and record the binding implications of:

- Python's documented `sys.platform == "darwin"` value for macOS:
  `https://docs.python.org/3/library/sys.html#sys.platform`;
- Apple's current Final Cut Pro requirements:
  `https://www.apple.com/final-cut-pro/`;
- the approved macOS-only design.

Write the result to:

```text
.superpowers/sdd/2026-07-27-macos-only-runtime/
task-macos-1-platform-boundary-brief.md
```

The brief must state that platform detection is dynamic at call time for
testability and that imports perform no platform check.

- [ ] **Step 2: Write failing policy and CLI-ordering tests**

Create `tests/test_platform_support.py` with tests equivalent to:

```python
from __future__ import annotations

import subprocess
import sys

import pytest

from fcp_mcp.contracts import ErrorCode, FCPMCPError


def test_require_macos_accepts_darwin_and_rejects_every_other_identifier():
    from fcp_mcp.platform_support import require_macos

    require_macos("darwin")
    for platform in ("linux", "win32", "cygwin", "freebsd"):
        with pytest.raises(FCPMCPError) as caught:
            require_macos(platform)
        assert caught.value.code is ErrorCode.UNSUPPORTED_PLATFORM
        assert str(caught.value) == (
            "unsupported_platform: fcp-mcp requires macOS"
        )


def test_pure_imports_do_not_consult_the_runtime_platform():
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; sys.platform = 'linux'; "
                "import fcp_mcp; "
                "import fcp_mcp.fcpxml.parser; "
                "import fcp_mcp.workflow.models"
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0
    assert completed.stdout == ""
    assert completed.stderr == ""
```

Extend `tests/test_cli.py`:

```python
@pytest.mark.parametrize(
    "arguments",
    [[], ["serve"], ["doctor"], ["doctor", "--json"]],
)
def test_non_macos_operations_fail_before_config_or_server(
    arguments,
    monkeypatch,
    capsys,
):
    def forbidden(*args, **kwargs):
        raise AssertionError("unsupported startup crossed a side-effect boundary")

    monkeypatch.setattr("fcp_mcp.platform_support.sys.platform", "linux")
    monkeypatch.setattr("fcp_mcp.cli.serve", forbidden)
    monkeypatch.setattr("fcp_mcp.cli.RuntimeConfig.from_env", forbidden)

    assert main(arguments) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "unsupported_platform: fcp-mcp requires macOS\n"
    )


def test_non_macos_help_and_version_remain_available(monkeypatch, capsys):
    monkeypatch.setattr("fcp_mcp.platform_support.sys.platform", "linux")
    assert main(["--version"]) == 0
    with pytest.raises(SystemExit) as help_exit:
        main(["--help"])
    assert help_exit.value.code == 0
```

Add a direct `server.main()` test whose mocked `mcp.run` raises if reached and
assert that the coded platform error is raised first.

- [ ] **Step 3: Run the tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_platform_support.py \
  tests/test_cli.py tests/test_server_contracts.py tests/test_diagnostics.py -q
```

Expected: FAIL because `platform_support.py` and
`UNSUPPORTED_PLATFORM` do not exist and operational entry points do not guard.

- [ ] **Step 4: Implement the minimal platform policy**

Create `src/fcp_mcp/platform_support.py`:

```python
from __future__ import annotations

import sys

from fcp_mcp.contracts import ErrorCode, FCPMCPError


def require_macos(platform: str | None = None) -> None:
    observed = sys.platform if platform is None else platform
    if observed != "darwin":
        raise FCPMCPError(
            ErrorCode.UNSUPPORTED_PLATFORM,
            "fcp-mcp requires macOS",
        )
```

Add this enum member in `contracts.py`:

```python
UNSUPPORTED_PLATFORM = "unsupported_platform"
```

In `cli.py`, let argparse handle `--help`, keep `--version` before the guard,
and call `require_macos()` before default serve, explicit serve, or doctor.
Translate only the platform error to:

```python
print(str(error), file=sys.stderr)
return 2
```

Call `require_macos()` at the beginning of `_doctor()` and `server.main()`.
Remove the non-Darwin skip branch from macOS Accessibility diagnostics; an
operational doctor cannot reach it off macOS.

- [ ] **Step 5: Run focused and import tests and verify GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/test_platform_support.py \
  tests/test_cli.py tests/test_server_contracts.py tests/test_diagnostics.py \
  tests/test_parser.py tests/workflow/test_models.py -q
.venv/bin/ruff check src/fcp_mcp/platform_support.py \
  src/fcp_mcp/cli.py src/fcp_mcp/server.py src/fcp_mcp/diagnostics.py \
  tests/test_platform_support.py tests/test_cli.py \
  tests/test_server_contracts.py tests/test_diagnostics.py
git diff --check
```

Expected: PASS with no stdout/stderr leakage beyond the asserted platform
error.

- [ ] **Step 6: Commit the runtime boundary**

```bash
git add src/fcp_mcp/platform_support.py src/fcp_mcp/contracts.py \
  src/fcp_mcp/cli.py src/fcp_mcp/server.py src/fcp_mcp/diagnostics.py \
  tests/test_platform_support.py tests/test_cli.py \
  tests/test_server_contracts.py tests/test_diagnostics.py
git commit -m "feat: require macOS for fcp-mcp runtime"
```

---

### Task 2: Make Distribution, Documentation, and CI Truthful

**Files:**

- Create: `tests/contracts/test_macos_only_distribution.py`
- Modify: `pyproject.toml`
- Modify: `README.md`
- Modify: `CONTRIBUTING.md`
- Modify: `ROADMAP.md`
- Modify: `MANIFEST.in`
- Modify: `.github/workflows/ci.yml`
- Modify: `.github/workflows/publish.yml`
- Delete: `smithery.yaml`

**Interfaces:**

- Consumes: the Task 1 operational guard.
- Produces: a Mac-only support statement, Mac-only product-execution jobs, and
  one artifact-only PyPI publishing exception.

- [ ] **Step 1: Refresh and record distribution research**

Record the current requirements from:

- PyPA core metadata:
  `https://packaging.python.org/en/latest/specifications/core-metadata/`;
- GitHub runner selection:
  `https://docs.github.com/en/actions/how-tos/write-workflows/choose-where-workflows-run/choose-the-runner-for-a-job`;
- PyPA's publish action, whose expected environment is Ubuntu:
  `https://github.com/marketplace/actions/pypi-publish`;
- Smithery's current local/remote distribution model:
  `https://smithery.ai/docs/build`.

Write:

```text
.superpowers/sdd/2026-07-27-macos-only-runtime/
task-macos-2-distribution-brief.md
```

The brief must distinguish product runtime support from an artifact-only
publishing control plane.

- [ ] **Step 2: Write failing distribution contract tests**

Create `tests/contracts/test_macos_only_distribution.py`:

```python
from __future__ import annotations

import re
import tomllib
from pathlib import Path


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
    assert set(re.findall(r"runs-on:\\s*([^\\s]+)", ci)) == {
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
```

- [ ] **Step 3: Run the contract test and verify RED**

Run:

```bash
.venv/bin/python -m pytest \
  tests/contracts/test_macos_only_distribution.py -q
```

Expected: FAIL on the OS-independent classifier, active cross-platform prose,
Smithery file, and Linux/Windows product-execution jobs.

- [ ] **Step 4: Update the distribution surface**

Make these exact changes:

- delete `Operating System :: OS Independent` from `pyproject.toml`;
- make README prerequisites say `macOS 15.6 or later` and remove “work
  anywhere,” Windows/Linux separator guidance, the Smithery tree entry, and
  the parity roadmap item;
- make `CONTRIBUTING.md` show only the macOS venv and Homebrew commands;
- change `ROADMAP.md` to “Python 3.10-3.13 CI on macOS”;
- delete `smithery.yaml` and remove it from `MANIFEST.in`;
- make every job in `ci.yml` run on `macos-latest`, retaining the Python
  3.10-3.13 test matrix;
- make the tagged verify/build/smoke job in `publish.yml` use
  `macos-latest`;
- leave only the final `publish` job on `ubuntu-latest`, and keep that job to
  `actions/download-artifact` plus the pinned PyPA publish action.

The `publish` job must contain no checkout, Python setup, shell command,
package installation, import, test, build, or smoke step.

- [ ] **Step 5: Run distribution and packaging checks**

Run:

```bash
.venv/bin/python -m pytest \
  tests/contracts/test_macos_only_distribution.py \
  tests/test_version_contracts.py tests/test_release_smoke.py -q
FCP_MCP_PROFILE=full .venv/bin/python scripts/check_contracts.py
.venv/bin/python -m build
.venv/bin/python -m twine check dist/*
git diff --check
```

Expected: PASS. Inspect the built metadata and confirm the Mac classifier is
present and the OS-independent classifier and `smithery.yaml` are absent.

- [ ] **Step 6: Commit the truthful distribution surface**

```bash
git add pyproject.toml README.md CONTRIBUTING.md ROADMAP.md MANIFEST.in \
  .github/workflows/ci.yml .github/workflows/publish.yml \
  tests/contracts/test_macos_only_distribution.py
git add -u smithery.yaml
git commit -m "build: make distribution runtime macOS-only"
```

---

### Task 3: Finish the Task 14 Ledger on the macOS Contract

**Files:**

- Modify: `src/fcp_mcp/workflow/ledger.py`
- Modify: `tests/workflow/test_ledger.py`
- Modify: `tests/workflow/test_ledger_properties.py`
- Modify:
  `.superpowers/sdd/2026-07-26-v0.3.0-transactional-workflows/task-14-implementer-report.md`

**Interfaces:**

- Preserves: every existing accepted Task 14 typed ledger record and CAS API.
- Produces: `list_pending_prune_runs(*, limit: int = 100)`.
- Extends: `append_event()` only for projection-free
  `artifact_prune_intent` and `artifacts_pruned` events on prune-eligible
  terminal runs.
- Reserves: `receipt_sha256` and `receipt_size_bytes` exclusively for the
  final commit receipt.

- [ ] **Step 1: Refresh and record the macOS ledger research**

Re-read the Task 14 brief, Apple's APFS/file guidance, SQLite transactions,
Online Backup, and `SQLITE_FCNTL_HAS_MOVED`. Record which prior findings remain
binding after Windows removal in:

```text
.superpowers/sdd/2026-07-27-macos-only-runtime/
task-macos-3-ledger-brief.md
```

The brief must explicitly reject Win32 handle/share-mode work and retain
macOS descriptor identity, bootstrap-token authentication, backup
authentication, fsync, and rollback-journal semantics.

- [ ] **Step 2: Reconcile the interrupted red/green test set**

Keep these already test-first additions:

```text
test_awaiting_approval_requires_complete_prepare_artifacts_and_expiry
test_decision_source_must_match_run_approval_mode
test_commit_started_requires_exact_complete_consistent_evidence
test_present_destination_commit_intent_binds_deterministic_backup
test_receipt_evidence_is_forbidden_during_prepare_and_required_on_commit
test_terminal_error_policy_matches_public_status_contract
test_terminal_prune_audits_are_closed_and_interrupted_intents_are_resumable
test_integrity_enforces_prepare_approval_commit_and_terminal_invariants
test_read_paths_never_run_stale_bootstrap_cleanup_but_write_paths_do
test_backup_destination_is_token_authenticated_before_sqlite_writes
test_backup_destination_displacement_is_rejected_before_backup_write
test_sqlite_second_open_retains_authenticated_main_descriptor
test_public_close_failures_are_sanitized_without_masking_primary_code
```

Delete these now-out-of-scope additions:

```text
test_simulated_windows_bootstrap_closes_temp_handle_before_publish
test_simulated_windows_backup_closes_temp_handle_before_replace
test_windows_ledger_open_contract_is_sqlite_second_open_compatible
```

Add one source-boundary regression:

```python
def test_ledger_has_no_windows_or_pathname_fallback_implementation():
    source = Path(ledger_module.__file__).read_text(encoding="utf-8")
    for marker in (
        "WinDLL",
        "msvcrt",
        "_windows_",
        "FILE_SHARE_",
        "reparse",
        "_fallback_open_file",
        'os.name == "nt"',
    ):
        assert marker not in source
```

- [ ] **Step 3: Run the retained acceptance tests and verify current RED**

Run:

```bash
.venv/bin/python -m pytest tests/workflow/test_ledger.py \
  tests/workflow/test_ledger_properties.py -q
```

Expected before repair completion: FAIL only on incomplete Task 14
platform-independent behavior or remaining Windows/fallback code. Record the
exact failures; do not weaken an assertion merely to obtain green.

- [ ] **Step 4: Remove non-macOS ledger machinery**

Remove:

```text
_requires_closed_publish_handles
_windows_ledger_open_contract
_open_windows_ledger_file
_windows_bootstrap_mutex
_is_reparse_stat
_fallback_open_file import and pathname branch
all os.name platform branches
```

Make `_RootAnchor.descriptor` a required integer. Open the root only through
the existing absolute `O_DIRECTORY | O_NOFOLLOW` descriptor chain. Implement
child stat/open/unlink/link/replace solely with `dir_fd`.

On macOS, retain the database/source descriptor while SQLite has its second
connection open. It is legal to unlink or rename an open inode, so bootstrap
and backup publication need no close-before-publish seam.

- [ ] **Step 5: Complete lifecycle and projection enforcement**

The allowed projection ownership is:

```python
_PREPARE_PROJECTION_EVENTS = {
    "source_sha256": frozenset({"source_inspected"}),
    "prior_destination_state": frozenset({"source_inspected"}),
    "prior_destination_sha256": frozenset({"source_inspected"}),
    "plan_sha256": frozenset({"plan_built", "plan_normalized"}),
    "expires_at": frozenset(
        {"awaiting_approval", "prepare_completed", "prepared", "preview_persisted"}
    ),
}

_COMMIT_RESULT_FIELDS = frozenset(
    {
        "destination_sha256",
        "committed_at",
        "backup_sha256",
        "receipt_sha256",
        "receipt_size_bytes",
    }
)
```

Enforce:

- `PREPARING -> AWAITING_APPROVAL` requires source, prior destination, plan,
  candidate artifact, diff artifact, matching artifact rows, and expiry;
- CLI mode accepts only CLI approval evidence; client mode accepts only client
  approval evidence;
- `APPROVED -> COMMITTING` requires exactly one canonical UUID attempt ID and
  backup evidence consistent with prior destination presence;
- `COMMITTING -> COMMITTED` requires all non-null final result fields,
  including receipt hash and size;
- terminal error code/summary are an atomic pair;
- `FAILED` and `RECOVERY_REQUIRED` require the pair;
- `STALE` and `ROLLED_BACK` may carry it;
- states not represented by the public error contract reject it.

Make the integrity verifier apply the same rules to stored rows. A mutation
that the verifier would later reject must not be writable.

- [ ] **Step 6: Complete terminal maintenance, reads, backup, and cleanup**

Permit only:

```text
artifact_prune_intent
artifacts_pruned
```

as projection-free same-state events on prune-eligible terminal runs.
`list_pending_prune_runs()` must find an intent without a later completion
regardless of `updated_at`, so interruption remains resumable after the
original cutoff traversal.

Move stale-bootstrap cleanup behind initialization/write locking. Read APIs
must never unlink, create, configure, or rewrite anything.

Seed every newly created migration-backup destination with a unique verified
bootstrap token before SQLite writes. Revalidate descriptor/path identity and
token before and after Online Backup, then publish atomically.

Route every public connection close through:

```python
def _close_public_connection(
    connection: sqlite3.Connection,
    primary: BaseException | None,
) -> None:
    try:
        connection.close()
    except _LEDGER_FAILURES as error:
        if primary is None:
            raise _ledger_unavailable(error) from error
        bounded = _MigrationError("connection close failed")
        bounded.__cause__ = error
        _attach_cleanup_failures(primary, [bounded])
```

If a primary coded error exists, attach bounded path-free cleanup evidence
without replacing it. Otherwise translate cleanup failure to sanitized
`ledger_unavailable`.

- [ ] **Step 7: Run Task 14 focused and whole-suite gates**

Run:

```bash
.venv/bin/python -m pytest tests/workflow/test_ledger.py \
  tests/workflow/test_ledger_properties.py -q
.venv/bin/python -m pytest -q
.venv/bin/ruff check src tests scripts
git diff --check
/opt/homebrew/bin/python3.10 -m compileall -q src tests
```

Expected: PASS. Also rerun the concurrent-initializer probe from the Task 14
test 100 times:

```bash
for attempt in {1..100}; do
  .venv/bin/python -m pytest \
    tests/workflow/test_ledger.py::test_concurrent_initializers_apply_migration_once \
    -q || exit 1
done
```

Record zero corrupt, migrated-wrong, or missing-database outcomes.

- [ ] **Step 8: Commit the accepted macOS ledger repair**

```bash
git add src/fcp_mcp/workflow/ledger.py tests/workflow/test_ledger.py \
  tests/workflow/test_ledger_properties.py
git commit -m "fix: complete macOS workflow ledger invariants"
```

The ignored implementer report is updated but not staged.

---

### Task 4: Collapse the Artifact Store to Its macOS Descriptor Path

**Files:**

- Modify: `src/fcp_mcp/workflow/artifacts.py`
- Modify: `tests/workflow/test_artifacts.py`
- Modify: `src/fcp_mcp/utils/atomic_write.py`
- Modify: `tests/test_atomic_write.py`

**Interfaces:**

- Preserves: `StatePaths`, `ArtifactStore.write()`, `ArtifactStore.read()`,
  `ArtifactStore.verify()`, `ArtifactStore.remove_bodies()`, and
  `ArtifactMetadataV1`.
- Removes: all Win32 handle, reparse, mutex, and fallback-path private APIs.
- Consumed by: ledger, prepare, recovery, and pruning tasks.

- [ ] **Step 1: Refresh and record the macOS filesystem research**

Review Apple's Application Support guidance, Python 3.10 `os.open`/`dir_fd`,
`fcntl.flock`, and the existing Task 13 adversarial report. Write:

```text
.superpowers/sdd/2026-07-27-macos-only-runtime/
task-macos-4-artifact-brief.md
```

Record that macOS supports the descriptor-relative path already exercised by
Task 13 and that deleting the fallback path reduces attack surface without
changing the public artifact API.

- [ ] **Step 2: Write the failing source-boundary test**

Add:

```python
def test_artifact_store_contains_only_the_macos_descriptor_implementation():
    source = Path(artifact_module.__file__).read_text(encoding="utf-8")
    for marker in (
        "WinDLL",
        "msvcrt",
        "_windows_",
        "_fallback_",
        "FILE_SHARE_",
        "reparse",
        'os.name == "nt"',
    ):
        assert marker not in source
```

Remove tests whose only subject is a Windows/fallback private API. Do not
remove descriptor identity, symlink-swap, mode, fsync, aggregate-limit,
cleanup, hash, or concurrent writer tests.

- [ ] **Step 3: Run the focused test and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/workflow/test_artifacts.py -q
```

Expected: FAIL on the source-boundary assertion while all retained macOS
behavior tests continue to execute.

- [ ] **Step 4: Delete the compatibility implementation**

Delete all Win32 constants/helpers and all fallback helpers/methods, including:

```text
_windows_attributes_mark_reparse
_close_windows_handle_values
_release_windows_mutex_handle
_adopt_windows_file_handle
_acquire_windows_directory_handles
_windows_directory_guard
_windows_run_mutex
_fallback_open_file
_replace_owned_fallback_file
_dispose_owned_fallback_temp
_validate_fallback_chain
_initialize_fallback_state
ArtifactStore._fallback_existing_sizes
ArtifactStore._write_fallback
ArtifactStore._write_guarded_fallback
ArtifactStore._read_fallback
```

Make `initialize()`, `write()`, `read()`, and body removal call only their
existing descriptor-relative implementations. Remove `os.name` dispatch and
unconditionalize macOS mode/fsync enforcement.

In `atomic_write.py`, remove the non-POSIX no-op branch from directory fsync;
macOS always opens and fsyncs the containing directory.

- [ ] **Step 5: Run artifact, ledger, atomic-write, and full gates**

Run:

```bash
.venv/bin/python -m pytest tests/workflow/test_artifacts.py \
  tests/workflow/test_ledger.py tests/workflow/test_ledger_properties.py \
  tests/test_atomic_write.py tests/test_fcpxml_transaction.py -q
.venv/bin/python -m pytest -q
.venv/bin/ruff check src tests scripts
git diff --check
```

Expected: PASS with a materially smaller artifact module and no public API
change.

- [ ] **Step 6: Commit the macOS artifact path**

```bash
git add src/fcp_mcp/workflow/artifacts.py \
  src/fcp_mcp/utils/atomic_write.py tests/workflow/test_artifacts.py \
  tests/test_atomic_write.py
git commit -m "refactor: keep only macOS artifact storage"
```

---

### Task 5: Add a Durable macOS-Only Architecture Gate

**Files:**

- Create: `scripts/check_macos_only.py`
- Create: `tests/contracts/test_macos_only_source.py`
- Modify: `.github/workflows/ci.yml`
- Modify: `.github/workflows/publish.yml`

**Interfaces:**

- Produces: `scripts.check_macos_only.check(root: Path) -> list[str]`.
- CI contract: any returned finding fails the static contract job.

- [ ] **Step 1: Refresh and record the final architecture-gate research**

Recheck the installed wheel metadata, current GitHub workflow syntax, and all
active support surfaces after Tasks 1-4. Write:

```text
.superpowers/sdd/2026-07-27-macos-only-runtime/
task-macos-5-architecture-gate-brief.md
```

List the exact forbidden implementation markers and the one allowed Ubuntu
publisher job.

- [ ] **Step 2: Write the failing checker tests**

Create `tests/contracts/test_macos_only_source.py`:

```python
from pathlib import Path

from scripts.check_macos_only import check


def test_repository_satisfies_macos_only_architecture_contract():
    assert check(Path(__file__).resolve().parents[2]) == []


def test_checker_reports_windows_source_and_linux_product_job(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "bad.py").write_text(
        "kernel32 = ctypes.WinDLL('kernel32')\n"
    )
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(
        "jobs:\n  test:\n    runs-on: ubuntu-latest\n"
    )

    findings = check(tmp_path)
    assert any("WinDLL" in finding for finding in findings)
    assert any("product job is not macOS" in finding for finding in findings)
```

- [ ] **Step 3: Run the checker tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest \
  tests/contracts/test_macos_only_source.py -q
```

Expected: FAIL because the checker does not exist.

- [ ] **Step 4: Implement the repository checker**

`check(root)` returns deterministic sorted findings and scans:

- `src/fcp_mcp/**/*.py` for `WinDLL`, `msvcrt`, `_windows_`, `FILE_SHARE_`,
  `_fallback_`, `reparse`, and `os.name`;
- all `runs-on` values in `ci.yml`, which must be `macos-latest`;
- the tagged verify job in `publish.yml`, which must be macOS;
- the isolated publish job, which may be Ubuntu but must contain only artifact
  download and the pinned PyPA publisher;
- `pyproject.toml`, README, contributing guide, roadmap, manifest, and absence
  of `smithery.yaml`.

Allow `sys.platform` only in `platform_support.py`. Emit paths and stable
marker names, never file contents.

Add:

```yaml
- name: Enforce macOS-only architecture
  run: python scripts/check_macos_only.py
```

to the macOS static/contract job and the tagged macOS verify job.

- [ ] **Step 5: Run full local release-candidate gates**

Run:

```bash
.venv/bin/python scripts/check_macos_only.py
.venv/bin/python -m pytest -q
.venv/bin/ruff check src tests scripts
.venv/bin/python scripts/check_contracts.py
.venv/bin/python -m pytest \
  --cov=fcp_mcp --cov-report=term-missing \
  --cov-report=json:coverage.json --cov-fail-under=60
.venv/bin/python scripts/check_new_module_coverage.py coverage.json
.venv/bin/pip-audit --local
rm -rf build dist
.venv/bin/python -m build
.venv/bin/python -m twine check dist/*
git diff --check
```

Create a clean macOS venv, install the built wheel, and run:

```bash
rm -rf "$TMPDIR/fcp-mcp-wheel-smoke" "$TMPDIR/fcp-mcp-output"
.venv/bin/python -m venv "$TMPDIR/fcp-mcp-wheel-smoke"
"$TMPDIR/fcp-mcp-wheel-smoke/bin/python" -m pip install --upgrade pip
"$TMPDIR/fcp-mcp-wheel-smoke/bin/python" -m pip install \
  dist/fcp_mcp-*.whl
mkdir -p "$TMPDIR/fcp-mcp-output"
FCP_MCP_OUTPUT_DIR="$TMPDIR/fcp-mcp-output" \
FCP_MCP_ALLOWED_ROOTS="$TMPDIR/fcp-mcp-output" \
FCP_MCP_ENABLE_LIVE_CONTROL=0 \
"$TMPDIR/fcp-mcp-wheel-smoke/bin/python" scripts/wheel_smoke.py \
  --command "$TMPDIR/fcp-mcp-wheel-smoke/bin/fcp-mcp"
```

Expected: every gate passes and wheel metadata contains only the Mac OS
classifier.

- [ ] **Step 6: Commit the durable architecture gate**

```bash
git add scripts/check_macos_only.py \
  tests/contracts/test_macos_only_source.py \
  .github/workflows/ci.yml .github/workflows/publish.yml
git commit -m "test: enforce macOS-only architecture"
```

---

## Controller acceptance and v0.3 resumption

After every task:

1. run an independent spec-compliance review against the macOS-only design;
2. run an independent code-quality/security review;
3. repair and re-review Important/Critical findings;
4. record commits, test counts, commands, and verdicts in the SDD progress
   ledger.

After Task 5 passes, resume the original v0.3 plan at Task 15. Its remaining
tasks inherit the macOS-only amendment:

- Task 17 uses only `os.kill(pid, 0)` for same-host lock liveness;
- Task 19 guards workflow runtime initialization through
  `require_macos()`;
- Task 24 runs product verification and the Final Cut Pro canary on macOS;
- no future task may reintroduce a Windows/Linux support branch or test matrix.
