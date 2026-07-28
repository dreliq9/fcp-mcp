from __future__ import annotations

import errno
import hashlib
import os
import stat
import subprocess
import sys
import threading
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

import fcp_mcp.workflow.artifacts as artifact_module
from fcp_mcp.config import RuntimeConfig
from fcp_mcp.contracts import ErrorCode, FCPMCPError
from fcp_mcp.workflow.artifacts import (
    ArtifactKind,
    ArtifactMetadataV1,
    ArtifactStore,
    StatePaths,
    enforce_source_size,
    render_diff_summary,
)

RUN_ID = "12345678-1234-4234-9234-123456789abc"
OTHER_RUN_ID = "87654321-4321-4321-8321-cba987654321"
LOCK_KEY = "a" * 64


def _config(tmp_path: Path) -> RuntimeConfig:
    return RuntimeConfig.from_env(
        {"FCP_MCP_STATE_DIR": str(tmp_path / "state")},
        home=tmp_path,
    )


def _store(tmp_path: Path, *, limit: int = 1024) -> ArtifactStore:
    paths = StatePaths.from_config(_config(tmp_path))
    return ArtifactStore(paths, max_artifact_bytes=limit)


def _store_with_replaceable_ancestors(
    tmp_path: Path,
    *,
    limit: int = 1024,
) -> ArtifactStore:
    root = tmp_path / "higher-ancestor" / "state-parent" / "state"
    config = RuntimeConfig.from_env(
        {"FCP_MCP_STATE_DIR": str(root)},
        home=tmp_path,
    )
    return ArtifactStore(
        StatePaths.from_config(config),
        max_artifact_bytes=limit,
    )


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def _assert_code(error: pytest.ExceptionInfo[FCPMCPError], code: ErrorCode) -> None:
    assert error.value.code is code


def _replace_canonical_artifact_chain(
    store: ArtifactStore,
    level: str,
) -> Path:
    root = store.paths.root
    artifacts = store.paths.artifacts
    run_directory = store.paths.run_dir(RUN_ID)
    if level == "run":
        detached = artifacts / f"{RUN_ID}.detached"
        run_directory.rename(detached)
        run_directory.mkdir(mode=0o700)
        return detached
    if level == "artifacts":
        detached = root / "artifacts.detached"
        artifacts.rename(detached)
        artifacts.mkdir(mode=0o700)
        run_directory.mkdir(mode=0o700)
        return detached / RUN_ID
    if level == "root":
        detached = root.with_name("state.detached")
        root.rename(detached)
        root.mkdir(mode=0o700)
        artifacts.mkdir(mode=0o700)
        store.paths.locks.mkdir(mode=0o700)
        run_directory.mkdir(mode=0o700)
        return detached / "artifacts" / RUN_ID
    raise AssertionError(f"unknown chain level: {level}")


def _replace_canonical_ancestor(
    store: ArtifactStore,
    ancestor: Path,
) -> Path:
    run_directory = store.paths.run_dir(RUN_ID)
    relative_run = run_directory.relative_to(ancestor)
    detached = ancestor.with_name(f"{ancestor.name}.detached")
    ancestor.rename(detached)
    store.paths.root.mkdir(mode=0o700, parents=True)
    os.chmod(store.paths.root, 0o700)
    store.paths.artifacts.mkdir(mode=0o700)
    store.paths.locks.mkdir(mode=0o700)
    run_directory.mkdir(mode=0o700)
    return detached / relative_run


def test_artifact_store_contains_only_the_macos_descriptor_implementation():
    source = Path(artifact_module.__file__).read_text(encoding="utf-8")
    for marker in (
        "WinDLL",
        "msvcrt",
        "_windows_",
        "_fallback_",
        "FILE_SHARE_",
        "reparse",
        'os.name == "nt"',
    ):
        assert marker not in source


def test_state_paths_create_private_layout_without_creating_database(tmp_path: Path):
    paths = StatePaths.from_config(_config(tmp_path))

    assert paths.root == tmp_path / "state"
    assert paths.database == tmp_path / "state" / "runs.sqlite3"
    assert paths.artifacts == tmp_path / "state" / "artifacts"
    assert paths.locks == tmp_path / "state" / "locks"
    assert paths.run_dir(RUN_ID) == tmp_path / "state" / "artifacts" / RUN_ID
    assert paths.artifact_path(RUN_ID, ArtifactKind.CANDIDATE).name == "candidate.fcpxml"
    assert paths.artifact_path(RUN_ID, ArtifactKind.DIFF).name == "diff.json"
    assert paths.lock_path(LOCK_KEY).name == f"{LOCK_KEY}.lock"
    assert paths.root.is_dir()
    assert paths.artifacts.is_dir()
    assert paths.locks.is_dir()
    assert not paths.database.exists()


def test_state_paths_reject_noncanonical_derived_children(tmp_path: Path):
    root = tmp_path / "state"
    with pytest.raises(ValidationError):
        StatePaths(
            root=root,
            database=root / "wrong.sqlite3",
            artifacts=tmp_path / "outside-artifacts",
            locks=root / "locks",
        )


def test_state_paths_reject_lexically_noncanonical_root(tmp_path: Path):
    root = tmp_path / "safe" / ".." / "outside"
    with pytest.raises(ValidationError):
        StatePaths(
            root=root,
            database=root / "runs.sqlite3",
            artifacts=root / "artifacts",
            locks=root / "locks",
        )


def test_from_config_rejects_noncanonical_root_before_any_mutation(tmp_path: Path):
    canonical = tmp_path / "outside"
    lexical = tmp_path / "safe" / ".." / "outside"
    config = replace(_config(tmp_path), state_dir=lexical)

    with pytest.raises(FCPMCPError) as error:
        StatePaths.from_config(config)

    _assert_code(error, ErrorCode.INVALID_CONFIGURATION)
    assert not canonical.exists()
    assert not (tmp_path / "safe").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX modes are not portable")
@pytest.mark.parametrize("process_umask", [0o000, 0o077])
def test_created_directories_and_files_have_exact_private_modes(
    tmp_path: Path,
    process_umask: int,
):
    previous = os.umask(process_umask)
    try:
        store = _store(tmp_path)
        metadata = store.write(RUN_ID, ArtifactKind.CANDIDATE, b"candidate")
    finally:
        os.umask(previous)

    assert _mode(store.paths.root) == 0o700
    assert _mode(store.paths.artifacts) == 0o700
    assert _mode(store.paths.locks) == 0o700
    assert _mode(store.paths.run_dir(RUN_ID)) == 0o700
    assert _mode(store.paths.root / metadata.relative_path) == 0o600


@pytest.mark.skipif(os.name != "posix", reason="POSIX modes are not portable")
def test_unsafe_existing_root_mode_is_rejected_without_chmod(tmp_path: Path):
    root = tmp_path / "state"
    root.mkdir(mode=0o755)
    os.chmod(root, 0o755)

    with pytest.raises(FCPMCPError) as error:
        StatePaths.from_config(_config(tmp_path))

    _assert_code(error, ErrorCode.INVALID_CONFIGURATION)
    assert _mode(root) == 0o755


