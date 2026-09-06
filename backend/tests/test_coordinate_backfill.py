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
    # The canary also failed in this fixture, so the stop is now EVIDENCE of an
    # outage rather than an inference from the page being empty.
    assert "geocoder is not answering" in capsys.readouterr().err


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


# ── Census batch CSV shape ──────────────────────────────────────────────────
#
# geocode_batch tries three column shapes in order, each on what the last
# missed. Measured on 150 rows from each population:
#
#                            clean addresses   never-geocoded residue
#   1. one-line in `street`     150/150 (100%)        0/150   (0%)
#   2. street+city+state        136/150  (91%)       23/150  (15%)
#   3. street+state+ZIP5        142/150  (95%)       23/150  (15%)
#   cascade                     150/150 (100%)       39/150  (26%)
#
# Shape 1 MUST stay first. It is the strongest shape for any address the
# geocoder can resolve, and the ~90% live rate in the module docstring above is
# real. Measuring it against `latitude IS NULL` rows alone returns 0% — but
# those are by definition the rows it already failed on, so that number is a
# tautology. An earlier revision of this file reordered the passes on exactly
# that mistake and would have cost ~9% on every clean address.

from data_integrations.census_geocoder import _split_oneline


def test_split_oneline_recovers_components_the_backfill_joined():
    """_one_line joins with ', ' — this must take it apart again exactly."""
    assert _split_oneline("323 VT ROUTE 132, Strafford, VT, 05072") == (
        "323 VT ROUTE 132", "Strafford", "VT", "05072",
    )
    # State and ZIP sharing one trailing chunk, as free-text callers write it.
    assert _split_oneline("1600 Pennsylvania Ave NW, Washington, DC 20500") == (
        "1600 Pennsylvania Ave NW", "Washington", "DC", "20500",
    )


def test_split_oneline_handles_unhyphenated_zip_plus_four():
    """The WY harvester stores ZIP+4 with no hyphen: '827188362'.

    A \\b anchor finds no boundary inside a digit run, so this parsed as
    neither ZIP nor state and the whole 'WY, 827188362' tail was swallowed
    into the city field — 29 of 200 sampled rows. Census then had a garbage
    locality and matched nothing.
    """
    assert _split_oneline("2102 MINT AVE, WY, 827188362") == (
        "2102 MINT AVE", "", "WY", "82718",
    )
    # Hyphenated ZIP+4 keeps only the leading five for the same reason:
    # measured 15 matches on 5-digit vs 6 on the full ZIP+4.
    assert _split_oneline("1216 CASCADE DR, EVERETT, WA, 98203-6505") == (
        "1216 CASCADE DR", "EVERETT", "WA", "98203",
    )


def test_split_oneline_never_invents_a_state_or_zip():
    """An unrecognised fragment stays in the street rather than being guessed."""
    assert _split_oneline("123 Main St") == ("123 Main St", "", "", "")
    assert _split_oneline("VALLEY DR, Rural, AR") == ("VALLEY DR", "Rural", "AR", "")
    assert _split_oneline("") == ("", "", "", "")


def test_geocode_batch_tries_oneline_first_then_splits():
    """Three POSTs in order: one-line, then city+state, then state+ZIP."""
    import asyncio
    import csv as _csv
    import io as _io

    from data_integrations.census_geocoder import CensusGeocoder

    sent: list[list[list[str]]] = []

    class _Cache:
        async def get_or_fetch(self, _ns, key, fetcher, ttl=None):  # noqa: ARG002
            return await fetcher()

    geo = CensusGeocoder()
    geo._cache = _Cache()

    async def fake_post(csv_bytes: bytes) -> str:
        rows = list(_csv.reader(_io.StringIO(csv_bytes.decode())))
        sent.append(rows)
        # Nothing matches, so every row falls through to the second pass.
        return "\n".join(f'"{r[0]}","in","No_Match"' for r in rows)

    geo._post_batch = fake_post
    one_line = "9281 Kasota Way, South Harbor Twp, MN, 56359"
    asyncio.run(geo.geocode_batch([one_line]))

    assert len(sent) == 3, "expected one-line, then city, then zip"
    first, second, third = sent[0][0], sent[1][0], sent[2][0]

    # Pass 1 is the ORIGINAL shape and must stay first — it is the best shape
    # for a resolvable address (100% vs 91%/95% on clean rows).
    assert first[1] == one_line, "pass 1 must send the whole one-line address"
    assert first[2] == "" and first[3] == "" and first[4] == ""

    # id, street, city, state, zip — split only in the retries.
    assert second[1] == "9281 Kasota Way"
    assert second[2] == "South Harbor Twp"
    assert second[3] == "MN"
    assert second[4] == "", "the city pass must omit the ZIP"

    assert third[2] == "", "the zip pass must omit the city"
    assert third[4] == "56359", "the zip pass carries the ZIP it holds"


