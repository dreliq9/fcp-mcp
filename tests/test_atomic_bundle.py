from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
import threading
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import pytest

import fcp_mcp.utils.atomic_write as atomic_write_module
from fcp_mcp.contracts import ErrorCode, FCPMCPError
from fcp_mcp.utils.atomic_write import (
    AtomicBundleItem,
    _write_exclusive,
    atomic_replace_bundle,
)

_PRIOR = (b"old timeline", b'{"old": true}\n')


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _valid_json(path: Path) -> None:
    try:
        json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FCPMCPError(
            ErrorCode.VALIDATION_FAILED,
            "JSON candidate is invalid",
        ) from error


def _bundle(
    tmp_path: Path,
    *,
    transaction_id: str | None = None,
) -> tuple[str, tuple[AtomicBundleItem, AtomicBundleItem]]:
    attempt = transaction_id or str(uuid.uuid4())
    return attempt, (
        AtomicBundleItem(tmp_path / "timeline.fcpxml", b"new timeline"),
        AtomicBundleItem(
            tmp_path / "timeline.sources.json",
            b'{"new": true}\n',
            validate=_valid_json,
        ),
    )


def _journal_paths(tmp_path: Path) -> list[Path]:
    return list(tmp_path.glob(".fcp-mcp.bundle.*.journal.json"))


def _stage_paths(tmp_path: Path) -> list[Path]:
    return list(tmp_path.glob(".*.stage"))


def _receipt_paths(tmp_path: Path) -> list[Path]:
    return list(tmp_path.glob(".fcp-mcp.bundle.*.receipt.json"))


