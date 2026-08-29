"""The coordinate backfill, proven without the Census endpoint.

8.59M public property records carry an address (93.8%) but only 4.3% carry
coordinates, so the map has almost nothing to plot and the radius comps search
— which migration 0076 built an index for — can only see 4% of the corpus. The
Census batch geocoder is keyless and already wired; nothing ever wrote its
answers back.

The geocoder is stubbed here so the suite never depends on a third-party
endpoint. What is pinned is the part that would corrupt data if wrong: which
rows are claimed, that the cursor advances, that unmatched rows are counted
rather than written, and that --dry-run writes nothing at all.

Live behaviour, measured 2026-08-29 against Delaware: 4,494 of 5,000 matched
(89.9%).
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest

import backfill_property_coordinates as backfill


class _Conn:
    def __init__(self, pages):
        self._pages = list(pages)
        self.written: list[tuple] = []
        self.claims: list[tuple] = []

    async def fetch(self, _query, after_id, state, limit):
        self.claims.append((after_id, state, limit))
        return self._pages.pop(0) if self._pages else []

    async def executemany(self, _query, rows):
        self.written.extend(rows)


def _row(id_, address="1 Main St", city="Milford", state="DE", zip_code="19963"):
    return {"id": id_, "address": address, "city": city, "state": state, "zip_code": zip_code}


def _install(monkeypatch, conn, results):
    @asynccontextmanager
    async def tx(_ctx):
        yield conn

    monkeypatch.setattr(backfill, "tenant_tx", tx)
    # Capture the real sleep first: `backfill.asyncio` IS the global module, so
    # patching through it replaces asyncio.sleep everywhere — including inside
    # the replacement, which recurses until the stack ends.
    real_sleep = asyncio.sleep

    async def _no_pause(_seconds):
        await real_sleep(0)

    monkeypatch.setattr(backfill.asyncio, "sleep", _no_pause)

    class _Geocoder:
        async def geocode_batch(self, addresses):
            assert len(addresses) == len(results), "one result per input, in order"
            return results

    monkeypatch.setattr(backfill, "CensusGeocoder", lambda: _Geocoder())


def test_matched_rows_are_written_and_unmatched_are_only_counted(monkeypatch, capsys):
    conn = _Conn([[_row("a"), _row("b"), _row("c")]])
    _install(monkeypatch, conn, [
        {"matched": True, "lat": 38.9, "lng": -75.4},
        {"matched": False, "lat": None, "lng": None},
        # Matched but coordinateless — the geocoder can resolve an address to a
        # census block without returning a point. Writing NULL would look like
        # progress while leaving the row exactly as unusable.
        {"matched": True, "lat": None, "lng": None},
    ])

    code = asyncio.run(backfill.run(state="DE", batch_size=3, dry_run=False, limit_batches=1, skip_preflight=True))

    assert code == 0
    assert conn.written == [("a", 38.9, -75.4)]
    assert "1/3 matched" in capsys.readouterr().out


def test_dry_run_writes_nothing_but_still_reports_the_rate(monkeypatch, capsys):
    conn = _Conn([[_row("a"), _row("b")]])
    _install(monkeypatch, conn, [
        {"matched": True, "lat": 1.0, "lng": 2.0},
        {"matched": True, "lat": 3.0, "lng": 4.0},
    ])

    asyncio.run(backfill.run(state=None, batch_size=2, dry_run=True, limit_batches=1, skip_preflight=True))

    assert conn.written == []
    assert "(dry run) geocoded 2 of 2" in capsys.readouterr().out


def test_the_cursor_advances_so_a_resumed_run_does_not_restart(monkeypatch):
    """Progress is the id cursor. Without it an interrupted 8M-row run would
    begin again at the first row every time."""
    conn = _Conn([[_row("a"), _row("b")], [_row("c")], []])
    _install(monkeypatch, conn, [{"matched": False, "lat": None, "lng": None}] * 2)

    # Pages differ in length here, so the strict one-result-per-input stub from
    # _install would not fit; a permissive one replaces it.
    class _Geocoder:
        async def geocode_batch(self, addresses):
            return [{"matched": False, "lat": None, "lng": None} for _ in addresses]

    monkeypatch.setattr(backfill, "CensusGeocoder", lambda: _Geocoder())

    asyncio.run(backfill.run(state=None, batch_size=2, dry_run=True, limit_batches=None, skip_preflight=True))

    after_ids = [claim[0] for claim in conn.claims]
    assert after_ids == [None, "b", "c"], "each page must resume after the last id seen"


def test_the_address_sent_carries_city_state_and_zip():
    """A bare street line matches far worse than a full one, and the columns
    are already in the row."""
    assert backfill._one_line(_row("a")) == "1 Main St, Milford, DE, 19963"
    # Blank components are dropped rather than sent as empty commas.
    assert backfill._one_line(_row("a", city="", zip_code="")) == "1 Main St, DE"


def test_an_endpoint_outage_is_reported_not_silently_successful(monkeypatch, capsys):
    """geocode_batch never raises — it degrades every row to unmatched. A run
    that resolved nothing must exit non-zero rather than look complete."""
    conn = _Conn([[_row("a")]])
    _install(monkeypatch, conn, [{"matched": False, "lat": None, "lng": None}])

    code = asyncio.run(backfill.run(state=None, batch_size=1, dry_run=False, limit_batches=1, skip_preflight=True))

    assert code == 1
    assert conn.written == []


# ── the preflight ────────────────────────────────────────────────────────────

def test_an_unreachable_geocoder_stops_before_touching_the_database(monkeypatch, capsys):
    """A blocked endpoint used to be indistinguishable from a zero-match run.

    `geocode_batch` never raises — it degrades every row to unmatched — so an
    operator would watch "0/5000 matched" scroll past for hours and conclude the
    addresses were bad. The probe converts that into an immediate, named failure.
    """
    claimed = False

    @asynccontextmanager
    async def tx(_ctx):
        nonlocal claimed
        claimed = True
        yield None

    monkeypatch.setattr(backfill, "tenant_tx", tx)
    monkeypatch.setattr(backfill, "_preflight", lambda *_a, **_k: "ConnectionResetError: nope")

    code = asyncio.run(
        backfill.run(state=None, batch_size=10, dry_run=False, limit_batches=1)
    )

    assert code == 2, "a distinct exit code, not the 1 that means 'resolved nothing'"
    assert claimed is False, "nothing should be read or written when the probe fails"
    err = capsys.readouterr().err
    assert "not reachable from this host" in err
    assert "Nothing was read or written" in err
    # Name the likely cause, since the operator cannot see the difference.
    assert "egress" in err or "PROXY" in err


def test_a_reachable_geocoder_proceeds(monkeypatch):
    conn = _Conn([[_row("a")]])
    _install(monkeypatch, conn, [{"matched": True, "lat": 5.0, "lng": 6.0}])
    monkeypatch.setattr(backfill, "_preflight", lambda *_a, **_k: None)

    code = asyncio.run(backfill.run(state=None, batch_size=1, dry_run=False, limit_batches=1))

    assert code == 0
    assert conn.written == [("a", 5.0, 6.0)]


# ── outage vs unmatchable data ───────────────────────────────────────────────

def test_an_outage_retries_the_same_page_and_then_stops(monkeypatch, capsys):
    """A failing endpoint must not walk the cursor through the whole corpus.

    geocode_batch degrades a transport failure into "every row unmatched", and
    Census is intermittent. Advancing past a failed page would march through all
    8.2M rows asking nothing, marking them seen, and reporting 0% as though the
    addresses were unmatchable — an eight-hour no-op that looks like a finding.
    """
    page = [_row(str(i)) for i in range(60)]   # >= _OUTAGE_MIN_BATCH
    conn = _Conn([page, page, page, page])
    attempts = 0

    class _DeadGeocoder:
        async def geocode_batch(self, addresses):
            nonlocal attempts
            attempts += 1
            return [{"matched": False, "lat": None, "lng": None} for _ in addresses]

    @asynccontextmanager
    async def tx(_ctx):
        yield conn

    real_sleep = asyncio.sleep

    async def _no_pause(_seconds):
        await real_sleep(0)

    monkeypatch.setattr(backfill, "tenant_tx", tx)
    monkeypatch.setattr(backfill.asyncio, "sleep", _no_pause)
    monkeypatch.setattr(backfill, "CensusGeocoder", lambda: _DeadGeocoder())
    monkeypatch.setattr(backfill, "_preflight", lambda *_a, **_k: None)

    code = asyncio.run(backfill.run(state=None, batch_size=60, dry_run=False, limit_batches=None))

    assert attempts == backfill._BATCH_ATTEMPTS, "the same page is retried, not skipped"
    assert conn.claims == [(None, None, 60)], "the cursor never advanced past the failed page"
    assert conn.written == []
    assert code == 1
    assert "geocoder outage" in capsys.readouterr().err


def test_a_genuinely_unmatchable_short_page_is_not_mistaken_for_an_outage(monkeypatch):
    """A handful of rural routes resolving to nothing is data, not an outage.

    Below _OUTAGE_MIN_BATCH the zero-match signal is too weak to act on, so the
    run continues rather than stopping on a tail page.
    """
    conn = _Conn([[_row("a"), _row("b")], []])
    _install(monkeypatch, conn, [{"matched": False, "lat": None, "lng": None}] * 2)
    monkeypatch.setattr(backfill, "_preflight", lambda *_a, **_k: None)

    code = asyncio.run(backfill.run(state=None, batch_size=2, dry_run=False, limit_batches=None))

    assert [c[0] for c in conn.claims] == [None, "b"], "a short page still advances"
    assert code == 1  # resolved nothing overall, which is still worth a non-zero exit
