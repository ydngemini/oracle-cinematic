"""Regression coverage for truthful, agent-ready lead delivery."""

from __future__ import annotations

import asyncio
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from harvest_health import classify_health
from harvesters.firehose import MultiStateFirehose
from lead_pipeline import (
    decode_cursor,
    encode_cursor,
    freshness_status,
    location_confidence,
    parse_request,
    priority_factors,
    scope_class,
    source_record_refreshed_at,
)


def test_pipeline_cursor_round_trip_and_rejects_malformed_input():
    stamp = datetime(2026, 7, 27, 12, 34, 56, tzinfo=timezone.utc)
    encoded = encode_cursor(72, stamp, "11111111-1111-1111-1111-111111111111")
    assert decode_cursor(encoded) == (72, stamp, "11111111-1111-1111-1111-111111111111")
    assert decode_cursor("not-a-cursor") is None


def test_pipeline_request_is_bounded_and_whitelisted():
    request = parse_request({
        "limit": 99_999,
        "state": "ca",
        "priority": "hot",
        "scope": "county",
        "detail": "comprehensive",
        "freshness": "fresh",
        "map_confidence": "source_coordinate",
        "query": "A" * 400,
    })
    assert request["limit"] == 200
    assert request["state"] == "CA"
    assert request["query"] == "a" * 120
    assert parse_request({"scope": "DROP TABLE", "limit": "bad"})["scope"] == "all"


def test_agent_facts_are_source_bounded_not_inferred():
    payload = {
        "is_absentee_owner": True,
        "distress_flags": ["open_violation"],
        "equity_percent": None,
        "owner_type": "corporate",
        "address": "10 Main Street",
    }
    assert priority_factors(payload, 88) == [
        "reported absentee", "public record signal", "entity ownership",
    ]
    assert location_confidence(payload) == "address_approximation"
    assert scope_class("county:San Diego") == "county"
    assert scope_class("statewide", geometry_only=True) == "geometry_only"
    assert freshness_status(datetime.now(timezone.utc) - timedelta(days=46)) == "verify"
    fallback = datetime.now(timezone.utc)
    source_time = source_record_refreshed_at(
        {"provenance": {"record_refreshed_at": "2026-01-01T00:00:00Z"}}, fallback
    )
    assert source_time == datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert freshness_status(source_time, now=datetime(2026, 3, 1, tzinfo=timezone.utc)) == "verify"


def test_source_health_is_honest_about_stale_and_failed_sources():
    now = datetime(2026, 7, 27, tzinfo=timezone.utc)
    assert classify_health(
        last_succeeded_at=now - timedelta(minutes=5), schedule_seconds=3_600,
        circuit_state="closed", failure_count=0, now=now,
    ) == "fresh"
    assert classify_health(
        last_succeeded_at=now - timedelta(hours=7), schedule_seconds=300,
        circuit_state="closed", failure_count=0, now=now,
    ) == "stale"
    assert classify_health(
        last_succeeded_at=None, schedule_seconds=3_600,
        circuit_state="open", failure_count=5, now=now,
    ) == "failed"


def test_multi_state_result_lists_failed_and_zero_result_jurisdictions():
    async def exercise():
        firehose = MultiStateFirehose(
            "00000000-0000-0000-0000-000000000000", states=["CA", "TX"], agent_id="test"
        )

        async def fake_run_one(self, state, _max_records, _sem):
            if state == "CA":
                return {"state": state, "fetched": 1, "inserted": 1}
            return {"state": state, "fetched": 0, "error": "fixture source unavailable"}

        firehose._run_one = types.MethodType(fake_run_one, firehose)
        result = await firehose.run(max_records_per_state=1, concurrency=1)
        assert result["totals"]["errors"] == 1
        assert result["totals"]["failed_states"] == ["TX"]
        assert result["totals"]["zero_result_states"] == []

    asyncio.run(exercise())


