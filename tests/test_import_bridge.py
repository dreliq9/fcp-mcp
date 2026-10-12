import json
import xml.etree.ElementTree as ET
from dataclasses import replace
from pathlib import Path

import pytest

from fcp_mcp.config import RuntimeConfig
from fcp_mcp.contracts import ErrorCode, FCPMCPError
from fcp_mcp.fcp_control import import_bridge as bridge
from fcp_mcp.profiles import ApprovalMode
from fcp_mcp.workflow.artifacts import ArtifactKind


@pytest.fixture
def setup(tmp_path, monkeypatch):
    config = RuntimeConfig.from_env(
        {
            "FCP_MCP_OUTPUT_DIR": str(tmp_path),
            "FCP_MCP_ALLOWED_ROOTS": str(tmp_path),
            "FCP_MCP_STATE_DIR": str(tmp_path / "state"),
            "FCP_MCP_ENABLE_LIVE_CONTROL": "1",
            "FCP_MCP_PROFILE": "full",
        },
        home=tmp_path,
    )
    library = tmp_path / 'Destination "quoted".fcpbundle'
    library.mkdir()
    media = tmp_path / "clip.mov"
    media.write_bytes(b"media path fixture; not decoded")
    source = tmp_path / "source.fcpxml"
    source.write_text(f'''<fcpxml version="1.11"><resources>
      <format id="r1" name="FFVideoFormat720p30" frameDuration="1/30s" width="1280" height="720"/>
      <asset id="r2" name="Clip" start="0s" duration="4s" hasVideo="1" format="r1">
        <media-rep kind="original-media" src="{media.as_uri()}"/>
      </asset></resources><event name="Unique Event"><project name="Unique Project">
      <sequence format="r1" duration="4s" tcStart="0s"><spine>
        <asset-clip ref="r2" offset="0s" start="0s" duration="4s"/>
      </spine></sequence></project></event></fcpxml>''')
    calls = []
    inventory = [{"name": library.name, "id": "lib-1", "file": library.as_uri(), "events": []}]

    def transport(program, args=(), **kwargs):
        calls.append((program, args))
        if program is bridge.INSPECT_LIBRARIES:
            return json.dumps(inventory)
        assert program is bridge.OPEN_DOCUMENT
        return "Open Document AppleEvent returned"

    monkeypatch.setattr(bridge.automation, "run_osascript", transport)
    return config, source, library, calls, inventory


def prepare(setup):
    config, source, library, _, _ = setup
    runtime = bridge.ImportBridge(config)
    return runtime, runtime.prepare(str(source), str(library))


def add_observed_project(inventory, duration="120/30s"):
    inventory[0]["events"] = [
        {
            "name": "Unique Event",
            "id": "event-1",
            "projects": [{"name": "Unique Project", "id": "project-1", "duration": duration}],
        }
    ]


def test_prepare_is_offline_and_targets_exact_library_with_real_dtd(setup):
    original = setup[1].read_bytes()
    _runtime, prepared = prepare(setup)
    assert setup[3] == []
    assert setup[1].read_bytes() == original
    root = ET.parse(prepared.candidate.path).getroot()
    assert root[0].tag == "import-options"
    assert {e.get("key"): e.get("value") for e in root[0]} == {
        "library location": setup[2].as_uri(),
        "copy assets": "1",
        "suppress warnings": "0",
    }
    assert prepared.capabilities.native_completion_signal is False
    assert prepared.capabilities.inspection == "declared_unverified"
    bridge._validate_candidate(Path(prepared.candidate.path))


@pytest.mark.parametrize(
    "mode", ["destination", "media", "missing_library", "multi_project", "bad_dtd"]
)
def test_prepare_rejects_unsafe_or_unsupported_input_before_app(setup, mode, tmp_path):
    config, source, library, calls, _ = setup
    data = source.read_text()
    if mode == "destination":
        data = data.replace(
            "<resources>",
            '<import-options><option key="library location" value="file:///elsewhere.fcpbundle"/></import-options><resources>',
        )
    elif mode == "media":
        data = data.replace((tmp_path / "clip.mov").as_uri(), "file:///outside/clip.mov")
    elif mode == "missing_library":
        library = tmp_path / "Absent.fcpbundle"
    elif mode == "multi_project":
        data = data.replace("</event>", '<project name="Another"/></event>')
    else:
        data = data.replace("<spine>", '<spine unexpected="invalid">')
    source.write_text(data)
    with pytest.raises(FCPMCPError):
        bridge.ImportBridge(config).prepare(str(source), str(library))
    assert calls == []
    assert not (tmp_path / "Absent.fcpbundle").exists()


@pytest.mark.parametrize(
    "mode", ["hash", "live", "cli", "expired", "source", "candidate", "library"]
)
def test_request_guards_before_any_app_call(setup, mode):
    runtime, prepared = prepare(setup)
    expected = prepared.plan_sha256
    if mode == "hash":
        expected = "0" * 64
    elif mode == "live":
        runtime = bridge.ImportBridge(replace(setup[0], live_control_enabled=False))
    elif mode == "cli":
        runtime = bridge.ImportBridge(replace(setup[0], workflow_approval=ApprovalMode.CLI))
    elif mode == "expired":
        plan, _ = runtime._load(prepared.prepared_id)
        plan = plan.model_copy(update={"expires_at": "2000-01-01T00:00:00+00:00"})
        metadata = runtime.store.write(
            prepared.prepared_id, ArtifactKind.DIFF, bridge.canonical_json(plan)
        )
        expected = metadata.sha256
    elif mode == "source":
        setup[1].write_text("changed")
    elif mode == "candidate":
        Path(prepared.candidate.path).write_text("changed")
    else:
        setup[2].rename(setup[2].with_name("Old.fcpbundle"))
        setup[2].mkdir()
    with pytest.raises(FCPMCPError):
        runtime.request(prepared.prepared_id, expected)
    assert setup[3] == []


