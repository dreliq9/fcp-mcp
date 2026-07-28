from __future__ import annotations

import hashlib
import json
import os
import socket
import stat
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from fcp_mcp.config import RuntimeConfig
from fcp_mcp.contracts import ErrorCode, FCPMCPError
from fcp_mcp.workflow.artifacts import StatePaths
from fcp_mcp.workflow.locking import (
    DestinationLock,
    OwnerLiveness,
    assess_owner_liveness,
)

RUN_ID = "12345678-1234-4234-9234-123456789abc"


def _paths(tmp_path: Path) -> StatePaths:
    config = RuntimeConfig.from_env(
        {"FCP_MCP_STATE_DIR": str(tmp_path / "state")},
        home=tmp_path,
    )
    return StatePaths.from_config(config)


def test_lock_is_exclusive_private_and_canonical(tmp_path: Path) -> None:
    destination = tmp_path / "project.fcpxml"
    first = DestinationLock(_paths(tmp_path), destination, RUN_ID)
    second = DestinationLock(_paths(tmp_path), destination, RUN_ID)

    first.acquire()
    expected_key = hashlib.sha256(str(destination.resolve()).encode()).hexdigest()
    assert first.path.name == f"{expected_key}.lock"
    assert stat.S_IMODE(first.path.lstat().st_mode) == 0o600
    payload = first.path.read_bytes()
    parsed = json.loads(payload)
    assert payload == (
        json.dumps(parsed, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    )
    assert parsed["destination_sha256"] == expected_key
    assert parsed["run_id"] == RUN_ID
    assert "destination" not in parsed

    with pytest.raises(FCPMCPError) as caught:
        second.acquire()
    assert caught.value.code is ErrorCode.RECOVERY_REQUIRED
    first.release()
    assert not first.path.exists()


def test_different_destinations_do_not_contend(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    first = DestinationLock(paths, tmp_path / "one.fcpxml", RUN_ID)
    second = DestinationLock(paths, tmp_path / "two.fcpxml", RUN_ID)
    first.acquire()
    second.acquire()
    second.release()
    first.release()


def test_owner_liveness_is_conservative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = socket.gethostname()
    assert assess_owner_liveness(os.getpid(), host) is OwnerLiveness.ALIVE
    assert assess_owner_liveness(os.getpid(), "different-host") is OwnerLiveness.UNKNOWN

    def missing(pid: int, signal: int) -> None:
        raise ProcessLookupError

    monkeypatch.setattr(os, "kill", missing)
    assert assess_owner_liveness(999_999, host) is OwnerLiveness.DEAD

    def denied(pid: int, signal: int) -> None:
        raise PermissionError

    monkeypatch.setattr(os, "kill", denied)
    assert assess_owner_liveness(999_999, host) is OwnerLiveness.UNKNOWN

    def ambiguous(pid: int, signal: int) -> None:
        raise OSError("ambiguous")

    monkeypatch.setattr(os, "kill", ambiguous)
    assert assess_owner_liveness(999_999, host) is OwnerLiveness.UNKNOWN


def test_age_never_breaks_existing_lock(tmp_path: Path) -> None:
    destination = tmp_path / "project.fcpxml"
    paths = _paths(tmp_path)
    first = DestinationLock(
        paths,
        destination,
        RUN_ID,
        clock=lambda: datetime(2000, 1, 1, tzinfo=timezone.utc),
    )
    first.acquire()
    contender = DestinationLock(
        paths,
        destination,
        RUN_ID,
        clock=lambda: datetime(2100, 1, 1, tzinfo=timezone.utc),
    )
    with pytest.raises(FCPMCPError) as caught:
        contender.acquire()
    assert caught.value.code is ErrorCode.RECOVERY_REQUIRED
    assert first.path.exists()
    first.release()


def test_release_never_unlinks_replaced_entry(tmp_path: Path) -> None:
    lock = DestinationLock(_paths(tmp_path), tmp_path / "project.fcpxml", RUN_ID)
    lock.acquire()
    displaced = lock.path.with_suffix(".owned")
    lock.path.rename(displaced)
    lock.path.write_text("attacker", encoding="utf-8")
    os.chmod(lock.path, 0o600)

    with pytest.raises(FCPMCPError) as caught:
        lock.release()
    assert caught.value.code is ErrorCode.RECOVERY_REQUIRED
    assert lock.path.read_text(encoding="utf-8") == "attacker"
    displaced.unlink()
    lock.path.unlink()


def test_release_sanitizes_directory_close_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock = DestinationLock(_paths(tmp_path), tmp_path / "project.fcpxml", RUN_ID)
    lock.acquire()
    original_close = os.close
    failed = False

    def close_then_fail(descriptor: int) -> None:
        nonlocal failed
        original_close(descriptor)
        if not failed:
            failed = True
            raise OSError("/private/raw close failure")

    monkeypatch.setattr(os, "close", close_then_fail)
    with pytest.raises(FCPMCPError) as caught:
        lock.release()
    assert caught.value.code is ErrorCode.RECOVERY_REQUIRED
    assert "/private/raw" not in str(caught.value)
    assert not lock.path.exists()


def test_same_destination_concurrency_has_exactly_one_owner(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    barrier = threading.Barrier(16)
    acquired: list[DestinationLock] = []
    failures: list[ErrorCode] = []
    guard = threading.Lock()

    def contend() -> None:
        lock = DestinationLock(paths, tmp_path / "project.fcpxml", RUN_ID)
        barrier.wait()
        try:
            lock.acquire()
        except FCPMCPError as error:
            with guard:
                failures.append(error.code)
        else:
            with guard:
                acquired.append(lock)

    threads = [threading.Thread(target=contend) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    assert len(acquired) == 1
    assert failures == [ErrorCode.RECOVERY_REQUIRED] * 15
    acquired[0].release()
