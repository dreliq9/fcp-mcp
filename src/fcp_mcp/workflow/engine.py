"""Deterministic, SDK-independent workflow prepare orchestration."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import monotonic
from typing import TextIO
from uuid import uuid4

from pydantic import ValidationError

from fcp_mcp.config import RuntimeConfig
from fcp_mcp.contracts import ErrorCode, FCPMCPError
from fcp_mcp.fcpxml.diff import append_operation_effects, build_workflow_diff
from fcp_mcp.fcpxml.parser import FCPXMLParser
from fcp_mcp.fcpxml.validator import FCPXMLValidator, ValidationResult
from fcp_mcp.observability import emit_event
from fcp_mcp.security.paths import PathPolicy
from fcp_mcp.workflow.approval import (
    ClientApproval,
)
from fcp_mcp.workflow.approval import (
    approve_cli as apply_cli_approval,
)
from fcp_mcp.workflow.approval import (
    approve_client as stage_client_approval,
)
from fcp_mcp.workflow.approval import (
    cancel as cancel_workflow,
)
from fcp_mcp.workflow.approval import (
    effective_state as effective_workflow_state,
)
from fcp_mcp.workflow.approval import (
    reject as reject_workflow,
)
from fcp_mcp.workflow.artifacts import (
    ArtifactKind,
    ArtifactMetadataV1,
    ArtifactStore,
    enforce_source_size,
)
from fcp_mcp.workflow.ledger import (
    ArtifactRecord,
    DecisionMutationResult,
    EventMutationResult,
    LedgerRunRecord,
    WorkflowLedger,
)
from fcp_mcp.workflow.models import (
    MAX_EVIDENCE_ITEMS,
    MAX_WARNINGS,
    FindingDisposition,
    OperationReceiptV1,
    PriorDestinationState,
    ValidationIssueV1,
    ValidationResultV1,
    WorkflowPlanV1,
    WorkflowPrepareRequestV1,
    WorkflowPreviewV1,
    WorkflowState,
    canonical_json,
)
from fcp_mcp.workflow.operations import (
    CandidateDisposition,
    execute_plan,
)

GRAPH_VERSION = "1"
RUN_VERSION = "1"
SCHEMA_VERSION = "1"
_READ_CHUNK = 64 * 1024
_SUMMARY_MAX_CHARS = 4096
_OMISSION = "\n[summary truncated; complete diff is in private evidence]"


@dataclass(frozen=True)
class _FileEvidence:
    state: PriorDestinationState
    sha256: str | None
    size: int
    identity: tuple[int, int, int] | None


@dataclass
class _DescriptorSnapshot:
    descriptor: int
    size: int
    sha256: str
    identity: tuple[int, int, int]

    @property
    def path(self) -> Path:
        if self.descriptor < 0:
            raise _coded(ErrorCode.TRANSACTION_FAILED, "private snapshot is closed")
        return Path(f"/dev/fd/{self.descriptor}")

    def verify(self) -> None:
        if self.descriptor < 0:
            raise _coded(ErrorCode.TRANSACTION_FAILED, "private snapshot is closed")
        result = os.fstat(self.descriptor)
        if (
            not stat.S_ISREG(result.st_mode)
            or _identity(result) != self.identity
            or result.st_size != self.size
        ):
            raise _coded(ErrorCode.WORKFLOW_STALE, "private snapshot identity changed")
        digest = hashlib.sha256()
        offset = 0
        while offset < self.size:
            chunk = os.pread(
                self.descriptor,
                min(_READ_CHUNK, self.size - offset),
                offset,
            )
            if not chunk:
                raise _coded(ErrorCode.WORKFLOW_STALE, "private snapshot was truncated")
            digest.update(chunk)
            offset += len(chunk)
        if digest.hexdigest() != self.sha256:
            raise _coded(ErrorCode.WORKFLOW_STALE, "private snapshot content changed")

    def invoke(self, call: Callable[[Path], object]) -> object:
        self.verify()
        os.lseek(self.descriptor, 0, os.SEEK_SET)
        result = call(self.path)
        self.verify()
        return result

    def close(self) -> None:
        if self.descriptor < 0:
            return
        descriptor, self.descriptor = self.descriptor, -1
        try:
            os.close(descriptor)
        except BaseException:  # noqa: BLE001 - sanitize the cleanup boundary
            raise _coded(
                ErrorCode.TRANSACTION_FAILED,
                "private snapshot cleanup failed",
            ) from None

    def close_preserving(self, primary: BaseException) -> None:
        try:
            self.close()
        except BaseException:  # noqa: BLE001 - active primary must survive cleanup
            cleanup = _coded(
                ErrorCode.TRANSACTION_FAILED,
                "private snapshot cleanup failed",
            )
            if primary.__context__ is None:
                primary.__context__ = cleanup


def _coded(
    code: ErrorCode,
    message: str,
    cause: BaseException | None = None,
) -> FCPMCPError:
    error = FCPMCPError(code, message)
    if cause is not None:
        error.__cause__ = cause
    return error


def _new_unlinked_descriptor(root: Path) -> tuple[int, tuple[int, int, int]]:
    directory = -1
    descriptor = -1
    name: str | None = None
    created_identity: tuple[int, int, int] | None = None

    def preserve_cleanup(primary: BaseException) -> None:
        cleanup: FCPMCPError | None = None
        if directory >= 0 and name is not None and created_identity is not None:
            try:
                entry = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if _identity(entry) == created_identity:
                    os.unlink(name, dir_fd=directory)
            except FileNotFoundError:
                pass
            except BaseException:  # noqa: BLE001 - cleanup cannot mask primary
                cleanup = _coded(
                    ErrorCode.TRANSACTION_FAILED,
                    "private snapshot cleanup failed",
                )
        for owned in (descriptor, directory):
            if owned < 0:
                continue
            try:
                os.close(owned)
            except BaseException:  # noqa: BLE001 - cleanup cannot mask primary
                cleanup = cleanup or _coded(
                    ErrorCode.TRANSACTION_FAILED,
                    "private snapshot cleanup failed",
                )
        if cleanup is not None and primary.__context__ is None:
            primary.__context__ = cleanup

    try:
        directory_flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        directory = os.open(root, directory_flags)
        directory_result = os.fstat(directory)
        root_result = root.lstat()
        if (
            not stat.S_ISDIR(directory_result.st_mode)
            or _identity(directory_result) != _identity(root_result)
        ):
            raise _coded(
                ErrorCode.TRANSACTION_FAILED,
                "private snapshot directory identity changed",
            )
        snapshot_flags = (
            os.O_RDWR
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        for _ in range(16):
            name = f".workflow-snapshot-{secrets.token_hex(16)}.fcpxml"
            try:
                descriptor = os.open(
                    name,
                    snapshot_flags,
                    0o600,
                    dir_fd=directory,
                )
                break
            except FileExistsError:
                continue
        if descriptor < 0:
            raise _coded(
                ErrorCode.TRANSACTION_FAILED,
                "private snapshot name allocation failed",
            )
        created = os.fstat(descriptor)
        created_identity = _identity(created)
        if (
            not stat.S_ISREG(created.st_mode)
            or stat.S_IMODE(created.st_mode) != 0o600
            or created.st_uid != os.geteuid()
            or created.st_nlink != 1
        ):
            raise _coded(
                ErrorCode.TRANSACTION_FAILED,
                "private snapshot creation invariants failed",
            )
        unlinked = False
        for attempt in range(2):
            entry = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if (
                _identity(entry) != created_identity
                or stat.S_IMODE(entry.st_mode) != 0o600
                or entry.st_uid != os.geteuid()
                or entry.st_nlink != 1
            ):
                raise _coded(
                    ErrorCode.TRANSACTION_FAILED,
                    "private snapshot name identity changed",
                )
            try:
                os.unlink(name, dir_fd=directory)
                unlinked = True
                break
            except OSError:
                if attempt:
                    raise
        if not unlinked:
            raise _coded(
                ErrorCode.TRANSACTION_FAILED,
                "private snapshot unlink failed",
            )
        os.fsync(directory)
        retained = os.fstat(descriptor)
        if (
            _identity(retained) != created_identity
            or not stat.S_ISREG(retained.st_mode)
            or stat.S_IMODE(retained.st_mode) != 0o600
            or retained.st_uid != os.geteuid()
            or retained.st_nlink != 0
        ):
            raise _coded(
                ErrorCode.TRANSACTION_FAILED,
                "private snapshot did not become anonymous",
            )
        owned_directory, directory = directory, -1
        try:
            os.close(owned_directory)
        except BaseException:  # noqa: BLE001 - sanitize the cleanup boundary
            raise _coded(
                ErrorCode.TRANSACTION_FAILED,
                "private snapshot cleanup failed",
            ) from None
        return descriptor, created_identity
    except OSError as error:
        primary = _coded(
            ErrorCode.TRANSACTION_FAILED,
            "private snapshot creation failed",
        )
        preserve_cleanup(primary)
        raise primary from error
    except BaseException as primary:
        preserve_cleanup(primary)
        raise


def _write_all(descriptor: int, payload: bytes | memoryview) -> None:
    view = memoryview(payload)
    written = 0
    while written < len(view):
        count = os.write(descriptor, view[written:])
        if count <= 0:
            raise OSError("private snapshot write made no progress")
        written += count


def _snapshot_bytes(payload: bytes, root: Path) -> _DescriptorSnapshot:
    descriptor, identity = _new_unlinked_descriptor(root)
    try:
        _write_all(descriptor, payload)
        os.fsync(descriptor)
        snapshot = _DescriptorSnapshot(
            descriptor=descriptor,
            size=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
            identity=identity,
        )
        snapshot.verify()
        return snapshot
    except BaseException as primary:
        try:
            os.close(descriptor)
        except BaseException:  # noqa: BLE001 - cleanup cannot mask primary
            if primary.__context__ is None:
                primary.__context__ = _coded(
                    ErrorCode.TRANSACTION_FAILED,
                    "private snapshot cleanup failed",
                )
        raise


def _snapshot_regular(
    path: Path,
    limit: int,
    root: Path,
) -> tuple[_DescriptorSnapshot, _FileEvidence]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    source = os.open(path, flags)
    snapshot_fd = -1
    primary: BaseException | None = None
    try:
        opened = os.fstat(source)
        if not stat.S_ISREG(opened.st_mode):
            raise _coded(ErrorCode.INVALID_PATH, "workflow input is not a regular file")
        enforce_source_size(opened.st_size, max_source_bytes=limit)
        snapshot_fd, snapshot_identity = _new_unlinked_descriptor(root)
        digest = hashlib.sha256()
        total = 0
        while total <= limit:
            chunk = os.read(source, min(_READ_CHUNK, limit + 1 - total))
            if not chunk:
                break
            digest.update(chunk)
            _write_all(snapshot_fd, chunk)
            total += len(chunk)
        enforce_source_size(total, max_source_bytes=limit)
        os.fsync(snapshot_fd)
        after = os.fstat(source)
        current = path.lstat()
        if (
            _identity(opened) != _identity(after)
            or opened.st_size != after.st_size
            or _identity(opened) != _identity(current)
            or total != opened.st_size
        ):
            raise _coded(ErrorCode.WORKFLOW_STALE, "workflow file changed during inspection")
        sha256 = digest.hexdigest()
        snapshot = _DescriptorSnapshot(
            descriptor=snapshot_fd,
            size=total,
            sha256=sha256,
            identity=snapshot_identity,
        )
        snapshot.verify()
        snapshot_fd = -1
        return snapshot, _FileEvidence(
            PriorDestinationState.PRESENT,
            sha256,
            total,
            _identity(opened),
        )
    except BaseException as error:
        primary = error
        raise
    finally:
        cleanup: FCPMCPError | None = None
        for descriptor in (source, snapshot_fd):
            if descriptor < 0:
                continue
            try:
                os.close(descriptor)
            except BaseException:  # noqa: BLE001 - cleanup cannot mask primary
                cleanup = cleanup or _coded(
                    ErrorCode.TRANSACTION_FAILED,
                    "private snapshot cleanup failed",
                )
        if cleanup is not None:
            if primary is None:
                raise cleanup
            if primary.__context__ is None:
                primary.__context__ = cleanup


def _canonical_utc(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise _coded(ErrorCode.INVALID_CONFIGURATION, "workflow clock must return UTC")
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _identity(result: os.stat_result) -> tuple[int, int, int]:
    return (result.st_dev, result.st_ino, stat.S_IFMT(result.st_mode))


def _read_regular(path: Path, limit: int, *, missing_ok: bool) -> _FileEvidence:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        if missing_ok:
            return _FileEvidence(PriorDestinationState.ABSENT, None, 0, None)
        raise
    except OSError as error:
        raise _coded(ErrorCode.INVALID_PATH, "workflow input cannot be inspected", error)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise _coded(ErrorCode.INVALID_PATH, "workflow input is not a regular file")
        enforce_source_size(opened.st_size, max_source_bytes=limit)
        digest = hashlib.sha256()
        total = 0
        while total <= limit:
            chunk = os.read(descriptor, min(_READ_CHUNK, limit + 1 - total))
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
        enforce_source_size(total, max_source_bytes=limit)
        after = os.fstat(descriptor)
        current = path.lstat()
        if (
            _identity(opened) != _identity(after)
            or opened.st_size != after.st_size
            or _identity(opened) != _identity(current)
            or total != opened.st_size
        ):
            raise _coded(ErrorCode.WORKFLOW_STALE, "workflow file changed during inspection")
        return _FileEvidence(
            PriorDestinationState.PRESENT,
            digest.hexdigest(),
            total,
            _identity(opened),
        )
    except FileNotFoundError as error:
        raise _coded(ErrorCode.WORKFLOW_STALE, "workflow file changed during inspection", error)
    finally:
        os.close(descriptor)


def _matches(path: Path, evidence: _FileEvidence, limit: int) -> bool:
    try:
        current = _read_regular(path, limit, missing_ok=True)
    except (FCPMCPError, OSError):
        return False
    return (
        current.state is evidence.state
        and current.sha256 == evidence.sha256
        and current.size == evidence.size
        and current.identity == evidence.identity
    )


def _artifact_metadata(record: ArtifactRecord) -> ArtifactMetadataV1:
    try:
        created = datetime.fromisoformat(record.created_at.replace("Z", "+00:00"))
        return ArtifactMetadataV1(
            run_id=record.run_id,
            kind=record.kind,
            relative_path=record.relative_path,
            sha256=record.sha256,
            byte_size=record.byte_size,
            created_at=created,
        )
    except (ValueError, ValidationError) as error:
        raise _coded(ErrorCode.ARTIFACT_CORRUPT, "artifact metadata is invalid", error)


def _bounded_summary(text: str, byte_limit: int) -> str:
    limit = min(byte_limit, _SUMMARY_MAX_CHARS * 4)
    encoded = text.encode("utf-8")
    if len(encoded) <= limit and len(text) <= _SUMMARY_MAX_CHARS:
        return text or "No modeled semantic changes."
    marker = _OMISSION
    available_bytes = max(0, limit - len(marker.encode("utf-8")))
    available_chars = max(0, _SUMMARY_MAX_CHARS - len(marker))
    prefix = encoded[:available_bytes].decode("utf-8", errors="ignore")[:available_chars]
    rendered = f"{prefix}{marker}"
    return rendered[-_SUMMARY_MAX_CHARS:] if len(rendered) > _SUMMARY_MAX_CHARS else rendered


class WorkflowEngine:
    """Run the fixed prepare graph against explicit durable dependencies."""

    def __init__(
        self,
        config: RuntimeConfig,
        path_policy: PathPolicy,
        ledger: WorkflowLedger,
        artifacts: ArtifactStore,
        *,
        uuid_factory: Callable[[], object] | None = None,
        utc_clock: Callable[[], datetime] | None = None,
        monotonic_clock: Callable[[], float] | None = None,
        validator: FCPXMLValidator | None = None,
        log_stream: TextIO | None = None,
        fault_hook: Callable[[str], None] | None = None,
    ) -> None:
        self.config = config
        self.path_policy = path_policy
        self.ledger = ledger
        self.artifacts = artifacts
        self.uuid_factory = uuid_factory or uuid4
        self.utc_clock = utc_clock or (lambda: datetime.now(timezone.utc))
        self.monotonic_clock = monotonic_clock or monotonic
        self.validator = validator or FCPXMLValidator()
        self.log_stream = log_stream
        self.fault_hook = fault_hook
        self.execute_plan = execute_plan

    def _fault(self, boundary: str) -> None:
        if self.fault_hook is not None:
            self.fault_hook(boundary)

    def _elapsed(self, started: float) -> int:
        return max(0, int((self.monotonic_clock() - started) * 1000))

    def _observe(
        self,
        *,
        run: LedgerRunRecord,
        sequence: int,
        node: str,
        started: float,
        disposition: str = "succeeded",
        error_code: ErrorCode | None = None,
    ) -> None:
        record: dict[str, object] = {
            "event": "fcpxml_workflow",
            "run_id": run.run_id,
            "sequence": sequence,
            "graph_version": run.graph_version,
            "run_version": run.run_version,
            "schema_version": SCHEMA_VERSION,
            "profile": run.profile.value,
            "approval_mode": run.approval_mode.value,
            "node": node,
            "attempt": 1,
            "elapsed_ms": self._elapsed(started),
            "disposition": disposition,
        }
        for field in (
            "source_sha256",
            "prior_destination_sha256",
            "plan_sha256",
            "candidate_sha256",
            "diff_sha256",
        ):
            value = getattr(run, field)
            if value is not None:
                record[field] = value
        if error_code is not None:
            record["error_code"] = error_code.value
        emit_event(record, format=self.config.log_format, stream=self.log_stream)

    def _observe_mutation(
        self,
        result: EventMutationResult,
        node: str,
        started: float,
    ) -> LedgerRunRecord:
        self._observe(
            run=result.run,
            sequence=result.event.sequence,
            node=node,
            started=started,
        )
        self._fault(node)
        return result.run

    def _request_sha256(self, request: WorkflowPrepareRequestV1) -> str:
        return hashlib.sha256(canonical_json(request)).hexdigest()

    def _validate_and_parse_candidate(
        self,
        payload: bytes,
    ) -> tuple[ValidationResultV1, object]:
        snapshot = _snapshot_bytes(payload, self.artifacts.paths.root)
        primary: BaseException | None = None
        try:
            result = snapshot.invoke(self.validator.validate_file)
            parsed = snapshot.invoke(FCPXMLParser().parse)
            converted = self._validation_result(result)
            if not converted.valid:
                raise _coded(ErrorCode.VALIDATION_FAILED, "candidate validation failed")
            return converted, parsed
        except FCPMCPError as error:
            primary = error
            raise
        except Exception as error:  # noqa: BLE001 - validator/parser trust boundary
            primary = _coded(
                ErrorCode.VALIDATION_FAILED,
                "candidate validation failed",
                error,
            )
            raise primary
        finally:
            if primary is None:
                snapshot.close()
            else:
                snapshot.close_preserving(primary)

    def _validate_candidate(self, payload: bytes) -> ValidationResultV1:
        validation, _ = self._validate_and_parse_candidate(payload)
        return validation

    def _validation_result(self, result: ValidationResult) -> ValidationResultV1:
        issues = []
        omitted = max(0, len(result.issues) - (MAX_EVIDENCE_ITEMS - 1))
        retained = result.issues[: MAX_EVIDENCE_ITEMS - (1 if omitted else 0)]
        for index, issue in enumerate(retained, start=1):
            severity = {
                "error": FindingDisposition.FAIL,
                "warning": FindingDisposition.WARN,
                "info": FindingDisposition.PASS,
            }.get(issue.severity, FindingDisposition.FAIL)
            summary = issue.message.replace("\n", " ")[:4096] or "validation issue"
            issues.append(
                ValidationIssueV1(
                    severity=severity,
                    code=f"validator-{index:03d}",
                    summary=summary,
                )
            )
        if omitted:
            has_omitted_error = any(
                issue.severity == "error" for issue in result.issues[len(retained) :]
            )
            issues.append(
                ValidationIssueV1(
                    severity=(
                        FindingDisposition.FAIL
                        if has_omitted_error
                        else FindingDisposition.WARN
                    ),
                    code="validator-omitted",
                    summary=f"{omitted} additional validation issues omitted",
                )
            )
        valid = result.valid and not any(
            issue.severity is FindingDisposition.FAIL for issue in issues
        )
        return ValidationResultV1(valid=valid, issues=tuple(issues))

    def _rehydrate(self, run: LedgerRunRecord) -> WorkflowPreviewV1:
        if run.state is not WorkflowState.AWAITING_APPROVAL:
            raise _coded(
                ErrorCode.WORKFLOW_STATE_CONFLICT,
                f"existing workflow {run.run_id} is {run.state.value}",
            )
        candidate_record = self.ledger.get_artifact(run.run_id, ArtifactKind.CANDIDATE)
        diff_record = self.ledger.get_artifact(run.run_id, ArtifactKind.DIFF)
        if candidate_record is None or diff_record is None:
            raise _coded(ErrorCode.ARTIFACT_CORRUPT, "prepared evidence is incomplete")
        candidate = self.artifacts.read(_artifact_metadata(candidate_record))
        diff_bytes = self.artifacts.read(_artifact_metadata(diff_record))
        if (
            hashlib.sha256(candidate).hexdigest() != run.candidate_sha256
            or hashlib.sha256(diff_bytes).hexdigest() != run.diff_sha256
        ):
            raise _coded(ErrorCode.ARTIFACT_CORRUPT, "prepared evidence hash mismatch")
        try:
            envelope = json.loads(diff_bytes.decode("utf-8"))
            if not isinstance(envelope, dict) or canonical_json(envelope) != diff_bytes:
                raise ValueError("diff envelope is not canonical")
            expected_keys = {
                "schema_version",
                "graph_version",
                "run_version",
                "run_id",
                "source_sha256",
                "plan_sha256",
                "candidate_sha256",
                "semantic_diff",
                "validation",
                "operation_receipts",
                "summary",
                "warnings",
            }
            if set(envelope) != expected_keys:
                raise ValueError("diff envelope fields are invalid")
            expected_binding = {
                "schema_version": SCHEMA_VERSION,
                "graph_version": run.graph_version,
                "run_version": run.run_version,
                "run_id": run.run_id,
                "source_sha256": run.source_sha256,
                "plan_sha256": run.plan_sha256,
                "candidate_sha256": run.candidate_sha256,
            }
            if any(envelope[field] != value for field, value in expected_binding.items()):
                raise ValueError("diff envelope binding mismatch")
            self._validate_semantic_diff_mapping(envelope["semantic_diff"])
            validation = ValidationResultV1.model_validate_json(
                json.dumps(envelope["validation"], separators=(",", ":"))
            )
            if self._validate_candidate(candidate) != validation:
                raise ValueError("recorded candidate validation evidence changed")
            receipts = tuple(
                OperationReceiptV1.model_validate_json(
                    json.dumps(item, separators=(",", ":"))
                )
                for item in envelope["operation_receipts"]
            )
            return WorkflowPreviewV1(
                graph_version=run.graph_version,
                package_version=run.package_version,
                run_version=run.run_version,
                run_id=run.run_id,
                state=run.state,
                source_path=run.source_path,
                destination_path=run.destination_path,
                source_sha256=run.source_sha256,
                prior_destination_state=run.prior_destination_state,
                prior_destination_sha256=run.prior_destination_sha256,
                plan_sha256=run.plan_sha256,
                candidate_sha256=run.candidate_sha256,
                candidate_size_bytes=run.candidate_size_bytes,
                diff_sha256=run.diff_sha256,
                diff_size_bytes=run.diff_size_bytes,
                validation=validation,
                operation_receipts=receipts,
                summary=envelope["summary"],
                warnings=tuple(envelope["warnings"]),
                approval_mode=run.approval_mode,
                approval_expires_at=run.expires_at,
                run_uri=f"fcp-workflow://runs/{run.run_id}",
                events_uri=f"fcp-workflow://runs/{run.run_id}/events",
                diff_uri=f"fcp-workflow://runs/{run.run_id}/diff",
            )
        except (KeyError, TypeError, ValueError, ValidationError) as error:
            raise _coded(ErrorCode.ARTIFACT_CORRUPT, "prepared diff evidence is invalid", error)

    @staticmethod
    def _validate_semantic_diff_mapping(value: object) -> None:
        if not isinstance(value, dict) or set(value) != {
            "schema_version",
            "project_count_source",
            "project_count_candidate",
            "change_count",
            "changes",
        }:
            raise ValueError("semantic diff fields are invalid")
        if value["schema_version"] != SCHEMA_VERSION:
            raise ValueError("semantic diff schema is unsupported")
        counts = (
            value["project_count_source"],
            value["project_count_candidate"],
            value["change_count"],
        )
        if any(type(item) is not int or item < 0 for item in counts):
            raise ValueError("semantic diff counts are invalid")
        changes = value["changes"]
        if not isinstance(changes, list) or len(changes) != value["change_count"]:
            raise ValueError("semantic diff change count is invalid")
        fields = {
            "project_name",
            "project_occurrence",
            "source_project_index",
            "candidate_project_index",
            "entity_kind",
            "entity_name",
            "entity_occurrence",
            "source_entity_index",
            "candidate_entity_index",
            "change_type",
            "field",
            "before",
            "after",
        }
        for change in changes:
            if not isinstance(change, dict) or set(change) != fields:
                raise ValueError("semantic diff change fields are invalid")
            if change["change_type"] not in {"added", "removed", "changed"}:
                raise ValueError("semantic diff change type is invalid")
            for name in (
                "project_occurrence",
                "entity_occurrence",
            ):
                if type(change[name]) is not int or change[name] < 1:
                    raise ValueError("semantic diff occurrence is invalid")
            for name in (
                "source_project_index",
                "candidate_project_index",
                "source_entity_index",
                "candidate_entity_index",
            ):
                item = change[name]
                if item is not None and (type(item) is not int or item < 0):
                    raise ValueError("semantic diff position is invalid")
            for name in (
                "project_name",
                "entity_kind",
                "entity_name",
                "field",
            ):
                if not isinstance(change[name], str) or not change[name]:
                    raise ValueError("semantic diff label is invalid")
            for name in ("before", "after"):
                if change[name] is not None and not isinstance(change[name], str):
                    raise ValueError("semantic diff value is invalid")

    def _fail(
        self,
        run: LedgerRunRecord,
        error: FCPMCPError,
        started: float,
        *,
        plan_sha256: str | None,
        receipts: tuple[OperationReceiptV1, ...],
    ) -> None:
        try:
            body = canonical_json(
                {
                    "schema_version": SCHEMA_VERSION,
                    "graph_version": GRAPH_VERSION,
                    "run_version": RUN_VERSION,
                    "run_id": run.run_id,
                    "plan_sha256": plan_sha256,
                    "error_code": error.code.value,
                    "operation_receipts": [
                        receipt.model_dump(mode="json")
                        for receipt in receipts
                    ],
                }
            )
            metadata = self.artifacts.write(
                run.run_id,
                ArtifactKind.FAILURE_EVIDENCE,
                body,
            )
            reopened = self.artifacts.read(metadata)
            if (
                reopened != body
                or hashlib.sha256(reopened).hexdigest() != metadata.sha256
            ):
                raise _coded(
                    ErrorCode.ARTIFACT_CORRUPT,
                    "failure evidence reopen mismatch",
                )
            try:
                parsed = json.loads(reopened.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as parse_error:
                raise _coded(
                    ErrorCode.ARTIFACT_CORRUPT,
                    "failure evidence reopen failed",
                    parse_error,
                )
            if not isinstance(parsed, dict) or canonical_json(parsed) != reopened:
                raise _coded(
                    ErrorCode.ARTIFACT_CORRUPT,
                    "failure evidence is not canonical",
                )
            result = self.ledger.record_prepare_failure(
                metadata,
                expected_revision=run.revision,
                error_code=error.code,
                error_summary="workflow prepare failed",
                event_type="failed",
                event_payload={"error_code": error.code.value},
                elapsed_ms=self._elapsed(started),
            )
            self._observe(
                run=result.run,
                sequence=result.event.sequence,
                node="failed",
                started=started,
                disposition="failed",
                error_code=error.code,
            )
        except BaseException:  # noqa: BLE001 - secondary must never mask primary
            if error.__context__ is None:
                error.__context__ = _coded(
                    ErrorCode.TRANSACTION_FAILED,
                    "secondary failure evidence persistence failed",
                )

    def prepare(self, request: WorkflowPrepareRequestV1) -> WorkflowPreviewV1:
        if not isinstance(request, WorkflowPrepareRequestV1):
            raise _coded(ErrorCode.INVALID_ARGUMENTS, "prepare request is invalid")
        try:
            request.enforce_operation_limit(self.config.max_operations)
        except (TypeError, ValueError) as error:
            raise _coded(ErrorCode.INVALID_ARGUMENTS, "prepare request exceeds limits", error)
        source = self.path_policy.resolve_input(
            request.source_path,
            kind="file",
            suffixes={".fcpxml"},
        )
        destination = self.path_policy.resolve_output(
            request.destination_path,
            input_path=source,
            suffixes={".fcpxml"},
        )
        request_sha256 = self._request_sha256(request)
        run_id = str(self.uuid_factory())
        started = self.monotonic_clock()
        created = self.ledger.create_run(
            run_id=run_id,
            graph_version=GRAPH_VERSION,
            run_version=RUN_VERSION,
            profile=self.config.profile,
            approval_mode=self.config.workflow_approval,
            source_path=str(source),
            destination_path=str(destination),
            event_type="run_created",
            event_payload={"node": "run_created", "attempt": 1},
            idempotency_key=request.idempotency_key,
            request_sha256=(
                request_sha256 if request.idempotency_key is not None else None
            ),
        )
        if created.existing:
            return self._rehydrate(created.run)
        run = created.run
        failure_plan_sha256: str | None = None
        failure_receipts: tuple[OperationReceiptV1, ...] = ()
        source_snapshot: _DescriptorSnapshot | None = None
        self._observe(run=run, sequence=1, node="run_created", started=started)
        self._fault("run_created")

        try:
            source_snapshot, source_evidence = _snapshot_regular(
                source,
                self.config.max_source_bytes,
                self.artifacts.paths.root,
            )
            prior_evidence = _read_regular(
                destination,
                self.config.max_source_bytes,
                missing_ok=True,
            )
            inspected = self.ledger.append_event(
                run.run_id,
                expected_state=WorkflowState.PREPARING,
                expected_revision=run.revision,
                event_type="source_inspected",
                payload={
                    "source_sha256": source_evidence.sha256,
                    "prior_destination_state": prior_evidence.state,
                    "prior_destination_sha256": prior_evidence.sha256,
                },
                elapsed_ms=self._elapsed(started),
                projection_patch={
                    "source_sha256": source_evidence.sha256,
                    "prior_destination_state": prior_evidence.state,
                    "prior_destination_sha256": prior_evidence.sha256,
                },
            )
            run = self._observe_mutation(inspected, "source_inspected", started)
            if (
                request.expected_source_sha256 is not None
                and request.expected_source_sha256 != source_evidence.sha256
            ):
                raise _coded(ErrorCode.WORKFLOW_STALE, "source hash precondition failed")

            source_document = source_snapshot.invoke(FCPXMLParser().parse)
            plan = WorkflowPlanV1(operations=request.operations)
            execution = source_snapshot.invoke(
                lambda snapshot_path: self.execute_plan(snapshot_path, plan)
            )
            plan_sha256 = execution.normalized_plan.caller_plan_sha256
            failure_receipts = execution.receipts
            failure_plan_sha256 = plan_sha256
            normalized_event = self.ledger.append_event(
                run.run_id,
                expected_state=WorkflowState.PREPARING,
                expected_revision=run.revision,
                event_type="plan_normalized",
                payload={"plan_sha256": plan_sha256},
                elapsed_ms=self._elapsed(started),
                projection_patch={"plan_sha256": plan_sha256},
            )
            run = self._observe_mutation(normalized_event, "plan_normalized", started)

            if execution.disposition is not CandidateDisposition.SUCCEEDED:
                raise _coded(
                    execution.error_code or ErrorCode.OPERATION_FAILED,
                    "workflow operation failed",
                )
            if execution.candidate_bytes is None:
                raise _coded(ErrorCode.OPERATION_FAILED, "workflow produced no candidate")
            source_snapshot.close()
            source_snapshot = None
            dry_run = self.ledger.append_event(
                run.run_id,
                expected_state=WorkflowState.PREPARING,
                expected_revision=run.revision,
                event_type="dry_run_completed",
                payload={
                    "operation_count": len(execution.receipts),
                    "disposition": "succeeded",
                },
                elapsed_ms=self._elapsed(started),
            )
            run = self._observe_mutation(dry_run, "dry_run_completed", started)

            if not _matches(source, source_evidence, self.config.max_source_bytes):
                raise _coded(ErrorCode.WORKFLOW_STALE, "source changed after execution")
            candidate_metadata = self.artifacts.write(
                run.run_id,
                ArtifactKind.CANDIDATE,
                execution.candidate_bytes,
            )
            self._fault("candidate_body_written")
            candidate_bytes = self.artifacts.read(candidate_metadata)
            if candidate_bytes != execution.candidate_bytes:
                raise _coded(ErrorCode.ARTIFACT_CORRUPT, "candidate reopen mismatch")
            validation, candidate_document = self._validate_and_parse_candidate(
                candidate_bytes
            )
            candidate_recorded = self.ledger.record_artifact(
                candidate_metadata,
                expected_state=WorkflowState.PREPARING,
                expected_revision=run.revision,
                event_type="candidate_validated",
                event_payload={
                    "candidate_sha256": candidate_metadata.sha256,
                    "candidate_size_bytes": candidate_metadata.byte_size,
                    "valid": validation.valid,
                },
                elapsed_ms=self._elapsed(started),
            )
            run = candidate_recorded.run
            self._observe(
                run=run,
                sequence=candidate_recorded.event.sequence,
                node="candidate_validated",
                started=started,
            )
            self._fault("candidate_validated")

            semantic_diff = append_operation_effects(
                build_workflow_diff(source_document, candidate_document),
                execution.receipts,
            )
            summary_lines = [
                f"{change.change_type} {change.entity_kind} "
                f"{change.entity_name} {change.field}: "
                f"{change.before or '-'} -> {change.after or '-'}"
                for change in semantic_diff.changes
            ]
            summary = _bounded_summary(
                "\n".join(summary_lines) or "No modeled semantic changes.",
                self.config.max_diff_bytes,
            )
            warnings = tuple(
                warning
                for receipt in execution.receipts
                for warning in receipt.warnings
            )[:MAX_WARNINGS]
            envelope = {
                "schema_version": SCHEMA_VERSION,
                "graph_version": GRAPH_VERSION,
                "run_version": RUN_VERSION,
                "run_id": run.run_id,
                "source_sha256": source_evidence.sha256,
                "plan_sha256": plan_sha256,
                "candidate_sha256": candidate_metadata.sha256,
                "semantic_diff": semantic_diff.to_mapping(),
                "validation": validation.model_dump(mode="json"),
                "operation_receipts": [
                    receipt.model_dump(mode="json") for receipt in execution.receipts
                ],
                "summary": summary,
                "warnings": list(warnings),
            }
            diff_bytes = canonical_json(envelope)
            diff_metadata = self.artifacts.write(
                run.run_id,
                ArtifactKind.DIFF,
                diff_bytes,
            )
            self._fault("diff_body_written")
            reopened_diff = self.artifacts.read(diff_metadata)
            try:
                parsed_envelope = json.loads(reopened_diff.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise _coded(ErrorCode.ARTIFACT_CORRUPT, "diff reopen failed", error)
            if reopened_diff != diff_bytes or canonical_json(parsed_envelope) != diff_bytes:
                raise _coded(ErrorCode.ARTIFACT_CORRUPT, "diff reopen mismatch")
            diff_recorded = self.ledger.record_artifact(
                diff_metadata,
                expected_state=WorkflowState.PREPARING,
                expected_revision=run.revision,
                event_type="diff_created",
                event_payload={
                    "diff_sha256": diff_metadata.sha256,
                    "diff_size_bytes": diff_metadata.byte_size,
                    "change_count": semantic_diff.change_count,
                },
                elapsed_ms=self._elapsed(started),
            )
            run = diff_recorded.run
            self._observe(
                run=run,
                sequence=diff_recorded.event.sequence,
                node="diff_created",
                started=started,
            )
            self._fault("diff_created")

            if not _matches(source, source_evidence, self.config.max_source_bytes):
                raise _coded(ErrorCode.WORKFLOW_STALE, "source changed before preview")
            if not _matches(destination, prior_evidence, self.config.max_source_bytes):
                raise _coded(ErrorCode.WORKFLOW_STALE, "destination changed before preview")
            expires_at = _canonical_utc(
                self.utc_clock()
                + timedelta(seconds=self.config.approval_ttl_seconds)
            )
            persisted = self.ledger.append_event(
                run.run_id,
                expected_state=WorkflowState.PREPARING,
                expected_revision=run.revision,
                event_type="preview_persisted",
                payload={"expires_at": expires_at},
                elapsed_ms=self._elapsed(started),
                projection_patch={"expires_at": expires_at},
            )
            run = self._observe_mutation(persisted, "preview_persisted", started)
            awaiting = self.ledger.transition(
                run.run_id,
                expected_state=WorkflowState.PREPARING,
                expected_revision=run.revision,
                target_state=WorkflowState.AWAITING_APPROVAL,
                event_type="awaiting_approval",
                payload={"expires_at": expires_at},
                elapsed_ms=self._elapsed(started),
                projection_patch={"expires_at": expires_at},
            )
            run = self._observe_mutation(awaiting, "awaiting_approval", started)
            return self._rehydrate(run)
        except FCPMCPError as error:
            current = self.ledger.get_run(run.run_id)
            if current is not None and current.state is WorkflowState.PREPARING:
                self._fail(
                    current,
                    error,
                    started,
                    plan_sha256=failure_plan_sha256,
                    receipts=failure_receipts,
                )
            raise _coded(error.code, "workflow prepare failed", error)
        except Exception as error:  # noqa: BLE001 - normalize the public engine boundary
            coded = _coded(ErrorCode.INTERNAL_ERROR, "workflow prepare failed", error)
            current = self.ledger.get_run(run.run_id)
            if current is not None and current.state is WorkflowState.PREPARING:
                self._fail(
                    current,
                    coded,
                    started,
                    plan_sha256=failure_plan_sha256,
                    receipts=failure_receipts,
                )
            raise coded
        finally:
            if source_snapshot is not None:
                snapshot, source_snapshot = source_snapshot, None
                primary = sys.exc_info()[1]
                if primary is None:
                    snapshot.close()
                else:
                    snapshot.close_preserving(primary)

    def reconcile_preparing(self, run_id: str) -> LedgerRunRecord:
        run = self.ledger.get_run(run_id)
        if run is None:
            raise _coded(ErrorCode.WORKFLOW_STATE_CONFLICT, "workflow run does not exist")
        if run.state is not WorkflowState.PREPARING:
            return run
        if run.prior_destination_state is None:
            raise _coded(
                ErrorCode.RECOVERY_REQUIRED,
                "preparing run lacks destination precondition evidence",
            )
        evidence = _FileEvidence(
            state=run.prior_destination_state,
            sha256=run.prior_destination_sha256,
            size=0,
            identity=None,
        )
        # The ledger intentionally persists state/hash rather than transient inode
        # identity. Reconciliation re-observes exact bytes and state.
        try:
            current = _read_regular(
                Path(run.destination_path),
                self.config.max_source_bytes,
                missing_ok=True,
            )
        except (FCPMCPError, OSError) as error:
            raise _coded(
                ErrorCode.RECOVERY_REQUIRED,
                "destination cannot be verified for reconciliation",
                error,
            )
        if (
            current.state is not evidence.state
            or current.sha256 != evidence.sha256
        ):
            raise _coded(
                ErrorCode.RECOVERY_REQUIRED,
                "destination precondition changed during prepare",
            )
        result = self.ledger.transition(
            run.run_id,
            expected_state=WorkflowState.PREPARING,
            expected_revision=run.revision,
            target_state=WorkflowState.FAILED,
            event_type="prepare_reconciled_failed",
            payload={"error_code": ErrorCode.TRANSACTION_FAILED.value},
            projection_patch={
                "terminal_error_code": ErrorCode.TRANSACTION_FAILED,
                "terminal_error_summary": "interrupted prepare reconciled",
            },
        )
        return result.run

    def _approval_run(self, run_id: str) -> LedgerRunRecord:
        run = self.ledger.get_run(run_id)
        if run is None:
            raise _coded(
                ErrorCode.WORKFLOW_STATE_CONFLICT,
                "workflow run does not exist",
            )
        return run

    def effective_state(self, run_id: str) -> WorkflowState:
        """Return effective approval expiry without changing the ledger."""
        return effective_workflow_state(
            self._approval_run(run_id),
            now=self.utc_clock(),
        )

    def approve_cli(
        self,
        run_id: str,
        *,
        input_stream: TextIO,
        output_stream: TextIO,
        yes: bool = False,
        expect_candidate_sha256: str | None = None,
        operator: str | None = None,
        host: str | None = None,
    ) -> DecisionMutationResult:
        """Apply the CLI approval policy to one exact prepared run."""
        return apply_cli_approval(
            self.ledger,
            self._approval_run(run_id),
            plan_schema_version=SCHEMA_VERSION,
            now=self.utc_clock(),
            input_stream=input_stream,
            output_stream=output_stream,
            yes=yes,
            expect_candidate_sha256=expect_candidate_sha256,
            operator=operator,
            host=host,
        )

    def approve_client(
        self,
        run_id: str,
        *,
        expect_candidate_sha256: str,
    ) -> ClientApproval:
        """Stage client evidence for Task 17's atomic commit boundary."""
        return stage_client_approval(
            self.ledger,
            self._approval_run(run_id),
            plan_schema_version=SCHEMA_VERSION,
            now=self.utc_clock(),
            expect_candidate_sha256=expect_candidate_sha256,
        )

    def reject(
        self,
        run_id: str,
        *,
        reason: str | None = None,
        operator: str | None = None,
        host: str | None = None,
        terminal_present: bool = False,
    ) -> DecisionMutationResult:
        """Persist one immutable approval rejection."""
        return reject_workflow(
            self.ledger,
            self._approval_run(run_id),
            plan_schema_version=SCHEMA_VERSION,
            reason=reason,
            operator=operator,
            host=host,
            terminal_present=terminal_present,
        )

    def cancel(
        self,
        run_id: str,
        *,
        reason: str | None = None,
    ) -> LedgerRunRecord:
        """Cancel a legal workflow run while preserving its evidence."""
        return cancel_workflow(
            self.ledger,
            self._approval_run(run_id),
            reason=reason,
        )


__all__ = ["GRAPH_VERSION", "RUN_VERSION", "SCHEMA_VERSION", "WorkflowEngine"]
