"""The homeowner's dossier never shows a title finding a reviewer dismissed.

`title_summary` is read by the person whose house it is. A finding a reviewer
set to `dismissed` was judged not to apply, so serving it states a lien or
judgment against the client's house that nobody stands behind (F11.04).
Possible and unresolved matches stay listed with their `match_status`, which
the page labels "Possible match" (AC11.04). A row carries only what the page
shows: the agent's free-text notes stay with the agent.

Driven through `read_scoped_dossier` with a fake connection; the route needs a
database, so the title query it sends is captured and asserted.
"""
from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import re
import uuid
from decimal import Decimal

import client_portal

TENANT = str(uuid.uuid4())
LEAD = str(uuid.uuid4())
PORTAL = str(uuid.uuid4())
NOW = dt.datetime(2026, 10, 17, tzinfo=dt.timezone.utc)


class _Conn:
    def __init__(self, scope: dict):
        self.scope = scope
        self.title_queries: list[str] = []

    async def fetchrow(self, query, *args):
        if "FROM client_portals" in query:
            return {
                "asset_scope": self.scope,
                "link_kind": "seller",
                "watermark_text": None,
            }
        if "FROM leads" in query:
            return {
                "id": uuid.UUID(LEAD), "parcel_id": "P-1", "state": "DE",
                "address": "1 Test Lane", "asking_price": None,
                "dossier_status": "draft", "contract_execution_date": None,
                "contract_expires_at": None, "updated_at": NOW,
            }
        raise AssertionError(f"unexpected fetchrow: {query}")

    async def fetch(self, query, *args):
        if "FROM title_findings" in query:
            self.title_queries.append(query)
            # Every column of the table, so a row passed through whole would
            # carry the agent's notes to the client.
            return [{
                "id": uuid.uuid4(), "tenant_id": uuid.UUID(TENANT),
                "property_key": "P-1", "finding_type": "lien", "record_id": "R-9",
                "amount": Decimal("45000"),
                "recorded_at": dt.date(2024, 1, 2), "released_at": None,
                "match_status": "possible_match", "chain_gap": False,
                "source_record_id": uuid.uuid4(),
                "notes": "Judgment v. a different owner of the same name, 88 Oak St",
                "review_status": "required", "reviewed_by": "agent-1",
                "reviewed_at": None, "created_at": NOW,
            }]
        raise AssertionError(f"unexpected fetch: {query}")


def _read(monkeypatch, scope: dict) -> tuple[dict, _Conn]:
    conn = _Conn(scope)

    @contextlib.asynccontextmanager
    async def fake_tx(_ctx):
        yield conn

    async def no_activity(*_a, **_k):
        return None

    monkeypatch.setattr(client_portal, "tenant_tx", fake_tx)
    monkeypatch.setattr(client_portal, "_record_portal_activity", no_activity)
    monkeypatch.setattr(client_portal, "_portal_session_claims", lambda _h: {
        "portal_id": PORTAL, "tenant_id": TENANT, "lead_id": LEAD, "link_kind": "seller",
    })
    payload = asyncio.run(client_portal.read_scoped_dossier(authorization="Bearer x"))
    return payload, conn


def _flat(query: str) -> str:
    return re.sub(r"\s+", " ", query)


def test_the_title_summary_excludes_dismissed_findings(monkeypatch):
    payload, conn = _read(monkeypatch, {"summary": True, "title_summary": True})

    assert len(conn.title_queries) == 1
    query = _flat(conn.title_queries[0])
    assert "review_status <> 'dismissed'" in query
    # Scoped to this lead's parcel; the tenant is RLS's job, not this query's.
    assert "property_key=$1" in query
    assert "tenant_id" not in query
    assert payload["assets"]["title_summary"][0]["review_status"] == "required"


def test_a_possible_match_keeps_its_match_status_for_the_page(monkeypatch):
    payload, _ = _read(monkeypatch, {"summary": True, "title_summary": True})

    finding = payload["assets"]["title_summary"][0]
    assert finding["match_status"] == "possible_match"
    assert finding["amount"] == 45000.0
    assert finding["warning"] == "Preliminary public-record finding; not an insured title search."


def test_a_finding_carries_only_what_the_page_shows(monkeypatch):
    payload, conn = _read(monkeypatch, {"summary": True, "title_summary": True})

    # The link is forwarded and opened on shared machines; the agent's working
    # notes (often a third party's court record) are not for the client.
    assert set(payload["assets"]["title_summary"][0]) == {
        "finding_type", "amount", "recorded_at", "released_at",
        "match_status", "review_status", "warning",
    }
    query = _flat(conn.title_queries[0])
    assert "notes" not in query
    assert "chain_gap" not in query


def test_no_title_query_without_the_grant(monkeypatch):
    payload, conn = _read(monkeypatch, {"summary": True})

    assert conn.title_queries == []
    assert "title_summary" not in payload["assets"]