def _write_checksummed_journal(path: Path, payload: dict[str, object]) -> None:
    body = dict(payload)
    body.pop("journal_sha256", None)
    canonical = json.dumps(
        body,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    body["journal_sha256"] = hashlib.sha256(canonical).hexdigest()
    path.write_text(
        json.dumps(body, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def _inject_boundary_failure(
    monkeypatch: pytest.MonkeyPatch,
    destinations: tuple[Path, Path],
    *,
    boundary: str,
    error: BaseException,
) -> None:
    real_replace = os.replace
    replacement_count = 0
    injected = False

    def replace(source: str | Path, destination: str | Path) -> None:
        nonlocal replacement_count, injected
        source_path = Path(source)
        destination_path = Path(destination)
        is_destination_replace = destination_path in destinations and source_path.name.endswith(
            ".stage"
        )
        if is_destination_replace:
            replacement_count += 1
            if not injected and (
                (boundary == "before_first" and replacement_count == 1)
                or (boundary == "between" and replacement_count == 2)
            ):
                injected = True
                raise error
        is_terminal_mark = destination_path.name.endswith(
            ".journal.json"
        ) and source_path.name.endswith(".journal.tmp")
        if not injected and boundary == "before_terminal" and is_terminal_mark:
            injected = True
            raise error
        real_replace(source, destination)

    monkeypatch.setattr(atomic_write_module.os, "replace", replace)


@dataclass(frozen=True)
class _HotBundle:
    attempt: str
    items: tuple[AtomicBundleItem, AtomicBundleItem]
    journal: Path

    @property
    def destinations(self) -> tuple[Path, Path]:
        return tuple(Path(item.destination) for item in self.items)


def _interrupt_hot_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    boundary: str = "before_first",
    committed: bool = False,
) -> _HotBundle:
    attempt, items = _bundle(tmp_path)
    destinations = tuple(Path(item.destination) for item in items)
    for destination, payload in zip(destinations, _PRIOR):
        destination.write_bytes(payload)
    if committed:
        original_write = atomic_write_module._write_exclusive

        def interrupt(path: Path, payload: bytes) -> None:
            if path.name.endswith(f".{attempt}.receipt.json"):
                raise KeyboardInterrupt("interrupted before receipt")
            original_write(path, payload)

        monkeypatch.setattr(atomic_write_module, "_write_exclusive", interrupt)
    else:
        _inject_boundary_failure(
            monkeypatch,
            destinations,
            boundary=boundary,
            error=KeyboardInterrupt(f"interrupted {boundary}"),
        )
    with pytest.raises(KeyboardInterrupt):
        atomic_replace_bundle(items, transaction_id=attempt)
    if committed:
        monkeypatch.setattr(atomic_write_module, "_write_exclusive", original_write)
    return _HotBundle(attempt, items, _journal_paths(tmp_path)[0])


def _record_journal_descriptors(
    monkeypatch: pytest.MonkeyPatch,
    journal: Path,
    after_open: Callable[[], None] | None = None,
) -> list[int]:
    original_open = os.open
    descriptors: list[int] = []

    def record_open(path: str | Path, flags: int, mode: int = 0o777) -> int:
        descriptor = original_open(path, flags, mode)
        if Path(path) == journal:
            descriptors.append(descriptor)
            if after_open is not None:
                after_open()
        return descriptor

    monkeypatch.setattr(atomic_write_module.os, "open", record_open)
    return descriptors


def _observed_pair(hot: _HotBundle) -> tuple[bytes, bytes]:
    return tuple(path.read_bytes() for path in hot.destinations)


def _assert_descriptors_closed(descriptors: list[int]) -> None:
    assert descriptors
    for descriptor in descriptors:
        with pytest.raises(OSError) as caught:
            os.fstat(descriptor)
        assert caught.value.errno == errno.EBADF


def _inject_growth_during_journal_read(
    monkeypatch: pytest.MonkeyPatch,
    journal: Path,
) -> None:
    journal_identity = (journal.stat().st_dev, journal.stat().st_ino)
    original_path_read = Path.read_bytes
    original_os_read = os.read
    path_grown = descriptor_grown = descriptor_eof = False

    def legacy_read(path: Path) -> bytes:
        nonlocal path_grown
        raw = original_path_read(path)
        if path == journal and not path_grown:
            with path.open("ab") as stream:
                stream.write(b" " * 65536)
            path_grown = True
        return raw

    def descriptor_read(descriptor: int, size: int) -> bytes:
        nonlocal descriptor_grown, descriptor_eof
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != journal_identity:
            return original_os_read(descriptor, size)
        if descriptor_grown and not descriptor_eof:
            descriptor_eof = True
            return b""
        raw = original_os_read(descriptor, size)
        if not descriptor_grown:
            with journal.open("ab") as stream:
                stream.write(b" " * 65536)
            descriptor_grown = True
        return raw

    monkeypatch.setattr(Path, "read_bytes", legacy_read)
    monkeypatch.setattr(atomic_write_module.os, "read", descriptor_read)


def test_exclusive_evidence_collision_never_deletes_existing_file(
    tmp_path: Path,
) -> None:
    evidence = tmp_path / ".occupied.stage"
    evidence.write_bytes(b"other transaction evidence")

    with pytest.raises(FCPMCPError) as caught:
        _write_exclusive(evidence, b"new evidence")

    assert caught.value.code is ErrorCode.RECOVERY_REQUIRED
    assert evidence.read_bytes() == b"other transaction evidence"


def test_journal_collision_during_prepare_preserves_foreign_evidence_and_pair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempt, items = _bundle(tmp_path)
    destinations = tuple(item.destination for item in items)
    destinations[0].write_bytes(b"old timeline")
    destinations[1].write_bytes(b'{"old": true}\n')
    foreign_journal = b"foreign transaction evidence"
    real_write_exclusive = atomic_write_module._write_exclusive
    injected = False

    def collide_when_publishing_journal(path: Path, payload: bytes) -> None:
        nonlocal injected
        if not injected and path.name.endswith(".journal.json"):
            path.write_bytes(foreign_journal)
            injected = True
        real_write_exclusive(path, payload)

    monkeypatch.setattr(
        atomic_write_module,
        "_write_exclusive",
        collide_when_publishing_journal,
    )

    with pytest.raises(FCPMCPError) as caught:
        atomic_replace_bundle(items, transaction_id=attempt)

    assert caught.value.code is ErrorCode.RECOVERY_REQUIRED
    assert destinations[0].read_bytes() == b"old timeline"
    assert destinations[1].read_bytes() == b'{"old": true}\n'
    journals = _journal_paths(tmp_path)
    assert len(journals) == 1
    assert journals[0].read_bytes() == foreign_journal


def test_bundle_rejects_invalid_sidecar_before_destination_mutation(
    tmp_path: Path,
) -> None:
    first = tmp_path / "timeline.fcpxml"
    second = tmp_path / "timeline.sources.json"
    first.write_bytes(b"old timeline")
    second.write_bytes(b'{"old": true}\n')
    attempt = str(uuid.uuid4())

    with pytest.raises(FCPMCPError) as caught:
        atomic_replace_bundle(
            (
                AtomicBundleItem(first, b"new timeline"),
                AtomicBundleItem(second, b"not-json", validate=_valid_json),
            ),
            transaction_id=attempt,
        )

    assert caught.value.code is ErrorCode.VALIDATION_FAILED
    assert first.read_bytes() == b"old timeline"
    assert second.read_bytes() == b'{"old": true}\n'
    assert _journal_paths(tmp_path) == []
    assert _stage_paths(tmp_path) == []
    assert _receipt_paths(tmp_path) == []


def test_atomic_bundle_is_narrowly_limited_to_one_output_pair(tmp_path: Path) -> None:
    items = (
        AtomicBundleItem(tmp_path / "one", b"one"),
        AtomicBundleItem(tmp_path / "two", b"two"),
        AtomicBundleItem(tmp_path / "three", b"three"),
    )

    with pytest.raises(FCPMCPError) as caught:
        atomic_replace_bundle(items, transaction_id=str(uuid.uuid4()))

    assert caught.value.code is ErrorCode.INVALID_ARGUMENTS
    assert all(not Path(item.destination).exists() for item in items)


def test_atomic_bundle_rejects_noncanonical_artifact_pairs(tmp_path: Path) -> None:
    items = (
        AtomicBundleItem(tmp_path / "timeline.fcpxml", b"timeline"),
        AtomicBundleItem(tmp_path / "other.sources.json", b"{}\n"),
    )

    with pytest.raises(FCPMCPError) as caught:
        atomic_replace_bundle(items, transaction_id=str(uuid.uuid4()))

    assert caught.value.code is ErrorCode.INVALID_PATH
    assert all(not Path(item.destination).exists() for item in items)


def test_short_stage_write_removes_partial_stage_without_mutating_pair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempt, items = _bundle(tmp_path)
    destinations = tuple(Path(item.destination) for item in items)
    for destination, payload in zip(destinations, _PRIOR):
        destination.write_bytes(payload)

    original_write = os.write

    def short_stage_write(descriptor: int, payload: bytes | bytearray | memoryview) -> int:
        opened = os.fstat(descriptor)
        for path in _stage_paths(tmp_path):
            if path.exists():
                identity = path.stat()
                if (identity.st_dev, identity.st_ino) == (opened.st_dev, opened.st_ino):
                    return 0
        return original_write(descriptor, payload)

    monkeypatch.setattr(atomic_write_module.os, "write", short_stage_write)

    with pytest.raises(FCPMCPError) as caught:
        atomic_replace_bundle(items, transaction_id=attempt)

    assert caught.value.code is ErrorCode.TRANSACTION_FAILED
    assert tuple(destination.read_bytes() for destination in destinations) == _PRIOR
    assert _stage_paths(tmp_path) == []
    assert _journal_paths(tmp_path) == []
    assert _receipt_paths(tmp_path) == []


@pytest.mark.parametrize("boundary", ["before_first", "between", "before_terminal"])
def test_handled_bundle_failure_restores_both_existing_destinations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    attempt, items = _bundle(tmp_path)
    destinations = tuple(item.destination for item in items)
    destinations[0].write_bytes(b"old timeline")
    destinations[1].write_bytes(b'{"old": true}\n')
    _inject_boundary_failure(
        monkeypatch,
        destinations,
        boundary=boundary,
        error=OSError(f"injected {boundary}"),
    )

    with pytest.raises(FCPMCPError) as caught:
        atomic_replace_bundle(items, transaction_id=attempt)

    assert caught.value.code is ErrorCode.TRANSACTION_FAILED
    assert destinations[0].read_bytes() == b"old timeline"
    assert destinations[1].read_bytes() == b'{"old": true}\n'
    assert _journal_paths(tmp_path) == []
    assert _stage_paths(tmp_path) == []
    assert _receipt_paths(tmp_path) == []


def test_rollback_contract_restores_bytes_and_existence_with_secure_evidence_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempt, items = _bundle(tmp_path)
    destinations = tuple(Path(item.destination) for item in items)
    prior = (b"old timeline", b'{"old": true}\n')
    for destination, payload in zip(destinations, prior):
        destination.write_bytes(payload)
        destination.chmod(0o644)
    _inject_boundary_failure(
        monkeypatch,
        destinations,
        boundary="before_terminal",
        error=OSError("injected rollback"),
    )

    with pytest.raises(FCPMCPError) as caught:
        atomic_replace_bundle(items, transaction_id=attempt)

    assert caught.value.code is ErrorCode.TRANSACTION_FAILED
    assert tuple(destination.read_bytes() for destination in destinations) == prior
    assert tuple(stat.S_IMODE(destination.stat().st_mode) for destination in destinations) == (
        0o600,
        0o600,
    )


def test_failure_between_replacements_restores_prior_absence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempt, items = _bundle(tmp_path)
    destinations = tuple(item.destination for item in items)
    _inject_boundary_failure(
        monkeypatch,
        destinations,
        boundary="between",
        error=OSError("injected between replacements"),
    )

    with pytest.raises(FCPMCPError) as caught:
        atomic_replace_bundle(items, transaction_id=attempt)

    assert caught.value.code is ErrorCode.TRANSACTION_FAILED
    assert all(not destination.exists() for destination in destinations)
    assert _journal_paths(tmp_path) == []
    assert _stage_paths(tmp_path) == []


def test_failure_with_one_unchanged_output_restores_the_changed_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempt = str(uuid.uuid4())
    first = tmp_path / "timeline.fcpxml"
    second = tmp_path / "timeline.sources.json"
    first.write_bytes(b"unchanged timeline")
    second.write_bytes(b'{"old": true}\n')
    items = (
        AtomicBundleItem(first, b"unchanged timeline"),
        AtomicBundleItem(second, b'{"new": true}\n', validate=_valid_json),
    )
    _inject_boundary_failure(
        monkeypatch,
        (first, second),
        boundary="before_terminal",
        error=OSError("injected after changed replacement"),
    )

    with pytest.raises(FCPMCPError) as caught:
        atomic_replace_bundle(items, transaction_id=attempt)

    assert caught.value.code is ErrorCode.TRANSACTION_FAILED
    assert first.read_bytes() == b"unchanged timeline"
    assert second.read_bytes() == b'{"old": true}\n'
    assert _journal_paths(tmp_path) == []
    assert _stage_paths(tmp_path) == []


def test_hot_recovery_commits_pair_with_one_unchanged_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempt = str(uuid.uuid4())
    first = tmp_path / "timeline.fcpxml"
    second = tmp_path / "timeline.sources.json"
    first.write_bytes(b"unchanged timeline")
    second.write_bytes(b'{"old": true}\n')
    items = (
        AtomicBundleItem(first, b"unchanged timeline"),
        AtomicBundleItem(second, b'{"new": true}\n', validate=_valid_json),
    )
    _inject_boundary_failure(
        monkeypatch,
        (first, second),
        boundary="before_terminal",
        error=KeyboardInterrupt("interrupted after changed replacement"),
    )

    with pytest.raises(KeyboardInterrupt):
        atomic_replace_bundle(items, transaction_id=attempt)

    receipt = atomic_replace_bundle(items, transaction_id=attempt)

    assert receipt.transaction_id == attempt
    assert receipt.replayed is True
    assert first.read_bytes() == b"unchanged timeline"
    assert second.read_bytes() == b'{"new": true}\n'
    assert _journal_paths(tmp_path) == []
    assert _stage_paths(tmp_path) == []


def test_terminal_cleanup_failure_rolls_back_the_committed_pair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempt, items = _bundle(tmp_path)
    destinations = tuple(item.destination for item in items)
    destinations[0].write_bytes(b"old timeline")
    destinations[1].write_bytes(b'{"old": true}\n')
    real_unlink = Path.unlink
    injected = False

    def fail_first_journal_cleanup(
        path: Path,
        missing_ok: bool = False,
    ) -> None:
        nonlocal injected
        if not injected and path.name.endswith(".journal.json"):
            injected = True
            real_unlink(path, missing_ok=missing_ok)
            raise OSError("injected terminal cleanup failure")
        real_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", fail_first_journal_cleanup)

    with pytest.raises(FCPMCPError) as caught:
        atomic_replace_bundle(items, transaction_id=attempt)

    assert caught.value.code is ErrorCode.TRANSACTION_FAILED
    assert destinations[0].read_bytes() == b"old timeline"
    assert destinations[1].read_bytes() == b'{"old": true}\n'
    assert _journal_paths(tmp_path) == []
    assert _stage_paths(tmp_path) == []
    assert _receipt_paths(tmp_path) == []


@pytest.mark.parametrize("boundary", ["before_first", "between", "before_terminal"])
def test_abandoned_journal_is_reconciled_at_each_replacement_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    attempt, items = _bundle(tmp_path)
    destinations = tuple(item.destination for item in items)
    destinations[0].write_bytes(b"old timeline")
    destinations[1].write_bytes(b'{"old": true}\n')
    _inject_boundary_failure(
        monkeypatch,
        destinations,
        boundary=boundary,
        error=KeyboardInterrupt(f"interrupted {boundary}"),
    )

    with pytest.raises(KeyboardInterrupt):
        atomic_replace_bundle(items, transaction_id=attempt)
    assert len(_journal_paths(tmp_path)) == 1

    receipt = atomic_replace_bundle(items, transaction_id=attempt)

    assert receipt.transaction_id == attempt
    assert destinations[0].read_bytes() == b"new timeline"
    assert destinations[1].read_bytes() == b'{"new": true}\n'
    assert _journal_paths(tmp_path) == []
    assert _stage_paths(tmp_path) == []


@pytest.mark.parametrize("boundary", ["before_first", "between", "before_terminal"])
def test_different_request_reconciles_each_hot_journal_boundary_before_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    first_attempt, first_items = _bundle(tmp_path)
    destinations = tuple(Path(item.destination) for item in first_items)
    destinations[0].write_bytes(b"old timeline")
    destinations[1].write_bytes(b'{"old": true}\n')
    _inject_boundary_failure(
        monkeypatch,
        destinations,
        boundary=boundary,
        error=KeyboardInterrupt(f"interrupted {boundary}"),
    )
    with pytest.raises(KeyboardInterrupt):
        atomic_replace_bundle(first_items, transaction_id=first_attempt)

    second_attempt = str(uuid.uuid4())
    second_items = (
        AtomicBundleItem(destinations[0], b"newer timeline"),
        AtomicBundleItem(
            destinations[1],
            b'{"newer": true}\n',
            validate=_valid_json,
        ),
    )
    receipt = atomic_replace_bundle(second_items, transaction_id=second_attempt)

    assert receipt.transaction_id == second_attempt
    assert receipt.replayed is False
    assert destinations[0].read_bytes() == b"newer timeline"
    assert destinations[1].read_bytes() == b'{"newer": true}\n'
    assert _journal_paths(tmp_path) == []
    assert _stage_paths(tmp_path) == []


@pytest.mark.parametrize(
    ("validation_error", "expected_code"),
    [
        (
            FCPMCPError(ErrorCode.VALIDATION_FAILED, "rejected recovered pair"),
            ErrorCode.VALIDATION_FAILED,
        ),
        (ValueError("rejected recovered pair"), ErrorCode.TRANSACTION_FAILED),
    ],
    ids=["domain-error", "non-domain-error"],
)
def test_recovered_prepared_pair_validation_restores_exact_prior_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    validation_error: Exception,
    expected_code: ErrorCode,
) -> None:
    """Catch omitted rollback or error normalization after recovered validation."""
    hot = _interrupt_hot_bundle(tmp_path, monkeypatch, boundary="before_terminal")

    def reject_committed(path: Path) -> None:
        if path == hot.destinations[0]:
            raise validation_error

    requested = (
        AtomicBundleItem(hot.destinations[0], hot.items[0].payload, reject_committed),
        hot.items[1],
    )
    with pytest.raises(FCPMCPError) as caught:
        atomic_replace_bundle(requested, transaction_id=hot.attempt)

    assert caught.value.code is expected_code
    assert _observed_pair(hot) == _PRIOR
    assert _journal_paths(tmp_path) == []
    assert _stage_paths(tmp_path) == []
    assert not list(tmp_path.glob(f"*.bak.{hot.attempt}"))
    assert _receipt_paths(tmp_path) == []


def test_committed_hot_journal_validation_failure_preserves_terminal_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch rollback of terminal evidence after committed-pair validation fails."""
    hot = _interrupt_hot_bundle(tmp_path, monkeypatch, committed=True)
    assert json.loads(hot.journal.read_text(encoding="utf-8"))["state"] == "committed"

    def reject_committed(path: Path) -> None:
        if path == hot.destinations[0]:
            raise ValueError("terminal output rejected")

    requested = (
        AtomicBundleItem(hot.destinations[0], hot.items[0].payload, reject_committed),
        hot.items[1],
    )
    evidence = set(tmp_path.iterdir())
    with pytest.raises(FCPMCPError) as caught:
        atomic_replace_bundle(requested, transaction_id=hot.attempt)

    assert caught.value.code is ErrorCode.RECOVERY_REQUIRED
    assert _observed_pair(hot) == tuple(item.payload for item in hot.items)
    assert all(path.exists() for path in evidence)


@pytest.mark.parametrize(
    ("source", "damage"),
    [
        ("hot", "none"),
        ("hot", "size"),
        ("replay", "state"),
        ("replay", "size"),
    ],
)
def test_terminal_receipt_evidence_must_match_committed_pair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    damage: str,
) -> None:
    """Catch replay from absent, false-state, or size-contradictory evidence."""
    if source == "hot":
        hot = _interrupt_hot_bundle(tmp_path, monkeypatch, boundary="before_terminal")
        payload = json.loads(hot.journal.read_text(encoding="utf-8"))
        payload["state"] = "committed"
        key = hot.journal.name.split(".")[3]
        receipt = tmp_path / f".fcp-mcp.bundle.{key}.{hot.attempt}.receipt.json"
    else:
        attempt, items = _bundle(tmp_path)
        atomic_replace_bundle(items, transaction_id=attempt)
        receipt = _receipt_paths(tmp_path)[0]
        payload = json.loads(receipt.read_text(encoding="utf-8"))
        hot = _HotBundle(
            attempt,
            items,
            receipt,
        )
    if damage == "state":
        payload["state"] = "rolled_back"
    elif damage == "size":
        payload["items"][0]["output_size_bytes"] += 1
    _write_checksummed_journal(receipt, payload)
    receipt.chmod(0o600)
    descriptors = _record_journal_descriptors(monkeypatch, hot.journal)

    if damage == "none":
        validated: list[Path] = []
        requested = tuple(
            AtomicBundleItem(item.destination, item.payload, validated.append) for item in hot.items
        )
        replay = atomic_replace_bundle(requested, transaction_id=hot.attempt)
        assert replay.replayed is True
        assert validated == list(hot.destinations)
        assert _journal_paths(tmp_path) == []
    else:
        backups = set(tmp_path.glob(f"*.bak.{hot.attempt}"))
        with pytest.raises(FCPMCPError) as caught:
            atomic_replace_bundle(hot.items, transaction_id=hot.attempt)
        assert caught.value.code is ErrorCode.RECOVERY_REQUIRED
        assert hot.journal.exists()
        assert receipt.exists()
        assert source == "replay" or all(path.exists() for path in backups)
    assert _observed_pair(hot) == tuple(item.payload for item in hot.items)
    _assert_descriptors_closed(descriptors)


def test_repeated_identical_bundle_is_a_noop_with_same_transaction_identity(
    tmp_path: Path,
) -> None:
    attempt, items = _bundle(tmp_path)

    first = atomic_replace_bundle(items, transaction_id=attempt)
    backup_snapshot = sorted(tmp_path.glob("*.bak.*"))
    second = atomic_replace_bundle(items, transaction_id=attempt)

    assert first.transaction_id == second.transaction_id == attempt
    assert second.replayed is True
    assert sorted(tmp_path.glob("*.bak.*")) == backup_snapshot
    assert _journal_paths(tmp_path) == []


@pytest.mark.parametrize("damage", ["deleted", "tampered"])
def test_identical_replay_requires_immutable_prior_receipt_evidence(
    tmp_path: Path,
    damage: str,
) -> None:
    attempt, items = _bundle(tmp_path)
    destinations = tuple(Path(item.destination) for item in items)
    destinations[0].write_bytes(b"old timeline")
    destinations[1].write_bytes(b'{"old": true}\n')
    first = atomic_replace_bundle(items, transaction_id=attempt)
    backup = first.items[0].backup_path
    assert backup is not None
    if damage == "deleted":
        backup.unlink()
    else:
        backup.write_bytes(b"tampered prior evidence")

    with pytest.raises(FCPMCPError) as caught:
        atomic_replace_bundle(items, transaction_id=attempt)

    assert caught.value.code is ErrorCode.RECOVERY_REQUIRED
    assert [destination.read_bytes() for destination in destinations] == [
        b"new timeline",
        b'{"new": true}\n',
    ]


def test_identical_retry_refuses_unjournaled_attempt_evidence(
    tmp_path: Path,
) -> None:
    attempt, items = _bundle(tmp_path)
    first = atomic_replace_bundle(items, transaction_id=attempt)
    stale_stage = tmp_path / f".{Path(items[0].destination).name}.{attempt}.stage"
    stale_stage.write_bytes(items[0].payload)

    with pytest.raises(FCPMCPError) as caught:
        atomic_replace_bundle(items, transaction_id=attempt)

    assert caught.value.code is ErrorCode.RECOVERY_REQUIRED
    assert stale_stage.read_bytes() == items[0].payload
    assert [Path(item.destination).read_bytes() for item in items] == [
        b"new timeline",
        b'{"new": true}\n',
    ]
    assert first.transaction_id == attempt


def test_conflicting_retry_refuses_another_attempts_orphan_stage(
    tmp_path: Path,
) -> None:
    attempt, items = _bundle(tmp_path)
    atomic_replace_bundle(items, transaction_id=attempt)
    orphan_attempt = str(uuid.uuid4())
    orphan_stage = tmp_path / f".{Path(items[0].destination).name}.{orphan_attempt}.stage"
    orphan_stage.write_bytes(items[0].payload)

    with pytest.raises(FCPMCPError) as caught:
        atomic_replace_bundle(items, transaction_id=attempt)

    assert caught.value.code is ErrorCode.RECOVERY_REQUIRED
    assert orphan_stage.read_bytes() == items[0].payload
    assert [Path(item.destination).read_bytes() for item in items] == [
        b"new timeline",
        b'{"new": true}\n',
    ]


def test_live_bundle_writer_is_not_recovered_as_abandoned(tmp_path: Path) -> None:
    attempt, items = _bundle(tmp_path)
    entered_validation = threading.Event()
    release_validation = threading.Event()

    def blocking_validation(path: Path) -> None:
        entered_validation.set()
        assert release_validation.wait(timeout=5)

    blocking_items = (
        AtomicBundleItem(items[0].destination, items[0].payload, blocking_validation),
        items[1],
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(
            atomic_replace_bundle,
            blocking_items,
            transaction_id=attempt,
        )
        assert entered_validation.wait(timeout=5)
        with pytest.raises(FCPMCPError) as caught:
            atomic_replace_bundle(
                items,
                transaction_id=str(uuid.uuid4()),
            )
        assert caught.value.code is ErrorCode.RECOVERY_REQUIRED
        release_validation.set()
        assert pending.result(timeout=5).transaction_id == attempt


@pytest.mark.parametrize("damage", ["malformed_journal", "missing_backup"])
def test_ambiguous_recovery_preserves_observed_destinations_and_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    damage: str,
) -> None:
    attempt, items = _bundle(tmp_path)
    destinations = tuple(item.destination for item in items)
    destinations[0].write_bytes(b"old timeline")
    destinations[1].write_bytes(b'{"old": true}\n')
    _inject_boundary_failure(
        monkeypatch,
        destinations,
        boundary="between",
        error=KeyboardInterrupt("interrupted between replacements"),
    )
    with pytest.raises(KeyboardInterrupt):
        atomic_replace_bundle(items, transaction_id=attempt)

    journal = _journal_paths(tmp_path)[0]
    if damage == "malformed_journal":
        journal.write_bytes(b"not-json")
    else:
        journal_payload = json.loads(journal.read_text(encoding="utf-8"))
        backup_name = journal_payload["items"][0]["backup_name"]
        assert isinstance(backup_name, str)
        (tmp_path / backup_name).unlink()
    observed = tuple(
        destination.read_bytes() if destination.exists() else None for destination in destinations
    )

    with pytest.raises(FCPMCPError) as caught:
        atomic_replace_bundle(items, transaction_id=attempt)

    assert caught.value.code is ErrorCode.RECOVERY_REQUIRED
    assert (
        tuple(
            destination.read_bytes() if destination.exists() else None
            for destination in destinations
        )
        == observed
    )
    assert journal.exists()


def test_hot_journal_growth_during_read_preserves_pair_and_recovery_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch removal of descriptor-bound journal size and stability checks."""
    hot = _interrupt_hot_bundle(tmp_path, monkeypatch)
    evidence = set(tmp_path.iterdir())
    _inject_growth_during_journal_read(monkeypatch, hot.journal)
    descriptors = _record_journal_descriptors(monkeypatch, hot.journal)

    with pytest.raises(FCPMCPError) as caught:
        atomic_replace_bundle(hot.items, transaction_id=hot.attempt)

    assert caught.value.code is ErrorCode.RECOVERY_REQUIRED
    assert _observed_pair(hot) == _PRIOR
    assert hot.journal.stat().st_size > 65536
    assert all(path.exists() for path in evidence)
    _assert_descriptors_closed(descriptors)


@pytest.mark.parametrize(
    "damage",
    [
        "unknown_destination",
        "root_not_object",
        "contract",
        "item_not_object",
        "duplicate_destination",
        "stage_identity",
        "backup_identity",
        "absent_prior_evidence",
    ],
)
def test_checksummed_malformed_hot_journal_never_mutates_observed_pair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    damage: str,
) -> None:
    """Catch guesses through unknown state or malformed checksummed evidence."""
    hot = _interrupt_hot_bundle(tmp_path, monkeypatch)
    if damage == "unknown_destination":
        hot.destinations[0].write_bytes(b"foreign timeline state")
    elif damage == "root_not_object":
        hot.journal.write_text("[]\n", encoding="utf-8")
    else:
        body = json.loads(hot.journal.read_text(encoding="utf-8"))
        item = body["items"][0]
        target, key, value = {
            "contract": (body, "schema", "foreign.schema/v1"),
            "item_not_object": (body["items"], 0, "not-an-object"),
            "duplicate_destination": (
                body["items"][1],
                "destination_name",
                item["destination_name"],
            ),
            "stage_identity": (item, "stage_name", ".foreign.stage"),
            "backup_identity": (item, "backup_name", "foreign.backup"),
            "absent_prior_evidence": (item, "prior_exists", False),
        }[damage]
        target[key] = value
        _write_checksummed_journal(hot.journal, body)
    observed = _observed_pair(hot)
    evidence = set(tmp_path.iterdir())

    with pytest.raises(FCPMCPError) as caught:
        atomic_replace_bundle(hot.items, transaction_id=hot.attempt)

    assert caught.value.code is ErrorCode.RECOVERY_REQUIRED
    assert _observed_pair(hot) == observed
    assert all(path.exists() for path in evidence)