@pytest.mark.parametrize("mode", ["closed", "duplicate", "conflict", "query_failure"])
def test_preflight_blocks_unsent_requests(setup, mode, monkeypatch):
    runtime, prepared = prepare(setup)
    inventory = setup[4]
    if mode == "closed":
        inventory.clear()
    elif mode == "duplicate":
        inventory.append(inventory[0].copy())
    elif mode == "conflict":
        add_observed_project(inventory)
    else:

        def failed(*args, **kwargs):
            raise FCPMCPError(ErrorCode.PERMISSION_DENIED, "query denied")

        monkeypatch.setattr(bridge.automation, "run_osascript", failed)
    result = runtime.request(prepared.prepared_id, prepared.plan_sha256)
    assert result.status == "blocked" and result.request_accepted is False
    assert all(program is bridge.INSPECT_LIBRARIES for program, _ in setup[3])


def test_acceptance_is_not_completion_and_retry_never_dispatches_twice(setup):
    runtime, prepared = prepare(setup)
    result = runtime.request(prepared.prepared_id, prepared.plan_sha256)
    assert result.status == "accepted_unverified" and result.request_accepted is True
    assert result.import_completed is None
    args = setup[3][-1][1]
    plan, _ = runtime._load(prepared.prepared_id)
    assert args == [str(runtime.staging.path(prepared.prepared_id) / "candidate.fcpxml")]
    assert Path(args[0]).read_bytes() == Path(prepared.candidate.path).read_bytes()
    assert plan.staged.files
    assert prepared.candidate.path not in bridge.OPEN_DOCUMENT.source
    assert "open POSIX file xmlPath" in bridge.OPEN_DOCUMENT.source
    again = bridge.ImportBridge(setup[0]).request(prepared.prepared_id, prepared.plan_sha256)
    assert again.status == "ambiguous"
    assert len([c for c in setup[3] if c[0] is bridge.OPEN_DOCUMENT]) == 1

    other = runtime.prepare(str(setup[1]), str(setup[2]))
    assert runtime.request(other.prepared_id, other.plan_sha256).status == "ambiguous"
    assert len([c for c in setup[3] if c[0] is bridge.OPEN_DOCUMENT]) == 1


def test_review_candidate_mutation_during_preflight_blocks_dispatch(setup, monkeypatch):
    runtime, prepared = prepare(setup)
    original = bridge.automation.run_osascript

    def mutate(program, args=(), **kwargs):
        if program is bridge.INSPECT_LIBRARIES:
            candidate = Path(prepared.candidate.path)
            candidate.write_bytes(
                candidate.read_bytes().replace(b"Unique Project", b"Unapproved Project")
            )
        return original(program, args, **kwargs)

    monkeypatch.setattr(bridge.automation, "run_osascript", mutate)
    with pytest.raises(FCPMCPError, match="artifact_corrupt"):
        runtime.request(prepared.prepared_id, prepared.plan_sha256)
    assert len(setup[3]) == 1
    assert runtime._record(prepared.prepared_id) is None


def test_media_symlink_retarget_cannot_escape_snapshot(setup, monkeypatch, tmp_path):
    _config, source, _library, calls, _ = setup
    media = tmp_path / "clip.mov"
    link = tmp_path / "link.mov"
    link.symlink_to(media)
    source.write_text(source.read_text().replace(media.as_uri(), link.as_uri()))
    runtime, prepared = prepare(setup)
    outside = tmp_path.parent / (tmp_path.name + "-outside.mov")
    outside.write_bytes(b"unapproved outside bytes")
    try:
        link.unlink()
        link.symlink_to(outside)
        media.write_bytes(b"changed in-scope bytes")
        assert (
            runtime.request(prepared.prepared_id, prepared.plan_sha256).status
            == "accepted_unverified"
        )
        xml_path = Path(calls[-1][1][0])
        locator = ET.parse(xml_path).find("resources/asset/media-rep").get("src")
        staged = bridge._file_path(locator)
        assert staged.is_relative_to(runtime.staging.path(prepared.prepared_id))
        assert staged.read_bytes() == b"media path fixture; not decoded"
        assert link.as_uri().encode() not in xml_path.read_bytes()
        add_observed_project(setup[4])
        assert runtime.reconcile(prepared.prepared_id).status == "metadata_observed"
        assert xml_path.is_file() and staged.is_file()
    finally:
        outside.unlink()


@pytest.mark.parametrize(
    "mode", ["xml", "media", "permissions", "directory", "symlink", "replacement"]
)
def test_corrupt_staging_blocks_before_native_call(setup, mode):
    import os

    runtime, prepared = prepare(setup)
    folder = runtime.staging.path(prepared.prepared_id)
    plan, _ = runtime._load(prepared.prepared_id)
    xml = folder / "candidate.fcpxml"
    media = folder / plan.staged.files[0].name
    if mode in {"xml", "media"}:
        path = xml if mode == "xml" else media
        path.chmod(0o600)
        path.write_bytes(b"unapproved")
        path.chmod(0o400)
    elif mode == "permissions":
        media.chmod(0o600)
    elif mode == "directory":
        folder.chmod(0o700)
    else:
        folder.chmod(0o700)
        saved = xml.read_bytes()
        xml.unlink()
        if mode == "symlink":
            xml.symlink_to(prepared.candidate.path)
        else:
            xml.write_bytes(saved)
            xml.chmod(0o400)
        folder.chmod(0o500)
    with pytest.raises(FCPMCPError):
        runtime.request(prepared.prepared_id, prepared.plan_sha256)
    assert setup[3] == []


def test_public_path_policy_cannot_modify_retained_staging(setup):
    runtime, prepared = prepare(setup)
    folder = runtime.staging.path(prepared.prepared_id)
    for method in (
        runtime.policy.resolve_input,
        runtime.policy.resolve_reference,
        runtime.policy.resolve_output,
    ):
        with pytest.raises(FCPMCPError) as error:
            method(str(folder / "candidate.fcpxml"))
        assert error.value.code is ErrorCode.PATH_OUTSIDE_SCOPE
    assert (folder.stat().st_mode & 0o777) == 0o500
    assert all((path.stat().st_mode & 0o777) == 0o400 for path in folder.iterdir())