def test_state_root_symlink_is_rejected(tmp_path: Path):
    target = tmp_path / "real-state"
    target.mkdir(mode=0o700)
    link = tmp_path / "state"
    link.symlink_to(target, target_is_directory=True)

    with pytest.raises(FCPMCPError) as error:
        StatePaths.from_config(_config(tmp_path))

    _assert_code(error, ErrorCode.INVALID_CONFIGURATION)


@pytest.mark.parametrize("name", ["artifacts", "locks"])
def test_contained_directory_symlink_is_rejected(tmp_path: Path, name: str):
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    target = tmp_path / f"real-{name}"
    target.mkdir(mode=0o700)
    (root / name).symlink_to(target, target_is_directory=True)

    with pytest.raises(FCPMCPError) as error:
        StatePaths.from_config(_config(tmp_path))

    _assert_code(error, ErrorCode.INVALID_CONFIGURATION)


def test_database_symlink_is_rejected(tmp_path: Path):
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    target = tmp_path / "database"
    target.write_bytes(b"")
    (root / "runs.sqlite3").symlink_to(target)

    with pytest.raises(FCPMCPError) as error:
        StatePaths.from_config(_config(tmp_path))

    _assert_code(error, ErrorCode.INVALID_CONFIGURATION)


@pytest.mark.parametrize(
    ("name", "make_wrong_type"),
    [
        ("runs.sqlite3", lambda path: path.mkdir()),
        ("artifacts", lambda path: path.write_bytes(b"x")),
        ("locks", lambda path: path.write_bytes(b"x")),
    ],
)
def test_wrong_top_level_filesystem_types_are_rejected(
    tmp_path: Path,
    name: str,
    make_wrong_type,
):
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    make_wrong_type(root / name)

    with pytest.raises(FCPMCPError) as error:
        StatePaths.from_config(_config(tmp_path))

    _assert_code(error, ErrorCode.INVALID_CONFIGURATION)


def test_run_directory_symlink_is_rejected_before_write(tmp_path: Path):
    store = _store(tmp_path)
    target = tmp_path / "outside"
    target.mkdir(mode=0o700)
    store.paths.run_dir(RUN_ID).symlink_to(target, target_is_directory=True)

    with pytest.raises(FCPMCPError) as error:
        store.write(RUN_ID, ArtifactKind.CANDIDATE, b"candidate")

    _assert_code(error, ErrorCode.ARTIFACT_CORRUPT)
    assert list(target.iterdir()) == []


@pytest.mark.parametrize("kind", list(ArtifactKind))
def test_existing_artifact_symlink_is_rejected_before_write(
    tmp_path: Path,
    kind: ArtifactKind,
):
    store = _store(tmp_path)
    run_dir = store.paths.run_dir(RUN_ID)
    run_dir.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.write_bytes(b"unchanged")
    store.paths.artifact_path(RUN_ID, kind).symlink_to(outside)

    with pytest.raises(FCPMCPError) as error:
        store.write(RUN_ID, kind, b"replacement")

    _assert_code(error, ErrorCode.ARTIFACT_CORRUPT)
    assert outside.read_bytes() == b"unchanged"


def test_existing_lock_symlink_is_rejected(tmp_path: Path):
    paths = StatePaths.from_config(_config(tmp_path))
    outside = tmp_path / "outside.lock"
    outside.write_bytes(b"")
    (paths.locks / f"{LOCK_KEY}.lock").symlink_to(outside)

    with pytest.raises(FCPMCPError) as error:
        paths.lock_path(LOCK_KEY)

    _assert_code(error, ErrorCode.ARTIFACT_CORRUPT)


@pytest.mark.parametrize(
    "run_id",
    [
        "",
        "not-a-uuid",
        "../12345678-1234-4234-9234-123456789abc",
        "12345678-1234-4234-9234-123456789ABC",
        "{12345678-1234-4234-9234-123456789abc}",
        "urn:uuid:12345678-1234-4234-9234-123456789abc",
        "00000000-0000-0000-0000-000000000000",
        "12345678/1234-4234-9234-123456789abc",
    ],
)
def test_noncanonical_run_ids_are_rejected(tmp_path: Path, run_id: str):
    paths = StatePaths.from_config(_config(tmp_path))

    with pytest.raises(FCPMCPError) as error:
        paths.run_dir(run_id)

    _assert_code(error, ErrorCode.INVALID_ARGUMENTS)


@pytest.mark.parametrize(
    "lock_key",
    ["", "a" * 63, "a" * 65, "A" * 64, "g" * 64, "../" + "a" * 64],
)
def test_noncanonical_lock_keys_are_rejected(tmp_path: Path, lock_key: str):
    paths = StatePaths.from_config(_config(tmp_path))

    with pytest.raises(FCPMCPError) as error:
        paths.lock_path(lock_key)

    _assert_code(error, ErrorCode.INVALID_ARGUMENTS)


def test_receipt_is_a_first_class_fixed_private_artifact(tmp_path: Path):
    paths = StatePaths.from_config(_config(tmp_path))

    assert paths.artifact_path(RUN_ID, ArtifactKind.RECEIPT) == (
        paths.artifacts / RUN_ID / "receipt.json"
    )
    store = _store(tmp_path)
    metadata = store.write(RUN_ID, ArtifactKind.RECEIPT, b'{"receipt":"exact"}')
    assert metadata.relative_path == f"artifacts/{RUN_ID}/receipt.json"
    assert _mode(paths.root / metadata.relative_path) == 0o600
    assert store.read(metadata) == b'{"receipt":"exact"}'


def test_artifact_kind_remains_closed_after_receipt(tmp_path: Path):
    paths = StatePaths.from_config(_config(tmp_path))

    with pytest.raises(FCPMCPError) as error:
        paths.artifact_path(RUN_ID, "unknown")  # type: ignore[arg-type]

    _assert_code(error, ErrorCode.INVALID_ARGUMENTS)


def test_write_returns_immutable_serializable_exact_metadata(tmp_path: Path):
    store = _store(tmp_path)
    mutable_payload = bytearray(b"candidate")

    metadata = store.write(RUN_ID, ArtifactKind.CANDIDATE, mutable_payload)
    mutable_payload[:] = b"XXXXXXXXX"

    target = store.paths.root / metadata.relative_path
    assert target.read_bytes() == b"candidate"
    assert metadata.schema_version == "1"
    assert metadata.run_id == RUN_ID
    assert metadata.kind is ArtifactKind.CANDIDATE
    assert metadata.relative_path == f"artifacts/{RUN_ID}/candidate.fcpxml"
    assert metadata.sha256 == hashlib.sha256(b"candidate").hexdigest()
    assert metadata.byte_size == 9
    assert metadata.created_at.tzinfo is not None
    assert metadata.created_at.utcoffset() == timezone.utc.utcoffset(metadata.created_at)
    assert ArtifactMetadataV1.model_validate_json(metadata.model_dump_json()) == metadata
    with pytest.raises(ValidationError):
        metadata.byte_size = 10  # type: ignore[misc]


