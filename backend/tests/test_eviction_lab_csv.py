"""Eviction Lab CSV rows are read whole or the download is a failure (audit
35-03 phase 5, B905). Every row of the published files carries exactly the
header's fields, so a short or long row is a damaged download. zip() used to
drop the missing columns, which reported real data as absent."""

from __future__ import annotations

import asyncio

import pytest

from data_integrations.eviction_lab import EvictionLabSource
from source_status import BAD_RESPONSE, UNAVAILABLE, SourceFailure

HEADER = "GEOID,year,name,eviction-rate\n"


def _source(monkeypatch, body: str) -> EvictionLabSource:
    source = EvictionLabSource()

    async def no_network(coro_factory, *, label):
        return body

    monkeypatch.setattr(source, "_with_retries", no_network)
    return source


def test_whole_rows_parse_with_snake_case_keys(monkeypatch):
    source = _source(monkeypatch, HEADER + "10001,2016,Kent County,2.5\n\n10003,2016,New Castle County,3.1\n")
    rows = asyncio.run(source._get_csv_rows("https://example.test/x.csv"))
    assert [r["geoid"] for r in rows] == ["10001", "10003"], "a blank line is skipped, not a row"
    assert rows[0]["eviction_rate"] == "2.5"


@pytest.mark.parametrize("bad", ["10001,2016,Kent County\n", "10001,2016,Kent County,2.5,extra\n"],
                         ids=["short", "long"])
def test_a_ragged_row_is_a_bad_response_not_missing_data(monkeypatch, bad):
    source = _source(monkeypatch, HEADER + "10003,2016,New Castle County,3.1\n" + bad)
    with pytest.raises(ValueError, match="line 3 has [35] fields; the header has 4"):
        asyncio.run(source._get_csv_rows("https://example.test/x.csv"))
    with pytest.raises(SourceFailure) as failure:
        asyncio.run(source.fetch(geography="counties", postal="DE"))
    assert (failure.value.status, failure.value.reason) == (UNAVAILABLE, BAD_RESPONSE)