@pytest.mark.parametrize(
    "invariant",
    ["wrong_mode", "hard_link", "path_replacement", "symlink", "premature_eof"],
)
def test_hot_journal_descriptor_invariants_fail_closed_and_release_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invariant: str,
) -> None:
    """Catch removal of descriptor identity, trust, bounded-read, or close checks."""
    hot = _interrupt_hot_bundle(tmp_path, monkeypatch)
    original_bytes = hot.journal.read_bytes()
    after_open: Callable[[], None] | None = None
    if invariant == "wrong_mode":
        hot.journal.chmod(0o640)
    elif invariant == "hard_link":
        os.link(hot.journal, hot.journal.with_suffix(".link"))
    elif invariant in {"path_replacement", "symlink"}:
        target = hot.journal.with_suffix(".target")
        if invariant == "symlink":
            hot.journal.replace(target)
            hot.journal.symlink_to(target.name)
            original_lstat = Path.lstat
            monkeypatch.setattr(
                Path,
                "lstat",
                lambda path: path.stat() if path == hot.journal else original_lstat(path),
            )
        else:

            def replace_path() -> None:
                hot.journal.replace(target)
                hot.journal.write_bytes(original_bytes)
                hot.journal.chmod(0o600)

            after_open = replace_path

    descriptors = _record_journal_descriptors(
        monkeypatch,
        hot.journal,
        after_open=after_open,
    )
    if invariant == "premature_eof":
        with hot.journal.open("ab") as stream:
            stream.write(b" ")
        assert stat.S_IMODE(hot.journal.stat().st_mode) == 0o600
        journal_identity = hot.journal.stat()
        original_read = os.read
        returned_partial = False
        injected = False

        def short_read(descriptor: int, size: int) -> bytes:
            nonlocal injected, returned_partial
            descriptor_identity = os.fstat(descriptor)
            if (
                descriptor_identity.st_dev != journal_identity.st_dev
                or descriptor_identity.st_ino != journal_identity.st_ino
            ):
                return original_read(descriptor, size)
            if returned_partial:
                return b""
            returned_partial = True
            raw = original_read(descriptor, size)
            assert raw.endswith(b" ")
            injected = True
            return raw[:-1]

        monkeypatch.setattr(atomic_write_module.os, "read", short_read)
    observed = _observed_pair(hot)
    evidence = set(tmp_path.iterdir())

    with pytest.raises(FCPMCPError) as caught:
        atomic_replace_bundle(hot.items, transaction_id=hot.attempt)

    assert caught.value.code is ErrorCode.RECOVERY_REQUIRED
    assert _observed_pair(hot) == observed
    assert all(path.exists() for path in evidence)
    if invariant == "symlink":
        assert descriptors == []
    else:
        _assert_descriptors_closed(descriptors)
    if invariant == "premature_eof":
        assert injected is True


