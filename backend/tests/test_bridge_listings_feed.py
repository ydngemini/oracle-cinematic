"""Contracts for the Bridge Interactive listings connector and the shared
MLS sink both it and RESOListingsFeed write through."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest

from data_integrations.bridge_listings_feed import (
    BridgeFeedConfig,
    BridgeListingsFeed,
    _bridge_config_from_env,
)
from data_integrations.mls_sink import (
    reject_reason,
    upsert_mls_records,
    upsert_mls_records_and_status,
)


def _config(**overrides) -> BridgeFeedConfig:
    values = {
        "dataset": "test",
        "access_token": "test-token",
        "base_url": "https://api.bridgedataoutput.com/api/v2",
        "mls_id": "bridge_dev",
        "mls_name": "Bridge Developer Dataset",
        "page_size": 2,
        "lookback_hours": 1.0,
        "max_pages": 10,
    }
    values.update(overrides)
    return BridgeFeedConfig(**values)


class FakeConnection:
    def __init__(self, since=None):
        self.executions: list[tuple[str, tuple]] = []
        self._since = since

    async def fetchrow(self, _query, *_args):
        return {"last_sync_at": self._since} if self._since else None

    async def execute(self, query, *args):
        self.executions.append((query, args))
        return "OK"


def test_env_config_requires_enabled_dataset_and_token(monkeypatch):
    for name in (
        "ORACLE_BRIDGE_ENABLED", "ORACLE_BRIDGE_DATASET", "ORACLE_BRIDGE_ACCESS_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)

    config, error = _bridge_config_from_env()
    assert config is None
    assert "ORACLE_BRIDGE_ENABLED" in error

    monkeypatch.setenv("ORACLE_BRIDGE_ENABLED", "true")
    config, error = _bridge_config_from_env()
    assert config is None
    assert "ORACLE_BRIDGE_DATASET" in error

    monkeypatch.setenv("ORACLE_BRIDGE_DATASET", "test")
    monkeypatch.setenv("ORACLE_BRIDGE_ACCESS_TOKEN", "tok")
    config, error = _bridge_config_from_env()
    assert error is None
    assert config.dataset == "test"
    assert config.mls_id == "bridge_dev"
    assert config.mls_name == "Bridge Developer Dataset"


def test_normalize_maps_bridge_record_to_canonical_shape_with_developer_provenance():
    feed = BridgeListingsFeed(_config())
    raw = {
        "ListingKey": "BRIDGE-1", "UnparsedAddress": "1 Test Way",
        "City": "Testville", "StateOrProvince": "DE", "PostalCode": "19901",
        "CountyOrParish": "Kent", "Latitude": 39.1, "Longitude": -75.5,
        "ListPrice": 450000, "OriginalListPrice": 460000,
        "StandardStatus": "Active", "PropertyType": "Residential",
        "BedroomsTotal": 4, "BathroomsFull": 2, "BathroomsHalf": 1,
        "LivingArea": 2200, "LotSizeSquareFeet": 8000, "YearBuilt": 1998,
        "AssociationFee": 45, "DaysOnMarket": 12,
        "ListingContractDate": "2026-08-01", "ParcelNumber": "PN-1",
        "ModificationTimestamp": "2026-08-15T12:00:00Z",
        "Media": [{"MediaURL": "https://example.test/1.jpg"}],
    }
    rec = feed.normalize(raw)
    assert rec["mls_id"] == "bridge_dev"
    assert rec["mls_number"] == "BRIDGE-1"
    assert rec["state_code"] == "DE"
    assert rec["list_price"] == 450000
    assert rec["status"] == "active"
    assert rec["photos"] == ["https://example.test/1.jpg"]
    assert rec["features"]["provenance"] == {
        "classification": "developer_listing_dataset",
        "provider": "Bridge Interactive",
        "provider_id": "bridge_dev",
        "standard": "Bridge API v2",
    }
    assert reject_reason(rec) is None


def test_sync_follows_offset_paging_and_advances_only_after_exhaustion(monkeypatch):
    config = _config(max_pages=5)
    feed = BridgeListingsFeed(config)
    monkeypatch.setenv(
        "ORACLE_INGEST_TENANT_ID", "00000000-0000-0000-0000-000000000000"
    )
    since = datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc)
    pages = [
        {
            "success": True, "status": 200, "total": 3,
            "bundle": [
                {
                    "ListingKey": "one", "UnparsedAddress": "1 Main St",
                    "StateOrProvince": "DE", "PostalCode": "19901",
                    "ModificationTimestamp": "2026-08-01T10:10:00Z",
                },
                {
                    "ListingKey": "two", "UnparsedAddress": "2 Main St",
                    "StateOrProvince": "DE", "PostalCode": "19901",
                    "ModificationTimestamp": "2026-08-01T10:11:00Z",
                },
            ],
        },
        {
            "success": True, "status": 200, "total": 3,
            "bundle": [
                {
                    "ListingKey": "three", "UnparsedAddress": "3 Main St",
                    "StateOrProvince": "DE", "PostalCode": "19901",
                    "ModificationTimestamp": "2026-08-01T10:12:00Z",
                },
                {"not": "a usable listing"},
            ],
        },
        {"success": True, "status": 200, "total": 3, "bundle": []},
    ]
    calls: list[dict] = []

    async def cached_page(**kwargs):
        calls.append(kwargs)
        return pages[len(calls) - 1]

    feed._cached_page = cached_page
    conn = FakeConnection(since=since)

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
    assert calls[0]["offset"] == 0
    assert calls[1]["offset"] == 2
    upserts = [e for e in conn.executions if "INSERT INTO oracle_mls_listings" in e[0]]
    assert len(upserts) == 3
    status_writes = [e for e in conn.executions if "INSERT INTO mls_sync_status" in e[0]]
    assert len(status_writes) == 1
    assert status_writes[0][1][2] == "Bridge_API_v2"  # feed_type positional arg


def test_is_configured_reflects_env(monkeypatch):
    for name in ("ORACLE_BRIDGE_ENABLED", "ORACLE_BRIDGE_DATASET", "ORACLE_BRIDGE_ACCESS_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    assert BridgeListingsFeed.is_configured() is False

    monkeypatch.setenv("ORACLE_BRIDGE_ENABLED", "true")
    monkeypatch.setenv("ORACLE_BRIDGE_DATASET", "test")
    monkeypatch.setenv("ORACLE_BRIDGE_ACCESS_TOKEN", "tok")
    assert BridgeListingsFeed.is_configured() is True


def test_sync_skips_cleanly_without_tenant(monkeypatch):
    monkeypatch.delenv("ORACLE_INGEST_TENANT_ID", raising=False)
    feed = BridgeListingsFeed(_config())
    result = asyncio.run(feed.sync_once())
    assert result == {"skipped": "ORACLE_INGEST_TENANT_ID unset"}


# -- shared sink -------------------------------------------------------- #

def _record(mls_number="one", **overrides) -> dict:
    rec = {
        "mls_id": "bridge_dev", "mls_number": mls_number, "address": "1 Main St",
        "city": "Testville", "state_code": "DE", "zip_code": "19901", "county": "Kent",
        "latitude": 39.1, "longitude": -75.5, "list_price": 100000.0,
        "orig_list_price": None, "status": "active", "property_type": "residential",
        "beds": 3, "baths_full": 2, "baths_half": 0, "sqft": 1500, "lot_sqft": 5000,
        "year_built": 1990, "hoa_monthly": None, "days_on_market": 5,
        "list_date": "2026-08-01", "close_date": None, "close_price": None,
        "description": None, "photos": [], "features": {"source_kind": "developer_listing_dataset"},
    }
    rec.update(overrides)
    return rec


def test_reject_reason_is_provider_agnostic():
    assert reject_reason(_record()) is None
    assert reject_reason(_record(mls_number="")) == "missing_listing_key"
    assert reject_reason(_record(state_code="")) == "invalid_state"
    assert reject_reason(_record(latitude=999)) == "invalid_latitude"
    assert reject_reason(_record(longitude=999)) == "invalid_longitude"


def test_upsert_mls_records_and_status_writes_both_rows_with_feed_type():
    conn = FakeConnection()
    upserted = asyncio.run(upsert_mls_records_and_status(
        conn,
        records=[_record("one"), _record("two")],
        mls_id="bridge_dev",
        mls_name="Bridge Developer Dataset",
        feed_type="Bridge_API_v2",
        last_sync_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
        notes={"state": "succeeded"},
    ))
    assert upserted == 2
    inserts = [e for e in conn.executions if "INSERT INTO oracle_mls_listings" in e[0]]
    status = [e for e in conn.executions if "INSERT INTO mls_sync_status" in e[0]]
    assert len(inserts) == 2
    assert len(status) == 1
    assert status[0][1][2] == "Bridge_API_v2"
