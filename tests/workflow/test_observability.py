from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from test_prepare import SOURCE_XML, _engine, _request


@pytest.mark.parametrize("log_format", ["json", "text"])
def test_prepare_observability_is_allowlisted_and_excludes_secrets(
    tmp_path: Path,
    log_format: str,
):
    sentinels = {
        "SOURCE_PATH_SECRET",
        "DEST_PATH_SECRET",
        "MARKER_VALUE_SECRET",
        "MARKER_NOTE_SECRET",
        "IDEMPOTENCY_SECRET",
        "MEDIA_SECRET",
        "OPERATOR_SECRET",
    }
    source = tmp_path / "SOURCE_PATH_SECRET.fcpxml"
    destination = tmp_path / "DEST_PATH_SECRET.fcpxml"
    source.write_bytes(SOURCE_XML.replace(b'name="Clip"', b'name="MEDIA_SECRET"'))
    stream = io.StringIO()
    engine, _, _ = _engine(tmp_path, log_stream=stream)
    engine.config = type(engine.config)(
        **{**engine.config.__dict__, "log_format": log_format}
    )

    engine.prepare(
        _request(
            source,
            destination,
            idempotency_key="IDEMPOTENCY_SECRET",
            clip_name="MEDIA_SECRET",
            value="MARKER_VALUE_SECRET",
            note="MARKER_NOTE_SECRET",
        )
    )

    emitted = stream.getvalue()
    for sentinel in sentinels:
        assert sentinel not in emitted
    lines = emitted.splitlines()
    assert len(lines) == 8
    if log_format == "json":
        records = [json.loads(line) for line in lines]
        allowed = {
            "timestamp",
            "package_version",
            "event",
            "run_id",
            "sequence",
            "graph_version",
            "run_version",
            "schema_version",
            "profile",
            "approval_mode",
            "node",
            "attempt",
            "elapsed_ms",
            "source_sha256",
            "prior_destination_sha256",
            "plan_sha256",
            "candidate_sha256",
            "diff_sha256",
            "disposition",
            "error_code",
        }
        assert all(set(record) <= allowed for record in records)
        assert [record["node"] for record in records] == [
            "run_created",
            "source_inspected",
            "plan_normalized",
            "dry_run_completed",
            "candidate_validated",
            "diff_created",
            "preview_persisted",
            "awaiting_approval",
        ]