def test_rolled_back_terminal_journal_never_reclassifies_outputs_as_committed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempt, items = _bundle(tmp_path)
    destinations = tuple(Path(item.destination) for item in items)
    destinations[0].write_bytes(b"old timeline")
    destinations[1].write_bytes(b'{"old": true}\n')
    _inject_boundary_failure(
        monkeypatch,
        destinations,
        boundary="between",
        error=KeyboardInterrupt("interrupted between replacements"),
    )
    with pytest.raises(KeyboardInterrupt):
        atomic_replace_bundle(items, transaction_id=attempt)

    journal = _journal_paths(tmp_path)[0]
    journal_payload = json.loads(journal.read_text(encoding="utf-8"))
    journal_payload["state"] = "rolled_back"
    _write_checksummed_journal(journal, journal_payload)
    for item in items:
        Path(item.destination).write_bytes(item.payload)

    with pytest.raises(FCPMCPError) as caught:
        atomic_replace_bundle(items, transaction_id=attempt)

    assert caught.value.code is ErrorCode.RECOVERY_REQUIRED
    assert [destination.read_bytes() for destination in destinations] == [
        b"new timeline",
        b'{"new": true}\n',
    ]
    assert journal.exists()


def test_valid_json_journal_tampering_fails_closed_without_restarting_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempt, items = _bundle(tmp_path)
    destinations = tuple(Path(item.destination) for item in items)
    destinations[0].write_bytes(b"old timeline")
    destinations[1].write_bytes(b'{"old": true}\n')
    _inject_boundary_failure(
        monkeypatch,
        destinations,
        boundary="before_first",
        error=KeyboardInterrupt("interrupted before first replacement"),
    )
    with pytest.raises(KeyboardInterrupt):
        atomic_replace_bundle(items, transaction_id=attempt)

    journal = _journal_paths(tmp_path)[0]
    journal_payload = json.loads(journal.read_text(encoding="utf-8"))
    journal_payload["state"] = "rolled_back"
    journal.write_text(json.dumps(journal_payload), encoding="utf-8")

    with pytest.raises(FCPMCPError) as caught:
        atomic_replace_bundle(items, transaction_id=attempt)

    assert caught.value.code is ErrorCode.RECOVERY_REQUIRED
    assert destinations[0].read_bytes() == b"old timeline"
    assert destinations[1].read_bytes() == b'{"old": true}\n'
    assert journal.exists()


