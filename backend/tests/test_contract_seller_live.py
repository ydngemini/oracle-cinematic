"""An assignment contract's Seller never comes from the lead's public-record
owner of record (F11.04, T11.03), against real PostgreSQL: the deal query as
Postgres evaluates it, FORCE RLS, the application login (skipped without
ORACLE_LIVE_DB_ADMIN_DSN).

Three buyer clients, each shown one lead whose payload carries an owner_name
(an unconfirmed parcel match):
  owner_only    no seller client, no stated seller -> no Seller at all, and
                the draft gate asks for one;
  linked        a linked seller client             -> that client's name;
  stated        payload.seller_name                -> that stated name.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")

OWNER = "Public Record Owner"


def test_the_owner_of_record_never_becomes_the_seller():
    asyncio.run(_scenario())


async def _scenario():
    import asyncpg

    from db import connection
    from ml_forge.synthetic_lawyer import (
        draft_assignment_contract,
        fetch_assignment_transaction_for_client,
    )
    from tenancy import Role, TenantContext

    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=3)
    tenant = uuid.uuid4()
    tag = str(tenant)[:8]
    owner = TenantContext(agent_id=f"owner-{tag}@contracts.test", tenant_id=str(tenant),
                          role=Role.BROKER_OWNER)
    cases = ("owner_only", "linked", "stated")
    buyer = {k: uuid.uuid4() for k in cases}
    lead = {k: uuid.uuid4() for k in cases}
    seller_client = uuid.uuid4()

    try:
        await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,'Contracts')",
                            tenant, f"contracts-{tag}")
        await admin.execute(
            "INSERT INTO clients (id, tenant_id, full_name, client_type) VALUES ($1,$2,'Live Linked Seller','seller')",
            seller_client, tenant)
        for k in cases:
            await admin.execute(
                "INSERT INTO clients (id, tenant_id, full_name, client_type) VALUES ($1,$2,$3,'buyer')",
                buyer[k], tenant, f"Live Cash Buyer {k}")
            payload = {"owner_name": OWNER, "earnest_money": 3000}
            if k == "stated":
                payload["seller_name"] = "Live Stated Seller"
            await admin.execute(
                "INSERT INTO leads (id, tenant_id, parcel_id, state, motivation_score, seller_client_id, "
                "address, payload, underwriting, contract_execution_date) "
                "VALUES ($1,$2,$3,'DE',50,$4,$5,$6::jsonb,$7::jsonb,'2026-06-15')",
                lead[k], tenant, f"SELLER-{tag}-{k}", seller_client if k == "linked" else None,
                f"{tag} {k} Street, Newark, DE 19711", json.dumps(payload),
                json.dumps({"wholesale_buy_price": 195000, "investor_buy_price": 220000}))
            await admin.execute("INSERT INTO showings (tenant_id, client_id, lead_id) VALUES ($1,$2,$3)",
                                tenant, buyer[k], lead[k])

        fetched = {k: await fetch_assignment_transaction_for_client(str(buyer[k]), owner) for k in cases}

        # The row was really read, and the owner of record was there to take.
        for k in cases:
            assert fetched[k]["assignee_name"] == f"Live Cash Buyer {k}"
            assert fetched[k]["payload"]["owner_name"] == OWNER

        bare = fetched["owner_only"]
        assert bare["seller_name"] is None
        result = draft_assignment_contract(bare)
        assert result["status"] == "FATAL_ERROR"
        assert "seller_name" in result["missing_variables"]

        assert fetched["linked"]["seller_name"] == "Live Linked Seller"
        assert fetched["stated"]["seller_name"] == "Live Stated Seller"
        for k in ("linked", "stated"):
            drafted = draft_assignment_contract({**fetched[k], "closing_date": "2099-08-01"})
            assert drafted["status"] == "SUCCESS", drafted["missing_variables"]
            assert OWNER not in drafted["final_contract_text"]
    finally:
        await admin.execute("SET session_replication_role = replica")
        try:
            tables = await admin.fetch(
                """SELECT c.table_name, c.data_type FROM information_schema.columns c
                     JOIN information_schema.tables t
                       ON t.table_name = c.table_name AND t.table_schema = c.table_schema
                    WHERE c.table_schema = 'public' AND c.column_name = 'tenant_id'
                      AND t.table_type = 'BASE TABLE'""")
            for row in tables:
                cast = "uuid" if row["data_type"] == "uuid" else "text"
                await admin.execute(f"DELETE FROM {row['table_name']} WHERE tenant_id = $1::{cast}", str(tenant))
            await admin.execute("DELETE FROM tenants WHERE id = $1", tenant)
        finally:
            await admin.execute("SET session_replication_role = origin")
            await admin.close()
            await connection.close_pool()
