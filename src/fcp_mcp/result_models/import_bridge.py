"""Evidence and limits for the documented Final Cut XML import bridge."""

from typing import Literal

from pydantic import Field

from .common import ArtifactReference
from .live import StrictFrozenModel


class ImportCapabilities(StrictFrozenModel):
    transport: Literal["open_document_appleevent"] = "open_document_appleevent"
    library_targeting: Literal["fcpxml_import_options"] = "fcpxml_import_options"
    inspection: Literal["declared_unverified", "observed"] = "declared_unverified"
    native_idempotency: Literal[False] = False
    native_completion_signal: Literal[False] = False
    native_atomic_preconditions: Literal[False] = False
    headless_export: Literal[False] = False
    headless_render: Literal[False] = False


class ImportIssue(StrictFrozenModel):
    code: str
    summary: str
    hint: str
    related_prepared_id: str | None = None


class PreparedImportResult(StrictFrozenModel):
    schema_version: Literal["1"] = "1"
    status: Literal["prepared"] = "prepared"
    prepared_id: str
    plan_sha256: str
    candidate: ArtifactReference
    library_path: str
    event_name: str
    project_name: str
    duration: str
    expires_at: str
    approval: Literal["client_hash_acknowledgement_not_verified_human"] = (
        "client_hash_acknowledgement_not_verified_human"
    )
    capabilities: ImportCapabilities = Field(default_factory=ImportCapabilities)
    warnings: list[str]


class ImportObservation(StrictFrozenModel):
    library_path: str
    library_id: str
    event_id: str
    project_id: str
    duration: str


class ImportBridgeResult(StrictFrozenModel):
    schema_version: Literal["1"] = "1"
    prepared_id: str
    status: Literal[
        "blocked", "accepted_unverified", "ambiguous", "not_observed", "metadata_observed"
    ]
    request_accepted: bool | None
    import_completed: None = None
    library_path: str
    event_name: str
    project_name: str
    capabilities: ImportCapabilities
    observations: list[ImportObservation] = Field(default_factory=list)
    issue: ImportIssue | None = None
    warnings: list[str]
