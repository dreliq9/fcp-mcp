"""Offline preparation and conservative, single-dispatch FCP XML requests."""

from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Literal
from urllib.parse import unquote, urlsplit
from uuid import UUID, uuid4, uuid5

from lxml import etree
from pydantic import Field, ValidationError, field_validator

from fcp_mcp.automation import osascript as automation
from fcp_mcp.config import RuntimeConfig
from fcp_mcp.contracts import ErrorCode, FCPMCPError
from fcp_mcp.fcp_control.import_staging import ImportStaging, SealedImport, read_source
from fcp_mcp.fcpxml.time_utils import RationalTime
from fcp_mcp.fcpxml.validator import FCPXMLValidator
from fcp_mcp.profiles import ApprovalMode
from fcp_mcp.result_models.common import ArtifactReference
from fcp_mcp.result_models.import_bridge import (
    ImportBridgeResult,
    ImportCapabilities,
    ImportIssue,
    ImportObservation,
    PreparedImportResult,
)
from fcp_mcp.result_models.live import StrictFrozenModel
from fcp_mcp.security.paths import PathPolicy
from fcp_mcp.utils.safe_xml import parse_string
from fcp_mcp.workflow.artifacts import ArtifactKind, ArtifactMetadataV1, ArtifactStore, StatePaths
from fcp_mcp.workflow.locking import DestinationLock
from fcp_mcp.workflow.models import canonical_json

FCP_RESOURCES = Path(
    "/Applications/Final Cut Pro.app/Contents/Frameworks/Interchange.framework/Versions/A/Resources"
)
MOTION_TEMPLATES = Path(
    "/Applications/Final Cut Pro.app/Contents/PlugIns/MediaProviders/MotionEffect.fxp/"
    "Contents/Resources/PETemplates.localized"
)
_GUARD_NAMESPACE = UUID("c314fc80-7ab5-4eb0-a87a-ea55c65abeb0")

OPEN_DOCUMENT = automation.OsaProgram(
    "AppleScript",
    """
on run argv
    set xmlPath to item 1 of argv
    tell application id "com.apple.FinalCut"
        activate
        open POSIX file xmlPath
    end tell
    return "Open Document AppleEvent returned"
end run
""".strip(),
)

INSPECT_LIBRARIES = automation.OsaProgram(
    "JavaScript",
    """
function run(argv) {
    const app = Application("com.apple.FinalCut");
    if (!app.running()) { throw new Error("Final Cut Pro must already be running"); }
    return JSON.stringify(app.libraries().map(function(library) {
        return {
            name: library.name(), id: library.id(), file: library.file().toString(),
            events: library.events().map(function(event) {
                return {
                    name: event.name(), id: event.id(),
                    projects: event.projects().map(function(project) {
                        const duration = project.sequence().duration();
                        return {
                            name: project.name(), id: project.id(),
                            duration: duration.value + "/" + duration.timescale + "s"
                        };
                    })
                };
            })
        };
    }));
}
""".strip(),
)


class _Project(StrictFrozenModel):
    name: str
    id: str
    duration: str

    @field_validator("duration")
    @classmethod
    def valid_duration(cls, value: str) -> str:
        if RationalTime.from_fcpxml(value).numerator < 0:
            raise ValueError("Negative observed duration")
        return value


class _Event(StrictFrozenModel):
    name: str
    id: str
    projects: list[_Project] = Field(max_length=10000)


class _Library(StrictFrozenModel):
    name: str
    id: str
    file: str
    events: list[_Event] = Field(max_length=10000)


class _Plan(StrictFrozenModel):
    schema_version: Literal["2"] = "2"
    prepared_id: str
    source: ArtifactReference
    candidate: ArtifactMetadataV1
    library_path: str
    library_device: int
    library_inode: int
    event_name: str
    project_name: str
    duration: str
    expires_at: str
    warnings: list[str]
    staged: SealedImport


class _Dispatch(StrictFrozenModel):
    prepared_id: str
    status: Literal["dispatching", "accepted_unverified", "ambiguous", "metadata_observed"]
    request_accepted: bool | None


def _coded(code: ErrorCode, summary: str, hint: str) -> FCPMCPError:
    return FCPMCPError(code, summary, {"hint": hint})


def _file_path(value: str) -> Path:
    url = urlsplit(value)
    if (
        url.scheme != "file"
        or url.netloc not in {"", "localhost"}
        or url.query
        or url.fragment
        or not url.path.startswith("/")
    ):
        raise _coded(
            ErrorCode.UNSUPPORTED_CONTRACT,
            "Expected an absolute local file URL",
            "Use an absolute file URL for library and media locations.",
        )
    return Path(unquote(url.path)).resolve()


