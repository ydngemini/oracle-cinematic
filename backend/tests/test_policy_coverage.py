"""The Platform Use Policy keeps describing what the platform does (CONTRACT C5.7).

Owner, 2026-10-13: keep the policy text current. Every POLICY_COVERAGE row names
a shipped capability, the file that proves it exists, and the words the policy
uses for it. If the capability is still there, the words must be too, in the
section the row names.
"""

from __future__ import annotations

import re
from pathlib import Path

import policy_contract
from policy_contract import PLATFORM_POLICY_DOCUMENT, PLATFORM_POLICY_VERSION, POLICY_COVERAGE

BACKEND = Path(__file__).resolve().parents[1]


def _section_text(heading: str) -> str:
    for section in PLATFORM_POLICY_DOCUMENT["sections"]:
        if section["heading"] == heading:
            return " ".join(section["paragraphs"])
    raise AssertionError(f"policy has no section {heading!r}")


def test_every_covered_capability_is_still_described():
    assert POLICY_COVERAGE, "the coverage list is the guard; it must not be empty"
    for row in POLICY_COVERAGE:
        assert (BACKEND / row["evidence"]).exists(), f"evidence for {row['capability']!r} moved: {row['evidence']}"
        assert row["phrase"] in _section_text(row["section"]), (
            f"the policy no longer describes {row['capability']!r}; update the text, bump "
            f"PLATFORM_POLICY_VERSION and keep this row (CONTRACT C5.7)")


def test_version_and_effective_date_agree():
    m = re.fullmatch(r"neoh-platform-use-policy-(\d{4})-(\d{2})-(\d{2})-v(\d+)", PLATFORM_POLICY_VERSION)
    assert m, PLATFORM_POLICY_VERSION
    months = ["January", "February", "March", "April", "May", "June", "July", "August",
              "September", "October", "November", "December"]
    year, month, day = int(m[1]), int(m[2]), int(m[3])
    assert PLATFORM_POLICY_DOCUMENT["effective_date"] == f"{months[month - 1]} {day}, {year}"


def test_no_retired_capability_is_promised():
    """MLS is out of scope (D-2026-10-12-01): the policy must not offer it."""
    text = " ".join(" ".join(s["paragraphs"]) for s in PLATFORM_POLICY_DOCUMENT["sections"])
    assert not re.search(r"\b(MLS|IDX|RESO)\b", text)
    assert policy_contract.PLATFORM_POLICY_DOCUMENT["title"] == "NEOH™ Platform Use Policy"
