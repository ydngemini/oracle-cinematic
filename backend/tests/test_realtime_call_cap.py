"""A brokerage's concurrent realtime AI phone calls are bounded (prompt 09,
T09.10, AC09.02).

Each open Plivo/Twilio media bridge holds a hosted realtime model session
billed to the brokerage for the whole call; nothing capped how many one
brokerage could hold. These run without a database or a carrier: the media
routes are driven with the same fakes as test_ai_budget_hosted_calls.py.
The count is per process (a stated limit), not cluster-wide.
"""

from __future__ import annotations

import asyncio

import pytest

import commands_api
from tests.test_ai_budget_hosted_calls import (  # noqa: F401  (controls is a fixture)
    CALLER,
    OTHER_TENANT,
    TENANT,
    _MEDIA_ROUTES,
    _bridge_recorder,
    _refusal_was_said,
    controls,
)

ROUTES = ["twilio", "plivo", "plivo-elevenlabs"]


@pytest.fixture(autouse=True)
def _clean_slots(monkeypatch):
    monkeypatch.setattr(commands_api, "_REALTIME_ACTIVE", {})
    monkeypatch.delenv("ORACLE_REALTIME_MAX_CALLS_PER_BROKERAGE", raising=False)


def _state(tenant=TENANT):
    return {"tenant_id": tenant, "caller": CALLER, "callee": CALLER, "stage": "greeting"}


@pytest.mark.parametrize("value,expected", [
    (None, 20), ("", 20), ("5", 5), ("0", 0), ("-3", 20), ("many", 20), (" 7 ", 7),
])
def test_cap_setting_parses_and_falls_back(monkeypatch, value, expected):
    if value is not None:
        monkeypatch.setenv("ORACLE_REALTIME_MAX_CALLS_PER_BROKERAGE", value)
    assert commands_api._realtime_call_cap() == expected


def test_slots_stop_at_the_cap_and_come_back_on_release(monkeypatch):
    monkeypatch.setenv("ORACLE_REALTIME_MAX_CALLS_PER_BROKERAGE", "2")
    assert commands_api._acquire_realtime_slot(TENANT)
    assert commands_api._acquire_realtime_slot(TENANT)
    assert not commands_api._acquire_realtime_slot(TENANT)
    assert commands_api._acquire_realtime_slot(OTHER_TENANT), "another brokerage has its own count"
    commands_api._release_realtime_slot(TENANT)
    assert commands_api._acquire_realtime_slot(TENANT)


def test_zero_means_no_cap(monkeypatch):
    monkeypatch.setenv("ORACLE_REALTIME_MAX_CALLS_PER_BROKERAGE", "0")
    assert all(commands_api._acquire_realtime_slot(TENANT) for _ in range(100))


@pytest.mark.parametrize("route", ROUTES)
def test_a_brokerage_at_its_cap_never_opens_another_bridge(monkeypatch, controls, caplog, route):
    monkeypatch.setenv("ORACLE_REALTIME_MAX_CALLS_PER_BROKERAGE", "2")
    commands_api._REALTIME_ACTIVE[TENANT] = 2
    opened: list = []
    caplog.set_level("INFO")
    run, socket, said = _MEDIA_ROUTES[route](monkeypatch, _state(), opened)
    asyncio.run(run)
    assert opened == [], "a realtime model was opened past the brokerage's call cap"
    _refusal_was_said(route, said())
    assert socket.closed_code == 1000
    assert "brokerage_call_cap" in caplog.text
    assert commands_api._REALTIME_ACTIVE == {TENANT: 2}, "a refused call must not change the count"


@pytest.mark.parametrize("route", ROUTES)
def test_another_brokerage_at_its_cap_does_not_block_this_one(monkeypatch, controls, route):
    monkeypatch.setenv("ORACLE_REALTIME_MAX_CALLS_PER_BROKERAGE", "2")
    commands_api._REALTIME_ACTIVE[OTHER_TENANT] = 2
    opened: list = []
    run, _socket, _said = _MEDIA_ROUTES[route](monkeypatch, _state(), opened)
    asyncio.run(run)
    assert opened == ["constructed", "run"]
    assert commands_api._REALTIME_ACTIVE == {OTHER_TENANT: 2}, "the slot taken was given back"


@pytest.mark.parametrize("route", ["plivo", "twilio"])
def test_the_slot_is_released_when_the_bridge_fails(monkeypatch, controls, route):
    import qwen_omni_realtime

    monkeypatch.setenv("ORACLE_REALTIME_MAX_CALLS_PER_BROKERAGE", "1")
    opened: list = []
    run, socket, _said = _MEDIA_ROUTES[route](monkeypatch, _state(), opened)

    class _Exploding(_bridge_recorder(opened)):
        async def run(self):
            raise RuntimeError("media dropped")

    monkeypatch.setattr(
        qwen_omni_realtime,
        "TwilioQwenRealtimeBridge" if route == "twilio" else "PlivoQwenRealtimeBridge",
        _Exploding,
    )
    asyncio.run(run)
    assert socket.closed_code == 1011
    assert commands_api._REALTIME_ACTIVE == {}
    assert commands_api._acquire_realtime_slot(TENANT), "the failed call left its slot taken"
