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

ROUTES = ["acs", "twilio", "plivo", "plivo-elevenlabs"]


@pytest.fixture(autouse=True)
def _clean_slots(monkeypatch):
    monkeypatch.setattr(commands_api, "_REALTIME_ACTIVE", {})
    monkeypatch.delenv("ORACLE_REALTIME_MAX_CALLS_PER_BROKERAGE", raising=False)


def _state(tenant=TENANT):
    return {"tenant_id": tenant, "caller": CALLER, "callee": CALLER, "stage": "greeting"}


@pytest.mark.parametrize("value,expected", [
    (None, 5), ("", 5), ("5", 5), ("20", 20), ("-3", 5), ("many", 5), (" 7 ", 7),
])
def test_cap_setting_parses_and_falls_back(monkeypatch, value, expected):
    """Pilot default 5 (owner 2026-11-16); 20 stays configurable."""
    if value is not None:
        monkeypatch.setenv("ORACLE_REALTIME_MAX_CALLS_PER_BROKERAGE", value)
    assert commands_api._realtime_call_cap() == expected


@pytest.mark.parametrize("is_dev, expected", [(True, 0), (False, 5)])
def test_no_cap_is_refused_outside_dev(monkeypatch, is_dev, expected):
    """0 means unlimited only in dev: production can never be made unlimited
    by a typo or a copied dev setting."""
    import config

    monkeypatch.setattr(config, "IS_DEV", is_dev)
    monkeypatch.setenv("ORACLE_REALTIME_MAX_CALLS_PER_BROKERAGE", "0")
    assert commands_api._realtime_call_cap() == expected


def test_slots_stop_at_the_cap_and_come_back_on_release(monkeypatch):
    monkeypatch.setenv("ORACLE_REALTIME_MAX_CALLS_PER_BROKERAGE", "2")
    assert commands_api._acquire_realtime_slot(TENANT)
    assert commands_api._acquire_realtime_slot(TENANT)
    assert not commands_api._acquire_realtime_slot(TENANT)
    assert commands_api._acquire_realtime_slot(OTHER_TENANT), "another brokerage has its own count"
    commands_api._release_realtime_slot(TENANT)
    assert commands_api._acquire_realtime_slot(TENANT)


def test_zero_means_no_cap_in_dev(monkeypatch):
    import config

    monkeypatch.setattr(config, "IS_DEV", True)
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


_BRIDGE_NAMES = {"twilio": "TwilioQwenRealtimeBridge", "plivo": "PlivoQwenRealtimeBridge",
                 "acs": "QwenOmniRealtimeBridge"}


@pytest.mark.parametrize("route", ["acs", "plivo", "twilio"])
def test_the_slot_is_released_when_the_bridge_cannot_be_built(monkeypatch, controls, route):
    """Construction failure (Twilio/Plivo close 4400; ACS falls back) must
    give the slot back, or each failed call shrinks the brokerage's cap."""
    import qwen_omni_realtime

    monkeypatch.setenv("ORACLE_REALTIME_MAX_CALLS_PER_BROKERAGE", "1")
    opened: list = []
    run, _socket, _said = _MEDIA_ROUTES[route](monkeypatch, _state(), opened)

    class _Unbuildable:
        def __init__(self, *_a, **_k):
            raise qwen_omni_realtime.QwenRealtimeError("no session")

    monkeypatch.setattr(qwen_omni_realtime, _BRIDGE_NAMES[route], _Unbuildable)
    if route == "acs":
        # ACS builds its bridge outside its error handling (unchanged here):
        # the error still propagates, but the slot must come back regardless.
        with pytest.raises(qwen_omni_realtime.QwenRealtimeError):
            asyncio.run(run)
    else:
        asyncio.run(run)
    assert commands_api._REALTIME_ACTIVE == {}, "a call whose bridge never opened kept its slot"


@pytest.mark.parametrize("route", ["acs", "plivo", "twilio"])
def test_the_slot_is_released_when_the_call_task_is_cancelled(monkeypatch, controls, route):
    """A cancelled handler (server shutdown, client gone mid-await) raises
    CancelledError, which no `except Exception` catches: only finally helps."""
    import qwen_omni_realtime

    monkeypatch.setenv("ORACLE_REALTIME_MAX_CALLS_PER_BROKERAGE", "1")
    opened: list = []
    run, _socket, _said = _MEDIA_ROUTES[route](monkeypatch, _state(), opened)

    class _Cancelled(_bridge_recorder(opened)):
        async def run(self):
            raise asyncio.CancelledError

    monkeypatch.setattr(qwen_omni_realtime, _BRIDGE_NAMES[route], _Cancelled)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run)
    assert commands_api._REALTIME_ACTIVE == {}, "a cancelled call kept its slot"


def test_plivo_takes_no_slot_when_the_engine_cannot_be_chosen(monkeypatch, controls):
    """Choosing the realtime engine happens before the slot is taken, so a
    failure there cannot strand a slot outside the releasing try/finally."""
    import elevenlabs_realtime

    monkeypatch.setenv("ORACLE_REALTIME_MAX_CALLS_PER_BROKERAGE", "1")
    opened: list = []
    run, _socket, _said = _MEDIA_ROUTES["plivo"](monkeypatch, _state(), opened)

    def _broken():
        raise RuntimeError("engine setting unreadable")

    monkeypatch.setattr(elevenlabs_realtime, "realtime_provider", _broken)
    with pytest.raises(RuntimeError):
        asyncio.run(run)
    assert commands_api._REALTIME_ACTIVE == {}
