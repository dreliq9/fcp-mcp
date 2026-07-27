import io
import json

from fcp_mcp.observability import emit_event


def test_json_event_is_one_stderr_safe_line():
    stream = io.StringIO()

    emit_event(
        {"event": "transaction", "disposition": "committed"},
        format="json",
        stream=stream,
    )

    lines = stream.getvalue().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["disposition"] == "committed"
