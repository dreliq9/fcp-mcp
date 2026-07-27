from __future__ import annotations

import hashlib
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from fcp_mcp.contracts import ErrorCode, FCPMCPError
from fcp_mcp.fcpxml.validator import FCPXMLValidator, ValidationResult
from fcp_mcp.observability import emit_event
from fcp_mcp.utils.atomic_write import atomic_replace_bytes


@dataclass(frozen=True)
class FCPXMLTransactionReceipt:
    transaction_id: str
    source: Path | None
    destination: Path
    backup_path: Path | None
    input_sha256: str | None
    prior_sha256: str | None
    output_sha256: str
    validation_warnings: tuple[str, ...]
    elapsed_ms: int
    disposition: Literal["committed"] = "committed"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate(
    path: Path,
    validator: FCPXMLValidator,
) -> ValidationResult:
    result = validator.validate_file(path)
    if not result.valid:
        raise FCPMCPError(
            ErrorCode.VALIDATION_FAILED,
            f"Generated FCPXML did not pass structural validation: {result.summary()}",
            {
                "issues": [
                    {
                        "severity": issue.severity,
                        "message": issue.message,
                        "location": issue.location,
                    }
                    for issue in result.issues
                ]
            },
        )
    return result


def commit_fcpxml(
    *,
    source: str | Path | None,
    destination: str | Path,
    xml_text: str,
    validator: FCPXMLValidator | None = None,
    validate_candidate: Callable[[Path], None] | None = None,
    event_format: str = "text",
    operation: str = "fcpxml_commit",
) -> FCPXMLTransactionReceipt:
    return commit_fcpxml_bytes(
        source=source,
        destination=destination,
        xml_bytes=xml_text.encode("utf-8"),
        validator=validator,
        validate_candidate=validate_candidate,
        event_format=event_format,
        operation=operation,
    )


def commit_fcpxml_bytes(
    *,
    source: str | Path | None,
    destination: str | Path,
    xml_bytes: bytes,
    validator: FCPXMLValidator | None = None,
    validate_candidate: Callable[[Path], None] | None = None,
    event_format: str = "text",
    operation: str = "fcpxml_commit",
) -> FCPXMLTransactionReceipt:
    started = time.perf_counter()
    transaction_id = str(uuid.uuid4())
    destination_path = Path(destination).expanduser().resolve()
    source_path = Path(source).expanduser().resolve() if source is not None else None

    if source_path is not None and source_path == destination_path:
        raise FCPMCPError(
            ErrorCode.SAME_FILE_FORBIDDEN,
            "Input and output must be different files",
        )

    input_sha256 = (
        _sha256(source_path)
        if source_path is not None and source_path.is_file()
        else None
    )
    active_validator = validator or FCPXMLValidator()
    latest_validation: ValidationResult | None = None

    def validate(path: Path) -> None:
        nonlocal latest_validation
        latest_validation = _validate(path, active_validator)
        if validate_candidate is not None:
            validate_candidate(path)

    try:
        receipt = atomic_replace_bytes(
            destination_path,
            xml_bytes,
            validate=validate,
            event_format=event_format,
            transaction_id=transaction_id,
        )
    except FCPMCPError as error:
        emit_event(
            {
                "event": "fcpxml_transaction",
                "operation": operation,
                "transaction_id": transaction_id,
                "input_path": str(source_path) if source_path else None,
                "output_path": str(destination_path),
                "input_sha256": input_sha256,
                "validation_outcome": "failed",
                "elapsed_ms": round((time.perf_counter() - started) * 1000),
                "disposition": "failed",
                "error_code": error.code.value,
            },
            format=event_format,
        )
        raise

    warnings = tuple(
        issue.message
        for issue in (latest_validation.issues if latest_validation else ())
        if issue.severity == "warning"
    )
    transaction_receipt = FCPXMLTransactionReceipt(
        transaction_id=receipt.transaction_id,
        source=source_path,
        destination=receipt.destination,
        backup_path=receipt.backup_path,
        input_sha256=input_sha256,
        prior_sha256=receipt.prior_sha256,
        output_sha256=receipt.output_sha256,
        validation_warnings=warnings,
        elapsed_ms=round((time.perf_counter() - started) * 1000),
    )
    emit_event(
        {
            "event": "fcpxml_transaction",
            "operation": operation,
            "transaction_id": transaction_receipt.transaction_id,
            "input_path": str(source_path) if source_path else None,
            "output_path": str(transaction_receipt.destination),
            "input_sha256": transaction_receipt.input_sha256,
            "prior_sha256": transaction_receipt.prior_sha256,
            "output_sha256": transaction_receipt.output_sha256,
            "backup_path": (
                str(transaction_receipt.backup_path)
                if transaction_receipt.backup_path
                else None
            ),
            "validation_outcome": "valid",
            "elapsed_ms": transaction_receipt.elapsed_ms,
            "disposition": transaction_receipt.disposition,
        },
        format=event_format,
    )
    return transaction_receipt
