from __future__ import annotations

import subprocess
import sys

import pytest

from fcp_mcp.contracts import ErrorCode, FCPMCPError


def test_require_macos_accepts_darwin_and_rejects_every_other_identifier():
    from fcp_mcp.platform_support import require_macos

    require_macos("darwin")
    for platform in ("linux", "win32", "cygwin", "freebsd"):
        with pytest.raises(FCPMCPError) as caught:
            require_macos(platform)
        assert caught.value.code is ErrorCode.UNSUPPORTED_PLATFORM
        assert str(caught.value) == "unsupported_platform: fcp-mcp requires macOS"


def test_pure_imports_do_not_consult_the_runtime_platform():
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys, sysconfig; sysconfig.get_config_vars(); "
                "sys.platform = 'linux'; "
                "import fcp_mcp; "
                "import fcp_mcp.fcpxml.parser; "
                "import fcp_mcp.workflow.models"
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0
    assert completed.stdout == ""
    assert completed.stderr == ""
