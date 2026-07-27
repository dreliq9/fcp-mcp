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


def test_missing_input_is_rejected(tmp_path: Path):
    with pytest.raises(FCPMCPError, match="source_not_found"):
        policy(tmp_path).resolve_input("missing.fcpxml")


def test_input_kind_is_enforced(tmp_path: Path):
    directory = tmp_path / "media"
    directory.mkdir()
    source = tmp_path / "media.mov"
    source.write_bytes(b"media")

    with pytest.raises(FCPMCPError, match="Expected a file"):
        policy(tmp_path).resolve_input(str(directory))
    with pytest.raises(FCPMCPError, match="Expected a directory"):
        policy(tmp_path).resolve_input(str(source), kind="dir")


def test_scoped_reference_may_be_missing_for_link_diagnostics(tmp_path: Path):
    expected = (tmp_path / "missing.mov").resolve()
    assert policy(tmp_path).resolve_reference("missing.mov") == expected


def test_input_suffix_is_enforced_case_insensitively(tmp_path: Path):
    source = tmp_path / "SHOW.FCPXML"
    source.write_text("<fcpxml/>")
    path_policy = policy(tmp_path)

    assert path_policy.resolve_input(
        str(source),
        suffixes={".fcpxml"},
    ) == source.resolve()
    with pytest.raises(FCPMCPError, match="invalid_path"):
        path_policy.resolve_reference(
            str(source),
            suffixes={".edl"},
        )


def test_output_resolution_modes(tmp_path: Path):
    source = tmp_path / "show.fcpxml"
    source.write_text("<fcpxml/>")
    path_policy = policy(tmp_path)

    assert path_policy.resolve_output("named.fcpxml") == (
        tmp_path / "named.fcpxml"
    ).resolve()
    assert path_policy.resolve_output(None, input_path=source) == (
        tmp_path / "show_modified.fcpxml"
    ).resolve()
    assert path_policy.resolve_output(None, default_name="generated.fcpxml") == (
        tmp_path / "generated.fcpxml"
    ).resolve()
    with pytest.raises(FCPMCPError, match="invalid_arguments"):
        path_policy.resolve_output(None)


def test_output_scope_and_suffix_are_enforced(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    path_policy = policy(root)

    with pytest.raises(FCPMCPError, match="path_outside_scope"):
        path_policy.resolve_output(str(tmp_path / "outside.fcpxml"))
    with pytest.raises(FCPMCPError, match="invalid_path"):
        path_policy.resolve_output(
            "output.txt",
            suffixes={".fcpxml"},
        )
