from pathlib import Path

import platformdirs
import pytest

from fcp_mcp.config import RuntimeConfig
from fcp_mcp.contracts import FCPMCPError
from fcp_mcp.profiles import ApprovalMode, Profile


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


def test_workflow_defaults_are_exact(tmp_path: Path):
    config = RuntimeConfig.from_env({}, home=tmp_path)
    assert config.profile.value == "workflow"
    assert config.workflow_approval.value == "cli"
    assert config.approval_ttl_seconds == 86400
    assert config.max_operations == 100
    assert config.max_source_bytes == 134217728
    assert config.max_artifact_bytes == 268435456
    assert config.max_diff_bytes == 204800


def test_profile_override_takes_precedence_over_environment(tmp_path: Path):
    config = RuntimeConfig.from_env(
        {"FCP_MCP_PROFILE": "inspect"},
        home=tmp_path,
        profile_override="edit",
    )
    assert config.profile is Profile.EDIT


def test_environment_profile_takes_precedence_over_workflow_default(tmp_path: Path):
    config = RuntimeConfig.from_env(
        {"FCP_MCP_PROFILE": "full"},
        home=tmp_path,
    )
    assert config.profile is Profile.FULL


def test_with_profile_returns_an_isolated_config(tmp_path: Path):
    config = RuntimeConfig.from_env({}, home=tmp_path)
    isolated = config.with_profile(Profile.INSPECT)
    assert isolated is not config
    assert isolated.profile is Profile.INSPECT
    assert config.profile is Profile.WORKFLOW
    assert isolated.output_dir == config.output_dir


def test_explicit_state_path_is_resolved_without_creating_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.chdir(tmp_path)
    config = RuntimeConfig.from_env({"FCP_MCP_STATE_DIR": "workflow-state"}, home=tmp_path)
    expected = (tmp_path / "workflow-state").resolve()
    assert config.state_dir == expected
    assert not expected.exists()


def test_explicit_state_path_preserves_root_symlink_evidence(
    tmp_path: Path,
):
    target = tmp_path / "real-state"
    target.mkdir()
    requested = tmp_path / "requested-state"
    requested.symlink_to(target, target_is_directory=True)

    config = RuntimeConfig.from_env(
        {"FCP_MCP_STATE_DIR": str(requested)},
        home=tmp_path,
    )

    assert config.state_dir == requested
    assert config.state_dir.is_symlink()


def test_default_state_path_uses_platformdirs_without_creating_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    expected = tmp_path / "platform-state"
    monkeypatch.setattr(platformdirs, "user_state_path", lambda *args, **kwargs: expected)
    config = RuntimeConfig.from_env({}, home=tmp_path)
    assert config.state_dir == expected.resolve()
    assert not expected.exists()


@pytest.mark.parametrize(
    ("name", "minimum", "maximum", "attribute"),
    [
        ("FCP_MCP_WORKFLOW_APPROVAL_TTL_SECONDS", 60, 604800, "approval_ttl_seconds"),
        ("FCP_MCP_WORKFLOW_MAX_OPERATIONS", 1, 1000, "max_operations"),
        ("FCP_MCP_WORKFLOW_MAX_SOURCE_BYTES", 1048576, 1073741824, "max_source_bytes"),
        ("FCP_MCP_WORKFLOW_MAX_ARTIFACT_BYTES", 1048576, 2147483648, "max_artifact_bytes"),
        ("FCP_MCP_WORKFLOW_MAX_DIFF_BYTES", 1024, 10485760, "max_diff_bytes"),
    ],
)
@pytest.mark.parametrize("value_name", ["minimum", "maximum"])
def test_workflow_limit_bounds_are_inclusive(
    tmp_path: Path,
    name: str,
    minimum: int,
    maximum: int,
    attribute: str,
    value_name: str,
):
    value = minimum if value_name == "minimum" else maximum
    config = RuntimeConfig.from_env({name: str(value)}, home=tmp_path)
    assert getattr(config, attribute) == value


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("FCP_MCP_PROFILE", "unknown"),
        ("FCP_MCP_WORKFLOW_APPROVAL", "automatic"),
        ("FCP_MCP_WORKFLOW_APPROVAL_TTL_SECONDS", "59"),
        ("FCP_MCP_WORKFLOW_MAX_OPERATIONS", "0"),
        ("FCP_MCP_WORKFLOW_MAX_DIFF_BYTES", "1023"),
        ("FCP_MCP_WORKFLOW_MAX_SOURCE_BYTES", "not-a-number"),
        ("FCP_MCP_WORKFLOW_MAX_ARTIFACT_BYTES", "2147483649"),
    ],
)
def test_invalid_workflow_configuration_is_coded(tmp_path: Path, name: str, value: str):
    with pytest.raises(FCPMCPError, match="invalid_configuration"):
        RuntimeConfig.from_env({name: value}, home=tmp_path)


@pytest.mark.parametrize("mode", [ApprovalMode.CLI.value, ApprovalMode.CLIENT.value])
def test_workflow_approval_modes_are_accepted(tmp_path: Path, mode: str):
    config = RuntimeConfig.from_env(
        {"FCP_MCP_WORKFLOW_APPROVAL": mode},
        home=tmp_path,
    )
    assert config.workflow_approval.value == mode
