"""
Tests for data_coverage — the national coverage map.

Guards two invariants:
  1. The curated LIVE_PROPERTY registry never drifts from the harvesters that
     actually exist (harvesters.firehose.REGISTRY).
  2. Compliance coverage is computed live and stays complete (50 + DC).

Run:  python -m pytest backend/tests/test_data_coverage.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import data_coverage as dc


def test_jurisdiction_count_is_51():
    # 50 states + DC.
    assert len(dc.US_JURISDICTIONS) == 51
    assert "DC" in dc.US_JURISDICTIONS


def test_live_property_matches_firehose_registry():
    """The curated registry must list exactly the states the firehose wires."""
    try:
        from harvesters.firehose import REGISTRY
    except Exception as exc:  # pragma: no cover - import env issue
        pytest.skip(f"firehose not importable in this env: {exc}")
    assert set(dc.LIVE_PROPERTY) == set(REGISTRY), (
        "LIVE_PROPERTY drifted from firehose.REGISTRY — update data_coverage."
    )


def test_every_live_property_state_is_a_real_jurisdiction():
    assert set(dc.LIVE_PROPERTY).issubset(set(dc.US_JURISDICTIONS))


def test_portal_hints_only_for_missing_states():
    """A hint for a state we already cover would be dead weight."""
    overlap = set(dc.PORTAL_HINTS) & set(dc.LIVE_PROPERTY)
    assert not overlap, f"PORTAL_HINTS overlaps live states: {overlap}"


def test_compliance_coverage_complete():
    counts = dc._compliance_rule_counts()
    missing = [c for c in dc.US_JURISDICTIONS if counts.get(c, 0) == 0]
    assert not missing, f"Jurisdictions with no compliance rules: {missing}"


def test_summary_shapes_and_totals():
    s = dc.summary()
    assert s["jurisdictions"] == 51
    assert s["property"]["live"] == len(dc.LIVE_PROPERTY)
    assert s["property"]["live"] == (
        s["property"]["live_statewide"] + s["property"]["city_scoped"]
    )
    assert s["compliance"]["live"] == 51
    assert s["compliance"]["total_rules"] > 0


def test_national_status_row_per_jurisdiction():
    rows = dc.national_status()
    assert len(rows) == 51
    codes = {r.code for r in rows}
    assert codes == set(dc.US_JURISDICTIONS)


def test_report_json_serialisable():
    import json
    blob = json.dumps(dc.report_json())
    assert len(blob) > 100


def test_report_markdown_renders():
    md = dc.report_markdown()
    assert "National Data Coverage" in md
    assert "Property gap" in md
