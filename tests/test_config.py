from pathlib import Path

import pytest

from fcp_mcp.config import RuntimeConfig
from fcp_mcp.contracts import FCPMCPError


def test_output_precedence_and_legacy_alias(tmp_path: Path):
    legacy = tmp_path / "legacy"
    canonical = tmp_path / "canonical"
    config = RuntimeConfig.from_env(
        {
            "FCP_PROJECTS_DIR": str(legacy),
            "FCP_MCP_OUTPUT_DIR": str(canonical),
        },
        home=tmp_path,
    )
    assert config.output_dir == canonical.resolve()
    assert config.allowed_roots == (canonical.resolve(),)


def test_default_output_is_home_movies(tmp_path: Path):
    config = RuntimeConfig.from_env({}, home=tmp_path)
    assert config.output_dir == (tmp_path / "Movies").resolve()


@pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
def test_live_control_true_values(tmp_path: Path, value: str):
    config = RuntimeConfig.from_env(
        {"FCP_MCP_ENABLE_LIVE_CONTROL": value},
        home=tmp_path,
    )
    assert config.live_control_enabled is True


def test_invalid_log_format_fails(tmp_path: Path):
    with pytest.raises(FCPMCPError, match="invalid_configuration"):
        RuntimeConfig.from_env({"FCP_MCP_LOG_FORMAT": "xml"}, home=tmp_path)
