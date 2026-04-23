"""Centralized safe XML parsing wrappers using defusedxml.

All XML parsing in the project MUST go through these functions
to prevent XXE, entity bombs, and other XML attacks.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Union

import defusedxml.ElementTree as SafeET


def parse_file(path: Union[str, Path]) -> ET.ElementTree:
    """Parse an XML file safely."""
    return SafeET.parse(str(path))


def parse_string(xml_string: str) -> ET.Element:
    """Parse an XML string safely, return root element."""
    return SafeET.fromstring(xml_string)


def parse_fcpxml(path: Union[str, Path]) -> ET.ElementTree:
    """Parse an FCPXML file or .fcpxmld bundle.

    If path points to a .fcpxmld directory, looks for Info.fcpxml inside.
    """
    path = Path(path)
    if path.is_dir() and path.suffix == ".fcpxmld":
        info_path = path / "Info.fcpxml"
        if not info_path.exists():
            raise FileNotFoundError(f"No Info.fcpxml found in bundle: {path}")
        path = info_path
    if not path.exists():
        raise FileNotFoundError(f"FCPXML file not found: {path}")
    return parse_file(path)


def write_xml(tree: ET.ElementTree, path: Union[str, Path]) -> Path:
    """Write an ElementTree to file with XML declaration."""
    path = Path(path)
    tree.write(str(path), encoding="unicode", xml_declaration=True)
    return path


def element_to_string(element: ET.Element) -> str:
    """Serialize an Element to a string."""
    return ET.tostring(element, encoding="unicode")