def _observed_library_path(value: str) -> Path:
    if value.startswith("/"):
        return Path(value).resolve()
    return _file_path(value)


def _normalize_factory_effect(effect: ET.Element) -> None:
    uid, src = effect.get("uid", ""), effect.get("src")
    if uid.startswith(".../"):
        relative = PurePosixPath(uid[4:])
        factory = MOTION_TEMPLATES.resolve()
        expected = factory.joinpath(relative).resolve()
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or not expected.is_relative_to(factory)
            or not expected.is_file()
            or expected.suffix not in {".moti", ".moef", ".motr", ".mopn"}
            or (src is not None and _file_path(src) != expected)
        ):
            raise _coded(
                ErrorCode.UNSUPPORTED_CONTRACT,
                "Effect is not an installed factory template",
                "Use an installed factory UID with its matching locator, or remove the external template.",
            )
        effect.attrib.pop("src", None)
    elif src is not None or "/" in uid or "\\" in uid or ":" in uid:
        raise _coded(
            ErrorCode.UNSUPPORTED_CONTRACT,
            "External effect locator is unsupported",
            "Use an installed factory template UID or a native Final Cut effect UID.",
        )


def _validate_candidate(path: Path) -> None:
    result = FCPXMLValidator().validate_file(path)
    if not result.valid:
        raise _coded(ErrorCode.VALIDATION_FAILED, result.summary(), "Correct the source XML.")
    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    tree = etree.parse(str(path), parser)
    version = tree.getroot().get("version", "")
    if re.fullmatch(r"\d+\.\d+", version) is None:
        raise _coded(
            ErrorCode.INVALID_ARGUMENTS, "Invalid FCPXML version", "Use an Apple FCPXML version."
        )
    dtd_path = FCP_RESOURCES / f"FCPXMLv{version.replace('.', '_')}.dtd"
    if not dtd_path.is_file():
        raise _coded(
            ErrorCode.DEPENDENCY_MISSING,
            "Matching installed Apple DTD is unavailable",
            "Prepare with a version supported by the installed Final Cut application.",
        )
    with dtd_path.open("rb") as handle:
        dtd = etree.DTD(handle)
    if not dtd.validate(tree):
        raise _coded(
            ErrorCode.VALIDATION_FAILED,
            "Candidate failed the installed Apple DTD",
            "Correct the XML structure before requesting import.",
        )