@pytest.mark.parametrize("payload", ["text", 1, object()])
def test_write_rejects_non_bytes_like_payload_before_creating_run(
    tmp_path: Path,
    payload: object,
):
    store = _store(tmp_path)

    with pytest.raises(FCPMCPError) as error:
        store.write(RUN_ID, ArtifactKind.CANDIDATE, payload)  # type: ignore[arg-type]

    _assert_code(error, ErrorCode.INVALID_ARGUMENTS)
    assert not store.paths.run_dir(RUN_ID).exists()


def test_write_uses_exclusive_sibling_temp_and_leaves_no_temp_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    store = _store(tmp_path)
    real_open = os.open
    observed: list[tuple[str | bytes | os.PathLike[str] | os.PathLike[bytes], int]] = []

    def recording_open(path, flags, mode=0o777, *, dir_fd=None):
        observed.append((path, flags))
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr("fcp_mcp.workflow.artifacts.os.open", recording_open)
    store.write(RUN_ID, ArtifactKind.CANDIDATE, b"candidate")

    temp_opens = [
        (path, flags)
        for path, flags in observed
        if isinstance(path, str) and path.startswith(".candidate.fcpxml.")
    ]
    assert len(temp_opens) == 1
    assert temp_opens[0][1] & os.O_EXCL
    assert list(store.paths.run_dir(RUN_ID).iterdir()) == [
        store.paths.artifact_path(RUN_ID, ArtifactKind.CANDIDATE)
    ]


def test_short_writes_are_retried_until_exact_payload_is_committed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    store = _store(tmp_path)
    real_write = os.write

    def short_write(fd: int, data: bytes) -> int:
        return real_write(fd, data[:2])

    monkeypatch.setattr("fcp_mcp.workflow.artifacts.os.write", short_write)
    metadata = store.write(RUN_ID, ArtifactKind.CANDIDATE, b"candidate")

    assert store.read(metadata) == b"candidate"


@pytest.mark.parametrize("failure_boundary", ["write", "file_fsync", "replace", "dir_fsync"])
def test_write_failure_boundaries_remove_temp_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_boundary: str,
):
    store = _store(tmp_path)
    run_dir = store.paths.run_dir(RUN_ID)
    run_dir.mkdir(mode=0o700)
    if os.name == "posix":
        os.chmod(run_dir, 0o700)
    real_write = os.write
    real_fsync = os.fsync
    real_replace = os.replace
    write_calls = 0

    def failing_write(fd: int, data: bytes) -> int:
        nonlocal write_calls
        write_calls += 1
        if write_calls == 2:
            raise OSError("injected write failure")
        return real_write(fd, data[:2])

    def failing_fsync(fd: int) -> None:
        result = os.fstat(fd)
        if (failure_boundary == "file_fsync" and stat.S_ISREG(result.st_mode)) or (
            failure_boundary == "dir_fsync" and stat.S_ISDIR(result.st_mode)
        ):
            raise OSError("injected fsync failure")
        real_fsync(fd)

    def failing_replace(*args, **kwargs) -> None:
        raise OSError("injected replace failure")

    if failure_boundary == "write":
        monkeypatch.setattr("fcp_mcp.workflow.artifacts.os.write", failing_write)
    elif failure_boundary in {"file_fsync", "dir_fsync"}:
        monkeypatch.setattr("fcp_mcp.workflow.artifacts.os.fsync", failing_fsync)
    elif failure_boundary == "replace":
        monkeypatch.setattr("fcp_mcp.workflow.artifacts.os.replace", failing_replace)

    with pytest.raises(FCPMCPError):
        store.write(RUN_ID, ArtifactKind.CANDIDATE, b"candidate")

    assert not any(path.name.startswith(".candidate.fcpxml.") for path in run_dir.iterdir())
    if failure_boundary != "dir_fsync":
        assert not store.paths.artifact_path(RUN_ID, ArtifactKind.CANDIDATE).exists()
    monkeypatch.setattr("fcp_mcp.workflow.artifacts.os.write", real_write)
    monkeypatch.setattr("fcp_mcp.workflow.artifacts.os.fsync", real_fsync)
    monkeypatch.setattr("fcp_mcp.workflow.artifacts.os.replace", real_replace)


def test_cleanup_retries_unlink_after_a_transient_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    store = _store(tmp_path)
    real_write = os.write
    real_unlink = os.unlink
    unlink_calls = 0

    def failing_write(fd: int, data: bytes) -> int:
        raise OSError("injected write failure")

    def transient_unlink(path, *, dir_fd=None) -> None:
        nonlocal unlink_calls
        unlink_calls += 1
        if unlink_calls == 1:
            raise OSError("injected cleanup failure")
        real_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr("fcp_mcp.workflow.artifacts.os.write", failing_write)
    monkeypatch.setattr("fcp_mcp.workflow.artifacts.os.unlink", transient_unlink)
    with pytest.raises(FCPMCPError):
        store.write(RUN_ID, ArtifactKind.CANDIDATE, b"candidate")

    assert unlink_calls == 2
    assert list(store.paths.run_dir(RUN_ID).iterdir()) == []
    monkeypatch.setattr("fcp_mcp.workflow.artifacts.os.write", real_write)
    monkeypatch.setattr("fcp_mcp.workflow.artifacts.os.unlink", real_unlink)


def test_replace_failure_preserves_existing_target_and_removes_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    store = _store(tmp_path)
    original = store.write(RUN_ID, ArtifactKind.CANDIDATE, b"old")

    def failing_replace(*args, **kwargs) -> None:
        raise OSError("injected replace failure")

    monkeypatch.setattr("fcp_mcp.workflow.artifacts.os.replace", failing_replace)
    with pytest.raises(FCPMCPError):
        store.write(RUN_ID, ArtifactKind.CANDIDATE, b"new")

    assert store.read(original) == b"old"
    assert list(store.paths.run_dir(RUN_ID).iterdir()) == [
        store.paths.artifact_path(RUN_ID, ArtifactKind.CANDIDATE)
    ]


def test_existing_target_is_atomically_replaced(tmp_path: Path):
    store = _store(tmp_path)
    first = store.write(RUN_ID, ArtifactKind.CANDIDATE, b"old")
    second = store.write(RUN_ID, ArtifactKind.CANDIDATE, b"new-candidate")

    assert store.read(second) == b"new-candidate"
    with pytest.raises(FCPMCPError) as error:
        store.verify(first)
    _assert_code(error, ErrorCode.ARTIFACT_CORRUPT)


