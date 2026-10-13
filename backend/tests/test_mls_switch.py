"""MLS is off by one switch (owner decision D-2026-10-12-01; prompt 11 T11.01):
every MLS route refuses, the sync jobs skip, AI tools skip the MLS leg, health
does not degrade -- and the public-records catalog keeps working at its new,
non-MLS address with the old path as an alias. Nothing is deleted."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

import mls_switch


def _app_routes():
    import os

    os.environ.setdefault("ORACLE_ENV", "dev")
    import server

    return server.app.routes


def _deps(route):
    names = set()

    def walk(dependant):
        for d in dependant.dependencies:
            if d.call is not None:
                names.add(getattr(d.call, "__name__", ""))
            walk(d)

    walk(route.dependant)
    return names


def test_off_unless_explicitly_enabled(monkeypatch):
    monkeypatch.delenv("ORACLE_MLS_ENABLED", raising=False)
    assert mls_switch.mls_enabled() is False
    with pytest.raises(HTTPException) as exc:
        asyncio.run(mls_switch.require_mls_enabled())
    assert exc.value.status_code == 404
    monkeypatch.setenv("ORACLE_MLS_ENABLED", "1")
    assert mls_switch.mls_enabled() is True
    asyncio.run(mls_switch.require_mls_enabled())


def test_every_mls_route_is_behind_the_switch_except_public_records():
    ungated = []
    for route in _app_routes():
        path = getattr(route, "path", "")
        if "/mls" not in path or not hasattr(route, "dependant"):
            continue
        if "/public-records" in path or path.startswith("/api/mls/pipeline"):
            continue   # the non-MLS catalog's legacy aliases stay open
        if path == "/api/admin/privacy/mls-feeds/{mls_id}/purge":
            continue   # privacy duty: a lapsed licence may REQUIRE a purge while MLS is off (platform admin only)
        if "require_mls_enabled" not in _deps(route):
            ungated.append(path)
    assert ungated == [], f"MLS routes not behind ORACLE_MLS_ENABLED: {ungated}"


def test_public_records_live_at_a_non_mls_address_and_the_alias_still_works():
    paths = {getattr(r, "path", ""): r for r in _app_routes()}
    for new, old in (("/api/property-records", "/api/mls/public-records"),
                     ("/api/property-records/{record_id}", "/api/mls/public-records/{record_id}")):
        assert new in paths and old in paths, (new, old)
        assert paths[new].endpoint is paths[old].endpoint, "the alias is the same handler"
        assert "require_mls_enabled" not in _deps(paths[new])


def test_the_sync_jobs_skip_while_mls_is_off(monkeypatch):
    from data_integrations import periodic

    monkeypatch.delenv("ORACLE_MLS_ENABLED", raising=False)
    for task in (periodic._new_listings_task, periodic._bridge_listings_task):
        assert asyncio.run(task()) == {"skipped": mls_switch.NOT_OFFERED}


def test_health_does_not_degrade_while_mls_is_off(monkeypatch):
    import component_health as ch

    monkeypatch.delenv("ORACLE_MLS_ENABLED", raising=False)
    result = ch._mls([{"mls_id": "actris", "last_error_class": "auth", "consecutive_failures": 9}])
    assert result["state"] == ch.NOT_CONFIGURED and result["state"] not in ch.ALERTING
