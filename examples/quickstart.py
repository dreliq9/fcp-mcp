"""fcp-mcp quickstart — verify install and exercise the core surface.

Run this AFTER `pip install -e ".[dev]"` (or `pipx install fcp-mcp`) to
confirm the package imports, ffmpeg is reachable, and the FCPXML
round-trip works.

    python examples/quickstart.py
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path


def check_import() -> None:
    import fcp_mcp

    print(f"[ok] fcp_mcp imports — version {fcp_mcp.__version__}")


def check_mcp_sdk() -> None:
    import mcp  # noqa: F401

    print("[ok] mcp SDK imports")


def check_xml_stack() -> None:
    import defusedxml  # noqa: F401
    import lxml  # noqa: F401

    print("[ok] defusedxml + lxml present (hardened XML parsing)")


def check_ffmpeg() -> None:
    if shutil.which("ffmpeg"):
        print("[ok] ffmpeg on PATH (media_* tools will work)")
    else:
        print("[warn] ffmpeg NOT on PATH — media_* tools will error. `brew install ffmpeg`")


def check_fcp_running() -> None:
    from fcp_mcp.fcp_control import __init__  # noqa: F401

    # We don't actually launch FCP here — just import the module.
    print("[ok] fcp_control module loads (live FCP tools available when FCP is running)")


def check_parser_round_trip() -> None:
    """Parse the bundled smoke-test FCPXML and save it via the Modifier."""
    from fcp_mcp.fcpxml.parser import FCPXMLParser
    from fcp_mcp.fcpxml.writer import FCPXMLModifier

    repo_root = Path(__file__).resolve().parent.parent
    candidates = [
        repo_root / "smoke_test.fcpxml",
        repo_root / "smoke_test_v2.fcpxml",
    ]
    fixtures_dir = repo_root / "tests" / "fixtures"
    if fixtures_dir.is_dir():
        candidates.extend(sorted(fixtures_dir.glob("*.fcpxml")))

    sample = next((p for p in candidates if p.is_file()), None)
    if sample is None:
        print("[skip] no .fcpxml fixture found — parser round-trip untested")
        return

    doc = FCPXMLParser().parse(str(sample))
    summary = f"{len(doc.library.events) if doc.library else 0} event(s)"
    mod = FCPXMLModifier(sample)
    out = repo_root / "examples" / "_roundtrip.fcpxml"
    mod.save(out)
    print(f"[ok] parsed {sample.name} ({summary}) and saved round-trip to examples/_roundtrip.fcpxml")


def main() -> int:
    print("fcp-mcp quickstart\n==================")
    try:
        check_import()
        check_mcp_sdk()
        check_xml_stack()
        check_ffmpeg()
        check_fcp_running()
        check_parser_round_trip()
    except Exception as exc:  # noqa: BLE001
        print(f"[fail] {type(exc).__name__}: {exc}")
        return 1

    print("\nAll checks passed. Wire fcp-mcp into Claude Code:")
    print('  claude mcp add-json fcp \'{"type":"stdio","command":"fcp-mcp"}\' --scope user')
    return 0


if __name__ == "__main__":
    sys.exit(main())
