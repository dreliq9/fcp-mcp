from pathlib import Path
import tomllib

ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_readme_leads_with_bounded_outcome_and_frozen_catalog():
    text = read("README.md")
    first_screen = "\n".join(text.splitlines()[:45]).lower()
    assert "trusted, local" in first_screen
    assert "human-approved" in first_screen
    assert "most capable" not in first_screen
    assert "94 tools" in text
    assert "93 tools" not in text
    assert "v0.3 release candidate" not in text.lower()


def test_package_metadata_has_a_human_maintainer():
    project = tomllib.loads(read("pyproject.toml"))["project"]
    expected = [{"name": "Adam Steen", "email": "dreliq9@gmail.com"}]
    assert project["authors"] == expected
    assert project["maintainers"] == expected


def test_public_navigation_links_launch_surfaces():
    text = read("README.md")
    for target in (
        "docs/FIRST_RUN.md",
        "docs/COMPATIBILITY.md",
        "SUPPORT.md",
        "SECURITY.md",
        "docs/releases/v0.3.0.md",
    ):
        assert target in text
