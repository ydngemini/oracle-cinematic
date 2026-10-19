"""A dead AVM credential must not generate unbounded provider traffic.

The hole this closes has a precise shape. value_property deliberately caches
only successes — negative-caching per PROPERTY would let one transient 429
poison that property for a week, and that choice is correct and untouched. But
an auth failure is not a fact about a property: a rejected key is wrong for
every address at once, so the per-property cache can never absorb it. Observed
live: an invalid RentCast key turned a UI sweep into an endless 403 stream,
one doomed provider call per valuation request.

The fix is a per-PROVIDER cooldown on 401/403 only. Its boundaries matter as
much as its existence, so they are pinned here too: transient failures (429,
5xx) must NOT trip it, and it must expire on its own so a rotated key is picked
up without a restart.
"""

import asyncio
import time

import pytest

import avm_client


@pytest.fixture(autouse=True)
def _clean_cooldowns():
    avm_client._credential_down_until.clear()
    yield
    avm_client._credential_down_until.clear()


class _FakeResp:
    def __init__(self, status: int, body: dict | None = None):
        self.status = status
        self._body = body or {}

    async def json(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    """Counts requests, so 'no call was made' is an assertion not a guess."""

    def __init__(self, status: int, body: dict | None = None):
        self.status = status
        self.body = body
        self.calls = 0

    def get(self, url, **kwargs):
        self.calls += 1
        return _FakeResp(self.status, self.body)


SUBJECT = {"address": "505 Northwest Front Street, Milford, DE"}


def _rentcast(session, monkeypatch):
    monkeypatch.setenv("RENTCAST_API_KEY", "some-key")
    return asyncio.run(avm_client._rentcast_avm(session, SUBJECT))


class TestCooldownTrips:
    @pytest.mark.parametrize("status", [401, 403])
    def test_auth_failure_stops_subsequent_calls(self, monkeypatch, status):
        session = _FakeSession(status)
        assert _rentcast(session, monkeypatch) is None
        assert session.calls == 1
        # The click-storm: 50 more valuation requests. Zero must reach RentCast.
        for _ in range(50):
            assert _rentcast(session, monkeypatch) is None
        assert session.calls == 1

    def test_cooldown_is_per_provider_not_global(self, monkeypatch):
        # A dead RentCast key must not silence ATTOM, which may be healthy.
        _rentcast(_FakeSession(403), monkeypatch)
        assert avm_client._credential_down("RentCast") is True
        assert avm_client._credential_down("ATTOM") is False

    def test_attom_auth_failure_also_trips(self, monkeypatch):
        monkeypatch.setenv("ATTOM_API_KEY", "some-key")
        session = _FakeSession(401)
        asyncio.run(avm_client._attom_avm(session, SUBJECT))
        asyncio.run(avm_client._attom_avm(session, SUBJECT))
        assert session.calls == 1


class TestCooldownBoundaries:
    """What must NOT trip it — this is where an overeager fix would hide real
    behaviour."""

    @pytest.mark.parametrize("status", [404, 429, 500, 503])
    def test_transient_and_data_failures_keep_retrying(self, monkeypatch, status):
        # A 429 today should not suppress a call tomorrow; a 404 for one address
        # says nothing about the key. Both must go through every time.
        session = _FakeSession(status)
        for _ in range(3):
            assert _rentcast(session, monkeypatch) is None
        assert session.calls == 3
        assert avm_client._credential_down("RentCast") is False

    def test_cooldown_expires_so_a_rotated_key_is_picked_up(self, monkeypatch):
        session = _FakeSession(403)
        _rentcast(session, monkeypatch)
        assert session.calls == 1
        # Simulate the cooldown lapsing rather than sleeping through it.
        avm_client._credential_down_until["RentCast"] = time.monotonic() - 1
        _rentcast(session, monkeypatch)
        assert session.calls == 2

    def test_success_after_recovery_flows_normally(self, monkeypatch):
        _rentcast(_FakeSession(403), monkeypatch)
        avm_client._credential_down_until.clear()   # operator rotated the key
        ok = _FakeSession(200, {
            "price": 250_000, "priceRangeLow": 230_000, "priceRangeHigh": 270_000,
            "comparables": [],
        })
        result = _rentcast(ok, monkeypatch)
        assert result is not None
        assert result.value == 250_000


# ── A provider failure reaches health (L19-05, AC19.07) ─────────────────────

class _SessionCtx:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *exc):
        return False


@pytest.mark.parametrize("provider, key, status, kind, health", [
    ("rentcast", "RENTCAST_API_KEY", 401, "credential_rejected", "AUTH_FAILED"),
    ("rentcast", "RENTCAST_API_KEY", 503, "http_error", None),
    ("rentcast", "RENTCAST_API_KEY", 429, "rate_limited", "RATE_LIMITED"),
    ("attom", "ATTOM_API_KEY", 401, "credential_rejected", "AUTH_FAILED"),
    ("attom", "ATTOM_API_KEY", 503, "http_error", None),
])
def test_an_injected_provider_failure_is_recorded_and_shown_in_health(monkeypatch, provider, key, status, kind,
                                                                         health):
    import component_health as ch
    import provider_health

    monkeypatch.delenv("RENTCAST_API_KEY", raising=False)
    monkeypatch.delenv("ATTOM_API_KEY", raising=False)
    monkeypatch.setenv(key, "some-key")
    recorded = []

    async def record(name, outcome):
        recorded.append((name, outcome))

    monkeypatch.setattr(provider_health, "record", record)
    monkeypatch.setattr(avm_client.aiohttp, "ClientSession", lambda **kw: _SessionCtx(_FakeSession(status)))
    asyncio.run(avm_client._value_property_uncached(SUBJECT))
    assert (provider, kind) in recorded, recorded
    other = "attom" if provider == "rentcast" else "rentcast"
    assert not any(name == other and outcome for name, outcome in recorded), "an unconfigured provider is not recorded"

    rows = [{"provider": provider, "consecutive_failures": 1, "last_failure_kind": kind}]
    state = ch._valuation_providers(rows)["state"]
    # A single 5xx is not yet an outage; three in a row are (DEGRADED).
    assert state == (health or ch.HEALTHY)
    rows[0]["consecutive_failures"] = 3
    if health is None:
        assert ch._valuation_providers(rows)["state"] == ch.DEGRADED
