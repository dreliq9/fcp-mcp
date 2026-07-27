from pathlib import Path

import pytest

from fcp_mcp.contracts import FCPMCPError
from fcp_mcp.utils.atomic_write import atomic_replace_bytes


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
