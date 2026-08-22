"""Tools that request an action, and the moments they must refuse instead.

Each of these has a real execution path behind it — an approved command goes to
a provider, an approved contract goes to the vault, an approved publication goes
in front of buyers. What is worth pinning is the part that decides whether a
request is even fit to put in front of a human: a contract whose terms were
invented, a calendar entry at an ambiguous instant, a listing whose audience the
model chose.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone

import pytest

import ai_tools_gated as gated
from tenancy import Role, TenantContext


CTX = TenantContext(
    agent_id="agent@tenant.test",
    tenant_id="11111111-1111-1111-1111-111111111111",
    role=Role.AGENT,
)
DEAL_ID = "22222222-2222-2222-2222-222222222222"
CLIENT_ID = "33333333-3333-3333-3333-333333333333"


class _Conn:
    def __init__(self, *routes):
        self.routes = list(routes)
        self.queries: list[str] = []

    def _answer(self, query, default):
        normalised = " ".join(query.split())
        self.queries.append(normalised)
        for fragment, value in self.routes:
            if fragment in normalised:
                return value
        return default

    async def fetch(self, query, *args):
        return self._answer(query, [])

    async def fetchrow(self, query, *args):
        return self._answer(query, None)

    async def fetchval(self, query, *args):
        return self._answer(query, None)


def _run(tool, conn, *, context_type="lead", context_id=DEAL_ID, **tool_input):
    return asyncio.run(gated.execute(
        conn, CTX, tool, tool_input, user_id="u1", message_id="m1",
        context_type=context_type, context_id=context_id,
    ))


_TEMPLATE = {
    "id": "44444444-4444-4444-4444-444444444444",
    "template_key": "seller-purchase-standard",
    "version": "1.0",
    "document_type": "seller_purchase",
    "status": "approved",
    "attorney_reviewed_by": "R. Alvarez, Esq.",
    "required_fields": ["current_date", "seller_name", "buyer_name",
                        "property_address", "purchase_price",
                        "earnest_money_deposit", "closing_date",
                        "approved_addenda"],
}
_LEAD = {"id": DEAL_ID, "address": "15 Main St", "seller_client_id": CLIENT_ID}


def _contract_conn(*, template=_TEMPLATE, transaction=None, parties=None):
    return _Conn(
        ("FROM leads WHERE id=$1::uuid", _LEAD),
        ("FROM contract_templates", template),
        ("FROM transactions WHERE lead_id", transaction),
        ("FROM transaction_parties", parties or []),
        ("SELECT full_name FROM clients", "Dana Reed"),
    )


# ---------------------------------------------------------------------------
# Contracts
# ---------------------------------------------------------------------------

def test_a_contract_with_unrecorded_terms_is_not_drafted_and_the_gaps_are_named():
    """The old behaviour was a flat refusal that said nothing useful. Naming the
    gaps is the whole improvement — and drafting around them would put invented
    terms inside a legal instrument a broker then signs off."""
    result = _run("draft_contract", _contract_conn(),
                  deal_id=DEAL_ID, template_key="seller-purchase-standard")

    assert result["ok"] is True
    assert result["drafted"] is False
    assert "document_id" not in result
    assert "purchase_price" in result["missing_fields"]
    assert "transactions.purchase_price" in result["missing_fields"]["purchase_price"]
    # A term no record holds says so, rather than pointing at a column that
    # does not exist.
    assert "someone has to decide" in result["missing_fields"]["approved_addenda"]


def test_terms_that_are_recorded_are_taken_from_the_records():
    conn = _contract_conn(
        transaction={"id": "t1", "property_address": "15 Main St",
                     "purchase_price": 240000, "earnest_money": 5000,
                     "closing_deadline": date(2026, 10, 1),
                     "accepted_offer_id": None},
        parties=[{"party_role": "seller", "display_name": "Dana Reed"},
                 {"party_role": "buyer", "display_name": "Kip Holdings LLC"}],
    )
    result = _run("draft_contract", conn, deal_id=DEAL_ID,
                  template_key="seller-purchase-standard")

    resolved = result["resolved_fields"]
    assert resolved["purchase_price"] == "240,000.00"
    assert resolved["seller_name"] == "Dana Reed"
    assert resolved["buyer_name"] == "Kip Holdings LLC"
    assert resolved["closing_date"] == "2026-10-01"
    assert resolved["current_date"] == date.today().isoformat()
    # approved_addenda is still unrecorded, so still no document.
    assert result["drafted"] is False
    assert list(result["missing_fields"]) == ["approved_addenda"]


def test_no_contract_is_drafted_from_an_unapproved_template():
    result = _run("draft_contract", _contract_conn(template=None),
                  deal_id=DEAL_ID, template_key="seller-purchase-standard")

    assert result["ok"] is False
    assert "approved" in result["error"]


def test_no_contract_names_a_reviewing_attorney_who_is_not_on_file():
    """An invented reviewer on a legal document is a forged attestation."""
    conn = _contract_conn(
        template={**_TEMPLATE, "attorney_reviewed_by": None,
                  "required_fields": ["current_date"]},
    )
    result = _run("draft_contract", conn, deal_id=DEAL_ID,
                  template_key="seller-purchase-standard")

    assert result["ok"] is False
    assert "reviewing attorney" in result["error"]


def test_an_unknown_template_key_lists_the_real_ones():
    result = _run("draft_contract", _contract_conn(), deal_id=DEAL_ID,
                  template_key="whatever-the-model-remembered")

    assert result["ok"] is False
    assert "seller-purchase-standard" in result["error"]


def test_a_deal_from_another_workspace_drafts_nothing():
    conn = _Conn(("FROM leads WHERE id=$1::uuid", None))
    result = _run("draft_contract", conn, deal_id=DEAL_ID,
                  template_key="seller-purchase-standard")
    assert result["ok"] is False
    assert "not in this workspace" in result["error"]


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------

def _calendar_conn():
    return _Conn(
        ("FROM clients", {"id": CLIENT_ID, "full_name": "Dana Reed",
                          "email": "dana@example.test", "phone": "+13025550134"}),
        ("SELECT state FROM leads", "DE"),
    )


def _future(hours: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def test_a_calendar_time_without_an_offset_is_refused(monkeypatch):
    """A naive timestamp is a different moment in every timezone, and the people
    in a showing are not all in one."""
    result = _run("schedule_event", _calendar_conn(),
                  context_type="client", context_id=CLIENT_ID,
                  client_id=CLIENT_ID, summary="Showing",
                  start="2026-09-01T14:00:00", end="2026-09-01T15:00:00")

    assert result["ok"] is False
    assert "UTC offset" in result["error"]


def test_a_meeting_cannot_be_scheduled_backwards():
    conn = _calendar_conn()
    past = _run("schedule_event", conn, context_type="client", context_id=CLIENT_ID,
                client_id=CLIENT_ID, summary="Showing",
                start=_future(-48), end=_future(-47))
    assert past["ok"] is False and "in the past" in past["error"]

    inverted = _run("schedule_event", conn, context_type="client", context_id=CLIENT_ID,
                    client_id=CLIENT_ID, summary="Showing",
                    start=_future(3), end=_future(2))
    assert inverted["ok"] is False and "after start" in inverted["error"]


def test_a_valid_event_is_staged_and_not_written(monkeypatch):
    import commands_api

    staged: list = []

    async def _stage_command(ctx, **kwargs):
        staged.append(kwargs)
        return {"command": {"id": "c1", "state": "awaiting_approval"},
                "approval": {"id": "a1"}, "created": True}

    monkeypatch.setattr(commands_api, "stage_command", _stage_command)
    result = _run("schedule_event", _calendar_conn(), context_type="client",
                  context_id=CLIENT_ID, client_id=CLIENT_ID, summary="Showing",
                  start=_future(24), end=_future(25))

    assert result["ok"] is True
    assert result["sent"] is False
    assert result["state"] == "awaiting_approval"
    assert staged[0]["command_type"] is commands_api.CommandType.CALENDAR
    # The invitee is whoever the record says.
    assert staged[0]["draft"]["event"]["attendee"] == "dana@example.test"


# ---------------------------------------------------------------------------
# Marketplace
# ---------------------------------------------------------------------------

def test_the_model_does_not_choose_the_listing_audience(monkeypatch):
    """Platform visibility puts a property in front of every brokerage. That is
    a disposition decision, so the request is always workspace-only and the
    approver widens it."""
    import marketplace_api

    captured: list = []

    async def _create(contract_id, body, ctx):
        captured.append(body)
        return {"publication": {"id": "p1", "state": "draft", "visibility": body.visibility},
                "approval": {"id": "a1"}}

    monkeypatch.setattr(marketplace_api, "create_publication_from_contract", _create)
    result = _run("publish_to_marketplace", _Conn(),
                  contract_id="55555555-5555-5555-5555-555555555555",
                  asking_price="240000")

    assert result["ok"] is True
    assert result["published"] is False
    assert captured[0].visibility == "tenant"
    assert captured[0].asking_price == 240000.0
    assert "not visible to any buyer yet" in result["detail"]


def test_a_malformed_contract_id_is_refused_before_anything_is_created():
    result = _run("publish_to_marketplace", _Conn(), contract_id="not-a-uuid")
    assert result["ok"] is False
    assert "must be a UUID" in result["error"]
