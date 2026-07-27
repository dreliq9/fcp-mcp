from pathlib import Path

import pytest

from fcp_mcp.config import RuntimeConfig
from fcp_mcp.contracts import FCPMCPError
from fcp_mcp.security.paths import PathPolicy


def policy(root: Path) -> PathPolicy:
    return PathPolicy(
        RuntimeConfig.from_env(
            {
                "FCP_MCP_OUTPUT_DIR": str(root),
                "FCP_MCP_ALLOWED_ROOTS": str(root),
            },
            home=root,
        )
    )


def test_relative_input_resolves_under_output_root(tmp_path: Path):
    source = tmp_path / "show.fcpxml"
    source.write_text("<fcpxml/>")
    assert policy(tmp_path).resolve_input("show.fcpxml") == source.resolve()


def test_parent_traversal_is_rejected(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.fcpxml"
    outside.write_text("<fcpxml/>")
    with pytest.raises(FCPMCPError, match="path_outside_scope"):
        policy(root).resolve_input("../outside.fcpxml")


def test_symlink_escape_is_rejected(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.fcpxml"
    outside.write_text("<fcpxml/>")
    (root / "escape.fcpxml").symlink_to(outside)
    with pytest.raises(FCPMCPError, match="path_outside_scope"):
        policy(root).resolve_input("escape.fcpxml")


def test_same_input_and_output_is_rejected(tmp_path: Path):
    source = tmp_path / "show.fcpxml"
    source.write_text("<fcpxml/>")
    with pytest.raises(FCPMCPError, match="same_file_forbidden"):
        policy(tmp_path).resolve_output(
            str(source),
            input_path=source,
            suffixes={".fcpxml"},
        )