@pytest.mark.parametrize(
    "mode", ["external", "mismatched", "remote", "traversal", "factory", "builtin"]
)
def test_motion_effect_locator_policy(setup, mode):
    uid = ".../Titles.localized/Bumper:Opener.localized/Basic Title.localized/Basic Title.moti"
    factory = bridge.MOTION_TEMPLATES / uid[4:]
    src = factory.as_uri()
    if mode == "external":
        uid, src = "Custom", "file:///outside/policy/Test.moti"
    elif mode == "mismatched":
        src = "file:///outside/policy/Test.moti"
    elif mode == "remote":
        src = "https://example.invalid/Test.moti"
    elif mode == "traversal":
        uid = ".../../../Test.moti"
    effect = f'<effect id="r3" name="Basic Title" uid="{uid}"'
    if mode != "builtin":
        effect += f' src="{src}"'
    effect += "/>"
    setup[1].write_text(setup[1].read_text().replace("</resources>", effect + "</resources>"))
    if mode in {"factory", "builtin"}:
        _, prepared = prepare(setup)
        effect_node = ET.parse(prepared.candidate.path).find("resources/effect")
        assert effect_node.get("uid") == uid and effect_node.get("src") is None
    else:
        with pytest.raises(FCPMCPError) as error:
            prepare(setup)
        assert error.value.code is ErrorCode.UNSUPPORTED_CONTRACT
    assert setup[3] == []


def test_bookmark_removed_and_locator_rejected(setup):
    source = setup[1]
    data = source.read_text()
    source.write_text(
        data.replace('kind="original-media" src=', 'kind="original-media" src=').replace(
            "/>\n      </asset>",
            "><bookmark>unreviewed locator bytes</bookmark></media-rep>\n      </asset>",
        )
    )
    _, prepared = prepare(setup)
    assert not ET.parse(prepared.candidate.path).findall(".//bookmark")
    source.write_text(
        data.replace("</resources>", '<locator id="r3" url="file:///outside/foo"/></resources>')
    )
    with pytest.raises(FCPMCPError, match="Locator"):
        prepare(setup)


def test_staging_budget_failure_leaves_no_dispatchable_plan(setup):
    config = replace(setup[0], max_artifact_bytes=10)
    runtime = bridge.ImportBridge(config)
    with pytest.raises(FCPMCPError, match="budget|limit|ARTIFACT_BYTES"):
        runtime.prepare(str(setup[1]), str(setup[2]))
    assert list(runtime.staging.root.iterdir()) == []
    assert setup[3] == []


@pytest.mark.parametrize("mode", ["source", "library", "staged_media"])
def test_preflight_state_change_never_sends_unreviewed_import(setup, monkeypatch, mode):
    runtime, prepared = prepare(setup)
    original = bridge.automation.run_osascript

    def changed(program, args=(), **kwargs):
        if program is bridge.INSPECT_LIBRARIES:
            if mode == "source":
                setup[1].write_text("changed")
            elif mode == "library":
                setup[2].rename(setup[2].with_name("Old.fcpbundle"))
                setup[2].mkdir()
            else:
                plan, _ = runtime._load(prepared.prepared_id)
                media = runtime.staging.path(prepared.prepared_id) / plan.staged.files[0].name
                media.chmod(0o600)
                media.write_bytes(b"unapproved")
                media.chmod(0o400)
        return original(program, args, **kwargs)

    monkeypatch.setattr(bridge.automation, "run_osascript", changed)
    with pytest.raises(FCPMCPError):
        runtime.request(prepared.prepared_id, prepared.plan_sha256)
    assert len(setup[3]) == 1
    assert runtime._record(prepared.prepared_id) is None


@pytest.mark.parametrize("mode", ["io", "ancestor", "inflight_source"])
def test_prepare_snapshot_failure_is_coded_and_cleans_unpublished_stage(setup, monkeypatch, mode):
    import os

    from fcp_mcp.fcp_control import import_staging as staging

    runtime = bridge.ImportBridge(setup[0])
    if mode == "io":

        def fail(*args, **kwargs):
            raise OSError("simulated disk failure")

        monkeypatch.setattr(staging.os, "write", fail)
    elif mode == "ancestor":
        original = staging.source_file

        def swapped(path):
            if path.suffix == ".mov":
                old = path.parent / "original-media"
                old.mkdir()
                path.rename(old / path.name)
                path.symlink_to(old / path.name)
            return original(path)

        monkeypatch.setattr(staging, "source_file", swapped)
    else:
        original = staging.os.read
        edited = False

        def mutate(fd, count):
            nonlocal edited
            data = original(fd, count)
            if data.startswith(b"media path fixture") and not edited:
                edited = True
                (setup[1].parent / "clip.mov").write_bytes(b"changed during copy")
            return data

        monkeypatch.setattr(staging.os, "read", mutate)
    with pytest.raises(FCPMCPError):
        runtime.prepare(str(setup[1]), str(setup[2]))
    assert setup[3] == []
    assert list(runtime.staging.root.iterdir()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name", ["fcpxml_prepare_import", "fcp_request_import", "fcp_reconcile_import"]
)
async def test_bridge_mcp_rejects_unknown_and_wrong_type_arguments(setup, monkeypatch, name):
    from mcp.server.mcpserver.exceptions import ToolError

    from fcp_mcp import server
    from fcp_mcp.mcp_boundary import build_mcp_server

    monkeypatch.setattr(server, "CONFIG", setup[0])
    mcp = build_mcp_server(setup[0], server.TOOLS, server.PROMPTS, server.RESOURCES)
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    assert tools[name].input_schema["additionalProperties"] is False
    if name == "fcpxml_prepare_import":
        args = {"path": str(setup[1]), "library_path": str(setup[2])}
    else:
        args = {"prepared_id": "12345678-1234-1234-1234-123456789abc"}
        if name == "fcp_request_import":
            args["expected_plan_sha256"] = "0" * 64
    with pytest.raises(ToolError, match="invalid_arguments"):
        await mcp.call_tool(name, {**args, "unrecognized": True})
    with pytest.raises(ToolError, match="invalid_arguments"):
        await mcp.call_tool(name, {**args, next(iter(args)): 123})
    assert setup[3] == []