def test_geocode_batch_stops_early_when_a_pass_matches_everything():
    """A healthy batch costs exactly one POST — the retries are for leftovers."""
    import asyncio
    import csv as _csv
    import io as _io

    from data_integrations.census_geocoder import CensusGeocoder

    posts = []

    class _Cache:
        async def get_or_fetch(self, _ns, key, fetcher, ttl=None):  # noqa: ARG002
            return await fetcher()

    geo = CensusGeocoder()
    geo._cache = _Cache()

    async def fake_post(csv_bytes: bytes) -> str:
        rows = list(_csv.reader(_io.StringIO(csv_bytes.decode())))
        posts.append(rows)
        return "\n".join(
            f'"{r[0]}","in","Match","exact","1 X ST, Y, ZZ, 00000","-70.0,43.0",'
            f'"1","L","23","005","000100","1001"'
            for r in rows
        )

    geo._post_batch = fake_post
    out = asyncio.run(geo.geocode_batch(["1 X St, Y, ZZ, 00000"]))

    assert len(posts) == 1, "a fully-matched pass must not trigger retries"
    assert out[0]["matched"] is True


def test_the_cache_key_distinguishes_the_passes():
    """Finding: the `shape` discriminator is load-bearing exactly when it matters.

    When a pass matches nothing, the next pass receives a byte-identical `rows`
    list. Without `shape` in the cache key the second POST would hash to the
    first's entry, replay its CSV, and no-op while still looking like it ran —
    silently halving the fix precisely in the case it was built for.

    The other guard test stubs the cache and ignores the key entirely, so
    deleting the discriminator would leave it green. This asserts the key.
    """
    import asyncio
    import csv as _csv
    import io as _io

    from data_integrations.census_geocoder import CensusGeocoder, _split_oneline

    keys = []

    class _Cache:
        async def get_or_fetch(self, _ns, key, fetcher, ttl=None):
            keys.append(key)
            return await fetcher()

    geo = CensusGeocoder()
    geo._cache = _Cache()

    async def never_matches(csv_bytes: bytes) -> str:
        rows = list(_csv.reader(_io.StringIO(csv_bytes.decode())))
        return "\n".join(f'"{r[0]}","in","No_Match"' for r in rows)

    geo._post_batch = never_matches
    asyncio.run(geo.geocode_batch(["9281 Kasota Way, South Harbor Twp, MN, 56359"]))

    assert len(keys) == 3, "expected one cache lookup per pass"
    shapes = [k.get("shape") for k in keys]
    assert shapes == ["oneline", "city", "zip"], shapes
    assert len({repr(sorted(k.items())) for k in keys}) == 3, (
        "the passes must not share a cache entry — identical rows, different columns"
    )


def test_split_oneline_keeps_a_unit_but_drops_a_duplicated_city():
    """Both behaviours are measured, not assumed.

    Over 20,000 ungeocoded rows: 564 carry a comma in `address`; 462 of them
    (82%) are the city repeated into the address column, and only 12 (2%) are a
    real unit. So an unrecognised middle chunk is DROPPED, and only a secondary
    address designator joins the street. Treating every leading chunk as street
    took the measured residue match rate from 26% to 0%.
    """
    assert _split_oneline("123 MAIN ST, UNIT 4, EVERETT, WA, 98203") == (
        "123 MAIN ST, UNIT 4", "EVERETT", "WA", "98203",
    )
    assert _split_oneline("456 OAK AVE, APT 2B, Providence, RI, 02903") == (
        "456 OAK AVE, APT 2B", "Providence", "RI", "02903",
    )
    # The address column already repeats the city; the repeat must not survive.
    assert _split_oneline(
        "189 N WOODRUN WAY, SARATOGA SPRINGS, UT, Saratoga Springs, UT"
    ) == ("189 N WOODRUN WAY", "Saratoga Springs", "UT", "")


