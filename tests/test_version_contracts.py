from importlib.metadata import metadata

from packaging.requirements import Requirement
from packaging.version import Version

from fcp_mcp import __version__
from fcp_mcp.contracts import ErrorCode, FCPMCPError
from fcp_mcp.version import distribution_version, package_version


def test_package_and_module_versions_are_0_2_1():
    assert package_version() == "0.2.1"
    assert __version__ == "0.2.1"


def test_runtime_dependency_excludes_mcp_v2():
    requirements = [
        Requirement(value) for value in metadata("fcp-mcp").get_all("Requires-Dist") or []
    ]
    mcp_requirement = next(requirement for requirement in requirements if requirement.name == "mcp")
    assert Version("1.27") in mcp_requirement.specifier
    assert Version("2.0") not in mcp_requirement.specifier


def test_domain_error_has_stable_code_and_readable_text():
    error = FCPMCPError(
        ErrorCode.TARGET_NOT_FOUND,
        "Clip 'missing' was not found",
        {"clip_name": "missing"},
    )
    assert error.code is ErrorCode.TARGET_NOT_FOUND
    assert error.details == {"clip_name": "missing"}
    assert str(error) == "target_not_found: Clip 'missing' was not found"


def test_distribution_version_reports_installed_sdk():
    assert distribution_version("mcp").count(".") >= 1
