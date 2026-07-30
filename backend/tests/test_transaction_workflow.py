"""Focused unit coverage for the canonical tenant-scoped deal workflow."""

from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import portfolio_api
from portfolio_api import (
    OfferAccept,
    OfferCreate,
    TransactionClose,
    TransactionCreate,
    TransactionUpdate,
    accept_offer,
    close_transaction,
    create_offer,
    create_transaction,
    list_offers,
    list_transactions,
    patch_transaction,
)
from tenancy import Role, TenantContext


TENANT_ID = "11111111-1111-1111-1111-111111111111"
TRANSACTION_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
LEAD_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")
MLS_LISTING_ID = uuid.UUID("55555555-5555-5555-5555-555555555555")
CLIENT_ID = uuid.UUID("66666666-6666-6666-6666-666666666666")
OFFER_ID = uuid.UUID("77777777-7777-7777-7777-777777777777")
CTX = TenantContext(
    agent_id="agent@tenant.test", tenant_id=TENANT_ID, role=Role.AGENT
)


def _fake_tenant_tx(conn):
    @asynccontextmanager
    async def tx(received_ctx):
        assert received_ctx == CTX
        conn.transaction_entries += 1
        yield conn

    return tx


def _transaction(*, status: str = "active", version: int = 1) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "id": TRANSACTION_ID,
        "tenant_id": uuid.UUID(TENANT_ID),
        "state_code": "DE",
        "property_type": "residential_1_4",
        "property_source": "pipeline",
        "property_id": LEAD_ID,
        "lead_id": LEAD_ID,
        "listing_id": None,
        "mls_listing_id": None,
        "client_id": CLIENT_ID,
        "client_party_role": "buyer",
        "property_address": "10 Main St",
        "property_city": "Dover",
        "property_postal_code": "19901",
        "source_provenance": {"source": "pipeline", "source_id": str(LEAD_ID)},
        "purchase_price": Decimal("200000.00"),
        "earnest_money": Decimal("2000.00"),
        "financing_amount": None,
        "offer_deadline": None,
        "inspection_deadline": None,
        "financing_deadline": None,
        "closing_deadline": date(2026, 9, 1),
        "notes": None,
        "accepted_offer_id": None,
        "closed_at": None,
        "status": status,
        "version": version,
        "created_at": now,
        "updated_at": now,
    }


def _offer(*, status: str = "submitted", version: int = 1) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "id": OFFER_ID,
        "tenant_id": uuid.UUID(TENANT_ID),
        "transaction_id": TRANSACTION_ID,
        "status": status,
        "amount": Decimal("210000.00"),
        "earnest_money": Decimal("3000.00"),
        "financing_type": "conventional",
        "proposed_closing_date": date(2026, 8, 28),
        "expires_at": now + timedelta(days=2),
        "contingencies": {"inspection": True},
        "notes": None,
        "version": version,
        "submitted_at": now,
        "accepted_at": now if status == "accepted" else None,
        "created_at": now,
        "updated_at": now,
    }


class _CreateConn:
    def __init__(self, source: str = "pipeline", *, missing_source: bool = False):
        self.source = source
        self.missing_source = missing_source
        self.queries: list[str] = []
        self.transaction_entries = 0

    async def fetchrow(self, query, *args):
        self.queries.append(query)
        if "FROM leads" in query:
            assert "tenant_id=$2::uuid" in query
            assert args == (LEAD_ID, TENANT_ID)
            if self.missing_source:
                return None
            return {
                "id": LEAD_ID,
                "parcel_id": "parcel-10",
                "state_code": "de",
                "address": "10 Main St",
                "city": "Dover",
                "postal_code": "19901",
                "property_type": "residential_1_4",
                "updated_at": datetime.now(timezone.utc),
            }
        if "FROM oracle_mls_listings" in query:
            assert args == (MLS_LISTING_ID,)
            if self.missing_source:
                return None
            return {
                "id": MLS_LISTING_ID,
                "mls_id": "bright",
                "mls_number": "DENC2042",
                "state_code": "DE",
                "address": "20 Market St",
                "city": "Wilmington",
                "postal_code": "19801",
                "property_type": "residential_1_4",
                "updated_at": datetime.now(timezone.utc),
            }
        if "FROM clients" in query:
            assert "tenant_id=$2::uuid" in query
            assert args == (CLIENT_ID, TENANT_ID)
            return {"id": CLIENT_ID, "full_name": "Casey Buyer"}
        if "INSERT INTO transactions" in query:
            property_id = LEAD_ID if self.source == "pipeline" else MLS_LISTING_ID
            assert args[0] == TENANT_ID
            assert args[3] == (LEAD_ID if self.source == "pipeline" else None)
            assert args[4] == (MLS_LISTING_ID if self.source == "mls" else None)
            assert args[7] == self.source
            assert args[8] == property_id
            provenance = json.loads(args[12])
            assert provenance["source"] == self.source
            assert provenance["source_id"] == str(property_id)
            result = _transaction()
            result.update(
                {
                    "property_source": self.source,
                    "property_id": property_id,
                    "lead_id": args[3],
                    "mls_listing_id": args[4],
                    "client_id": args[5],
                    "client_party_role": args[6],
                    "property_address": args[9],
                    "source_provenance": provenance,
                }
            )
            return result
        if "INSERT INTO transaction_parties" in query:
            return {
                "id": uuid.uuid4(),
                "tenant_id": uuid.UUID(TENANT_ID),
                "transaction_id": TRANSACTION_ID,
                "party_role": args[2],
                "client_id": args[3],
                "display_name": args[4],
                "created_at": datetime.now(timezone.utc),
            }
        raise AssertionError(f"Unexpected fetchrow query: {query}")


