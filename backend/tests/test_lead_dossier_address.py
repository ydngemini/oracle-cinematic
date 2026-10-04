"""The lead dossier names the property by its street address.

Mission 3 browser journey: a property created in Property View (create_subject
stores the address on the row, payload stays empty) opened as "pv:ef32f869…" —
the synthetic parcel key — in the sheet header and in Neoh's "Talking about …"
chip, because the dossier never returned leads.address.
"""
from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager

import lead_dossier
from tenancy import Role, TenantContext


def test_dossier_returns_the_rows_address(monkeypatch):
    seen = {}

    class _Conn:
        async def fetchrow(self, query, *args):
            seen["query"] = " ".join(query.split())
            return {
                "parcel_id": "pv:ef32f86956b84a322122ce3f5deed5ee", "address": "100 W 10th St, Wilmington, DE 19801",
                "state": "DE", "motivation_score": 0, "underwriting": {}, "payload": {}, "dossier_status": "draft",
                "contract_execution_date": None, "contract_expires_at": None, "marketing_payload": None,
                "marketing_generated_at": None, "acquisition_entity": None, "mls_overlay": None,
            }

        async def fetch(self, query, *args):
            return []

    @asynccontextmanager
    async def tx(_ctx):
        yield _Conn()

    monkeypatch.setattr(lead_dossier, "tenant_tx", tx)
    monkeypatch.setattr(lead_dossier, "clean_mls_overlay", lambda _v: None)
    ctx = TenantContext(agent_id="owner@example.test", tenant_id=str(uuid.uuid4()), role=Role.BROKER_OWNER)
    out = asyncio.run(lead_dossier.get_dossier(lead_id=str(uuid.uuid4()), ctx=ctx))
    assert "SELECT parcel_id, address," in seen["query"]
    assert out["address"] == "100 W 10th St, Wilmington, DE 19801"
