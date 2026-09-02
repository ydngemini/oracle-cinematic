"""Regression tests for the provenance-first public lead payload contract."""

from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from harvesters.base import (
    LEAD_PAYLOAD_SCHEMA_VERSION,
    build_clean_lead_payload,
    persist_leads,
    promote_public_characteristics,
    upsert_public_records,
)
from harvesters.property_adapter import PropertyRecord


def _record(**overrides) -> PropertyRecord:
    data = {
        "parcel_id": "PIN-123",
        "address": "10 Main Street\n",
        "city": "Exampletown",
        "state": "CA",
        "zip_code": "90210",
        "owner_name": "Example Holdings LLC",
        "owner_type": "corporate",
        "estimated_value": 425_000,
        "equity_percent": 0.0,
        "is_absentee_owner": True,
        "distress_flags": ["Open Violation", "open violation", "  Tax Lien  "],
        "last_sale_date": "2023-05-17",
        "zoning_district": "R-2",
        "lot_area_sqft": 6_500,
        "building_area_sqft": 1_900,
        "land_use": "Single Family",
        "latitude": 34.0522,
        "longitude": -118.2437,
        "source_metadata": {"source_record": "abc-1", "nested": {"revision": 2}},
    }
    data.update(overrides)
    return PropertyRecord(**data)


def test_public_payload_preserves_observed_detail_and_source_scope():
    payload = build_clean_lead_payload(
        _record(),
        source_label="San Diego County parcels",
        source_key="firehose:CA",
        refreshed_at=datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc),
    )

    assert payload["schema_version"] == LEAD_PAYLOAD_SCHEMA_VERSION
    assert payload["address"] == "10 Main Street"
    assert payload["distress_flags"] == ["open_violation", "tax_lien"]
    assert payload["equity_percent"] is None  # 0.0 is the harvester unknown sentinel.
    assert payload["provenance"] == {
        "source_key": "firehose:CA",
        "source_name": "San Diego County parcels",
        "coverage_scope": "county:San Diego",
        "data_classification": "public_property_record",
        "record_refreshed_at": "2026-07-27T12:00:00+00:00",
        "dataset_version": None,
    }
    assert payload["data_quality"]["detail_level"] == "comprehensive"
    assert "public_record_value" in payload["data_quality"]["observed_fields"]
    assert "equity_percent" not in payload["data_quality"]["observed_fields"]


def test_public_payload_drops_placeholder_and_malformed_values_without_guessing():
    payload = build_clean_lead_payload(
        _record(
            address="PIN PIN-123",
            estimated_value=-10,
            last_sale_date="05/17/2023",
            latitude=95,
            longitude=-118.2437,
            source_metadata={"note": "\x00 source supplied"},
        ),
        source_key="firehose:VA",
    )

    assert payload["address"] is None
    assert payload["estimated_value"] is None
    assert payload["last_sale_date"] is None
    assert payload["latitude"] is None
    assert payload["longitude"] is None
    assert payload["source_metadata"] == {"note": "source supplied"}
    assert payload["data_quality"]["address_was_not_published"] is True
    assert "address" in payload["data_quality"]["unavailable_fields"]
    assert "coordinates" in payload["data_quality"]["unavailable_fields"]


def test_all_jurisdictions_promote_only_explicit_source_characteristics():
    record = promote_public_characteristics(
        _record(
            county=None,
            bedrooms=None,
            bathrooms=None,
            rooms=None,
            year_built=None,
            property_class=None,
            last_sale_price=None,
        ),
        {
            "BEDROOMS": "4",
            "FULL_BATHS": "2",
            "HALF_BATHS": "1",
            "TOTAL_ROOMS": "8",
            "YEAR_BUILT": "1925",
            "PROPERTY_CLASS": "203",
            "SALE_PRICE": "160000",
        },
    )
    payload = build_clean_lead_payload(
        record,
        source_label="Example assessor",
        source_key="regional_parcels_ca",
    )

    assert payload["bedrooms"] == 4
    assert payload["bathrooms"] == 2.5
    assert payload["rooms"] == 8
    assert payload["year_built"] == 1925
    assert payload["property_class"] == "203"
    assert payload["last_sale_price"] == 160000
    assert {"bedrooms", "bathrooms", "year_built", "last_sale_price"}.issubset(
        payload["data_quality"]["observed_fields"]
    )


def test_characteristic_aliases_cover_common_cama_column_names():
    record = promote_public_characteristics(
        _record(
            bedrooms=None,
            bathrooms=None,
            rooms=None,
            year_built=None,
            last_sale_price=None,
            building_area_sqft=None,
        ),
        {
            "number_of_bedrooms": "3",
            "number_of_bathrooms": "1.5",
            "number_of_rooms": "7",
            "yearbuilt": "1948",
            "sale_prc1": "275000",
            "total_livable_area": "1542",
        },
    )
    assert record.bedrooms == 3
    assert record.bathrooms == 1.5
    assert record.rooms == 7
    assert record.year_built == 1948
    assert record.last_sale_price == 275_000
    assert record.building_area_sqft == 1542


def test_characteristic_aliases_cover_fdor_fields_and_formatted_numbers():
    record = promote_public_characteristics(
        _record(
            year_built=None,
            property_class=None,
            building_area_sqft=None,
            lot_area_sqft=None,
        ),
        {
            "ACT_YR_BLT": "2019",
            "DOR_UC": "001",
            "TOT_LVG_AR": "1,563",
            "LND_SQFOOT": "43,560",
        },
    )

    assert record.year_built == 2019
    assert record.property_class == "001"
    assert record.building_area_sqft == 1_563
    assert record.lot_area_sqft == 43_560


