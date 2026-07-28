from pathlib import Path
from types import SimpleNamespace

import pytest

import fcp_mcp.utils.atomic_write as atomic_write_module
from fcp_mcp.contracts import FCPMCPError
from fcp_mcp.utils.atomic_write import _sync_directory, atomic_replace_bytes


def test_directory_sync_has_no_unsupported_runtime_noop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    calls: list[tuple[str, object]] = []
    fake_os = SimpleNamespace(
        name="nt",
        O_RDONLY=0,
        open=lambda path, flags: calls.append(("open", path)) or 17,
        fsync=lambda descriptor: calls.append(("fsync", descriptor)),
        close=lambda descriptor: calls.append(("close", descriptor)),
    )
    monkeypatch.setattr(atomic_write_module, "os", fake_os)

    _sync_directory(tmp_path)

    assert calls == [("open", tmp_path), ("fsync", 17), ("close", 17)]


def test_existing_destination_is_backed_up(tmp_path: Path):
    destination = tmp_path / "out.txt"
    destination.write_bytes(b"before")

    receipt = atomic_replace_bytes(destination, b"after")

    assert destination.read_bytes() == b"after"
    assert receipt.backup_path is not None
    assert receipt.backup_path.read_bytes() == b"before"


def test_post_commit_failure_restores_existing_destination(tmp_path: Path):
    destination = tmp_path / "out.txt"
    destination.write_bytes(b"before")

    def validate(path: Path) -> None:
        if path == destination:
            raise ValueError("post-commit rejection")

    with pytest.raises(FCPMCPError, match="transaction_failed"):
        atomic_replace_bytes(destination, b"after", validate=validate)

    assert destination.read_bytes() == b"before"
    assert list(tmp_path.glob("*.tmp")) == []


def test_post_commit_failure_removes_new_destination(tmp_path: Path):
    destination = tmp_path / "new.txt"

    def validate(path: Path) -> None:
        if path == destination:
            raise ValueError("post-commit rejection")

    with pytest.raises(FCPMCPError, match="transaction_failed"):
        atomic_replace_bytes(destination, b"after", validate=validate)

    assert destination.exists() is False
    assert list(tmp_path.glob("*.tmp")) == []