def test_create_requires_client_and_party_role_as_an_explicit_pair():
    with pytest.raises(ValidationError, match="provided together"):
        TransactionCreate(
            property_source="pipeline", property_id=LEAD_ID, client_id=CLIENT_ID
        )


def test_pipeline_creation_uses_only_selected_property_and_tenant_client(monkeypatch):
    conn = _CreateConn("pipeline")
    monkeypatch.setattr(portfolio_api, "tenant_tx", _fake_tenant_tx(conn))

    result = asyncio.run(
        create_transaction(
            TransactionCreate(
                property_source="pipeline",
                property_id=LEAD_ID,
                client_id=CLIENT_ID,
                party_role="buyer",
                purchase_price=Decimal("200000"),
                closing_deadline=date(2026, 9, 1),
            ),
            CTX,
        )
    )

    assert conn.transaction_entries == 1
    assert result["transaction"]["property_source"] == "pipeline"
    assert result["transaction"]["property_id"] == str(LEAD_ID)
    assert result["transaction"]["source_provenance"]["parcel_id"] == "parcel-10"
    assert result["party"]["party_role"] == "buyer"
    assert result["party"]["display_name"] == "Casey Buyer"
    assert "showings" not in "\n".join(conn.queries).lower()


def test_mls_creation_snapshots_normalized_source_without_client_inference(monkeypatch):
    conn = _CreateConn("mls")
    monkeypatch.setattr(portfolio_api, "tenant_tx", _fake_tenant_tx(conn))

    result = asyncio.run(
        create_transaction(
            TransactionCreate(property_source="mls", property_id=MLS_LISTING_ID),
            CTX,
        )
    )

    transaction = result["transaction"]
    assert transaction["property_source"] == "mls"
    assert transaction["mls_listing_id"] == str(MLS_LISTING_ID)
    assert transaction["lead_id"] is None
    assert transaction["client_id"] is None
    assert transaction["source_provenance"]["mls_number"] == "DENC2042"
    assert result["party"] is None
    assert not any("FROM clients" in query for query in conn.queries)


def test_unknown_or_cross_tenant_property_is_a_non_enumerating_404(monkeypatch):
    conn = _CreateConn("pipeline", missing_source=True)
    monkeypatch.setattr(portfolio_api, "tenant_tx", _fake_tenant_tx(conn))

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            create_transaction(
                TransactionCreate(property_source="pipeline", property_id=LEAD_ID),
                CTX,
            )
        )

    assert exc.value.status_code == 404
    assert exc.value.detail == "Property source not found."
    assert not any("INSERT INTO transactions" in query for query in conn.queries)


def test_transaction_list_shape_and_filters_remain_explicitly_tenant_scoped(monkeypatch):
    class _ListTransactionsConn:
        transaction_entries = 0

        async def fetchval(self, query, *args):
            assert "WHERE t.tenant_id=$1::uuid" in query
            assert args == (TENANT_ID, "active", "pipeline", CLIENT_ID)
            return 61

        async def fetch(self, query, *args):
            assert "WHERE t.tenant_id=$1::uuid" in query
            assert args == (TENANT_ID, "active", "pipeline", CLIENT_ID, 25, 50)
            return [{**_transaction(), "client_name": "Casey Buyer"}]

    conn = _ListTransactionsConn()
    monkeypatch.setattr(portfolio_api, "tenant_tx", _fake_tenant_tx(conn))
    result = asyncio.run(
        list_transactions(
            transaction_status="active",
            property_source="pipeline",
            client_id=CLIENT_ID,
            limit=25,
            offset=50,
            ctx=CTX,
        )
    )

    assert result["total"] == 61
    assert result["limit"] == 25
    assert result["offset"] == 50
    assert result["transactions"][0]["client_name"] == "Casey Buyer"
    assert "total_count" not in result["transactions"][0]