def test_ambiguous_send_is_recorded_before_transport_and_survives_restart(setup, monkeypatch):
    runtime, prepared = prepare(setup)
    original = bridge.automation.run_osascript

    def timeout(program, args=(), **kwargs):
        if program is bridge.INSPECT_LIBRARIES:
            return original(program, args, **kwargs)
        assert runtime._record(prepared.prepared_id).status == "dispatching"
        plan, _ = runtime._load(prepared.prepared_id)
        assert runtime._record(runtime._guard_id(plan)).prepared_id == prepared.prepared_id
        raise FCPMCPError(ErrorCode.COMMAND_FAILED, "timeout")

    monkeypatch.setattr(bridge.automation, "run_osascript", timeout)
    result = runtime.request(prepared.prepared_id, prepared.plan_sha256)
    assert result.status == "ambiguous" and result.request_accepted is None
    again = bridge.ImportBridge(setup[0]).request(prepared.prepared_id, prepared.plan_sha256)
    assert again.status == "ambiguous"


@pytest.mark.parametrize("mode", ["pending", "wrong_library", "duplicate", "duration", "success"])
def test_reconciliation_observes_metadata_without_resending(setup, mode):
    runtime, prepared = prepare(setup)
    runtime.request(prepared.prepared_id, prepared.plan_sha256)
    inventory = setup[4]
    if mode != "pending":
        add_observed_project(inventory, "5s" if mode == "duration" else "120/30s")
    if mode == "wrong_library":
        inventory[0]["file"] = setup[2].with_name("Other.fcpbundle").as_uri()
    elif mode == "duplicate":
        inventory.append(inventory[0].copy())
    result = bridge.ImportBridge(setup[0]).reconcile(prepared.prepared_id)
    assert result.import_completed is None
    assert result.status == {"pending": "not_observed", "success": "metadata_observed"}.get(
        mode, "ambiguous"
    )
    assert len([c for c in setup[3] if c[0] is bridge.OPEN_DOCUMENT]) == 1
    if mode == "success":
        assert result.observations[0].library_path == str(setup[2])
        assert result.capabilities.inspection == "observed"
        plan, _ = runtime._load(prepared.prepared_id)
        assert runtime._record(runtime._guard_id(plan)).status == "metadata_observed"


def test_reconcile_unsent_does_not_contact_app(setup):
    runtime, prepared = prepare(setup)
    assert runtime.reconcile(prepared.prepared_id).issue.code == "not_dispatched"
    assert setup[3] == []


def test_crash_after_durable_intent_prevents_duplicate_after_restart(setup, monkeypatch):
    runtime, prepared = prepare(setup)
    original = bridge.automation.run_osascript

    def crash(program, args=(), **kwargs):
        if program is bridge.INSPECT_LIBRARIES:
            return original(program, args, **kwargs)
        raise SystemExit("simulated worker death")

    monkeypatch.setattr(bridge.automation, "run_osascript", crash)
    with pytest.raises(SystemExit):
        runtime.request(prepared.prepared_id, prepared.plan_sha256)
    restarted = bridge.ImportBridge(setup[0])
    result = restarted.request(prepared.prepared_id, prepared.plan_sha256)
    assert result.status == "ambiguous" and result.request_accepted is None
    assert result.issue.related_prepared_id == prepared.prepared_id
    monkeypatch.setattr(bridge.automation, "run_osascript", original)
    add_observed_project(setup[4])
    assert restarted.reconcile(prepared.prepared_id).status == "metadata_observed"


def test_new_import_can_follow_terminal_observation_but_old_request_cannot_repeat(setup):
    runtime, prepared = prepare(setup)
    runtime.request(prepared.prepared_id, prepared.plan_sha256)
    add_observed_project(setup[4])
    runtime.reconcile(prepared.prepared_id)
    source = setup[1]
    source.write_text(source.read_text().replace("Unique Event", "Next Event"))
    next_prepared = runtime.prepare(str(source), str(setup[2]))
    assert (
        runtime.request(next_prepared.prepared_id, next_prepared.plan_sha256).status
        == "accepted_unverified"
    )
    assert runtime.reconcile(prepared.prepared_id).status == "metadata_observed"
    assert (
        runtime.request(next_prepared.prepared_id, next_prepared.plan_sha256).status == "ambiguous"
    )
    assert len([c for c in setup[3] if c[0] is bridge.OPEN_DOCUMENT]) == 2


def test_media_disappearance_uses_retained_snapshot(setup):
    runtime, prepared = prepare(setup)
    (setup[1].parent / "clip.mov").unlink()
    assert (
        runtime.request(prepared.prepared_id, prepared.plan_sha256).status == "accepted_unverified"
    )
    root = ET.parse(setup[3][-1][1][0]).getroot()
    staged = bridge._file_path(root.find("resources/asset/media-rep").get("src"))
    assert staged.read_bytes() == b"media path fixture; not decoded"


