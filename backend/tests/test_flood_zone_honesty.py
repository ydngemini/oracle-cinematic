"""Flood-zone lookup must never assert low risk it cannot support.

Flood status is a materially disclosable condition in a real-estate
transaction. The failure mode these tests exist to prevent is quiet and
plausible-looking: an endpoint with no data behind it answering "zone X,
no flood insurance required" — the same payload a genuine survey of a
low-risk parcel produces, and indistinguishable from one downstream.
"""

from __future__ import annotations

import asyncio

import apis.property_data as property_data
import state_compliance.routes_market as market
from tenancy import Role, TenantContext


TENANT_ID = "11111111-1111-1111-1111-111111111111"
CTX = TenantContext(agent_id="broker@tenant.test", tenant_id=TENANT_ID, role=Role.BROKER_OWNER)

# Bald Head Island, NC — coastal, and firmly inside a mapped VE zone.
LAT, LNG = 33.8654, -77.9908


def _call():
    return asyncio.run(market.get_flood_zone(lat=LAT, lng=LNG, ctx=CTX))


def test_reports_unknown_when_no_source_can_answer(monkeypatch):
    """The regression guard: no local rows and no live service = UNKNOWN.

    Returning zone X here would claim the parcel was surveyed and found
    outside the Special Flood Hazard Area, on the strength of having no data
    at all.
    """
    async def no_local_rows(*_args, **_kwargs):
        return None

    async def service_down(*_args, **_kwargs):
        return None

    monkeypatch.setattr(market, "_fetchrow", no_local_rows)
    monkeypatch.setattr(property_data, "get_flood_zone", service_down)

    result = _call()

    assert result.fema_zone == "UNKNOWN"
    assert result.flood_insurance_required is None, (
        "False here reads downstream as 'no flood insurance needed'"
    )
    assert result.data_available is False
    assert "not a finding of low risk" in result.zone_description


def test_unmapped_nfhl_coverage_is_unknown_not_minimal_hazard(monkeypatch):
    """FEMA answering with no polygon means "outside NFHL coverage".

    Zone X is itself a mapped NFHL polygon, so an empty feature list is the
    absence of a survey, not the result of one.
    """
    async def no_local_rows(*_args, **_kwargs):
        return None

    async def unmapped(*_args, **_kwargs):
        return {"zone": "UNKNOWN", "risk": "unknown", "in_sfha": False, "mapped": False}

    monkeypatch.setattr(market, "_fetchrow", no_local_rows)
    monkeypatch.setattr(property_data, "get_flood_zone", unmapped)

    result = _call()

    assert result.fema_zone == "UNKNOWN"
    assert result.flood_insurance_required is None
    assert result.data_available is False


def test_live_service_answer_is_used_when_the_local_table_is_empty(monkeypatch):
    """Nothing writes the local NFHL table, so this is the live path."""
    async def no_local_rows(*_args, **_kwargs):
        return None

    async def mapped_sfha(*_args, **_kwargs):
        return {"zone": "VE", "risk": "very_high", "in_sfha": True, "mapped": True}

    monkeypatch.setattr(market, "_fetchrow", no_local_rows)
    monkeypatch.setattr(property_data, "get_flood_zone", mapped_sfha)

    result = _call()

    assert result.fema_zone == "VE"
    assert result.flood_insurance_required is True
    assert result.data_available is True
    assert "wave action" in result.zone_description


def test_local_nfhl_rows_win_and_skip_the_live_call(monkeypatch):
    """A deployment that loaded NFHL extracts is served without a round trip."""
    live_calls = []

    async def local_hit(*_args, **_kwargs):
        return {
            "flood_zone": "AE",
            "firm_panel": "3720123400J",
            "firm_date": None,
            "community_name": "Brunswick County",
            "community_number": "370295",
        }

    async def record_live(*_args, **_kwargs):
        live_calls.append((_args, _kwargs))
        return None

    monkeypatch.setattr(market, "_fetchrow", local_hit)
    monkeypatch.setattr(property_data, "get_flood_zone", record_live)

    result = _call()

    assert result.fema_zone == "AE"
    assert result.flood_insurance_required is True
    assert result.data_available is True
    assert result.community_name == "Brunswick County"
    assert live_calls == []