class _PatchConn:
    def __init__(self, *, present: bool = True):
        self.present = present
        self.transaction_entries = 0
        self.queries: list[str] = []

    async def fetchrow(self, query, *args):
        self.queries.append(query)
        if "SELECT * FROM transactions" in query:
            assert "tenant_id=$2::uuid" in query
            return _transaction(version=4) if self.present else None
        raise AssertionError(f"Patch should not write after failed preflight: {query}")


@pytest.mark.parametrize(
    ("present", "expected_status"), [(True, 409), (False, 404)]
)
def test_patch_distinguishes_stale_version_from_inaccessible_row(
    monkeypatch, present, expected_status
):
    conn = _PatchConn(present=present)
    monkeypatch.setattr(portfolio_api, "tenant_tx", _fake_tenant_tx(conn))

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            patch_transaction(
                TRANSACTION_ID,
                TransactionUpdate(version=3, notes="updated"),
                CTX,
            )
        )
    assert exc.value.status_code == expected_status
    if present:
        assert exc.value.detail == {
            "code": "version_conflict",
            "resource": "transaction",
            "current_version": 4,
        }


def test_patch_updates_only_whitelisted_fields_and_increments_version(monkeypatch):
    class _PatchSuccessConn:
        transaction_entries = 0

        async def fetchrow(self, query, *args):
            if "SELECT * FROM transactions" in query:
                return _transaction(version=1)
            if "UPDATE transactions" in query:
                assert "closing_deadline=$4" in query
                assert "notes=$5" in query
                assert "version=version+1" in query
                assert "tenant_id=$2::uuid AND version=$3" in query
                assert args == (
                    TRANSACTION_ID,
                    TENANT_ID,
                    1,
                    date(2026, 9, 15),
                    "ready for review",
                    CTX.agent_id,
                )
                updated = _transaction(version=2)
                updated.update(
                    {"closing_deadline": args[3], "notes": args[4]}
                )
                return updated
            raise AssertionError(f"Unexpected query: {query}")

    conn = _PatchSuccessConn()
    monkeypatch.setattr(portfolio_api, "tenant_tx", _fake_tenant_tx(conn))
    result = asyncio.run(
        patch_transaction(
            TRANSACTION_ID,
            TransactionUpdate(
                version=1,
                notes="ready for review",
                closing_deadline=date(2026, 9, 15),
            ),
            CTX,
        )
    )

    assert result["transaction"]["version"] == 2
    assert result["transaction"]["closing_deadline"] == "2026-09-15"


class _OfferConn:
    def __init__(self):
        self.transaction_entries = 0
        self.queries: list[str] = []

    async def fetchrow(self, query, *args):
        self.queries.append(query)
        if "SELECT id,status FROM transactions" in query:
            assert "tenant_id=$2::uuid" in query
            return {"id": TRANSACTION_ID, "status": "active"}
        if "INSERT INTO transaction_offers" in query:
            result = _offer()
            result.update(
                {
                    "amount": args[2],
                    "earnest_money": args[3],
                    "financing_type": args[4],
                    "contingencies": json.loads(args[7]),
                }
            )
            return result
        raise AssertionError(f"Unexpected fetchrow query: {query}")


def test_offer_create_and_list_shapes_are_tenant_scoped(monkeypatch):
    create_conn = _OfferConn()
    monkeypatch.setattr(portfolio_api, "tenant_tx", _fake_tenant_tx(create_conn))
    created = asyncio.run(
        create_offer(
            TRANSACTION_ID,
            OfferCreate(
                amount=Decimal("210000"),
                earnest_money=Decimal("3000"),
                financing_type="conventional",
                contingencies={"inspection": True},
            ),
            CTX,
        )
    )
    assert created["offer"]["status"] == "submitted"
    assert created["offer"]["version"] == 1

    class _ListConn:
        transaction_entries = 0

        async def fetchval(self, query, *args):
            assert "tenant_id=$2::uuid" in query
            return 1

        async def fetch(self, query, *args):
            assert "tenant_id=$2::uuid" in query
            return [_offer()]

    list_conn = _ListConn()
    monkeypatch.setattr(portfolio_api, "tenant_tx", _fake_tenant_tx(list_conn))
    listed = asyncio.run(list_offers(TRANSACTION_ID, CTX))
    assert listed["total"] == 1
    assert listed["offers"][0]["id"] == str(OFFER_ID)


