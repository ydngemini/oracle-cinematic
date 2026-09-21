"""
Enforce that docs/data-access-tiers.md stays in sync with the actual data integrations.

This test scans backend/data_integrations/*.py and ensures every module that pulls
from an external source is documented in the "Register" table of docs/data-access-tiers.md.

The purpose is to prevent the documented tiers from drifting out of sync with the code.
Any time a new data source is added to the backend, its licence tier and gate must be
added to data-access-tiers.md *in the same commit*, and this test will catch omissions.
"""

import os
import re
from pathlib import Path


def _load_data_tiers_module_row_map():
    """Parse docs/data-access-tiers.md and return a dict of module_path -> (source, tier, gate).

    Scans the "Register" table for lines matching:
    | `path/to/module.py` | Source name | **Tier** | Gate | Notes |
    """
    tiers_path = Path(__file__).parent.parent.parent / "docs" / "data-access-tiers.md"
    if not tiers_path.exists():
        raise FileNotFoundError(f"Missing: {tiers_path}")

    content = tiers_path.read_text()
    # Find the Register section (starts with "## Register")
    if "## Register" not in content:
        raise ValueError("data-access-tiers.md missing '## Register' section")

    register_start = content.find("## Register")
    # Extract from "## Register" to the next "## " or EOF
    next_section = content.find("## ", register_start + 1)
    register_section = content[register_start : next_section if next_section > 0 else len(content)]

    module_map = {}
    # Regex: | `path/to/module.py` |
    for line in register_section.split("\n"):
        match = re.search(r"^\|\s*`([^`]+\.py)`\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|", line)
        if match:
            module_path, source, tier, gate = match.groups()
            module_map[module_path] = {
                "source": source.strip(),
                "tier": tier.strip(),
                "gate": gate.strip(),
            }

    return module_map


def _extract_external_sources_from_modules():
    """Scan backend/data_integrations/*.py and extract which modules pull from external sources.

    Returns a dict of module_name -> {"path": path, "sources": ["source1", "source2", ...]}
    A module is included if it makes actual HTTP requests (has aiohttp, requests, httpx calls).
    """
    data_int_dir = Path(__file__).parent.parent / "data_integrations"
    if not data_int_dir.exists():
        raise FileNotFoundError(f"Missing: {data_int_dir}")

    modules = {}
    for py_file in sorted(data_int_dir.glob("*.py")):
        # Skip __init__, base, exceptions, etc.
        if py_file.name.startswith(("__", "base", "exceptions", "conftest")):
            continue

        content = py_file.read_text()

        # Check if this module actually makes HTTP requests (the real marker of a data integration)
        has_http_calls = bool(
            re.search(r"(aiohttp|requests|httpx|urllib)\..*\.(get|post|put|delete)", content, re.IGNORECASE) or
            re.search(r"(aiohttp|requests|httpx)\..*session", content, re.IGNORECASE) or
            re.search(r"async with.*session\.(get|post)", content, re.IGNORECASE)
        )

        if not has_http_calls:
            continue

        # Extract module docstring to get the source name
        docstring_match = re.search(r'^"""(.*?)"""', content, re.MULTILINE | re.DOTALL)
        docstring = docstring_match.group(1) if docstring_match else ""

        # Extract mentioned URLs/sources from docstring
        urls = re.findall(r"https?://[^\s<>\"]+", docstring)
        # Also extract service names like "RentCast", "Census", "Regrid", etc.
        service_names = re.findall(r"\b([A-Z][a-zA-Z]+(?:\s+(?:API|Web|Data|OData))?)\b", docstring[:500])

        modules[py_file.name] = {
            "path": str(py_file.relative_to(py_file.parent.parent)),
            "sources": list(set(urls + service_names))[:2],  # Just first 2 for brevity
        }

    return modules


def test_data_access_tiers_completeness():
    """Ensure all backend/data_integrations/*.py modules are documented in data-access-tiers.md."""

    documented_modules = _load_data_tiers_module_row_map()
    code_modules = _extract_external_sources_from_modules()

    # Check that every code module is documented
    missing = []
    for module_name, module_info in code_modules.items():
        module_path = module_info["path"]
        if module_path not in documented_modules:
            missing.append(f"{module_path} (pulls {', '.join(module_info['sources'][:2])}...)")

    if missing:
        raise AssertionError(
            f"docs/data-access-tiers.md is missing {len(missing)} module(s). "
            f"Add a row for each external data source:\n" + "\n".join(f"  - {m}" for m in missing)
        )


def test_data_access_tiers_syntax():
    """Ensure the Register table in data-access-tiers.md is properly formatted."""
    documented_modules = _load_data_tiers_module_row_map()

    # Should have at least some modules documented
    assert len(documented_modules) > 0, "data-access-tiers.md Register table is empty or unparseable"

    # Each row should have non-empty fields
    for module, info in documented_modules.items():
        assert info["source"].strip(), f"{module}: source field is empty"
        assert info["tier"].strip(), f"{module}: tier field is empty"
        assert info["gate"].strip() or "none" in info["gate"].lower(), f"{module}: gate field is suspiciously empty"