def test_write_rejects_target_replaced_during_payload_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    store = _store(tmp_path)
    real_read_opened = artifact_module._read_opened_payload
    target = store.paths.artifact_path(RUN_ID, ArtifactKind.CANDIDATE)
    attacker_payload = b"ATTACKER!"

    def replacing_read(fd: int, expected_size: int) -> bytes:
        payload = real_read_opened(fd, expected_size)
        replacement = target.with_name("attacker")
        replacement.write_bytes(attacker_payload)
        os.chmod(replacement, 0o600)
        os.replace(replacement, target)
        return payload

    monkeypatch.setattr(artifact_module, "_read_opened_payload", replacing_read)

    with pytest.raises(FCPMCPError) as error:
        store.write(RUN_ID, ArtifactKind.CANDIDATE, b"candidate")

    _assert_code(error, ErrorCode.ARTIFACT_CORRUPT)
    assert target.read_bytes() == attacker_payload


@pytest.mark.parametrize("level", ["run", "artifacts", "root"])
def test_write_rejects_canonical_directory_replacement_and_cleans_detached_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    level: str,
):
    store = _store(tmp_path)
    real_read_opened = artifact_module._read_opened_payload
    real_fsync = os.fsync
    detached_run: Path | None = None
    synced_directories: list[tuple[int, int]] = []

    def replacing_read(fd: int, expected_size: int) -> bytes:
        nonlocal detached_run
        if detached_run is None:
            detached_run = _replace_canonical_artifact_chain(store, level)
        return real_read_opened(fd, expected_size)

    def recording_fsync(fd: int) -> None:
        result = os.fstat(fd)
        if stat.S_ISDIR(result.st_mode):
            synced_directories.append((result.st_dev, result.st_ino))
        real_fsync(fd)

    monkeypatch.setattr(artifact_module, "_read_opened_payload", replacing_read)
    monkeypatch.setattr(artifact_module.os, "fsync", recording_fsync)

    with pytest.raises(FCPMCPError) as error:
        store.write(RUN_ID, ArtifactKind.CANDIDATE, b"candidate")

    _assert_code(error, ErrorCode.ARTIFACT_CORRUPT)
    assert detached_run is not None
    detached_result = detached_run.stat()
    detached_identity = (detached_result.st_dev, detached_result.st_ino)
    assert synced_directories.count(detached_identity) >= 2
    assert not (detached_run / "candidate.fcpxml").exists()
    assert not store.paths.artifact_path(RUN_ID, ArtifactKind.CANDIDATE).exists()


@pytest.mark.parametrize("ancestor_level", ["state-parent", "higher-ancestor"])
def test_write_rejects_canonical_ancestor_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ancestor_level: str,
):
    store = _store_with_replaceable_ancestors(tmp_path)
    ancestor = (
        store.paths.root.parent
        if ancestor_level == "state-parent"
        else store.paths.root.parent.parent
    )
    real_open_directory = artifact_module._open_directory_at
    matching_opens = 0
    detached_run: Path | None = None

    def replacing_open(*args, **kwargs):
        nonlocal matching_opens, detached_run
        opened = real_open_directory(*args, **kwargs)
        if args[1] == ancestor.name:
            matching_opens += 1
            if matching_opens == 2:
                detached_run = _replace_canonical_ancestor(store, ancestor)
        return opened

    monkeypatch.setattr(artifact_module, "_open_directory_at", replacing_open)

    with pytest.raises(FCPMCPError) as error:
        store.write(RUN_ID, ArtifactKind.CANDIDATE, b"candidate")

    _assert_code(error, ErrorCode.ARTIFACT_CORRUPT)
    assert detached_run is not None
    assert not (detached_run / "candidate.fcpxml").exists()
    assert not store.paths.artifact_path(RUN_ID, ArtifactKind.CANDIDATE).exists()


def test_swapped_run_inode_cannot_bypass_aggregate_locking_and_accounting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    store = _store(tmp_path, limit=5)
    real_read_opened = artifact_module._read_opened_payload
    detached_run: Path | None = None
    canonical_diff: ArtifactMetadataV1 | None = None

    def replacing_read(fd: int, expected_size: int) -> bytes:
        nonlocal detached_run, canonical_diff
        if detached_run is None:
            detached_run = _replace_canonical_artifact_chain(store, "run")
            canonical_diff = store._write_posix(
                RUN_ID,
                ArtifactKind.DIFF,
                b"123",
            )
        return real_read_opened(fd, expected_size)

    monkeypatch.setattr(artifact_module, "_read_opened_payload", replacing_read)

    with pytest.raises(FCPMCPError) as error:
        store.write(RUN_ID, ArtifactKind.CANDIDATE, b"123")

    _assert_code(error, ErrorCode.ARTIFACT_CORRUPT)
    assert detached_run is not None
    assert not (detached_run / "candidate.fcpxml").exists()
    assert canonical_diff is not None
    assert store.read(canonical_diff) == b"123"
    assert not store.paths.artifact_path(RUN_ID, ArtifactKind.CANDIDATE).exists()


@pytest.mark.parametrize("ancestor_level", ["state-parent", "higher-ancestor"])
def test_swapped_ancestor_cannot_bypass_aggregate_locking_and_accounting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ancestor_level: str,
):
    store = _store_with_replaceable_ancestors(tmp_path, limit=5)
    ancestor = (
        store.paths.root.parent
        if ancestor_level == "state-parent"
        else store.paths.root.parent.parent
    )
    real_open_directory = artifact_module._open_directory_at
    matching_opens = 0
    detached_run: Path | None = None
    canonical_diff: ArtifactMetadataV1 | None = None

    def replacing_open(*args, **kwargs):
        nonlocal matching_opens, detached_run, canonical_diff
        opened = real_open_directory(*args, **kwargs)
        if args[1] == ancestor.name:
            matching_opens += 1
            if matching_opens == 2:
                detached_run = _replace_canonical_ancestor(store, ancestor)
                canonical_diff = store._write_posix(
                    RUN_ID,
                    ArtifactKind.DIFF,
                    b"123",
                )
        return opened

    monkeypatch.setattr(artifact_module, "_open_directory_at", replacing_open)

    with pytest.raises(FCPMCPError) as error:
        store.write(RUN_ID, ArtifactKind.CANDIDATE, b"123")

    _assert_code(error, ErrorCode.ARTIFACT_CORRUPT)
    assert detached_run is not None
    assert not (detached_run / "candidate.fcpxml").exists()
    assert canonical_diff is not None
    assert store.read(canonical_diff) == b"123"
    assert not store.paths.artifact_path(RUN_ID, ArtifactKind.CANDIDATE).exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX cleanup ownership regression")
def test_posix_exclusive_open_failure_does_not_unlink_preexisting_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    store = _store(tmp_path)
    run_directory = store.paths.run_dir(RUN_ID)
    run_directory.mkdir(mode=0o700)
    temp = run_directory / ".candidate.fcpxml.fixed.tmp"
    temp.write_bytes(b"preexisting")
    os.chmod(temp, 0o600)
    monkeypatch.setattr(artifact_module.secrets, "token_hex", lambda size: "fixed")

    with pytest.raises(FCPMCPError):
        store.write(RUN_ID, ArtifactKind.CANDIDATE, b"candidate")

    assert temp.read_bytes() == b"preexisting"


