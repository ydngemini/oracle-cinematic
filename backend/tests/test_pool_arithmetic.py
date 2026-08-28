"""Pool capacity arithmetic and the connection health probe.

Two things P3 changed, neither of which had coverage:

1. **`ORACLE_DB_POOL_MAX` now means what it says.** `ws_hub` keeps one
   connection open for the process lifetime to run LISTEN, so a pool configured
   for 10 could only ever hand 9 to requests. `init_pool` widens the pool by
   `LISTENER_RESERVED_CONNECTIONS` so the configured number stays the number an
   operator sizes against Postgres `max_connections`.

2. **The health probe is age-based.** `SELECT 1` on every checkout costs a full
   round trip, and this deployment runs North Central US against a database in
   Central US. Connections used recently skip it.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from db import connection as dbc


# ---------------------------------------------------------------------------
# Capacity arithmetic
# ---------------------------------------------------------------------------

def test_the_listener_reservation_is_a_named_constant():
    """A bare `+ 1` in init_pool and a bare `- 1` in pool_stats would be two
    independent claims about the same fact."""
    assert dbc.LISTENER_RESERVED_CONNECTIONS == 1

    init_source = inspect.getsource(dbc.init_pool)
    stats_source = inspect.getsource(dbc.pool_stats)
    assert "LISTENER_RESERVED_CONNECTIONS" in init_source
    assert "LISTENER_RESERVED_CONNECTIONS" in stats_source


def test_init_pool_widens_the_requested_maximum(monkeypatch):
    """The pool asyncpg builds must be larger than what the operator asked for,
    by exactly the reserved amount."""
    captured: dict = {}

    class _FakePool:
        def get_min_size(self):
            return captured["min_size"]

        def get_max_size(self):
            return captured["max_size"]

        def get_size(self):
            return 0

        def get_idle_size(self):
            return 0

        def acquire(self):
            # init_pool now verifies the connection role is one RLS applies to;
            # a superuser or BYPASSRLS role means tenant isolation is off.
            # oracle_app_login is neither, so the check passes quietly.
            from contextlib import asynccontextmanager

            @asynccontextmanager
            async def _ctx():
                class _Conn:
                    async def fetchrow(self, _query):
                        return {"role": "oracle_app_login", "exempt": False}

                yield _Conn()

            return _ctx()

    async def _fake_create_pool(**kwargs):
        captured.update(kwargs)
        return _FakePool()

    import sys
    import types

    fake_asyncpg = types.ModuleType("asyncpg")
    fake_asyncpg.create_pool = _fake_create_pool
    monkeypatch.setitem(sys.modules, "asyncpg", fake_asyncpg)
    monkeypatch.setattr(dbc, "_pool", None)
    monkeypatch.setattr(dbc, "DB_PASSWORD", "local-dev")

    asyncio.run(dbc.init_pool(min_size=2, max_size=10))

    assert captured["max_size"] == 10 + dbc.LISTENER_RESERVED_CONNECTIONS, (
        "the pool was not widened, so a request can only obtain 9 of a configured 10"
    )
    assert captured["min_size"] == 2, "the floor is unaffected by the reservation"

    stats = dbc.pool_stats()
    assert stats["usable_for_requests"] == 10, (
        "usable capacity must equal what the operator configured"
    )
    assert stats["reserved_for_listener"] == dbc.LISTENER_RESERVED_CONNECTIONS
    monkeypatch.setattr(dbc, "_pool", None)


def test_pool_stats_is_empty_before_initialisation(monkeypatch):
    """An uninitialised pool reports nothing rather than fabricating zeros that
    would read as a real measurement of a real pool."""
    monkeypatch.setattr(dbc, "_pool", None)
    assert dbc.pool_stats() == {}


def test_usable_never_goes_negative(monkeypatch):
    """A pool smaller than the reservation is a misconfiguration, but it must
    not produce a negative capacity that then propagates into sizing advice."""

    class _TinyPool:
        def get_min_size(self):
            return 0

        def get_max_size(self):
            return 0

        def get_size(self):
            return 0

        def get_idle_size(self):
            return 0

    monkeypatch.setattr(dbc, "_pool", _TinyPool())
    assert dbc.pool_stats()["usable_for_requests"] == 0
    monkeypatch.setattr(dbc, "_pool", None)


# ---------------------------------------------------------------------------
# The health probe
# ---------------------------------------------------------------------------

class _Conn:
    """Counts probes.

    Mimics asyncpg's PoolConnectionProxy contract, which is the detail that
    bit in production: the proxy defines __slots__ WITHOUT __weakref__, so it
    can neither be weak-referenced nor tagged with an attribute. The cache must
    therefore key on `get_server_pid()` — public API on the proxy — and these
    doubles enforce both properties so a test double can never again be more
    permissive than the real object.
    """

    __slots__ = ("probes", "_pid")  # no __weakref__, like the real proxy

    _next_pid = iter(range(40_000, 50_000))

    def __init__(self, pid: int | None = None):
        self.probes = 0
        self._pid = pid if pid is not None else next(self._next_pid)

    def get_server_pid(self) -> int:
        return self._pid

    async def execute(self, query):
        assert query == "SELECT 1"
        self.probes += 1


def test_the_real_proxy_shape_cannot_be_weakly_referenced():
    """Pin the constraint that broke the first implementation. If this ever
    starts passing weakly, the cache could go back to WeakKeyDictionary."""
    import weakref

    with pytest.raises(TypeError):
        weakref.ref(_Conn())


def test_the_first_checkout_is_always_probed(monkeypatch):
    """A connection never seen before has no verification to skip."""
    monkeypatch.setattr(dbc, "_connection_last_verified", {})
    conn = _Conn()
    asyncio.run(dbc._health_check_connection(conn))
    assert conn.probes == 1


def test_a_recently_verified_connection_skips_the_round_trip(monkeypatch):
    """The whole point: back-to-back checkouts do not each pay a cross-region
    round trip."""
    monkeypatch.setattr(dbc, "_connection_last_verified", {})
    conn = _Conn()
    for _ in range(5):
        asyncio.run(dbc._health_check_connection(conn))
    assert conn.probes == 1, f"probed {conn.probes} times; the interval is not being honoured"


def test_a_replacement_connection_does_not_inherit_verification(monkeypatch):
    """asyncpg replaces a broken connection with a new one, which has a new
    backend PID. The old entry must not vouch for it."""
    monkeypatch.setattr(dbc, "_connection_last_verified", {})
    old = _Conn(pid=51_001)
    asyncio.run(dbc._health_check_connection(old))

    replacement = _Conn(pid=51_002)
    asyncio.run(dbc._health_check_connection(replacement))
    assert replacement.probes == 1, "a fresh connection skipped its first probe"


def test_a_connection_idle_past_the_interval_is_probed_again(monkeypatch):
    """The stale connection — the one a firewall or failover may have dropped —
    is exactly the case still checked."""
    monkeypatch.setattr(dbc, "_connection_last_verified", {})
    conn = _Conn()
    asyncio.run(dbc._health_check_connection(conn))

    # Advance the clock past the interval rather than sleeping through it.
    real_monotonic = dbc.time.monotonic
    offset = dbc.HEALTH_CHECK_INTERVAL_SECONDS + 1
    monkeypatch.setattr(dbc.time, "monotonic", lambda: real_monotonic() + offset)

    asyncio.run(dbc._health_check_connection(conn))
    assert conn.probes == 2


def test_a_zero_interval_restores_probe_on_every_checkout(monkeypatch):
    """The escape hatch has to actually work — an operator who distrusts the
    optimisation can set ORACLE_DB_HEALTH_CHECK_INTERVAL=0 and get the old
    behaviour back without a code change."""
    monkeypatch.setattr(dbc, "_connection_last_verified", {})
    monkeypatch.setattr(dbc, "HEALTH_CHECK_INTERVAL_SECONDS", 0.0)
    conn = _Conn()
    for _ in range(3):
        asyncio.run(dbc._health_check_connection(conn))
    assert conn.probes == 3


def test_a_connection_without_a_pid_is_probed_every_time(monkeypatch):
    """No key, no cache entry — degradation is more probing, never less."""
    monkeypatch.setattr(dbc, "_connection_last_verified", {})

    class _NoPid:
        def __init__(self):
            self.probes = 0

        def get_server_pid(self):
            raise RuntimeError("connection is being torn down")

        async def execute(self, query):
            self.probes += 1

    conn = _NoPid()
    for _ in range(3):
        asyncio.run(dbc._health_check_connection(conn))
    assert conn.probes == 3
    assert dbc._connection_last_verified == {}


def test_the_verification_cache_is_pruned_rather_than_growing_forever(monkeypatch):
    """One tiny entry per replaced connection still adds up over months.
    Entries older than the interval change no behaviour, so they are dropped
    once the dict outgrows its bound."""
    monkeypatch.setattr(dbc, "_connection_last_verified", {})

    real_monotonic = dbc.time.monotonic
    base = real_monotonic()
    stale_by = dbc.HEALTH_CHECK_INTERVAL_SECONDS + 5

    # Fill past the bound with entries that are already stale...
    for pid in range(dbc._VERIFICATION_CACHE_MAX + 5):
        dbc._connection_last_verified[pid] = base - stale_by

    # ...then verify one live connection, which triggers the prune.
    asyncio.run(dbc._health_check_connection(_Conn(pid=60_000)))

    assert len(dbc._connection_last_verified) == 1, (
        "stale entries survived the prune"
    )
    assert 60_000 in dbc._connection_last_verified


# ---------------------------------------------------------------------------
# Worker count, which was the one hardcoded number among tunable siblings
# ---------------------------------------------------------------------------

def test_voice_worker_count_is_env_tunable():
    import voice_intel

    source = inspect.getsource(voice_intel)
    assert "ORACLE_VOICE_WORKERS" in source
    assert voice_intel.WORKER_COUNT >= 1, "at least one worker, or nothing transcribes"