@pytest.mark.asyncio
async def test_public_mcp_schema_and_calls_preserve_truthful_results(setup, monkeypatch):
    from mcp.server.mcpserver.exceptions import ToolError

    from fcp_mcp import server
    from fcp_mcp.mcp_boundary import build_mcp_server
    from fcp_mcp.result_models.import_bridge import ImportBridgeResult, PreparedImportResult

    config, source, library, _, _ = setup
    monkeypatch.setattr(server, "CONFIG", config)
    mcp = build_mcp_server(config, server.TOOLS, server.PROMPTS, server.RESOURCES)
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    assert tools["fcpxml_prepare_import"].input_schema["required"] == ["path", "library_path"]
    assert tools["fcpxml_prepare_import"].output_schema == PreparedImportResult.model_json_schema()
    assert tools["fcp_request_import"].input_schema["required"] == [
        "prepared_id",
        "expected_plan_sha256",
    ]
    assert tools["fcp_request_import"].output_schema == ImportBridgeResult.model_json_schema()
    result = await mcp.call_tool(
        "fcpxml_prepare_import", {"path": str(source), "library_path": str(library)}
    )
    prepared = result.structured_content
    with pytest.raises(ToolError, match="approval_required"):
        await mcp.call_tool(
            "fcp_request_import",
            {"prepared_id": prepared["prepared_id"], "expected_plan_sha256": "0" * 64},
        )
    response = await mcp.call_tool(
        "fcp_request_import",
        {"prepared_id": prepared["prepared_id"], "expected_plan_sha256": prepared["plan_sha256"]},
    )
    assert response.structured_content["status"] == "accepted_unverified"
    assert response.structured_content["import_completed"] is None


def test_native_posix_library_observation_can_match_exact_target(setup):
    runtime, prepared = prepare(setup)
    setup[4][0]["file"] = str(setup[2])
    assert (
        runtime.request(prepared.prepared_id, prepared.plan_sha256).status == "accepted_unverified"
    )


def test_acknowledgement_persistence_failure_stays_ambiguous_without_resend(setup, monkeypatch):
    runtime, prepared = prepare(setup)
    original = runtime._write_record

    def fail_ack(run_id, record):
        if record.status == "accepted_unverified":
            raise FCPMCPError(ErrorCode.TRANSACTION_FAILED, "ack disk failure")
        return original(run_id, record)

    monkeypatch.setattr(runtime, "_write_record", fail_ack)
    result = runtime.request(prepared.prepared_id, prepared.plan_sha256)
    assert result.status == "ambiguous" and result.request_accepted is True
    assert (
        bridge.ImportBridge(setup[0]).request(prepared.prepared_id, prepared.plan_sha256).status
        == "ambiguous"
    )
    assert len([c for c in setup[3] if c[0] is bridge.OPEN_DOCUMENT]) == 1