def test_aggregate_artifact_limit_rejects_without_partial_write(tmp_path: Path):
    store = _store(tmp_path, limit=5)
    candidate = store.write(RUN_ID, ArtifactKind.CANDIDATE, b"123")

    with pytest.raises(FCPMCPError) as error:
        store.write(RUN_ID, ArtifactKind.DIFF, b"456")

    _assert_code(error, ErrorCode.INVALID_ARGUMENTS)
    assert store.read(candidate) == b"123"
    assert not store.paths.artifact_path(RUN_ID, ArtifactKind.DIFF).exists()


def test_aggregate_limit_accounts_for_replaced_target_size(tmp_path: Path):
    store = _store(tmp_path, limit=7)
    store.write(RUN_ID, ArtifactKind.CANDIDATE, b"1234")
    diff = store.write(RUN_ID, ArtifactKind.DIFF, b"567")
    candidate = store.write(RUN_ID, ArtifactKind.CANDIDATE, b"12")

    assert store.read(candidate) == b"12"
    assert store.read(diff) == b"567"


@pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor locking regression")
def test_concurrent_writers_serialize_aggregate_limit_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    store = _store(tmp_path, limit=5)
    real_write = os.write
    candidate_entered = threading.Event()
    release_candidate = threading.Event()
    outcomes: list[tuple[str, ErrorCode | None]] = []

    def controlled_write(fd: int, data: bytes) -> int:
        if threading.current_thread().name == "candidate-writer":
            candidate_entered.set()
            assert release_candidate.wait(timeout=5)
        return real_write(fd, data)

    def write_kind(kind: ArtifactKind) -> None:
        try:
            store.write(RUN_ID, kind, b"123")
        except FCPMCPError as error:
            outcomes.append((kind.value, error.code))
        else:
            outcomes.append((kind.value, None))

    monkeypatch.setattr("fcp_mcp.workflow.artifacts.os.write", controlled_write)
    candidate = threading.Thread(
        target=write_kind,
        args=(ArtifactKind.CANDIDATE,),
        name="candidate-writer",
    )
    diff = threading.Thread(
        target=write_kind,
        args=(ArtifactKind.DIFF,),
        name="diff-writer",
    )
    candidate.start()
    assert candidate_entered.wait(timeout=5)
    diff.start()
    time.sleep(0.05)
    diff_was_serialized = diff.is_alive()
    assert len(artifact_module._RUN_LOCKS) == 1
    release_candidate.set()
    candidate.join(timeout=5)
    diff.join(timeout=5)

    assert diff_was_serialized is True
    assert sorted(outcomes) == [
        ("candidate", None),
        ("diff", ErrorCode.INVALID_ARGUMENTS),
    ]
    assert artifact_module._RUN_LOCKS == {}