def test_harvest_persists_private_lead_and_shared_public_catalog(monkeypatch):
    class FakeConnection:
        def __init__(self):
            self.calls = []

        async def executemany(self, query, args):
            self.calls.append((query, list(args)))

    conn = FakeConnection()

    @asynccontextmanager
    async def fake_tenant_tx(_ctx):
        yield conn

    import db.connection

    monkeypatch.setattr(db.connection, "tenant_tx", fake_tenant_tx)
    metrics = {
        "source": "San Diego County parcels",
        "source_key": "firehose:CA",
        "inserted": 0,
    }

    inserted = asyncio.run(
        persist_leads(
            "11111111-1111-1111-1111-111111111111",
            "harvester",
            [_record()],
            metrics=metrics,
        )
    )

    assert inserted == 1
    assert len(conn.calls) == 2
    private_query, private_args = conn.calls[0]
    catalog_query, catalog_args = conn.calls[1]
    assert "INSERT INTO leads" in private_query
    assert "INSERT INTO public_property_records" in catalog_query
    assert "COALESCE(EXCLUDED.bedrooms" in catalog_query
    assert "NULLIF(EXCLUDED.property_class, '')" in catalog_query
    assert "published_field_sources" in catalog_query
    assert "public_property_records.source_metadata" in catalog_query
    assert private_args[0][0] == "11111111-1111-1111-1111-111111111111"
    assert catalog_args[0][0:3] == ("firehose:CA", "PIN-123", "CA")
    assert isinstance(catalog_args[0][27], datetime)
    assert catalog_args[0][27].tzinfo is not None
    assert "tenant_id" not in catalog_query
    assert metrics["inserted"] == 1


def test_targeted_public_reconciliation_binds_a_real_timestamp(monkeypatch):
    class FakeConnection:
        def __init__(self):
            self.calls = []

        async def executemany(self, query, args):
            self.calls.append((query, list(args)))

    conn = FakeConnection()

    @asynccontextmanager
    async def fake_tenant_tx(_ctx):
        yield conn

    import db.connection

    monkeypatch.setattr(db.connection, "tenant_tx", fake_tenant_tx)
    inserted = asyncio.run(
        upsert_public_records(
            "11111111-1111-1111-1111-111111111111",
            "address-reconciler",
            [_record()],
            metrics={
                "source": "San Diego County parcels",
                "source_key": "firehose:CA",
            },
        )
    )

    assert inserted == 1
    assert len(conn.calls) == 1
    query, args = conn.calls[0]
    assert "INSERT INTO public_property_records" in query
    assert "COALESCE(EXCLUDED.latitude" in query
    assert "NULLIF(EXCLUDED.land_use, '')" in query
    assert isinstance(args[0][27], datetime)
    assert args[0][27].tzinfo is not None


def test_a_migration_indexes_the_current_payload_schema_version():
    """Bumping LEAD_PAYLOAD_SCHEMA_VERSION must ship a matching partial index.

    `normalize_public_leads` asks which harvested leads still carry a stale
    payload envelope. Nothing can index that predicate in general, so migration
    0086 indexes the *outstanding work* instead — a partial index whose WHERE
    clause carries the schema version as a literal. When the constant matches,
    the check is a probe of an empty index. When it does not, the planner
    ignores the index and the query reverts to a measured 24.8s parallel seq
    scan over 8.4M rows, which exceeds the pool's command_timeout=30 under load
    and dead-letters the job with a bare `TimeoutError()`.

    That regression is invisible — the job still returns correct results, just
    slowly enough to die. So the coupling is asserted here rather than left to
    be rediscovered from a dead-letter queue.
    """
    migrations = Path(__file__).parent.parent / "db" / "migrations"
    files = sorted(migrations.glob("*.sql"))
    corpus = {f.name: f.read_text() for f in files}
    current = f"COALESCE(payload->>'schema_version', '') <> '{LEAD_PAYLOAD_SCHEMA_VERSION}'"

    assert any(current in text for text in corpus.values()), (
        f"LEAD_PAYLOAD_SCHEMA_VERSION is {LEAD_PAYLOAD_SCHEMA_VERSION} but no migration "
        f"creates the partial index for it. Add one containing:\n  {current}\n"
        "See 0086_normalization_and_owner_lookup_indexes.sql for the shape."
    )

    # The superseded index must also be dropped, and this is the more dangerous
    # half. A predicate of `<> '3'` is EMPTY while the constant is 3 — that is
    # what makes the probe free. Bump to 4, let the normalizer finish, and every
    # row satisfies `<> '3'`, so the old index inflates from 0 to all 8.4M rows
    # and every write to `leads` pays to maintain an index nothing reads. The
    # bump guard above would still pass, because a new index was added.
    stale = [
        v for v in range(1, int(LEAD_PAYLOAD_SCHEMA_VERSION))
        if any(f"COALESCE(payload->>'schema_version', '') <> '{v}'" in text
               for text in corpus.values())
    ]
    for version in stale:
        creating = [
            name for name, text in corpus.items()
            if f"COALESCE(payload->>'schema_version', '') <> '{version}'" in text
            and "CREATE INDEX" in text
        ]
        dropped = any(
            "DROP INDEX" in text and "idx_leads_pending_payload_normalization" in text
            for text in corpus.values()
        )
        assert dropped, (
            f"a partial index for the superseded schema version {version} is still "
            f"created by {creating} and never dropped. Once the normalizer finishes, "
            f"its predicate matches EVERY row, turning a deliberately-empty index into "
            f"a full one on the hottest table. Add a DROP INDEX for it."
        )