def test_bundle_rejects_collisions_symlinks_nonregular_files_and_other_parents(
    tmp_path: Path,
) -> None:
    attempt = str(uuid.uuid4())
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    target = tmp_path / "target.txt"
    target.write_bytes(b"target")
    second.symlink_to(target)

    invalid_bundles = (
        (AtomicBundleItem(first, b"one"), AtomicBundleItem(first, b"two")),
        (AtomicBundleItem(first, b"one"), AtomicBundleItem(second, b"two")),
        (
            AtomicBundleItem(first, b"one"),
            AtomicBundleItem(tmp_path.parent / "outside.txt", b"two"),
        ),
    )
    for items in invalid_bundles:
        with pytest.raises(FCPMCPError) as caught:
            atomic_replace_bundle(items, transaction_id=attempt)
        assert caught.value.code is ErrorCode.INVALID_PATH

    second.unlink()
    second.mkdir()
    with pytest.raises(FCPMCPError) as caught:
        atomic_replace_bundle(
            (AtomicBundleItem(first, b"one"), AtomicBundleItem(second, b"two")),
            transaction_id=attempt,
        )
    assert caught.value.code is ErrorCode.INVALID_PATH


def test_bundle_receipt_hashes_normal_create_and_overwrite(tmp_path: Path) -> None:
    first_attempt, first_items = _bundle(tmp_path)
    created = atomic_replace_bundle(first_items, transaction_id=first_attempt)

    assert created.replayed is False
    assert [item.output_sha256 for item in created.items] == [
        _sha256_bytes(b"new timeline"),
        _sha256_bytes(b'{"new": true}\n'),
    ]
    assert [item.prior_sha256 for item in created.items] == [None, None]
    assert [item.output_size_bytes for item in created.items] == [
        len(b"new timeline"),
        len(b'{"new": true}\n'),
    ]

    second_attempt, second_items = _bundle(
        tmp_path,
        transaction_id=str(uuid.uuid4()),
    )
    second_items = (
        AtomicBundleItem(second_items[0].destination, b"newer timeline"),
        AtomicBundleItem(
            second_items[1].destination,
            b'{"newer": true}\n',
            validate=_valid_json,
        ),
    )
    overwritten = atomic_replace_bundle(second_items, transaction_id=second_attempt)

    assert [item.prior_sha256 for item in overwritten.items] == [
        _sha256_bytes(b"new timeline"),
        _sha256_bytes(b'{"new": true}\n'),
    ]
    assert all(item.backup_path is not None for item in overwritten.items)
    assert [item.backup_path.read_bytes() for item in overwritten.items] == [
        b"new timeline",
        b'{"new": true}\n',
    ]
    terminal_receipts = list(tmp_path.glob(".fcp-mcp.bundle.*.receipt.json"))
    assert len(terminal_receipts) == 2
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in terminal_receipts)