def test_health_probe_does_not_read_or_advance_harvest_checkpoint():
    async def exercise():
        calls = {}
        firehose = MultiStateFirehose(
            "00000000-0000-0000-0000-000000000000",
            states=["CA"],
            agent_id="probe-test",
            mode="probe",
        )

        class FakeHarvester:
            SOURCE_LABEL = "Fixture public parcels"

            async def harvest(self, **kwargs):
                calls["harvest"] = kwargs
                calls["cache"] = type(self._cache).__name__
                return {
                    "state": "CA",
                    "source": self.SOURCE_LABEL,
                    "fetched": 1,
                    "inserted": 0,
                    "checkpoint": 1,
                    "checkpoint_complete": False,
                }

        async def checkpoint_must_not_run(_self, _state):
            raise AssertionError("probe read the production checkpoint")

        async def save_must_not_run(_self, _state, _metrics, _adapter):
            raise AssertionError("probe advanced the production checkpoint")

        async def record_probe(_self, state, metrics, adapter):
            calls["probe"] = (state, metrics["fetched"], adapter)

        firehose._build = lambda _state: FakeHarvester()
        firehose._checkpoint = types.MethodType(checkpoint_must_not_run, firehose)
        firehose._save_checkpoint = types.MethodType(save_must_not_run, firehose)
        firehose._record_probe_success = types.MethodType(record_probe, firehose)

        result = await firehose.run(max_records_per_state=1, concurrency=1)
        assert calls["harvest"] == {
            "max_records": 1,
            "checkpoint": 0,
            "persist": False,
        }
        assert calls["probe"] == ("CA", 1, "FakeHarvester")
        assert calls["cache"] == "_LiveProbeCache"
        assert result["totals"]["inserted"] == 0

    asyncio.run(exercise())


def test_characteristics_backfill_uses_separate_cursor_and_public_catalog_only(monkeypatch):
    async def exercise():
        calls = {}
        firehose = MultiStateFirehose(
            "00000000-0000-0000-0000-000000000000",
            states=["CA"],
            agent_id="backfill-test",
            mode="catalog_backfill",
        )

        class FakeHarvester:
            def __init__(self):
                self._records = [object(), object()]

            async def harvest(self, **kwargs):
                calls["harvest"] = kwargs
                return {
                    "state": "CA",
                    "source": "Fixture assessor",
                    "source_key": "firehose:CA",
                    "fetched": 2,
                    "inserted": 0,
                    "checkpoint": 25,
                    "checkpoint_complete": False,
                }

        async def checkpoint(_self, state):
            calls["checkpoint_key"] = _self._tracking_source_key(state)
            return 23

        async def save(_self, state, metrics, adapter):
            calls["saved"] = (
                _self._tracking_source_key(state),
                metrics["checkpoint"],
                adapter,
            )

        async def public_only(tenant_id, agent_id, records, *, metrics):
            calls["public_only"] = (tenant_id, agent_id, len(records), metrics["source_key"])
            return len(records)

        import harvesters.base

        monkeypatch.setattr(harvesters.base, "upsert_public_records", public_only)
        firehose._build = lambda _state: FakeHarvester()
        firehose._checkpoint = types.MethodType(checkpoint, firehose)
        firehose._save_checkpoint = types.MethodType(save, firehose)

        result = await firehose.run(max_records_per_state=2, concurrency=1)
        assert calls["checkpoint_key"] == "property_characteristics_backfill_ca"
        assert calls["harvest"] == {
            "max_records": 2,
            "checkpoint": 23,
            "persist": False,
        }
        assert calls["public_only"][2:] == (2, "firehose:CA")
        assert calls["saved"] == (
            "property_characteristics_backfill_ca",
            25,
            "FakeHarvester",
        )
        assert result["totals"]["inserted"] == 2

    asyncio.run(exercise())


def test_pipeline_migration_contains_partial_and_health_contracts():
    migration = (Path(__file__).parents[1] / "db/migrations/0048_agent_ready_pipeline.sql").read_text()
    assert "'partial'" in migration
    assert "health_status" in migration
    assert "last_health_checked_at" in migration
