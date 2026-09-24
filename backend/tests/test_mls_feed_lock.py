"""The per-feed sync lock, and the fact that both feeds actually take it.

Concurrency here is not a performance concern. Two concurrent walks of one feed
interleave their cursor advances, and the loser's cursor can move the feed PAST
records the winner never wrote. A later delta sync will never fetch those
records, because the cursor says they are done — permanent data loss with no
error anywhere.

The durable-job layer dedupes SCHEDULED runs. It does not dedupe an operator's
`run_now`, which mints a fresh idempotency key on every click.
"""

from __future__ import annotations

import asyncio
import inspect
from contextlib import asynccontextmanager

import pytest

import mls_feed_lock
from mls_feed_lock import FeedBusy, feed_sync_lock, feed_sync_session_lock


class FakeConn:
    """Records the advisory-lock calls and answers the first one."""

    def __init__(self, granted: bool = True, in_transaction: bool = True):
        self.granted = granted
        self._in_tx = in_transaction
        self.calls: list[tuple[str, tuple]] = []

    def is_in_transaction(self) -> bool:
        return self._in_tx

    async def fetchval(self, sql, *args):
        self.calls.append((sql, args))
        return self.granted

    async def execute(self, sql, *args):
        self.calls.append((sql, args))
        return "SELECT 1"


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        @asynccontextmanager
        async def _acquire():
            yield conn

        return _acquire()


def _pool_of(monkeypatch, *, granted: bool) -> FakeConn:
    """Point the lock at a stub pool. Patches the real `db.connection.get_pool`
    rather than swapping the module, so a rename of that function breaks these
    tests instead of silently bypassing them."""
    import db.connection

    conn = FakeConn(granted=granted)
    monkeypatch.setattr(db.connection, "get_pool", lambda: FakePool(conn))
    return conn


# ── the session-scoped lock, which is what a sync actually uses ─────────────

def test_session_lock_is_released_on_the_same_connection_that_took_it(monkeypatch):
    conn = _pool_of(monkeypatch, granted=True)

    async def run():
        async with feed_sync_session_lock("bridge_dev") as acquired:
            assert acquired is True

    asyncio.run(run())

    sqls = [c[0] for c in conn.calls]
    assert any("pg_try_advisory_lock" in s for s in sqls), "should take a session lock"
    assert any("pg_advisory_unlock" in s for s in sqls), (
        "a session lock is owned by its session and must be released explicitly — "
        "unlike the xact form, it does not go away on commit"
    )
    # Same key both times, or the unlock releases nothing.
    assert conn.calls[0][1] == conn.calls[-1][1]


def test_session_lock_releases_even_when_the_sync_raises(monkeypatch):
    conn = _pool_of(monkeypatch, granted=True)

    async def run():
        with pytest.raises(ValueError):
            async with feed_sync_session_lock("bridge_dev"):
                raise ValueError("board timed out mid-walk")

    asyncio.run(run())

    assert any("pg_advisory_unlock" in c[0] for c in conn.calls), (
        "a feed left locked by a failed sync would never sync again"
    )


def test_a_busy_feed_yields_false_rather_than_waiting(monkeypatch):
    conn = _pool_of(monkeypatch, granted=False)
    seen = []

    async def run():
        async with feed_sync_session_lock("bridge_dev") as acquired:
            seen.append(acquired)

    asyncio.run(run())

    assert seen == [False]
    assert not any("pg_advisory_unlock" in c[0] for c in conn.calls), (
        "unlocking a lock this connection never held would release another "
        "worker's claim on the feed"
    )


# ── the transaction-scoped form keeps its guard ────────────────────────────

def test_xact_lock_refuses_autocommit():
    """A transaction-scoped lock taken outside a transaction excludes nothing,
    and would report success while doing so."""
    conn = FakeConn(granted=True, in_transaction=False)

    async def run():
        async with feed_sync_lock(conn, "bridge_dev"):
            pass

    with pytest.raises(RuntimeError, match="requires an open transaction"):
        asyncio.run(run())


def test_xact_lock_raises_feed_busy_when_not_granted():
    conn = FakeConn(granted=False)

    async def run():
        async with feed_sync_lock(conn, "bridge_dev"):
            pass

    with pytest.raises(FeedBusy):
        asyncio.run(run())


# ── and the part that was actually missing: somebody calls it ──────────────

@pytest.mark.parametrize(
    "module_name, class_name",
    [
        ("data_integrations.bridge_listings_feed", "BridgeListingsFeed"),
        ("data_integrations.listings_feed", "RESOListingsFeed"),
    ],
)
def test_every_feed_takes_the_lock_before_syncing(module_name, class_name):
    """The lock existed, was correct, and was called by nothing at all for its
    whole first life. Correct and unreferenced is indistinguishable from absent,
    so the wiring is the thing under test here — not the implementation.
    """
    module = __import__(module_name, fromlist=[class_name])
    feed = getattr(module, class_name)

    source = inspect.getsource(feed.sync_once)
    assert "feed_sync_session_lock" in source, (
        f"{class_name}.sync_once does not take the per-feed lock"
    )
    assert hasattr(feed, "_sync_once_unlocked"), (
        "the unlocked body should stay reachable for the lock wrapper to call"
    )
    # The cheap config check must come first: a lock is a pooled connection.
    assert source.index("ORACLE_INGEST_TENANT_ID") < source.index("async with feed_sync_session_lock"), (
        "taking a connection to discover the feed is unconfigured is work on "
        "every tick of a deployment that has no ingest tenant"
    )