def test_concurrent_call_never_sends_second_appleevent(setup, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    runtime, prepared = prepare(setup)
    original = bridge.automation.run_osascript
    sending, release = threading.Event(), threading.Event()

    def transport(program, args=(), **kwargs):
        if program is bridge.OPEN_DOCUMENT:
            sending.set()
            assert release.wait(5)
        return original(program, args, **kwargs)

    monkeypatch.setattr(bridge.automation, "run_osascript", transport)
    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(runtime.request, prepared.prepared_id, prepared.plan_sha256)
        assert sending.wait(5)
        try:
            with pytest.raises(FCPMCPError) as error:
                bridge.ImportBridge(setup[0]).request(prepared.prepared_id, prepared.plan_sha256)
            assert error.value.code is ErrorCode.RECOVERY_REQUIRED
        finally:
            release.set()
        assert first.result().status == "accepted_unverified"
    assert len([c for c in setup[3] if c[0] is bridge.OPEN_DOCUMENT]) == 1


def test_expiry_during_native_preflight_blocks_before_intent(setup, monkeypatch):
    from datetime import datetime, timedelta, timezone

    runtime, prepared = prepare(setup)
    expiry = datetime.fromisoformat(prepared.expires_at)
    clock = {"now": expiry - timedelta(seconds=1)}
    original = bridge.automation.run_osascript

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["now"].astimezone(tz or timezone.utc)

    def delayed(program, args=(), **kwargs):
        clock["now"] = expiry + timedelta(seconds=1)
        return original(program, args, **kwargs)

    monkeypatch.setattr(bridge, "datetime", Clock)
    monkeypatch.setattr(bridge.automation, "run_osascript", delayed)
    with pytest.raises(FCPMCPError) as error:
        runtime.request(prepared.prepared_id, prepared.plan_sha256)
    assert error.value.code is ErrorCode.APPROVAL_EXPIRED
    assert len(setup[3]) == 1 and setup[3][0][0] is bridge.INSPECT_LIBRARIES
    assert runtime._record(prepared.prepared_id) is None


@pytest.mark.parametrize("expiry", ["not-a-date", "2027-01-01T00:00:00"])
def test_invalid_persisted_expiry_cannot_authorize_a_send(setup, expiry):
    runtime, prepared = prepare(setup)
    plan, _ = runtime._load(prepared.prepared_id)
    metadata = runtime.store.write(
        prepared.prepared_id,
        ArtifactKind.DIFF,
        bridge.canonical_json(plan.model_copy(update={"expires_at": expiry})),
    )
    with pytest.raises(FCPMCPError) as error:
        runtime.request(prepared.prepared_id, metadata.sha256)
    assert error.value.code is ErrorCode.ARTIFACT_CORRUPT
    assert setup[3] == []


@pytest.mark.parametrize("phase", ["request", "reconcile"])
@pytest.mark.parametrize(
    "mode", ["denied", "not_json", "not_list", "unknown_field", "bad_duration"]
)
def test_bad_native_inspection_is_typed_and_never_clears_pending(setup, monkeypatch, phase, mode):
    runtime, prepared = prepare(setup)
    if phase == "reconcile":
        runtime.request(prepared.prepared_id, prepared.plan_sha256)
    before = len(setup[3])

    def invalid(program, args=(), **kwargs):
        assert program is bridge.INSPECT_LIBRARIES
        if mode == "denied":
            raise FCPMCPError(ErrorCode.PERMISSION_DENIED, "native access denied")
        if mode == "not_json":
            return "native output is unavailable"
        if mode == "not_list":
            return "{}"
        inventory = json.loads(json.dumps(setup[4]))
        if mode == "unknown_field":
            inventory[0]["unexpected"] = True
        else:
            add_observed_project(inventory, "1/0s")
        return json.dumps(inventory)

    monkeypatch.setattr(bridge.automation, "run_osascript", invalid)
    result = (
        runtime.request(prepared.prepared_id, prepared.plan_sha256)
        if phase == "request"
        else runtime.reconcile(prepared.prepared_id)
    )
    assert result.status == ("blocked" if phase == "request" else "ambiguous")
    assert result.import_completed is None
    assert result.issue.code == ("permission_denied" if mode == "denied" else "command_failed")
    assert len(setup[3]) == before
    plan, _ = runtime._load(prepared.prepared_id)
    if phase == "reconcile":
        assert runtime._record(runtime._guard_id(plan)).status == "dispatching"
    else:
        assert runtime._record(prepared.prepared_id) is None


@pytest.mark.parametrize("failed_write", [1, 2])
def test_partial_intent_persistence_never_sends_and_restart_is_conservative(
    setup, monkeypatch, failed_write
):
    runtime, prepared = prepare(setup)
    original = runtime._write_record
    calls = 0

    def fail(run_id, record):
        nonlocal calls
        calls += 1
        if calls == failed_write:
            raise FCPMCPError(ErrorCode.TRANSACTION_FAILED, "simulated intent disk failure")
        return original(run_id, record)

    monkeypatch.setattr(runtime, "_write_record", fail)
    with pytest.raises(FCPMCPError) as error:
        runtime.request(prepared.prepared_id, prepared.plan_sha256)
    assert error.value.code is ErrorCode.TRANSACTION_FAILED
    assert all(call[0] is bridge.INSPECT_LIBRARIES for call in setup[3])
    restarted = bridge.ImportBridge(setup[0])
    if failed_write == 2:
        result = restarted.request(prepared.prepared_id, prepared.plan_sha256)
        assert result.status == "ambiguous" and result.request_accepted is None
        assert result.issue.related_prepared_id == prepared.prepared_id
        assert restarted.reconcile(prepared.prepared_id).issue.code == "not_dispatched"
    else:
        # Neither intent exists: no native send was attempted. A reviewed unsent
        # plan remains usable once the disk failure is resolved.
        assert (
            restarted.request(prepared.prepared_id, prepared.plan_sha256).status
            == "accepted_unverified"
        )
    assert len([call for call in setup[3] if call[0] is bridge.OPEN_DOCUMENT]) == (
        0 if failed_write == 2 else 1
    )


@pytest.mark.parametrize("record_kind", ["attempt", "library_guard"])
@pytest.mark.parametrize("mode", ["json", "extra", "wrong_type", "symlink"])
def test_corrupted_persistent_record_blocks_a_second_send(setup, record_kind, mode):
    runtime, prepared = prepare(setup)
    runtime.request(prepared.prepared_id, prepared.plan_sha256)
    plan, _ = runtime._load(prepared.prepared_id)
    run_id = prepared.prepared_id if record_kind == "attempt" else runtime._guard_id(plan)
    path = runtime.paths.artifact_path(run_id, ArtifactKind.RECEIPT)
    if mode == "json":
        path.write_bytes(b"{corrupt")
    elif mode == "symlink":
        saved = path.with_name("saved.json")
        path.rename(saved)
        path.symlink_to(saved)
    else:
        body = json.loads(path.read_text())
        body["unexpected" if mode == "extra" else "request_accepted"] = 1
        path.write_text(json.dumps(body))
    before = len(setup[3])
    with pytest.raises(FCPMCPError) as error:
        bridge.ImportBridge(setup[0]).request(prepared.prepared_id, prepared.plan_sha256)
    assert error.value.code is ErrorCode.ARTIFACT_CORRUPT
    assert len(setup[3]) == before


@pytest.mark.parametrize(
    "mode", ["source_denied", "media_parent_symlink", "extra_staged_file", "stage_parent_symlink"]
)
def test_permission_and_path_changes_fail_closed(setup, monkeypatch, mode):
    import os

    from fcp_mcp.fcp_control import import_staging as staging

    if mode == "media_parent_symlink":
        directory = setup[1].parent / "media-directory"
        directory.mkdir()
        media = setup[1].parent / "clip.mov"
        media.rename(directory / media.name)
        setup[1].write_text(
            setup[1].read_text().replace(media.as_uri(), (directory / media.name).as_uri())
        )
        original = staging.source_file

        def swap(path):
            if path.suffix == ".mov":
                moved = directory.with_name("moved-directory")
                directory.rename(moved)
                directory.symlink_to(moved, target_is_directory=True)
            return original(path)

        monkeypatch.setattr(staging, "source_file", swap)
        with pytest.raises(FCPMCPError) as error:
            prepare(setup)
        assert error.value.code is ErrorCode.ARTIFACT_CORRUPT
    else:
        runtime, prepared = prepare(setup)
        folder = runtime.staging.path(prepared.prepared_id)
        if mode == "source_denied":
            original_open = staging.os.open

            def denied(path, *args, **kwargs):
                if path == setup[1].name:
                    raise PermissionError("denied source read")
                return original_open(path, *args, **kwargs)

            monkeypatch.setattr(staging.os, "open", denied)
        elif mode == "extra_staged_file":
            folder.chmod(0o700)
            (folder / "unreviewed.mov").write_bytes(b"unreviewed")
            folder.chmod(0o500)
        else:
            parent = runtime.staging.root
            moved = parent.with_name("moved-stage-root")
            parent.rename(moved)
            parent.symlink_to(moved, target_is_directory=True)
        with pytest.raises(FCPMCPError) as error:
            runtime.request(prepared.prepared_id, prepared.plan_sha256)
        assert error.value.code is ErrorCode.ARTIFACT_CORRUPT
    assert setup[3] == []


def test_changed_references_fail_but_repeated_media_refs_snapshot_once(setup):
    source = setup[1]
    data = source.read_text()
    root = ET.fromstring(data)
    asset = root.find("resources/asset")
    ET.SubElement(
        asset, "media-rep", {"kind": "proxy-media", "src": asset.find("media-rep").get("src")}
    )
    source.write_text(ET.tostring(root, encoding="unicode"))
    runtime, prepared = prepare(setup)
    plan, _ = runtime._load(prepared.prepared_id)
    assert len(plan.staged.files) == 2  # one media copy plus XML
    refs = ET.parse(prepared.candidate.path).findall("resources/asset/media-rep")
    assert len(refs) == 2 and refs[0].get("src") == refs[1].get("src")
    source.write_text(source.read_text().replace("clip.mov", "changed.mov"))
    with pytest.raises(FCPMCPError) as error:
        runtime.request(prepared.prepared_id, prepared.plan_sha256)
    assert error.value.code is ErrorCode.WORKFLOW_STALE
    assert setup[3] == []


@pytest.mark.parametrize("mode", ["factory_escape", "scoped_external", "native_uid"])
def test_effect_policy_checks_trusted_factory_containment(setup, monkeypatch, mode):
    base = setup[1].parent
    factory = base / "factory"
    factory.mkdir()
    outside = base / "external.moti"
    outside.write_bytes(b"fixture template; never decoded")
    effect = ET.Element("effect", {"id": "r3", "name": "Fixture", "uid": "FFGaussianBlur"})
    if mode == "factory_escape":
        (factory / "linked.moti").symlink_to(outside)
        monkeypatch.setattr(bridge, "MOTION_TEMPLATES", factory)
        effect.set("uid", ".../linked.moti")
    elif mode == "scoped_external":
        effect.set("src", outside.as_uri())
    root = ET.fromstring(setup[1].read_text())
    root.find("resources").append(effect)
    setup[1].write_text(ET.tostring(root, encoding="unicode"))
    if mode == "native_uid":
        _, prepared = prepare(setup)
        assert (
            ET.parse(prepared.candidate.path).find("resources/effect").get("uid")
            == "FFGaussianBlur"
        )
    else:
        with pytest.raises(FCPMCPError) as error:
            prepare(setup)
        assert error.value.code is ErrorCode.UNSUPPORTED_CONTRACT
    assert setup[3] == []


def test_ambiguous_reconciliation_preserves_guard_across_restart(setup):
    runtime, prepared = prepare(setup)
    runtime.request(prepared.prepared_id, prepared.plan_sha256)
    add_observed_project(setup[4])
    setup[4].append(json.loads(json.dumps(setup[4][0])))
    result = runtime.reconcile(prepared.prepared_id)
    assert result.status == "ambiguous" and result.issue.code == "destination_mismatch"
    plan, _ = runtime._load(prepared.prepared_id)
    assert runtime._record(runtime._guard_id(plan)).status == "dispatching"
    restarted = bridge.ImportBridge(setup[0])
    assert restarted.request(prepared.prepared_id, prepared.plan_sha256).status == "ambiguous"
    setup[4].pop()
    observed = restarted.reconcile(prepared.prepared_id)
    assert observed.status == "metadata_observed" and observed.import_completed is None
    assert len([c for c in setup[3] if c[0] is bridge.OPEN_DOCUMENT]) == 1


@pytest.mark.asyncio
async def test_mcp_results_reject_false_completion_and_validate_success_schema(setup, monkeypatch):
    from jsonschema import validate
    from mcp.server.mcpserver.exceptions import ToolError

    from fcp_mcp import server
    from fcp_mcp.mcp_boundary import build_mcp_server

    monkeypatch.setattr(server, "CONFIG", setup[0])
    mcp = build_mcp_server(setup[0], server.TOOLS, server.PROMPTS, server.RESOURCES)
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    prepared = await mcp.call_tool(
        "fcpxml_prepare_import", {"path": str(setup[1]), "library_path": str(setup[2])}
    )
    validate(prepared.structured_content, tools["fcpxml_prepare_import"].output_schema)
    body = prepared.structured_content
    requested = await mcp.call_tool(
        "fcp_request_import",
        {"prepared_id": body["prepared_id"], "expected_plan_sha256": body["plan_sha256"]},
    )
    validate(requested.structured_content, tools["fcp_request_import"].output_schema)
    assert requested.structured_content["import_completed"] is None
    add_observed_project(setup[4])
    observed = await mcp.call_tool("fcp_reconcile_import", {"prepared_id": body["prepared_id"]})
    validate(observed.structured_content, tools["fcp_reconcile_import"].output_schema)
    assert observed.structured_content["import_completed"] is None
    invalid = {**observed.structured_content, "import_completed": True}
    monkeypatch.setattr(bridge.ImportBridge, "reconcile", lambda self, prepared_id: invalid)
    with pytest.raises(ToolError, match="internal_error"):
        await mcp.call_tool("fcp_reconcile_import", {"prepared_id": body["prepared_id"]})


_PROCESS_REQUEST = """
import json, os, sys, time
from pathlib import Path
from fcp_mcp.config import RuntimeConfig
from fcp_mcp.contracts import FCPMCPError
from fcp_mcp.fcp_control import import_bridge as bridge
spec=json.loads(Path(sys.argv[1]).read_text())
config=RuntimeConfig.from_env(spec['environment'])
def transport(program, args=(), **kwargs):
    if program is bridge.INSPECT_LIBRARIES:
        return json.dumps(spec['inventory'])
    assert program is bridge.OPEN_DOCUMENT
    with Path(spec['log']).open('a') as handle:
        handle.write('mock-send\\n');handle.flush();os.fsync(handle.fileno())
    if spec['hold']:
        Path(spec['entered']).write_text('entered')
        deadline=time.monotonic()+12
        while not Path(spec['release']).exists():
            if time.monotonic()>deadline:raise RuntimeError('test release timeout')
            time.sleep(0.01)
    return 'Open Document AppleEvent returned'
bridge.automation.run_osascript=transport
try:
    result=bridge.ImportBridge(config).request(spec['prepared_id'],spec['plan_sha256'])
except FCPMCPError as error:
    print(json.dumps({'error':error.code.value}))
else:
    print(result.model_dump_json())
"""


@pytest.mark.parametrize("worker_death", [False, True])
def test_cross_process_duplicate_and_worker_death_never_resend(setup, worker_death):
    import os
    import subprocess
    import sys
    import time

    from fcp_mcp.workflow.locking import OwnerLiveness, assess_owner_liveness

    runtime, prepared = prepare(setup)
    base = setup[1].parent
    specification = {
        "environment": {
            "FCP_MCP_OUTPUT_DIR": str(base),
            "FCP_MCP_ALLOWED_ROOTS": str(base),
            "FCP_MCP_STATE_DIR": str(setup[0].state_dir),
            "FCP_MCP_PROFILE": "full",
            "FCP_MCP_ENABLE_LIVE_CONTROL": "1",
        },
        "inventory": setup[4],
        "prepared_id": prepared.prepared_id,
        "plan_sha256": prepared.plan_sha256,
        "log": str(base / "mock-sends.log"),
        "entered": str(base / "entered"),
        "release": str(base / "release"),
        "hold": True,
    }
    spec = base / "process.json"
    spec.write_text(json.dumps(specification))
    environment = {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
    }
    process = subprocess.Popen(
        [sys.executable, "-c", _PROCESS_REQUEST, str(spec)],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 8
        while not Path(specification["entered"]).exists():
            assert process.poll() is None, process.communicate()
            assert time.monotonic() < deadline
            time.sleep(0.01)
        second = subprocess.run(
            [sys.executable, "-c", _PROCESS_REQUEST, str(spec)],
            env=environment,
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
        assert second.returncode == 0, second.stderr
        assert json.loads(second.stdout)["error"] == "recovery_required"
        if worker_death:
            process.kill()
            process.communicate(timeout=5)
            lock_files = list(runtime.paths.locks.glob("*.lock"))
            assert len(lock_files) == 1
            owner = json.loads(lock_files[0].read_text())
            assert assess_owner_liveness(owner["pid"], owner["host"]) is OwnerLiveness.DEAD
            with pytest.raises(FCPMCPError) as error:
                bridge.ImportBridge(setup[0]).request(prepared.prepared_id, prepared.plan_sha256)
            assert error.value.code is ErrorCode.RECOVERY_REQUIRED
            assert runtime._record(prepared.prepared_id).status == "dispatching"
        else:
            Path(specification["release"]).write_text("release")
            stdout, stderr = process.communicate(timeout=8)
            assert process.returncode == 0, stderr
            assert json.loads(stdout)["status"] == "accepted_unverified"
            assert (
                bridge.ImportBridge(setup[0])
                .request(prepared.prepared_id, prepared.plan_sha256)
                .status
                == "ambiguous"
            )
        assert Path(specification["log"]).read_text().splitlines() == ["mock-send"]
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)


@pytest.mark.asyncio
@pytest.mark.parametrize("changed_mode", [False, True])
async def test_real_stdio_external_state_survives_handoff_or_rejects_mode_change(
    setup, changed_mode
):
    import asyncio
    import os
    import subprocess
    import sys

    from mcp import Client, StdioServerParameters
    from mcp.client.stdio import stdio_client

    base = setup[1].parent
    output, state = base / "stdio-output", base / "external-state"
    output.mkdir()
    environment = {
        **os.environ,
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "FCP_MCP_PROFILE": "full",
        "FCP_MCP_ENABLE_LIVE_CONTROL": "0",
        "FCP_MCP_ALLOWED_ROOTS": str(base),
        "FCP_MCP_OUTPUT_DIR": str(output),
        "FCP_MCP_STATE_DIR": str(state),
    }
    params = StdioServerParameters(command=sys.executable, args=["-m", "fcp_mcp"], env=environment)
    async with Client(stdio_client(params), mode="auto", raise_exceptions=True) as client:
        invalid = await client.call_tool(
            "fcpxml_prepare_import",
            {"path": str(setup[1]), "library_path": str(setup[2]), "typo": True},
        )
        assert invalid.is_error
        assert "invalid_arguments" in "".join(item.text for item in invalid.content)
        result = await client.call_tool(
            "fcpxml_prepare_import", {"path": str(setup[1]), "library_path": str(setup[2])}
        )
        prepared = result.structured_content
        assert prepared["status"] == "prepared"
        disabled = await client.call_tool(
            "fcp_request_import",
            {
                "prepared_id": prepared["prepared_id"],
                "expected_plan_sha256": prepared["plan_sha256"],
            },
        )
        assert disabled.is_error
        assert "live_control_disabled" in "".join(item.text for item in disabled.content)
    folder = state / "import-bridge/staged" / prepared["prepared_id"]
    assert folder.stat().st_mode & 0o777 == 0o500
    assert not folder.is_relative_to(output)
    if changed_mode:
        folder.chmod(0o700)  # Only this scratch fixture; the gate must still reject.
    verify = """
import os,sys
from fcp_mcp.config import RuntimeConfig
from fcp_mcp.contracts import FCPMCPError
from fcp_mcp.fcp_control.import_bridge import ImportBridge
runtime=ImportBridge(RuntimeConfig.from_env(dict(os.environ)))
plan,sha=runtime._load(sys.argv[1]);assert sha==sys.argv[2]
assert runtime._record(plan.prepared_id) is None
try:runtime._verify_snapshots(plan)
except FCPMCPError as error:
    assert sys.argv[3]=='reject' and error.code.value=='artifact_corrupt'
else:assert sys.argv[3]=='accept'
print('verified')
"""
    reopened = await asyncio.to_thread(
        subprocess.run,
        [
            sys.executable,
            "-c",
            verify,
            prepared["prepared_id"],
            prepared["plan_sha256"],
            "reject" if changed_mode else "accept",
        ],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert reopened.returncode == 0, reopened.stderr
    assert reopened.stdout.strip() == "verified"
    assert setup[3] == []