def test_split_oneline_does_not_read_a_street_suffix_as_a_state():
    """`[A-Za-z]{2}$` reads the "St" in "10 Downing St" as a state and eats it.

    Matching against the real code set is what makes that impossible.
    """
    assert _split_oneline("10 Downing St, London") == ("10 Downing St", "London", "", "")
    assert _split_oneline("1 Sunset Dr, Springfield") == ("1 Sunset Dr", "Springfield", "", "")


def test_split_oneline_recovers_the_state_behind_a_malformed_zip():
    """A trailing fragment that is not a ZIP must not hide the state or become the city."""
    assert _split_oneline("1000 ROUTE 9, HOWELL, NJ, 07731 1234") == (
        "1000 ROUTE 9", "HOWELL", "NJ", "",
    )
    # 4 digits: a leading zero lost somewhere upstream. Still not a ZIP.
    assert _split_oneline("1000 ROUTE 9, HOWELL, NJ, 7731") == (
        "1000 ROUTE 9", "HOWELL", "NJ", "",
    )


# ── Outage-versus-unmatchable ────────────────────────────────────────────────
#
# The guard used to infer an outage from a zero-match page, reasoning that real
# data matches ~90%. That was true of the corpus as a whole and is false of what
# remains: every easily-matched row has been geocoded and removed from the queue,
# so measured match rates are 92-100% for rows already done and 0-26% for the
# residue. A zero page is now an ordinary result, and inferring an outage from it
# halts the job at the first hard patch — permanently, because the cursor never
# advances past it. 17 backfill runs went `partial` that way while the service
# was healthy throughout.
#
# The canary replaces that inference with evidence: ask the batch endpoint for a
# known-good address and believe the answer.

def test_canary_distinguishes_a_dead_endpoint_from_unmatchable_addresses():
    import asyncio

    from backfill_property_coordinates import _batch_endpoint_is_answering

    class _Geo:
        def __init__(self, responder):
            self._post_batch = responder

    async def healthy(_payload):
        return ('"0","1600 PENNSYLVANIA AVE NW, WASHINGTON, DC, 20500","Match",'
                '"exact","1600 PENNSYLVANIA AVE NW","-77.0,38.9","1","L","11","001","006202","1031"')

    async def answers_but_matches_nothing(_payload):
        return '"0","1600 Pennsylvania Ave NW","No_Match"'

    async def transport_dead(_payload):
        raise OSError("connection reset by peer")

    assert asyncio.run(_batch_endpoint_is_answering(_Geo(healthy))) is True
    assert asyncio.run(_batch_endpoint_is_answering(_Geo(answers_but_matches_nothing))) is False
    assert asyncio.run(_batch_endpoint_is_answering(_Geo(transport_dead))) is False


def test_the_canary_bypasses_the_cache():
    """It calls _post_batch, not geocode_batch.

    geocode_batch is cached, so a cached hit would report the service healthy
    during an outage — the one moment the answer has to be trusted. Asserted
    against the source because a future refactor routing it through the cached
    path would still pass every behavioural test above.
    """
    import ast
    from pathlib import Path as _Path

    src = (_Path(__file__).parent.parent / "backfill_property_coordinates.py").read_text()
    fn = next(
        n for n in ast.parse(src).body
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_batch_endpoint_is_answering"
    )
    # Every attribute this function actually calls. Parsed rather than grepped:
    # the docstring names geocode_batch to explain why it is NOT used, and a
    # substring check cannot tell prose from a call.
    called = {
        node.func.attr for node in ast.walk(fn)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "_post_batch" in called
    assert "geocode_batch" not in called, "the canary must not go through the cached path"
