import hashlib
from pathlib import Path

import pytest

from fcp_mcp.config import RuntimeConfig
from fcp_mcp.contracts import FCPMCPError
from fcp_mcp.workflow.artifacts import ArtifactKind, ArtifactStore

RUN_ID = "f761fe55-1113-4a65-a70e-1cbfae781921"


def store(tmp_path: Path):
    config = RuntimeConfig.from_env({"FCP_MCP_STATE_DIR": str(tmp_path / "state")}, home=tmp_path)
    return ArtifactStore.from_config(config)


def test_current_read_returns_current_hash_without_weakening_pinned_read(tmp_path):
    artifacts = store(tmp_path)
    old = artifacts.write(RUN_ID, ArtifactKind.RECEIPT, b"old")
    artifacts.write(RUN_ID, ArtifactKind.RECEIPT, b"current")
    metadata, payload = artifacts.read_current(RUN_ID, ArtifactKind.RECEIPT)
    assert payload == b"current"
    assert metadata.sha256 == hashlib.sha256(payload).hexdigest()
    with pytest.raises(FCPMCPError):
        artifacts.read(old)


@pytest.mark.parametrize("mode", ["symlink", "permissions", "oversize", "missing"])
def test_current_read_rejects_untrusted_record_shape(tmp_path, mode):
    artifacts = store(tmp_path)
    artifacts.write(RUN_ID, ArtifactKind.RECEIPT, b"record")
    path = artifacts.paths.artifact_path(RUN_ID, ArtifactKind.RECEIPT)
    if mode == "symlink":
        external = tmp_path / "external"
        external.write_bytes(b"external")
        path.unlink()
        path.symlink_to(external)
    elif mode == "permissions":
        path.chmod(0o644)
    elif mode == "oversize":
        artifacts.max_artifact_bytes = 3
    else:
        path.unlink()
    with pytest.raises(FCPMCPError):
        artifacts.read_current(RUN_ID, ArtifactKind.RECEIPT)
