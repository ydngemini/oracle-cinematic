"""The access bundle is complete or nothing (external audit 29.8); the real
database path is tests/test_dsar_subject_truth_live.py."""
from __future__ import annotations

import asyncio
import inspect

import pytest

import privacy_lifecycle as pl
import privacy_requests

TENANT = "00000000-0000-0000-0000-0000000000d1"
ROW = "00000000-0000-0000-0000-00000000d001"


class NoQueries:
    async def fetch(self, *_a):
        raise AssertionError("nothing may be read without the key")


@pytest.mark.parametrize("table", ["agent_contacts", "inbound_voice_calls", "contact_intake_sessions",
                                   "lead_intake_events", "contact_property_relationships"])
def test_encrypted_records_without_the_key_fail_instead_of_vanishing(monkeypatch, table):
    monkeypatch.delenv("ORACLE_ENCRYPTION_MASTER_KEY", raising=False)
    with pytest.raises(pl.LifecycleError) as err:
        asyncio.run(privacy_requests._access_bundle(NoQueries(), TENANT, {table: [ROW]}))
    assert err.value.status_code == 503


def test_no_table_is_capped_and_no_query_error_is_swallowed():
    src = inspect.getsource(privacy_requests._access_bundle)
    assert "LIMIT" not in src
    assert "except Exception" not in src
