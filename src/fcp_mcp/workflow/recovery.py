"""Bounded crash assessment and selected-run verification helpers."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import stat
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from fcp_mcp.contracts import ErrorCode, FCPMCPError
from fcp_mcp.workflow.artifacts import ArtifactKind, ArtifactMetadataV1, ArtifactStore
from fcp_mcp.workflow.ledger import (
    LedgerRunRecord,
    WorkflowLedger,
    approval_binding_for_run,
)
from fcp_mcp.workflow.locking import OwnerLiveness, assess_owner_liveness
from fcp_mcp.workflow.models import (
    FindingDisposition,
    LockState,
    ObservedFileState,
    PriorDestinationState,
    RecoveryAssessmentV1,
    RecoveryBranch,
    VerificationFindingV1,
    WorkflowCommitReceiptV1,
    WorkflowState,
    WorkflowVerificationResultV1,
    canonical_json,
)

_READ_CHUNK = 64 * 1024
_MAX_LOCK_BYTES = 4096


@dataclass(frozen=True)
class FileObservation:
    state: ObservedFileState
    sha256: str | None
    size: int | None


@dataclass(frozen=True)
class LockObservation:
    state: LockState
    sha256: str | None
    path: Path


def _coded(code: ErrorCode, message: str) -> FCPMCPError:
    return FCPMCPError(code, message)


def _artifact_metadata(
    run_id: str,
    kind: ArtifactKind,
    relative_path: str,
    sha256: str,
    byte_size: int,
    created_at: str,
) -> ArtifactMetadataV1:
    return ArtifactMetadataV1(
        run_id=run_id,
        kind=kind,
        relative_path=relative_path,
        sha256=sha256,
        byte_size=byte_size,
        created_at=datetime.fromisoformat(created_at.replace("Z", "+00:00")),
    )


def read_regular_bytes(
    path: Path,
    limit: int,
    *,
    required_mode: int | None = None,
) -> tuple[FileObservation, bytes | None]:
    """Read and hash one stable regular file without following the final entry."""
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        result = os.fstat(descriptor)
        if (
            not stat.S_ISREG(result.st_mode)
            or result.st_size > limit
            or (
                required_mode is not None
                and stat.S_IMODE(result.st_mode) != required_mode
            )
        ):
            return FileObservation(ObservedFileState.UNREADABLE, None, None), None
        digest = hashlib.sha256()
        chunks: list[bytes] = []
        total = 0
        while total < result.st_size:
            chunk = os.read(descriptor, min(_READ_CHUNK, result.st_size - total))
            if not chunk:
                return FileObservation(ObservedFileState.UNREADABLE, None, None), None
            digest.update(chunk)
            chunks.append(chunk)
            total += len(chunk)
        current = os.stat(path, follow_symlinks=False)
        if not os.path.samestat(result, current):
            return FileObservation(ObservedFileState.UNREADABLE, None, None), None
        return (
            FileObservation(
                ObservedFileState.PRESENT,
                digest.hexdigest(),
                result.st_size,
            ),
            b"".join(chunks),
        )
    except FileNotFoundError:
        return FileObservation(ObservedFileState.ABSENT, None, None), None
    except OSError:
        return FileObservation(ObservedFileState.UNREADABLE, None, None), None
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass


def observe_file(path: Path, limit: int) -> FileObservation:
    """Hash one regular file without following the final path component."""
    observation, _ = read_regular_bytes(path, limit)
    return observation


def _lock_path(locks: Path, destination: Path) -> Path:
    resolved = destination.expanduser().resolve()
    key = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()
    return locks / f"{key}.lock"


def _read_lock(path: Path) -> tuple[bytes, os.stat_result] | None:
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        result = os.fstat(descriptor)
        if (
            not stat.S_ISREG(result.st_mode)
            or stat.S_IMODE(result.st_mode) != 0o600
            or result.st_uid != os.geteuid()
            or result.st_nlink != 1
            or result.st_size > _MAX_LOCK_BYTES
            or result.st_size <= 0
        ):
            return None
        payload = os.read(descriptor, _MAX_LOCK_BYTES + 1)
        current = os.stat(path, follow_symlinks=False)
        if len(payload) != result.st_size or not os.path.samestat(result, current):
            return None
        return payload, result
    except (FileNotFoundError, OSError):
        return None
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass


def observe_lock(locks: Path, destination: Path, run_id: str) -> LockObservation:
    path = _lock_path(locks, destination)
    try:
        path.lstat()
    except FileNotFoundError:
        return LockObservation(LockState.ABSENT, None, path)
    except OSError:
        return LockObservation(LockState.UNKNOWN, None, path)
    opened = _read_lock(path)
    if opened is None:
        return LockObservation(LockState.UNKNOWN, None, path)
    payload, _ = opened
    digest = hashlib.sha256(payload).hexdigest()
    try:
        body = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return LockObservation(LockState.UNKNOWN, digest, path)
    expected_destination = hashlib.sha256(
        str(destination.expanduser().resolve()).encode("utf-8")
    ).hexdigest()
    if (
        not isinstance(body, dict)
        or body.get("schema_version") != "1"
        or body.get("run_id") != run_id
        or body.get("destination_sha256") != expected_destination
        or type(body.get("pid")) is not int
        or not isinstance(body.get("host"), str)
    ):
        return LockObservation(LockState.UNKNOWN, digest, path)
    owner = assess_owner_liveness(body["pid"], body["host"])
    if owner == OwnerLiveness.DEAD:
        state = LockState.STALE
    elif owner == OwnerLiveness.ALIVE:
        state = LockState.HELD
    else:
        state = LockState.UNKNOWN
    return LockObservation(state, digest, path)


def _candidate_bytes(
    ledger: WorkflowLedger,
    artifacts: ArtifactStore,
    run: LedgerRunRecord,
) -> bytes | None:
    record = ledger.get_artifact(run.run_id, ArtifactKind.CANDIDATE)
    if record is None:
        return None
    try:
        return artifacts.read(
            _artifact_metadata(
                record.run_id,
                record.kind,
                record.relative_path,
                record.sha256,
                record.byte_size,
                record.created_at,
            )
        )
    except FCPMCPError:
        return None


def assess_run(
    *,
    ledger: WorkflowLedger,
    artifacts: ArtifactStore,
    run: LedgerRunRecord,
    max_file_bytes: int,
    validate_candidate: Callable[[bytes], object],
    receipt_evidence_status: Callable[[LedgerRunRecord], str],
) -> RecoveryAssessmentV1:
    """Return a pure classification from durable ledger and filesystem evidence."""
    destination = Path(run.destination_path)
    destination_observation = observe_file(destination, max_file_bytes)
    backup_observation = (
        observe_file(Path(run.expected_backup_path), max_file_bytes)
        if run.expected_backup_path is not None
        else FileObservation(ObservedFileState.ABSENT, None, None)
    )
    lock = observe_lock(artifacts.paths.locks, destination, run.run_id)
    evidence = [
        f"run_state={run.state.value}",
        f"destination_state={destination_observation.state.value}",
        f"backup_state={backup_observation.state.value}",
        f"lock_state={lock.state.value}",
    ]
    ambiguity: list[str] = []
    branch = RecoveryBranch.NONE

    if run.state is WorkflowState.COMMITTING:
        receipt_status = receipt_evidence_status(run)
        evidence.append(f"receipt_state={receipt_status}")
        if receipt_status == "invalid":
            ambiguity.append("unrecorded receipt evidence is inconsistent")
        candidate = _candidate_bytes(ledger, artifacts, run)
        candidate_valid = False
        if candidate is None or hashlib.sha256(candidate).hexdigest() != run.candidate_sha256:
            ambiguity.append("candidate artifact is missing or corrupt")
        else:
            try:
                validate_candidate(candidate)
            except (FCPMCPError, OSError, ValueError):
                ambiguity.append("candidate artifact is not valid FCPXML")
            else:
                candidate_valid = True

        backup_consistent = (
            run.prior_destination_state is PriorDestinationState.ABSENT
            and run.expected_backup_path is None
            and backup_observation.state is ObservedFileState.ABSENT
        ) or (
            run.prior_destination_state is PriorDestinationState.PRESENT
            and run.expected_backup_path is not None
            and (
                backup_observation.state is ObservedFileState.ABSENT
                or (
                    backup_observation.state is ObservedFileState.PRESENT
                    and backup_observation.sha256 == run.prior_destination_sha256
                )
            )
        )
        candidate_installed = (
            candidate_valid
            and destination_observation.state is ObservedFileState.PRESENT
            and destination_observation.sha256 == run.candidate_sha256
        )
        required_backup_present = (
            run.prior_destination_state is PriorDestinationState.ABSENT
            or (
                backup_observation.state is ObservedFileState.PRESENT
                and backup_observation.sha256 == run.prior_destination_sha256
            )
        )
        prior_restored = (
            run.prior_destination_state is PriorDestinationState.ABSENT
            and destination_observation.state is ObservedFileState.ABSENT
        ) or (
            run.prior_destination_state is PriorDestinationState.PRESENT
            and destination_observation.state is ObservedFileState.PRESENT
            and destination_observation.sha256 == run.prior_destination_sha256
        )
        if (
            receipt_status in {"absent", "valid"}
            and candidate_installed
            and required_backup_present
        ):
            branch = RecoveryBranch.FINALIZE_COMMITTED
        elif (
            receipt_status == "absent"
            and candidate_valid
            and prior_restored
            and backup_consistent
        ):
            branch = RecoveryBranch.MARK_ROLLED_BACK
        else:
            if receipt_status == "valid" and not candidate_installed:
                ambiguity.append("receipt claims commit but destination is not candidate")
            if candidate_installed and not required_backup_present:
                ambiguity.append("required matching backup is missing")
            if not candidate_installed and not prior_restored:
                ambiguity.append("destination matches neither candidate nor prior state")
            if not backup_consistent:
                ambiguity.append("backup evidence contradicts commit intent")
            branch = RecoveryBranch.MARK_RECOVERY_REQUIRED
    elif run.state is WorkflowState.APPROVED and lock.state is LockState.STALE:
        source_observation = observe_file(Path(run.source_path), max_file_bytes)
        prior_unchanged = (
            run.prior_destination_state is PriorDestinationState.ABSENT
            and destination_observation.state is ObservedFileState.ABSENT
        ) or (
            run.prior_destination_state is PriorDestinationState.PRESENT
            and destination_observation.state is ObservedFileState.PRESENT
            and destination_observation.sha256 == run.prior_destination_sha256
        )
        if source_observation.sha256 == run.source_sha256 and prior_unchanged:
            branch = RecoveryBranch.CLEAR_ORPHAN_LOCK
        else:
            ambiguity.append("approved preconditions changed while lock was orphaned")
            branch = RecoveryBranch.MARK_RECOVERY_REQUIRED

    return RecoveryAssessmentV1(
        destination_state=destination_observation.state,
        destination_sha256=destination_observation.sha256,
        backup_state=backup_observation.state,
        backup_sha256=backup_observation.sha256,
        lock_state=lock.state,
        lock_sha256=lock.sha256,
        recommended_branch=branch,
        evidence=tuple(evidence),
        ambiguity_reasons=tuple(ambiguity),
    )


def remove_stale_lock(locks: Path, destination: Path, run_id: str) -> None:
    """Descriptor-relatively unlink one reverified dead-owner lock."""
    observed = observe_lock(locks, destination, run_id)
    if observed.state is not LockState.STALE:
        raise _coded(ErrorCode.RECOVERY_REQUIRED, "lock owner is not proven dead")
    directory = -1
    descriptor = -1
    try:
        directory = os.open(
            locks,
            os.O_RDONLY
            | os.O_DIRECTORY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        descriptor = os.open(
            observed.path.name,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory,
        )
        opened = os.fstat(descriptor)
        current = os.stat(
            observed.path.name,
            dir_fd=directory,
            follow_symlinks=False,
        )
        payload = os.read(descriptor, _MAX_LOCK_BYTES + 1)
        if (
            not os.path.samestat(opened, current)
            or hashlib.sha256(payload).hexdigest() != observed.sha256
        ):
            raise _coded(ErrorCode.RECOVERY_REQUIRED, "lock identity changed")
        body = json.loads(payload)
        if (
            body.get("host") != socket.gethostname()
            or assess_owner_liveness(body.get("pid"), body.get("host"))
            != OwnerLiveness.DEAD
        ):
            raise _coded(ErrorCode.RECOVERY_REQUIRED, "lock owner is not proven dead")
        os.unlink(observed.path.name, dir_fd=directory)
        os.fsync(directory)
    except FCPMCPError:
        raise
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        raise _coded(ErrorCode.RECOVERY_REQUIRED, "orphan lock removal failed") from error
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if directory >= 0:
            try:
                os.close(directory)
            except OSError:
                pass


def _finding(
    disposition: FindingDisposition,
    summary: str,
    *evidence: str,
) -> VerificationFindingV1:
    return VerificationFindingV1(
        disposition=disposition,
        summary=summary,
        evidence=evidence,
    )


def verify_run(
    *,
    ledger: WorkflowLedger,
    artifacts: ArtifactStore,
    run: LedgerRunRecord,
    max_file_bytes: int,
) -> WorkflowVerificationResultV1:
    """Independently grade one run without repairing any evidence."""
    integrity = ledger.verify_integrity(run.run_id)
    integrity_disposition = (
        FindingDisposition.PASS if integrity.valid else FindingDisposition.FAIL
    )
    ledger_finding = _finding(
        integrity_disposition,
        "selected run event chain is valid"
        if integrity.valid
        else "selected run event chain is invalid",
        *(finding.code for finding in integrity.findings),
    )
    database_finding = _finding(
        integrity_disposition,
        "database schema and projections are consistent"
        if integrity.valid
        else "database integrity checks found inconsistencies",
        f"checked_runs={integrity.checked_runs}",
        f"checked_events={integrity.checked_events}",
    )

    artifact_errors: list[str] = []
    for record in ledger.list_artifacts(run.run_id, limit=100):
        try:
            artifacts.verify(
                _artifact_metadata(
                    record.run_id,
                    record.kind,
                    record.relative_path,
                    record.sha256,
                    record.byte_size,
                    record.created_at,
                )
            )
        except FCPMCPError:
            artifact_errors.append(f"{record.kind.value} artifact is corrupt")
    artifacts_finding = _finding(
        FindingDisposition.FAIL if artifact_errors else FindingDisposition.PASS,
        "recorded artifacts are contained and hash-valid"
        if not artifact_errors
        else "recorded artifact verification failed",
        *artifact_errors,
    )

    approval = ledger.get_approval(run.run_id)
    if run.approval_source is None:
        approval_finding = _finding(
            FindingDisposition.PASS,
            "approval is not yet applicable",
        )
    elif approval is None:
        approval_finding = _finding(
            FindingDisposition.FAIL,
            "authoritative approval record is missing",
        )
    else:
        try:
            expected_binding = approval_binding_for_run(
                run,
                plan_schema_version="1",
            )
        except FCPMCPError:
            expected_binding = ""
        valid_approval = (
            approval.source is run.approval_source
            and approval.binding_sha256 == expected_binding
        )
        approval_finding = _finding(
            FindingDisposition.PASS if valid_approval else FindingDisposition.FAIL,
            "approval binding is consistent"
            if valid_approval
            else "approval binding is inconsistent",
        )

    receipt_record = ledger.get_artifact(run.run_id, ArtifactKind.RECEIPT)
    if run.state is WorkflowState.COMMITTED and receipt_record is not None:
        try:
            receipt_payload = artifacts.read(
                _artifact_metadata(
                    receipt_record.run_id,
                    receipt_record.kind,
                    receipt_record.relative_path,
                    receipt_record.sha256,
                    receipt_record.byte_size,
                    receipt_record.created_at,
                )
            )
            receipt = WorkflowCommitReceiptV1.model_validate_json(receipt_payload)
            logical = hashlib.sha256(
                canonical_json(
                    {
                        **receipt.model_dump(mode="json"),
                        "receipt_sha256": "0" * 64,
                    }
                )
            ).hexdigest()
            valid_receipt = (
                receipt.receipt_sha256 == logical
                and receipt_record.sha256 == run.receipt_sha256
                and receipt_record.byte_size == run.receipt_size_bytes
                and receipt.run_id == run.run_id
                and receipt.candidate_sha256 == run.candidate_sha256
                and receipt.output_sha256 == run.destination_sha256
                and receipt.source_sha256 == run.source_sha256
                and receipt.prior_destination_sha256
                == run.prior_destination_sha256
                and receipt.destination_path == run.destination_path
                and receipt.backup_path == run.expected_backup_path
                and receipt.commit_attempt_id == run.commit_attempt_id
                and approval is not None
                and receipt.approval_source is approval.source
                and receipt.approval_binding_sha256 == approval.binding_sha256
            )
            if valid_receipt and receipt.backup_path is not None:
                backup = observe_file(Path(receipt.backup_path), max_file_bytes)
                valid_receipt = (
                    backup.state is ObservedFileState.PRESENT
                    and backup.sha256 == run.backup_sha256
                )
        except (FCPMCPError, ValueError):
            valid_receipt = False
        receipt_finding = _finding(
            FindingDisposition.PASS if valid_receipt else FindingDisposition.FAIL,
            "commit receipt is internally consistent"
            if valid_receipt
            else "commit receipt is missing or inconsistent",
        )
    elif run.state is WorkflowState.COMMITTED:
        receipt_finding = _finding(
            FindingDisposition.FAIL,
            "committed run is missing its receipt",
        )
    else:
        receipt_finding = _finding(
            FindingDisposition.PASS,
            "commit receipt is not yet applicable",
        )

    destination = observe_file(Path(run.destination_path), max_file_bytes)
    if run.state is WorkflowState.COMMITTED:
        destination_valid = (
            destination.state is ObservedFileState.PRESENT
            and destination.sha256 == run.destination_sha256 == run.candidate_sha256
        )
        destination_finding = _finding(
            FindingDisposition.PASS if destination_valid else FindingDisposition.FAIL,
            "current destination matches the committed candidate"
            if destination_valid
            else "current destination no longer matches the committed candidate",
        )
    else:
        destination_finding = _finding(
            FindingDisposition.PASS,
            "no committed destination claim exists",
        )

    findings = (
        ledger_finding,
        database_finding,
        artifacts_finding,
        approval_finding,
        receipt_finding,
        destination_finding,
    )
    severity = {
        FindingDisposition.PASS: 0,
        FindingDisposition.WARN: 1,
        FindingDisposition.FAIL: 2,
    }
    overall = max((finding.disposition for finding in findings), key=severity.get)
    return WorkflowVerificationResultV1(
        run_id=run.run_id,
        ledger=ledger_finding,
        database=database_finding,
        artifacts=artifacts_finding,
        approval=approval_finding,
        receipt=receipt_finding,
        destination=destination_finding,
        overall=overall,
    )


__all__ = [
    "assess_run",
    "observe_file",
    "read_regular_bytes",
    "remove_stale_lock",
    "verify_run",
]
