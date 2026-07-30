"""Contracts for licensed multi-MLS ingestion and clean lead enrichment."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest

from data_integrations.base import DataIntegrationError
from data_integrations.listings_feed import (
    RESOFeedConfig,
    RESOListingsAggregator,
    RESOListingsFeed,
    load_reso_feed_configs,
)
from mls_enrichment import MLS_OVERLAY_SELECT, clean_mls_overlay


def _config(mls_id: str = "board-one", **overrides) -> RESOFeedConfig:
    values = {
        "mls_id": mls_id,
        "mls_name": f"{mls_id} MLS",
        "url": f"https://{mls_id}.example/RESO/OData/Property",
        "token": "test-token",
        "page_size": 2,
        "lookback_hours": 1.0,
        "max_pages": 10,
    }
    values.update(overrides)
    return RESOFeedConfig(**values)


def test_multi_feed_config_uses_secret_env_and_deduplicates(monkeypatch):
    for name in (
        "ORACLE_RESO_URL", "ORACLE_RESO_TOKEN", "ORACLE_RESO_MLS_ID",
        "ORACLE_RESO_FEEDS_JSON",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ORACLE_ENV", "prod")
    monkeypatch.setenv("ORACLE_RESO_ALLOWED_HOSTS", "a.example,b.example,c.example")
    monkeypatch.setenv("RESO_TOKEN_A", "secret-a")
    monkeypatch.setenv(
        "ORACLE_RESO_FEEDS_JSON",
        """[
          {"id":"board-a","name":"Board A","url":"https://a.example/Property",
           "token_env":"RESO_TOKEN_A"},
          {"id":"board-a","name":"Duplicate","url":"https://b.example/Property",
           "token":"secret-b"},
          {"id":"board-c","url":"http://c.example/Property","token":"secret-c"}
        ]""",
    )

    feeds, errors = load_reso_feed_configs()

    assert [feed.mls_id for feed in feeds] == ["board-a"]
    assert feeds[0].token == "secret-a"
    assert any("duplicate id" in error for error in errors)
    assert any("URL must use HTTPS" in error for error in errors)
    assert all("secret-" not in error for error in errors)


def test_reso_normalization_preserves_provenance_and_match_keys():
    feed = RESOListingsFeed(_config("bright"))
    record = feed.normalize({
        "ListingKey": "listing-key-1",
        "ListingId": "MLS-100",
        "UnparsedAddress": "10 Main St",
        "City": "Dover",
        "StateOrProvince": "de",
        "PostalCode": "19901",
        "CountyOrParish": "Kent",
        "ParcelNumber": "ED-05-067.00-01-01.00",
        "ListPrice": "275000",
        "StandardStatus": "Active Under Contract",
        "ModificationTimestamp": "2026-07-27T12:00:00Z",
        "OriginatingSystemName": "Bright",
        "Media": [
            {"MediaURL": "https://cdn.example/one.jpg"},
            {"MediaURL": "http://insecure.example/two.jpg"},
        ],
    })

    assert record["mls_id"] == "bright"
    assert record["mls_number"] == "MLS-100"
    assert record["state_code"] == "DE"
    assert record["list_price"] == 275_000
    assert record["status"] == "active_under_contract"
    assert record["photos"] == ["https://cdn.example/one.jpg"]
    assert record["features"]["parcel_number"] == "ED-05-067.00-01-01.00"
    assert record["features"]["source_kind"] == "licensed_mls"
    assert record["features"]["provenance"]["standard"] == "RESO Web API"


def test_reso_next_link_cannot_exfiltrate_bearer_token():
    feed = RESOListingsFeed(_config())

    assert feed._safe_next_url("/RESO/OData/Property?$skiptoken=abc").startswith(
        "https://board-one.example/"
    )
    with pytest.raises(DataIntegrationError, match="cross-origin"):
        feed._safe_next_url("https://attacker.example/steal")


def test_production_feed_requires_direct_host_allowlist(monkeypatch):
    monkeypatch.setenv("ORACLE_ENV", "prod")
    monkeypatch.setenv("ORACLE_RESO_FEEDS_JSON", """[
      {"id":"direct-board","url":"https://board.example/Property","token":"token"}
    ]""")
    monkeypatch.delenv("ORACLE_RESO_ALLOWED_HOSTS", raising=False)

    feeds, errors = load_reso_feed_configs()

    assert feeds == []
    assert any("must explicitly authorize" in error for error in errors)


def test_sync_follows_server_paging_and_advances_only_after_exhaustion(monkeypatch):
    config = _config(max_pages=5)
    feed = RESOListingsFeed(config)
    monkeypatch.setenv(
        "ORACLE_INGEST_TENANT_ID", "00000000-0000-0000-0000-000000000000"
    )
    since = datetime(2026, 7, 27, 10, 0, tzinfo=timezone.utc)
    pages = [
        {
            "value": [
                {
                    "ListingKey": "one", "UnparsedAddress": "1 Main St",
                    "StateOrProvince": "DE", "PostalCode": "19901",
                    "ModificationTimestamp": "2026-07-27T10:10:00Z",
                },
                {
                    "ListingKey": "two", "UnparsedAddress": "2 Main St",
                    "StateOrProvince": "DE", "PostalCode": "19901",
                    "ModificationTimestamp": "2026-07-27T10:11:00Z",
                },
            ],
            "@odata.nextLink": "/RESO/OData/Property?$skiptoken=next",
        },
        {
            "value": [
                {
                    "ListingKey": "three", "UnparsedAddress": "3 Main St",
                    "StateOrProvince": "DE", "PostalCode": "19901",
                    "ModificationTimestamp": "2026-07-27T10:12:00Z",
                },
                {"not": "a usable listing"},
            ],
        },
        {"value": []},
    ]
    calls: list[dict] = []

    async def cached_page(**kwargs):
        calls.append(kwargs)
        return pages[len(calls) - 1]

    feed._cached_page = cached_page

    class FakeConnection:
        def __init__(self):
            self.executions: list[tuple[str, tuple]] = []

        async def fetchrow(self, _query, *_args):
            return {"last_sync_at": since}

        async def execute(self, query, *args):
            self.executions.append((query, args))
            return "OK"

    conn = FakeConnection()

    @asynccontextmanager
    async def fake_tenant_tx(_ctx):
        yield conn

    import db.connection

    monkeypatch.setattr(db.connection, "tenant_tx", fake_tenant_tx)
    result = asyncio.run(feed.sync_once())

    assert result["state"] == "succeeded"
    assert result["upserted"] == 3
    assert result["rejected"] == {"missing_listing_key": 1}
    assert result["cursor_advanced"] is True
    assert calls[1]["next_url"].endswith("$skiptoken=next")
    upserts = [entry for entry in conn.executions if "INSERT INTO oracle_mls_listings" in entry[0]]
    assert len(upserts) == 3


def test_aggregator_isolates_board_failures(monkeypatch):
    configs = [_config("board-a"), _config("board-b")]
    monkeypatch.setattr(
        "data_integrations.listings_feed.load_reso_feed_configs",
        lambda: (configs, []),
    )

    async def sync_once(self):
        if self.mls_id == "board-b":
            raise RuntimeError("board unavailable")
        return {"mls_id": self.mls_id, "state": "succeeded", "upserted": 7}

    monkeypatch.setattr(RESOListingsFeed, "sync_once", sync_once)
    result = asyncio.run(RESOListingsAggregator().sync_once())

    assert result["state"] == "partial"
    assert result["upserted"] == 7
    assert result["failed_feeds"] == ["board-b"]


def test_overlay_requires_identity_evidence_and_remains_separate():
    sql = MLS_OVERLAY_SELECT
    assert "parcel_number" in sql
    assert "normalized_address_and_zip" in sql
    assert "m.zip_code = COALESCE(leads.payload->>'zip_code','')" in sql
    assert "latitude" not in sql
    assert "longitude" not in sql
    assert clean_mls_overlay({"listing_id": "one", "list_price": 1}) == {
        "listing_id": "one", "list_price": 1,
    }
    assert clean_mls_overlay({}) is None