class ImportBridge:
    def __init__(self, config: RuntimeConfig):
        self.config = config
        self.policy = PathPolicy(config)
        self.paths = StatePaths.from_config(
            replace(config, state_dir=config.state_dir / "import-bridge")
        )
        self.store = ArtifactStore(self.paths, max_artifact_bytes=config.max_artifact_bytes)
        self.staging = ImportStaging(self.paths.root / "staged", config.max_artifact_bytes)

    def _load(self, prepared_id: str) -> tuple[_Plan, str]:
        metadata, payload = self.store.read_current(prepared_id, ArtifactKind.DIFF)
        try:
            plan = _Plan.model_validate_json(payload)
        except ValidationError as error:
            raise _coded(
                ErrorCode.ARTIFACT_CORRUPT, "Import plan is corrupt", "Prepare a new plan."
            ) from error
        if (
            plan.prepared_id != prepared_id
            or plan.candidate.run_id != prepared_id
            or plan.candidate.kind is not ArtifactKind.CANDIDATE
        ):
            raise _coded(
                ErrorCode.ARTIFACT_CORRUPT, "Import plan identity mismatch", "Prepare a new plan."
            )
        return plan, metadata.sha256

    def _record(self, run_id: str) -> _Dispatch | None:
        path = self.paths.artifact_path(run_id, ArtifactKind.RECEIPT)
        if not path.exists():
            return None
        _, payload = self.store.read_current(run_id, ArtifactKind.RECEIPT)
        try:
            return _Dispatch.model_validate_json(payload)
        except ValidationError as error:
            raise _coded(
                ErrorCode.ARTIFACT_CORRUPT,
                "Import dispatch record is corrupt",
                "Inspect retained evidence; do not repeat the import.",
            ) from error

    def _write_record(self, run_id: str, record: _Dispatch) -> None:
        self.store.write(run_id, ArtifactKind.RECEIPT, canonical_json(record))

    def _guard_id(self, plan: _Plan) -> str:
        return str(uuid5(_GUARD_NAMESPACE, plan.library_path))

    def _libraries(self) -> list[_Library]:
        raw = automation.run_osascript(INSPECT_LIBRARIES, config=self.config)
        try:
            values = json.loads(raw)
            if not isinstance(values, list) or len(values) > 1000:
                raise ValueError("Invalid library inventory")
            return [_Library.model_validate(value) for value in values]
        except (ValueError, TypeError) as error:
            raise _coded(
                ErrorCode.COMMAND_FAILED,
                "FCP library inspection is unavailable or invalid",
                "Check native scripting access; use manual observation without repeating import.",
            ) from error

    def _target(self, plan: _Plan) -> Path:
        path = self.policy.resolve_input(plan.library_path, kind="dir", suffixes={".fcpbundle"})
        identity = path.stat()
        if (identity.st_dev, identity.st_ino) != (plan.library_device, plan.library_inode):
            raise _coded(
                ErrorCode.WORKFLOW_STALE,
                "Destination library identity changed",
                "Prepare and review a new import plan.",
            )
        return path

    def _result(
        self,
        plan: _Plan,
        status: str,
        accepted: bool | None,
        *,
        observed: bool = False,
        issue: ImportIssue | None = None,
        observations: list[ImportObservation] | None = None,
    ) -> ImportBridgeResult:
        return ImportBridgeResult(
            prepared_id=plan.prepared_id,
            status=status,
            request_accepted=accepted,
            library_path=plan.library_path,
            event_name=plan.event_name,
            project_name=plan.project_name,
            capabilities=ImportCapabilities(
                inspection="observed" if observed else "declared_unverified"
            ),
            issue=issue,
            observations=observations or [],
            warnings=[
                "No native completion or idempotency guarantee. Export/render require Final Cut.",
                "Matching metadata does not verify imported timeline contents or rendered output.",
                "Preflight is not atomic with FCP processing; app state can change and import may prompt.",
            ],
        )

    def prepare(self, source_path: str, library_path: str) -> PreparedImportResult:
        source = self.policy.resolve_input(source_path, suffixes={".fcpxml"})
        library = self.policy.resolve_input(library_path, kind="dir", suffixes={".fcpbundle"})
        data = read_source(source, self.config.max_source_bytes)
        if len(data) > self.config.max_source_bytes:
            raise _coded(
                ErrorCode.INVALID_ARGUMENTS,
                "Source XML exceeds configured limit",
                "Use smaller XML.",
            )
        try:
            root = parse_string(data)
        except Exception as error:
            raise _coded(
                ErrorCode.VALIDATION_FAILED, "Cannot parse source XML", "Correct the source XML."
            ) from error
        events = root.findall(".//event")
        projects = root.findall(".//project")
        if (
            root.tag != "fcpxml"
            or len(events) != 1
            or len(projects) != 1
            or projects[0] not in list(events[0])
        ):
            raise _coded(
                ErrorCode.UNSUPPORTED_CONTRACT,
                "Import slice requires one event and one project",
                "Create a single explicitly named event/project XML document.",
            )
        event_name, project_name = events[0].get("name", ""), projects[0].get("name", "")
        sequence = projects[0].find("sequence")
        if not event_name.strip() or not project_name.strip() or sequence is None:
            raise _coded(
                ErrorCode.INVALID_ARGUMENTS,
                "Event/project names or sequence are missing",
                "Give the event and project distinct, explicit names.",
            )
        duration = sequence.get("duration", "")
        try:
            valid_duration = RationalTime.from_fcpxml(duration)
        except ValueError as error:
            raise _coded(
                ErrorCode.INVALID_ARGUMENTS, "Invalid sequence duration", "Correct sequence timing."
            ) from error
        if valid_duration.numerator < 0:
            raise _coded(
                ErrorCode.INVALID_ARGUMENTS, "Invalid sequence duration", "Correct sequence timing."
            )
        options = root.findall("import-options")
        if len(options) > 1:
            raise _coded(
                ErrorCode.INVALID_ARGUMENTS,
                "Duplicate import-options",
                "Use one import-options element.",
            )
        locations = [
            o.get("value", "")
            for o in root.findall("import-options/option")
            if o.get("key") == "library location"
        ]
        locations += [el.get("location") for el in root.findall("library") if el.get("location")]
        if any(_file_path(value) != library for value in locations):
            raise _coded(
                ErrorCode.INVALID_ARGUMENTS,
                "Embedded destination differs from requested library",
                "Resolve the destination mismatch explicitly before preparing XML.",
            )
        # locators/bookmarks can carry alternate filesystem capabilities. This
        # slice supports explicit local media URLs and installed factory effects.
        if root.findall(".//locator"):
            raise _coded(
                ErrorCode.UNSUPPORTED_CONTRACT,
                "Locator resources are unsupported",
                "Use explicit local media URLs and installed factory effects.",
            )
        for media in root.findall("resources/asset/media-rep"):
            for bookmark in media.findall("bookmark"):
                media.remove(bookmark)
        if root.findall(".//bookmark"):
            raise _coded(
                ErrorCode.UNSUPPORTED_CONTRACT,
                "Alternate bookmarks are unsupported",
                "Use explicit local media URLs.",
            )
        for effect in root.findall("resources/effect"):
            _normalize_factory_effect(effect)
        for embedded_library in root.findall("library"):
            if embedded_library.get("location"):
                embedded_library.set("location", library.as_uri())
        media_refs = []
        for element in root.iter():
            if "src" not in element.attrib:
                continue
            if element.tag not in {"media-rep", "asset"}:
                raise _coded(
                    ErrorCode.UNSUPPORTED_CONTRACT,
                    "Unsupported file reference",
                    "Use asset media-rep references or installed factory effects.",
                )
            media_refs.append(
                (element, self.policy.resolve_input(str(_file_path(element.get("src", "")))))
            )
        for old in options:
            root.remove(old)
        options_el = ET.Element("import-options")
        for key, value in [
            ("library location", library.as_uri()),
            ("copy assets", "1"),
            ("suppress warnings", "0"),
        ]:
            ET.SubElement(options_el, "option", {"key": key, "value": value})
        root.insert(0, options_el)
        prepared_id = str(uuid4())
        with self.staging.build(prepared_id) as stage:
            copied = {}
            for element, media in media_refs:
                if media not in copied:
                    copied[media] = stage.copy_media(media)
                element.set("src", copied[media].as_uri())
            ET.indent(root, space="    ")
            payload = b'<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n' + ET.tostring(
                root, encoding="utf-8"
            )
            candidate = self.store.write(prepared_id, ArtifactKind.CANDIDATE, payload)
            _validate_candidate(self.paths.artifact_path(prepared_id, ArtifactKind.CANDIDATE))
            staged = stage.seal(payload)
        identity = library.stat()
        plan = _Plan(
            prepared_id=prepared_id,
            source=ArtifactReference(
                path=str(source),
                media_type="application/xml",
                sha256=hashlib.sha256(data).hexdigest(),
                size_bytes=len(data),
            ),
            candidate=candidate,
            library_path=str(library),
            library_device=identity.st_dev,
            library_inode=identity.st_ino,
            event_name=event_name,
            project_name=project_name,
            duration=duration,
            expires_at=(
                datetime.now(timezone.utc) + timedelta(seconds=self.config.approval_ttl_seconds)
            ).isoformat(),
            warnings=[
                "Target must be open and event name unused before dispatch.",
                "Media is snapshotted within the configured artifact byte budget; FCP is asked to copy assets.",
                "Snapshots are retained for asynchronous app reads, including after metadata observation.",
                "Read-only staging protects ordinary tool writes, not malicious same-user or privileged processes.",
                "External Motion templates and locator resources are unsupported; factory effect UIDs remain supported.",
                "Apple may create a library if the target disappears before it processes the request.",
                "Warnings remain visible. Preparation requires an existing library.",
            ],
            staged=staged,
        )
        manifest = self.store.write(prepared_id, ArtifactKind.DIFF, canonical_json(plan))
        return PreparedImportResult(
            prepared_id=prepared_id,
            plan_sha256=manifest.sha256,
            candidate=ArtifactReference(
                path=str(self.paths.artifact_path(prepared_id, ArtifactKind.CANDIDATE)),
                media_type="application/xml",
                sha256=candidate.sha256,
                size_bytes=candidate.byte_size,
            ),
            library_path=str(library),
            event_name=event_name,
            project_name=project_name,
            duration=duration,
            expires_at=plan.expires_at,
            warnings=plan.warnings,
        )

    def _verify_snapshots(self, plan: _Plan) -> Path:
        self.store.read(plan.candidate)
        xml = [item for item in plan.staged.files if item.name == "candidate.fcpxml"]
        if len(xml) != 1 or (xml[0].sha256, xml[0].size_bytes) != (
            plan.candidate.sha256,
            plan.candidate.byte_size,
        ):
            raise _coded(
                ErrorCode.ARTIFACT_CORRUPT,
                "Review and dispatch snapshots disagree",
                "Prepare and review a new import.",
            )
        return self.staging.verify(plan.prepared_id, plan.staged)

    def request(self, prepared_id: str, expected_plan_sha256: str) -> ImportBridgeResult:
        automation.require_live_control(self.config)
        plan, actual_sha = self._load(prepared_id)
        if (
            self.config.workflow_approval is not ApprovalMode.CLIENT
            or expected_plan_sha256 != actual_sha
        ):
            raise _coded(
                ErrorCode.APPROVAL_REQUIRED,
                "Exact client approval of import plan is required",
                "Review the prepared artifact and target, then provide its plan hash in client mode.",
            )
        try:
            expires_at = datetime.fromisoformat(plan.expires_at)
            if expires_at.tzinfo is None:
                raise ValueError("Missing timezone")
        except ValueError as error:
            raise _coded(
                ErrorCode.ARTIFACT_CORRUPT, "Invalid approval expiry", "Prepare a new plan."
            ) from error
        if datetime.now(timezone.utc) >= expires_at:
            raise _coded(
                ErrorCode.APPROVAL_EXPIRED,
                "Import approval expired",
                "Prepare and review a new plan.",
            )
        target = self._target(plan)
        self._verify_snapshots(plan)
        source = self.policy.resolve_input(plan.source.path, suffixes={".fcpxml"})
        current_source = read_source(source, self.config.max_source_bytes)
        if hashlib.sha256(current_source).hexdigest() != plan.source.sha256:
            raise _coded(
                ErrorCode.WORKFLOW_STALE, "Source XML changed", "Prepare and review the new source."
            )
        guard_id = self._guard_id(plan)
        with DestinationLock(self.paths, target, prepared_id):
            prior = self._record(guard_id)
            attempt = self._record(prepared_id)
            if attempt is not None or (prior is not None and prior.status != "metadata_observed"):
                return self._result(
                    plan,
                    "ambiguous",
                    attempt.request_accepted if attempt else None,
                    issue=ImportIssue(
                        code="dispatch_already_recorded",
                        summary="This library has a recorded or pending dispatch",
                        hint="Reconcile the recorded prepared_id; do not repeat the import.",
                        related_prepared_id=attempt.prepared_id if attempt else prior.prepared_id,
                    ),
                )
            try:
                libraries = self._libraries()
                matches = [lib for lib in libraries if _observed_library_path(lib.file) == target]
            except FCPMCPError as error:
                return self._result(
                    plan,
                    "blocked",
                    False,
                    issue=ImportIssue(
                        code=error.code.value,
                        summary="Native inspection did not establish a safe destination",
                        hint="Verify scripting access and the destination library before requesting import.",
                    ),
                )
            if len(matches) != 1:
                return self._result(
                    plan,
                    "blocked",
                    False,
                    observed=True,
                    issue=ImportIssue(
                        code="destination_unavailable",
                        summary="The exact target library is not uniquely open",
                        hint="Open and verify the intended library, then retry this unsent request.",
                    ),
                )
            if any(event.name == plan.event_name for event in matches[0].events):
                return self._result(
                    plan,
                    "blocked",
                    False,
                    observed=True,
                    issue=ImportIssue(
                        code="naming_conflict",
                        summary="The target event name already exists",
                        hint="Prepare a new uniquely named event; existing content will not be replaced.",
                    ),
                )
            # Revalidate after native preflight, while holding the destination
            # lock. FCP receives the retained sealed copy, never the review file.
            self._target(plan)
            dispatch_path = self._verify_snapshots(plan)
            if datetime.now(timezone.utc) >= expires_at:
                raise _coded(
                    ErrorCode.APPROVAL_EXPIRED,
                    "Import approval expired during preflight",
                    "Prepare and review a new plan.",
                )
            if (
                hashlib.sha256(read_source(source, self.config.max_source_bytes)).hexdigest()
                != plan.source.sha256
            ):
                raise _coded(
                    ErrorCode.WORKFLOW_STALE,
                    "Source XML changed during preflight",
                    "Prepare and review a new plan.",
                )
            intent = _Dispatch(prepared_id=prepared_id, status="dispatching", request_accepted=None)
            self._write_record(guard_id, intent)
            self._write_record(prepared_id, intent)
            try:
                self._target(plan)
                response = automation.run_osascript(
                    OPEN_DOCUMENT,
                    [str(dispatch_path)],
                    timeout=30,
                    config=self.config,
                )
                if response != "Open Document AppleEvent returned":
                    raise _coded(
                        ErrorCode.COMMAND_FAILED,
                        "Unexpected AppleEvent response",
                        "Reconcile without sending another import.",
                    )
            except FCPMCPError as error:
                record = _Dispatch(
                    prepared_id=prepared_id, status="ambiguous", request_accepted=None
                )
                try:
                    self._write_record(prepared_id, record)
                except FCPMCPError:
                    pass  # Durable pre-dispatch intent still blocks another send.
                return self._result(
                    plan,
                    "ambiguous",
                    None,
                    observed=True,
                    issue=ImportIssue(
                        code=error.code.value,
                        summary="Open Document outcome is uncertain",
                        hint="Reconcile or inspect Final Cut; never repeat this dispatch automatically.",
                    ),
                )
            record = _Dispatch(
                prepared_id=prepared_id, status="accepted_unverified", request_accepted=True
            )
            try:
                self._write_record(prepared_id, record)
            except FCPMCPError as error:
                return self._result(
                    plan,
                    "ambiguous",
                    True,
                    observed=True,
                    issue=ImportIssue(
                        code=error.code.value,
                        summary="AppleEvent returned, but acknowledgement could not be persisted",
                        hint="Retained dispatch intent blocks resending. Reconcile or inspect Final Cut.",
                    ),
                )
            return self._result(plan, "accepted_unverified", True, observed=True)

    def reconcile(self, prepared_id: str) -> ImportBridgeResult:
        automation.require_live_control(self.config)
        plan, _ = self._load(prepared_id)
        target = self._target(plan)
        with DestinationLock(self.paths, target, prepared_id):
            attempt = self._record(prepared_id)
            if attempt is None:
                return self._result(
                    plan,
                    "blocked",
                    False,
                    issue=ImportIssue(
                        code="not_dispatched",
                        summary="No dispatch is recorded",
                        hint="Review and request import first.",
                    ),
                )
            try:
                libraries = self._libraries()
                found = [
                    ImportObservation(
                        library_path=str(_observed_library_path(lib.file)),
                        library_id=lib.id,
                        event_id=event.id,
                        project_id=project.id,
                        duration=project.duration,
                    )
                    for lib in libraries
                    for event in lib.events
                    if event.name == plan.event_name
                    for project in event.projects
                    if project.name == plan.project_name
                ]
            except FCPMCPError as error:
                return self._result(
                    plan,
                    "ambiguous",
                    attempt.request_accepted,
                    issue=ImportIssue(
                        code=error.code.value,
                        summary="Native reconciliation is unavailable",
                        hint="Inspect the retained request and Final Cut manually; do not resend.",
                    ),
                )
            if not found:
                return self._result(
                    plan,
                    "not_observed",
                    attempt.request_accepted,
                    observed=True,
                    issue=ImportIssue(
                        code="not_observed",
                        summary="Expected project is not visible yet",
                        hint="Wait or resolve app prompts, then reconcile again without importing.",
                    ),
                )
            if len(found) != 1 or found[0].library_path != str(target):
                return self._result(
                    plan,
                    "ambiguous",
                    attempt.request_accepted,
                    observed=True,
                    observations=found,
                    issue=ImportIssue(
                        code="destination_mismatch",
                        summary="Project location or identity is ambiguous",
                        hint="Inspect both libraries; do not import again or replace content.",
                    ),
                )
            if RationalTime.from_fcpxml(found[0].duration) != RationalTime.from_fcpxml(
                plan.duration
            ):
                return self._result(
                    plan,
                    "ambiguous",
                    attempt.request_accepted,
                    observed=True,
                    observations=found,
                    issue=ImportIssue(
                        code="duration_mismatch",
                        summary="Observed sequence duration differs",
                        hint="Inspect the project and export XML for a fuller comparison.",
                    ),
                )
            guard = self._record(self._guard_id(plan))
            if guard is not None and guard.prepared_id == prepared_id:
                self._write_record(
                    self._guard_id(plan),
                    _Dispatch(
                        prepared_id=prepared_id,
                        status="metadata_observed",
                        request_accepted=attempt.request_accepted,
                    ),
                )
            return self._result(
                plan,
                "metadata_observed",
                attempt.request_accepted,
                observed=True,
                observations=found,
            )
