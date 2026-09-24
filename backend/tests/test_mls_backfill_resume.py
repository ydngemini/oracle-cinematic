"""A backfill must survive a restart.

A real board is hundreds of thousands of rows. The walk position used to live
only in a local variable, and the dispatch keyed on "is there a status row?" —
so a partial walk wrote a status row, and every later run took the DELTA path
instead of finishing. A delta only sees records modified inside the lookback
window, which for a reference dataset with frozen timestamps is none of them.

Net effect: a backfill interrupted at page 30 of 264 stayed at page 30 forever
while reporting success. These tests are the proof it no longer can.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest

from data_integrations.bridge_listings_feed import BridgeListingsFeed


class FakeConn:
    def __init__(self, status_row=None):
        self.status_row = status_row
        self.executed: list[tuple] = []
        self.upserts = 0

    async def fetchrow(self, query, *args):
        if "mls_sync_status" in query:
            return self.status_row
        return None

    async def execute(self, query, *args):
        self.executed.append((query, args))
        return "UPDATE 1"

    async def fetch(self, query, *args):
        return []


pytestmark = pytest.mark.usefixtures("granted_feed_lock")


def fake_tx(conn):
    @asynccontextmanager
    async def tx(_ctx):
        yield conn
    return tx


def feed(**over):
    from data_integrations.bridge_listings_feed import BridgeFeedConfig
    cfg = {
        "dataset": "brightmls", "access_token": "t", "mls_id": "bright",
        "mls_name": "Bright", "page_size": 2, "lookback_hours": 1,
        "max_pages": 2, "backfill_max_pages": 2,
    }
    cfg.update(over)
    return BridgeListingsFeed(BridgeFeedConfig(**cfg))


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def test_no_status_row_means_backfill():
    f = feed()
    conn = FakeConn(status_row=None)
    called = {}

    async def fake_backfill(ctx, tx):
        called["backfill"] = True
        return {"state": "succeeded"}

    f._backfill_once = fake_backfill
    import data_integrations.bridge_listings_feed as mod
    import os
    os.environ["ORACLE_INGEST_TENANT_ID"] = "00000000-0000-0000-0000-000000000000"
    orig = mod.__dict__.get("tenant_tx")
    try:
        asyncio.run(_run_sync(f, conn))
    finally:
        pass
    assert called.get("backfill")


def test_an_unfinished_backfill_resumes_instead_of_falling_through_to_delta():
    """The bug: a partial walk wrote a status row, so every later run took the
    delta path and the remaining pages were never fetched."""
    f = feed()
    conn = FakeConn(status_row={"last_sync_at": None, "backfill_complete": False})
    called = {}

    async def fake_backfill(ctx, tx):
        called["backfill"] = True
        return {"state": "partial"}

    f._backfill_once = fake_backfill
    asyncio.run(_run_sync(f, conn))
    assert called.get("backfill"), "a partial backfill must resume, not switch to delta"


def test_a_completed_backfill_hands_over_to_delta():
    f = feed()
    conn = FakeConn(status_row={"last_sync_at": None, "backfill_complete": True})
    called = {}

    async def fake_backfill(ctx, tx):
        called["backfill"] = True
        return {}

    f._backfill_once = fake_backfill
    # The delta path will fail on network; we only care that backfill was not chosen.
    try:
        asyncio.run(_run_sync(f, conn))
    except Exception:
        pass
    assert not called.get("backfill")


async def _run_sync(f, conn):
    import data_integrations.bridge_listings_feed as mod
    import os
    os.environ["ORACLE_INGEST_TENANT_ID"] = "00000000-0000-0000-0000-000000000000"

    @asynccontextmanager
    async def tx(_ctx):
        yield conn

    # sync_once imports tenant_tx locally, so patch where it looks it up.
    import db.connection as dbc
    real = dbc.tenant_tx
    dbc.tenant_tx = tx
    try:
        return await f.sync_once()
    finally:
        dbc.tenant_tx = real


# ---------------------------------------------------------------------------
# Licence on backfilled rows
# ---------------------------------------------------------------------------

def test_backfilled_rows_carry_the_feeds_licence(monkeypatch):
    monkeypatch.setenv("ORACLE_MLS_LICENSED", "1")
    monkeypatch.setenv("ORACLE_MLS_AGREEMENT_REF", "BRIGHT-2026-001")
    assert feed()._license_classification() == "licensed_property_listing"


def test_backfilled_rows_default_to_developer(monkeypatch):
    monkeypatch.delenv("ORACLE_MLS_LICENSED", raising=False)
    assert feed()._license_classification() == "developer_listing_dataset"


def test_a_reference_dataset_backfills_as_developer(monkeypatch):
    monkeypatch.setenv("ORACLE_MLS_LICENSED", "1")
    monkeypatch.setenv("ORACLE_MLS_AGREEMENT_REF", "ACTRIS-2026")
    assert feed(dataset="actris_ref")._license_classification() == "developer_listing_dataset"
