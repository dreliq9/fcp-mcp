"""Retained import snapshots; assumes a trusted OS account, not same-user isolation."""

from __future__ import annotations

import hashlib
import os
import stat
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

from fcp_mcp.contracts import ErrorCode, FCPMCPError
from fcp_mcp.result_models.live import StrictFrozenModel


class StagedFile(StrictFrozenModel):
    name: str
    sha256: str
    size_bytes: int
    device: int
    inode: int


class SealedImport(StrictFrozenModel):
    device: int
    inode: int
    files: list[StagedFile]


def _failure(message: str) -> FCPMCPError:
    return FCPMCPError(
        ErrorCode.ARTIFACT_CORRUPT,
        message,
        {"hint": "Prepare and review a new import; do not alter private snapshots."},
    )


@contextmanager
def directory(path: Path):
    """Open an absolute canonical directory through no-follow descriptor edges."""
    descriptors = []
    edges = []
    try:
        descriptors.append(os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW))
        for part in path.parts[1:]:
            parent = descriptors[-1]
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            descriptors.append(child)
            edges.append((parent, part, child))
        yield descriptors[-1]
        for parent, name, child in edges:
            observed = os.stat(name, dir_fd=parent, follow_symlinks=False)
            opened = os.fstat(child)
            if (observed.st_dev, observed.st_ino) != (opened.st_dev, opened.st_ino):
                raise _failure("Import path ancestor changed")
    except OSError as error:
        raise _failure("Cannot safely access import snapshot or source") from error
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


@contextmanager
def source_file(path: Path):
    with directory(path.parent) as parent:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode):
                raise _failure("Import source must be a regular file")
            yield fd, before
            after = os.fstat(fd)
            current = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
            if identity(before) != identity(after) or identity(before) != identity(current):
                raise _failure("Import source changed while reading")
        finally:
            os.close(fd)


def read_source(path: Path, limit: int) -> bytes:
    with source_file(path) as (fd, before):
        if before.st_size > limit:
            raise _failure("Import source exceeds the configured byte limit")
        chunks, total = [], 0
        while chunk := os.read(fd, min(65536, limit + 1 - total)):
            chunks.append(chunk)
            total += len(chunk)
            if total > limit:
                raise _failure("Import source exceeds the configured byte limit")
        return b"".join(chunks)