class _TransitionConn:
    def __init__(self, transaction_status: str = "active", transaction_version: int = 1):
        self.current_transaction = _transaction(
            status=transaction_status, version=transaction_version
        )
        self.current_offer = _offer()
        self.transaction_entries = 0
        self.queries: list[str] = []
        self.executed: list[tuple[str, tuple]] = []

    async def fetchrow(self, query, *args):
        self.queries.append(query)
        if "SELECT * FROM transactions" in query:
            assert "tenant_id=$2::uuid" in query
            return self.current_transaction
        if "SELECT * FROM transaction_offers" in query:
            assert "tenant_id=$3::uuid" in query
            return self.current_offer
        if "UPDATE transaction_offers" in query:
            accepted = dict(self.current_offer)
            accepted.update(
                {"status": "accepted", "version": 2, "accepted_at": datetime.now(timezone.utc)}
            )
            return accepted
        if "UPDATE transactions" in query:
            updated = dict(self.current_transaction)
            if "status='under_contract'" in query:
                updated.update(
                    {
                        "status": "under_contract",
                        "version": self.current_transaction["version"] + 1,
                        "accepted_offer_id": OFFER_ID,
                        "purchase_price": self.current_offer["amount"],
                        "earnest_money": self.current_offer["earnest_money"],
                    }
                )
            elif "status='closed'" in query:
                updated.update(
                    {
                        "status": "closed",
                        "version": self.current_transaction["version"] + 1,
                        "closed_at": datetime.now(timezone.utc),
                    }
                )
            return updated
        raise AssertionError(f"Unexpected fetchrow query: {query}")

    async def execute(self, query, *args):
        self.executed.append((query, args))
        return "UPDATE 1"


def test_accept_offer_moves_all_tenant_owned_stages_atomically(monkeypatch):
    conn = _TransitionConn()
    monkeypatch.setattr(portfolio_api, "tenant_tx", _fake_tenant_tx(conn))

    result = asyncio.run(
        accept_offer(
            TRANSACTION_ID,
            OFFER_ID,
            OfferAccept(transaction_version=1, offer_version=1),
            CTX,
        )
    )

    assert conn.transaction_entries == 1
    assert result["transaction"]["status"] == "under_contract"
    assert result["transaction"]["version"] == 2
    assert result["offer"]["status"] == "accepted"
    assert result["offer"]["version"] == 2
    executed_sql = "\n".join(query for query, _ in conn.executed)
    assert "status='rejected'" in executed_sql
    assert "UPDATE clients SET stage=$1" in executed_sql
    assert "UPDATE leads SET dossier_status=$1" in executed_sql
    assert "UPDATE listings SET status=$1" in executed_sql
    assert "oracle_mls_listings" not in executed_sql
    assert "outreach" not in executed_sql.lower()
    assert "contract_documents" not in executed_sql.lower()
    stage_args = [args for query, args in conn.executed if "UPDATE clients" in query]
    assert stage_args == [("under_contract", CLIENT_ID, TENANT_ID)]


def test_close_moves_transaction_client_lead_and_listing_to_closed(monkeypatch):
    conn = _TransitionConn(transaction_status="under_contract", transaction_version=2)
    monkeypatch.setattr(portfolio_api, "tenant_tx", _fake_tenant_tx(conn))

    result = asyncio.run(
        close_transaction(
            TRANSACTION_ID,
            TransactionClose(version=2),
            CTX,
        )
    )

    assert result["transaction"]["status"] == "closed"
    assert result["transaction"]["version"] == 3
    stages = {
        "client": next(args for query, args in conn.executed if "UPDATE clients" in query)[0],
        "lead": next(args for query, args in conn.executed if "UPDATE leads" in query)[0],
        "listing": next(args for query, args in conn.executed if "UPDATE listings" in query)[0],
    }
    assert stages == {"client": "closed", "lead": "closed", "listing": "sold"}


def test_migration_forces_rls_and_uses_composite_tenant_parent_keys():
    migration = (
        Path(__file__).parents[1]
        / "db"
        / "migrations"
        / "0039_transaction_workflow.sql"
    ).read_text(encoding="utf-8")

    assert "ALTER TABLE transactions FORCE ROW LEVEL SECURITY" in migration
    assert "ALTER TABLE transaction_offers FORCE ROW LEVEL SECURITY" in migration
    assert "FOREIGN KEY (tenant_id,transaction_id)" in migration
    assert "REFERENCES transactions(tenant_id,id)" in migration
    assert "UNIQUE (tenant_id,transaction_id,id)" in migration
    assert "('submitted','accepted','rejected','withdrawn','expired')" in migration
    assert "uq_transaction_offers_one_accepted" in migration