@pytest.mark.skipif(os.name != "posix", reason="POSIX cross-process flock regression")
def test_cross_process_writers_cannot_both_pass_aggregate_limit(tmp_path: Path):
    state = tmp_path / "state"
    entered = tmp_path / "entered"
    release = tmp_path / "release"
    script = """
import os
import sys
import time
from pathlib import Path

import fcp_mcp.workflow.artifacts as module
from fcp_mcp.config import RuntimeConfig
from fcp_mcp.contracts import FCPMCPError
from fcp_mcp.workflow.artifacts import ArtifactKind, ArtifactStore, StatePaths

state, kind, entered, release, hold = sys.argv[1:]
config = RuntimeConfig.from_env({"FCP_MCP_STATE_DIR": state})
store = ArtifactStore(StatePaths.from_config(config), max_artifact_bytes=5)
if hold == "1":
    real_write = module.os.write
    def held_write(fd, data):
        Path(entered).write_text("entered")
        deadline = time.monotonic() + 5
        while not Path(release).exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        return real_write(fd, data)
    module.os.write = held_write
try:
    store.write(__RUN_ID__, ArtifactKind(kind), b"123")
except FCPMCPError as error:
    print(error.code.value)
    raise SystemExit(2)
""".replace("__RUN_ID__", repr(RUN_ID))
    candidate = subprocess.Popen(
        [
            sys.executable,
            "-c",
            script,
            str(state),
            ArtifactKind.CANDIDATE.value,
            str(entered),
            str(release),
            "1",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 5
    while not entered.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert entered.exists()
    diff = subprocess.Popen(
        [
            sys.executable,
            "-c",
            script,
            str(state),
            ArtifactKind.DIFF.value,
            str(entered),
            str(release),
            "0",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    time.sleep(0.1)
    diff_was_serialized = diff.poll() is None
    release.write_text("release")
    candidate_stdout, candidate_stderr = candidate.communicate(timeout=5)
    diff_stdout, diff_stderr = diff.communicate(timeout=5)

    assert diff_was_serialized is True
    assert candidate.returncode == 0, candidate_stderr
    assert diff.returncode == 2, diff_stderr
    assert diff_stdout.strip() == ErrorCode.INVALID_ARGUMENTS.value
    assert candidate_stdout == ""


@pytest.mark.skipif(os.name != "posix", reason="POSIX lock-release regression")
def test_run_lock_is_released_after_write_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    store = _store(tmp_path)
    real_write = os.write

    def failing_write(fd: int, data: bytes) -> int:
        raise OSError("injected failure")

    monkeypatch.setattr(artifact_module.os, "write", failing_write)
    with pytest.raises(FCPMCPError):
        store.write(RUN_ID, ArtifactKind.CANDIDATE, b"candidate")
    monkeypatch.setattr(artifact_module.os, "write", real_write)

    outcome: list[ArtifactMetadataV1] = []
    retry = threading.Thread(
        target=lambda: outcome.append(
            store.write(RUN_ID, ArtifactKind.CANDIDATE, b"candidate")
        )
    )
    retry.start()
    retry.join(timeout=5)
    assert retry.is_alive() is False
    assert outcome[0].byte_size == 9


@pytest.mark.skipif(os.name != "posix", reason="POSIX cleanup failure regression")
def test_posix_unlock_failure_is_coded_and_still_closes_directory_fds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    import fcntl

    store = _store(tmp_path)
    real_flock = fcntl.flock
    real_close_run = store._close_posix_run
    close_run_called = False

    def failing_unlock(fd: int, operation: int) -> None:
        if operation == fcntl.LOCK_UN:
            raise OSError("injected unlock failure")
        real_flock(fd, operation)

    def recording_close(root_fd: int, artifacts_fd: int, run_fd: int) -> None:
        nonlocal close_run_called
        close_run_called = True
        real_close_run(root_fd, artifacts_fd, run_fd)

    monkeypatch.setattr(fcntl, "flock", failing_unlock)
    monkeypatch.setattr(store, "_close_posix_run", recording_close)

    with pytest.raises(FCPMCPError) as error:
        store.write(RUN_ID, ArtifactKind.CANDIDATE, b"candidate")

    _assert_code(error, ErrorCode.TRANSACTION_FAILED)
    assert close_run_called is True


@pytest.mark.skipif(os.name != "posix", reason="POSIX close-all regression")
def test_close_posix_run_attempts_every_descriptor_after_close_failure(
    monkeypatch: pytest.MonkeyPatch,
):
    descriptors: list[int] = []
    writers: list[int] = []
    for _ in range(3):
        reader, writer = os.pipe()
        descriptors.append(reader)
        writers.append(writer)
    real_close = os.close
    failed_fd = descriptors[2]
    attempted: list[int] = []

    def one_failing_close(fd: int) -> None:
        attempted.append(fd)
        if fd == failed_fd:
            raise OSError("injected close failure")
        real_close(fd)

    monkeypatch.setattr(artifact_module.os, "close", one_failing_close)
    with pytest.raises(FCPMCPError) as error:
        ArtifactStore._close_posix_run(
            descriptors[0],
            descriptors[1],
            descriptors[2],
        )
    monkeypatch.setattr(artifact_module.os, "close", real_close)

    _assert_code(error, ErrorCode.TRANSACTION_FAILED)
    assert set(attempted) == set(descriptors)
    real_close(failed_fd)
    for writer in writers:
        real_close(writer)



@pytest.mark.skipif(os.name != "posix", reason="POSIX partial-open cleanup regression")
def test_open_posix_run_failure_closes_all_acquired_descriptors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    store = _store(tmp_path)
    real_close = os.close
    real_open_directory_at = artifact_module._open_directory_at
    attempted: list[int] = []
    failed_fd: int | None = None

    def first_failing_close(fd: int) -> None:
        nonlocal failed_fd
        attempted.append(fd)
        if failed_fd is None:
            failed_fd = fd
            raise OSError("injected close failure")
        real_close(fd)

    def missing_run(parent_fd: int, name: str, **kwargs):
        if name == RUN_ID:
            monkeypatch.setattr(artifact_module.os, "close", first_failing_close)
            raise FileNotFoundError(name)
        return real_open_directory_at(parent_fd, name, **kwargs)

    monkeypatch.setattr(artifact_module, "_open_directory_at", missing_run)
    with pytest.raises(FCPMCPError) as error:
        store._open_posix_run(RUN_ID, create=False)
    monkeypatch.setattr(artifact_module.os, "close", real_close)

    _assert_code(error, ErrorCode.ARTIFACT_CORRUPT)
    assert len(attempted) == 2
    assert error.value.details["cleanup_errors"]
    assert failed_fd is not None
    real_close(failed_fd)


def test_absolute_chain_parent_close_failure_closes_acquired_child_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    target = tmp_path / "state"
    target.mkdir(mode=0o700)
    real_open = os.open
    real_close = os.close
    opened: list[int] = []
    close_attempts: list[int] = []
    failed_parent: int | None = None

    def recording_open(*args, **kwargs) -> int:
        descriptor = real_open(*args, **kwargs)
        opened.append(descriptor)
        return descriptor

    def failing_parent_close(descriptor: int) -> None:
        nonlocal failed_parent
        close_attempts.append(descriptor)
        if failed_parent is None:
            failed_parent = descriptor
            raise OSError("injected parent close failure")
        real_close(descriptor)

    monkeypatch.setattr(artifact_module.os, "open", recording_open)
    monkeypatch.setattr(artifact_module.os, "close", failing_parent_close)
    with pytest.raises(FCPMCPError) as error:
        artifact_module._open_absolute_directory_chain(
            target,
            create=False,
            code=ErrorCode.INVALID_CONFIGURATION,
            description="workflow state root",
        )
    monkeypatch.setattr(artifact_module.os, "open", real_open)
    monkeypatch.setattr(artifact_module.os, "close", real_close)

    _assert_code(error, ErrorCode.INVALID_CONFIGURATION)
    assert failed_parent is not None
    assert close_attempts.count(failed_parent) == 1
    assert len(opened) == 2
    assert close_attempts.count(opened[1]) == 1
    with pytest.raises(OSError):
        os.fstat(opened[1])
    os.fstat(failed_parent)
    real_close(failed_parent)


def test_directory_validation_and_close_failures_preserve_coded_primary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    parent = tmp_path / "parent"
    parent.mkdir(mode=0o700)
    child = parent / "child"
    child.mkdir(mode=0o755)
    parent_fd = os.open(parent, artifact_module._directory_open_flags())
    real_close = os.close
    child_fd: int | None = None

    def failing_child_close(descriptor: int) -> None:
        nonlocal child_fd
        child_fd = descriptor
        raise OSError("injected child close failure")

    monkeypatch.setattr(artifact_module.os, "close", failing_child_close)
    with pytest.raises(FCPMCPError) as error:
        artifact_module._open_directory_at(
            parent_fd,
            "child",
            create=False,
            require_private_mode=True,
            code=ErrorCode.INVALID_CONFIGURATION,
            description="workflow child directory",
        )
    monkeypatch.setattr(artifact_module.os, "close", real_close)

    _assert_code(error, ErrorCode.INVALID_CONFIGURATION)
    assert child_fd is not None
    assert error.value.details["cleanup_errors"]
    assert str(tmp_path) not in repr(error.value.details["cleanup_errors"])
    assert len(repr(error.value.details["cleanup_errors"])) < 512
    real_close(child_fd)
    real_close(parent_fd)


def test_parent_fsync_and_child_close_failures_preserve_coded_primary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    parent = tmp_path / "parent"
    parent.mkdir(mode=0o700)
    parent_fd = os.open(parent, artifact_module._directory_open_flags())
    real_close = os.close
    child_fd: int | None = None

    def failing_fsync(descriptor: int) -> None:
        assert descriptor == parent_fd
        raise OSError("injected parent fsync failure")

    def failing_child_close(descriptor: int) -> None:
        nonlocal child_fd
        child_fd = descriptor
        raise OSError("injected child close failure")

    monkeypatch.setattr(artifact_module.os, "fsync", failing_fsync)
    monkeypatch.setattr(artifact_module.os, "close", failing_child_close)
    with pytest.raises(FCPMCPError) as error:
        artifact_module._open_directory_at(
            parent_fd,
            "child",
            create=True,
            require_private_mode=True,
            code=ErrorCode.INVALID_CONFIGURATION,
            description="workflow child directory",
        )
    monkeypatch.setattr(artifact_module.os, "close", real_close)

    _assert_code(error, ErrorCode.INVALID_CONFIGURATION)
    assert child_fd is not None
    assert error.value.details["cleanup_errors"]
    assert str(tmp_path) not in repr(error.value.details["cleanup_errors"])
    assert len(repr(error.value.details["cleanup_errors"])) < 512
    real_close(child_fd)
    real_close(parent_fd)


def test_cleanup_failure_redacts_all_path_bearing_oserror_fields():
    strerror_secret = "/private/secret/in-strerror"
    filename_secret = "/private/secret/in-filename"
    filename2_secret = "/private/secret/in-filename2"
    error = OSError(
        errno.EIO,
        f"injected failure at {strerror_secret}",
        filename_secret,
        None,
        filename2_secret,
    )

    detail = artifact_module._cleanup_failure("close opened directory", error)

    assert strerror_secret not in detail
    assert filename_secret not in detail
    assert filename2_secret not in detail
    assert len(detail) <= 240


def test_complete_diff_artifact_is_not_limited_by_rendered_summary_limit(tmp_path: Path):
    store = _store(tmp_path, limit=100)
    complete = "αβγ complete canonical diff".encode()

    metadata = store.write(RUN_ID, ArtifactKind.DIFF, complete)
    summary = render_diff_summary(complete.decode(), max_bytes=5)

    assert store.read(metadata) == complete
    assert summary == "αβ"
    assert len(summary.encode()) <= 5


def test_failure_evidence_has_fixed_private_path_and_aggregate_protection(tmp_path: Path):
    store = _store(tmp_path, limit=9)

    evidence = store.write(RUN_ID, ArtifactKind.FAILURE_EVIDENCE, b"failure")

    assert evidence.relative_path == f"artifacts/{RUN_ID}/failure.json"
    assert store.read(evidence) == b"failure"
    with pytest.raises(FCPMCPError) as error:
        store.write(RUN_ID, ArtifactKind.CANDIDATE, b"xxx")
    _assert_code(error, ErrorCode.INVALID_ARGUMENTS)
    assert not store.paths.artifact_path(RUN_ID, ArtifactKind.CANDIDATE).exists()


@pytest.mark.parametrize(("size", "limit"), [(4, 3), (1, 0), (-1, 3)])
def test_source_limit_rejects_out_of_bounds_counts(size: int, limit: int):
    with pytest.raises(FCPMCPError) as error:
        enforce_source_size(size, max_source_bytes=limit)

    _assert_code(error, ErrorCode.INVALID_ARGUMENTS)


def test_source_limit_accepts_exact_boundary():
    assert enforce_source_size(3, max_source_bytes=3) == 3


@pytest.mark.parametrize(
    ("text", "limit", "expected"),
    [("αβγ", 0, ""), ("αβγ", 1, ""), ("αβγ", 2, "α"), ("αβγ", 4, "αβ"), ("abc", 3, "abc")],
)
def test_rendered_diff_summary_preserves_utf8_boundaries(
    text: str,
    limit: int,
    expected: str,
):
    assert render_diff_summary(text, max_bytes=limit) == expected


def test_rendered_diff_summary_requires_text_and_nonnegative_limit():
    with pytest.raises(FCPMCPError):
        render_diff_summary(b"bytes", max_bytes=3)  # type: ignore[arg-type]
    with pytest.raises(FCPMCPError):
        render_diff_summary("text", max_bytes=-1)


def test_read_and_verify_accept_only_complete_metadata_match(tmp_path: Path):
    store = _store(tmp_path)
    metadata = store.write(RUN_ID, ArtifactKind.CANDIDATE, b"candidate")

    assert store.read(metadata) == b"candidate"
    assert store.verify(metadata) is True


def test_read_revalidates_all_metadata_fields_after_model_copy(tmp_path: Path):
    store = _store(tmp_path)
    metadata = store.write(RUN_ID, ArtifactKind.CANDIDATE, b"candidate")
    invalid_updates = [
        {"schema_version": "2"},
        {"run_id": RUN_ID.upper()},
        {"kind": "candidate"},
        {"relative_path": "../../outside"},
        {"sha256": "A" * 64},
        {"byte_size": "9"},
        {"created_at": datetime.now()},  # noqa: DTZ005 - intentionally naive
    ]

    for update in invalid_updates:
        with pytest.raises(FCPMCPError) as error:
            store.read(metadata.model_copy(update=update))
        _assert_code(error, ErrorCode.ARTIFACT_CORRUPT)


def test_read_bounds_each_request_to_recorded_size_plus_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    store = _store(tmp_path, limit=1024 * 1024)
    metadata = store.write(RUN_ID, ArtifactKind.CANDIDATE, b"x")
    real_read = os.read
    requested_sizes: list[int] = []

    def recording_read(fd: int, byte_count: int) -> bytes:
        requested_sizes.append(byte_count)
        return real_read(fd, byte_count)

    monkeypatch.setattr("fcp_mcp.workflow.artifacts.os.read", recording_read)
    assert store.read(metadata) == b"x"
    assert requested_sizes
    assert max(requested_sizes) <= metadata.byte_size + 1


@pytest.mark.parametrize("mutation", ["missing", "size", "hash", "growth"])
def test_read_reports_missing_size_hash_and_growth_as_corruption(
    tmp_path: Path,
    mutation: str,
):
    store = _store(tmp_path, limit=20)
    metadata = store.write(RUN_ID, ArtifactKind.CANDIDATE, b"candidate")
    target = store.paths.root / metadata.relative_path
    if mutation == "missing":
        target.unlink()
    elif mutation == "size":
        target.write_bytes(b"short")
        os.chmod(target, 0o600)
    elif mutation == "hash":
        target.write_bytes(b"XXXXXXXXX")
        os.chmod(target, 0o600)
    else:
        target.write_bytes(b"x" * 21)
        os.chmod(target, 0o600)

    with pytest.raises(FCPMCPError) as error:
        store.read(metadata)

    _assert_code(error, ErrorCode.ARTIFACT_CORRUPT)


def test_read_rejects_artifact_symlink_substitution(tmp_path: Path):
    store = _store(tmp_path)
    metadata = store.write(RUN_ID, ArtifactKind.CANDIDATE, b"candidate")
    target = store.paths.root / metadata.relative_path
    outside = tmp_path / "outside"
    outside.write_bytes(b"candidate")
    target.unlink()
    target.symlink_to(outside)

    with pytest.raises(FCPMCPError) as error:
        store.read(metadata)

    _assert_code(error, ErrorCode.ARTIFACT_CORRUPT)


@pytest.mark.skipif(os.name != "posix", reason="POSIX fd identity regression")
def test_posix_read_rejects_target_replaced_after_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    store = _store(tmp_path)
    metadata = store.write(RUN_ID, ArtifactKind.CANDIDATE, b"candidate")
    target = store.paths.root / metadata.relative_path
    replacement = target.with_name("replacement")
    replacement.write_bytes(b"replacement")
    os.chmod(replacement, 0o600)
    real_read = os.read
    replaced = False

    def replacing_read(fd: int, byte_count: int) -> bytes:
        nonlocal replaced
        chunk = real_read(fd, byte_count)
        if chunk and not replaced:
            replaced = True
            os.replace(replacement, target)
        return chunk

    monkeypatch.setattr(artifact_module.os, "read", replacing_read)

    with pytest.raises(FCPMCPError) as error:
        store.read(metadata)

    _assert_code(error, ErrorCode.ARTIFACT_CORRUPT)


def test_read_rejects_target_replaced_during_final_chain_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    store = _store_with_replaceable_ancestors(tmp_path)
    metadata = store.write(RUN_ID, ArtifactKind.CANDIDATE, b"candidate")
    target = store.paths.root / metadata.relative_path
    replacement = target.with_name("attacker")
    replacement.write_bytes(b"ATTACKER!")
    os.chmod(replacement, 0o600)
    state_parent = store.paths.root.parent
    real_open_directory = artifact_module._open_directory_at
    matching_opens = 0

    def replacing_open(*args, **kwargs):
        nonlocal matching_opens
        opened = real_open_directory(*args, **kwargs)
        if args[1] == state_parent.name:
            matching_opens += 1
            if matching_opens == 2:
                os.replace(replacement, target)
        return opened

    monkeypatch.setattr(artifact_module, "_open_directory_at", replacing_open)

    with pytest.raises(FCPMCPError) as error:
        store.read(metadata)

    _assert_code(error, ErrorCode.ARTIFACT_CORRUPT)
    assert target.read_bytes() == b"ATTACKER!"


@pytest.mark.parametrize("level", ["run", "artifacts", "root"])
def test_read_rejects_canonical_directory_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    level: str,
):
    store = _store(tmp_path)
    metadata = store.write(RUN_ID, ArtifactKind.CANDIDATE, b"candidate")
    real_read_fd = store._read_fd
    detached_run: Path | None = None

    def replacing_read(fd: int, recorded: ArtifactMetadataV1) -> bytes:
        nonlocal detached_run
        payload = real_read_fd(fd, recorded)
        detached_run = _replace_canonical_artifact_chain(store, level)
        return payload

    monkeypatch.setattr(store, "_read_fd", replacing_read)

    with pytest.raises(FCPMCPError) as error:
        store.read(metadata)

    _assert_code(error, ErrorCode.ARTIFACT_CORRUPT)
    assert detached_run is not None
    assert (detached_run / "candidate.fcpxml").read_bytes() == b"candidate"
    assert not store.paths.artifact_path(RUN_ID, ArtifactKind.CANDIDATE).exists()


@pytest.mark.parametrize("ancestor_level", ["state-parent", "higher-ancestor"])
def test_read_rejects_canonical_ancestor_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ancestor_level: str,
):
    store = _store_with_replaceable_ancestors(tmp_path)
    metadata = store.write(RUN_ID, ArtifactKind.CANDIDATE, b"candidate")
    ancestor = (
        store.paths.root.parent
        if ancestor_level == "state-parent"
        else store.paths.root.parent.parent
    )
    real_open_directory = artifact_module._open_directory_at
    matching_opens = 0
    detached_run: Path | None = None

    def replacing_open(*args, **kwargs):
        nonlocal matching_opens, detached_run
        opened = real_open_directory(*args, **kwargs)
        if args[1] == ancestor.name:
            matching_opens += 1
            if matching_opens == 2:
                detached_run = _replace_canonical_ancestor(store, ancestor)
        return opened

    monkeypatch.setattr(artifact_module, "_open_directory_at", replacing_open)

    with pytest.raises(FCPMCPError) as error:
        store.read(metadata)

    _assert_code(error, ErrorCode.ARTIFACT_CORRUPT)
    assert detached_run is not None
    assert (detached_run / "candidate.fcpxml").read_bytes() == b"candidate"
    assert not store.paths.artifact_path(RUN_ID, ArtifactKind.CANDIDATE).exists()


def test_read_rejects_nonregular_artifact(tmp_path: Path):
    store = _store(tmp_path)
    metadata = store.write(RUN_ID, ArtifactKind.CANDIDATE, b"candidate")
    target = store.paths.root / metadata.relative_path
    target.unlink()
    target.mkdir()

    with pytest.raises(FCPMCPError) as error:
        store.read(metadata)

    _assert_code(error, ErrorCode.ARTIFACT_CORRUPT)


@pytest.mark.skipif(os.name != "posix", reason="POSIX modes are not portable")
def test_read_rejects_artifact_with_unsafe_mode(tmp_path: Path):
    store = _store(tmp_path)
    metadata = store.write(RUN_ID, ArtifactKind.CANDIDATE, b"candidate")
    target = store.paths.root / metadata.relative_path
    os.chmod(target, 0o644)

    with pytest.raises(FCPMCPError) as error:
        store.read(metadata)

    _assert_code(error, ErrorCode.ARTIFACT_CORRUPT)


@pytest.mark.parametrize(
    "changes",
    [
        {"relative_path": f"artifacts/{OTHER_RUN_ID}/candidate.fcpxml"},
        {"relative_path": "../../outside"},
        {"kind": ArtifactKind.DIFF},
        {"run_id": OTHER_RUN_ID},
    ],
)
def test_metadata_path_kind_or_run_tampering_is_rejected_before_open(
    tmp_path: Path,
    changes: dict[str, object],
):
    store = _store(tmp_path)
    metadata = store.write(RUN_ID, ArtifactKind.CANDIDATE, b"candidate")
    tampered = metadata.model_copy(update=changes)

    with pytest.raises(FCPMCPError) as error:
        store.read(tampered)

    _assert_code(error, ErrorCode.ARTIFACT_CORRUPT)


def test_metadata_validation_is_strict_and_canonical():
    valid = {
        "schema_version": "1",
        "run_id": RUN_ID,
        "kind": ArtifactKind.CANDIDATE,
        "relative_path": f"artifacts/{RUN_ID}/candidate.fcpxml",
        "sha256": "a" * 64,
        "byte_size": 9,
        "created_at": datetime.now(timezone.utc),
    }
    for changes in [
        {"schema_version": 1},
        {"run_id": RUN_ID.upper()},
        {"kind": "receipt"},
        {"relative_path": "/absolute/candidate.fcpxml"},
        {"sha256": "A" * 64},
        {"byte_size": "9"},
        {"created_at": datetime.now()},  # noqa: DTZ005 - intentionally naive adversarial input
    ]:
        with pytest.raises(ValidationError):
            ArtifactMetadataV1(**(valid | changes))


def test_artifact_module_import_is_sdk_server_ledger_and_engine_independent():
    blocker = """
import importlib.abc
import sys

class Blocker(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        banned = (
            "mcp",
            "fcp_mcp.server",
            "fcp_mcp.workflow.ledger",
            "fcp_mcp.workflow.engine",
        )
        if fullname == banned[0] or any(
            fullname == name or fullname.startswith(name + ".") for name in banned[1:]
        ):
            raise RuntimeError(f"forbidden import: {fullname}")
        return None

sys.meta_path.insert(0, Blocker())
import fcp_mcp.workflow.artifacts
"""
    result = subprocess.run(
        [sys.executable, "-c", blocker],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