class ImportStaging:
    def __init__(self, root: Path, limit: int):
        self.root, self.limit = root, limit

    def path(self, prepared_id: str) -> Path:
        try:
            if str(UUID(prepared_id)) != prepared_id or UUID(prepared_id).int == 0:
                raise ValueError
        except ValueError as error:
            raise _failure("Invalid import snapshot identity") from error
        return self.root / prepared_id

    @contextmanager
    def build(self, prepared_id: str):
        # StatePaths initializes the private parent before this method is called.
        with directory(self.root.parent) as parent:
            try:
                os.mkdir(self.root.name, 0o700, dir_fd=parent)
            except FileExistsError:
                pass
        with directory(self.root) as staging:
            mode = os.fstat(staging)
            if stat.S_IMODE(mode.st_mode) != 0o700 or mode.st_uid != os.getuid():
                raise _failure("Import staging root must be private")
            name = self.path(prepared_id).name
            os.mkdir(name, 0o700, dir_fd=staging)
            with directory(self.path(prepared_id)) as fd:
                builder = _Builder(self.path(prepared_id), fd, self.limit)
                try:
                    yield builder
                    if not builder.sealed:
                        raise _failure("Import snapshot was not sealed")
                    os.fsync(staging)
                except BaseException:
                    # Remove only unpublished files created by this builder.
                    os.fchmod(fd, 0o700)
                    for item in builder.names:
                        os.unlink(item, dir_fd=fd)
                    os.rmdir(name, dir_fd=staging)
                    raise

    def verify(self, prepared_id: str, manifest: SealedImport) -> Path:
        path = self.path(prepared_id)
        with directory(path) as fd:
            info = os.fstat(fd)
            if (info.st_dev, info.st_ino, stat.S_IMODE(info.st_mode), info.st_uid) != (
                manifest.device,
                manifest.inode,
                0o500,
                os.getuid(),
            ):
                raise _failure("Import snapshot directory changed")
            names = [item.name for item in manifest.files]
            if "candidate.fcpxml" not in names or len(set(names)) != len(names):
                raise _failure("Invalid import snapshot manifest")
            if sorted(os.listdir(fd)) != sorted(names):
                raise _failure("Import snapshot inventory changed")
            total = 0
            for item in manifest.files:
                if Path(item.name).name != item.name or item.name in {".", "..", ""}:
                    raise _failure("Invalid import snapshot filename")
                handle = os.open(item.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                try:
                    before = os.fstat(handle)
                    if not stat.S_ISREG(before.st_mode) or (
                        before.st_dev,
                        before.st_ino,
                        before.st_size,
                        stat.S_IMODE(before.st_mode),
                        before.st_uid,
                    ) != (item.device, item.inode, item.size_bytes, 0o400, os.getuid()):
                        raise _failure("Import snapshot file identity or permissions changed")
                    total += item.size_bytes
                    if item.size_bytes < 0 or total > self.limit:
                        raise _failure("Import snapshots exceed the configured byte limit")
                    digest, count = hashlib.sha256(), 0
                    while chunk := os.read(handle, min(65536, item.size_bytes + 1 - count)):
                        digest.update(chunk)
                        count += len(chunk)
                        if count > item.size_bytes:
                            raise _failure("Import snapshot grew while reading")
                    after = os.fstat(handle)
                    current = os.stat(item.name, dir_fd=fd, follow_symlinks=False)
                    if (
                        count != item.size_bytes
                        or digest.hexdigest() != item.sha256
                        or (before.st_mtime_ns, before.st_ctime_ns)
                        != (after.st_mtime_ns, after.st_ctime_ns)
                        or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)
                    ):
                        raise _failure("Import snapshot content changed")
                finally:
                    os.close(handle)
        return path / "candidate.fcpxml"


class _Builder:
    def __init__(self, path: Path, fd: int, limit: int):
        self.path, self.fd, self.limit = path, fd, limit
        self.names: list[str] = []
        self.files: list[StagedFile] = []
        self.total = 0
        self.sealed = False

    def _write(self, name: str, chunks):
        fd = os.open(
            name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self.fd
        )
        self.names.append(name)
        digest, size = hashlib.sha256(), 0
        try:
            for chunk in chunks:
                self.total += len(chunk)
                size += len(chunk)
                if self.total > self.limit:
                    raise _failure("Import snapshots exceed FCP_MCP_MAX_ARTIFACT_BYTES")
                digest.update(chunk)
                view = memoryview(chunk)
                while view:
                    written = os.write(fd, view)
                    if written <= 0:
                        raise _failure("Cannot write import snapshot")
                    view = view[written:]
            os.fchmod(fd, 0o400)
            os.fsync(fd)
            info = os.fstat(fd)
            self.files.append(
                StagedFile(
                    name=name,
                    sha256=digest.hexdigest(),
                    size_bytes=size,
                    device=info.st_dev,
                    inode=info.st_ino,
                )
            )
        finally:
            os.close(fd)
        return self.path / name

    def copy_media(self, path: Path) -> Path:
        name = f"media-{len(self.files):04d}{path.suffix}"
        with source_file(path) as (fd, info):
            if info.st_size > self.limit - self.total:
                raise _failure("Import media exceeds the remaining snapshot byte budget")

            def chunks():
                while chunk := os.read(fd, 65536):
                    yield chunk

            return self._write(name, chunks())

    def seal(self, payload: bytes) -> SealedImport:
        self._write("candidate.fcpxml", [payload])
        os.fchmod(self.fd, 0o500)
        os.fsync(self.fd)
        info = os.fstat(self.fd)
        self.sealed = True
        return SealedImport(device=info.st_dev, inode=info.st_ino, files=self.files)
