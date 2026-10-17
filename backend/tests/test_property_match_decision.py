"""A broker owner confirms, rejects or withdraws that a public property record
is a linked client's house (T11.03, F11.04).

The Houses search says what it matched (an exact key, or a possible match),
and nothing recorded a person deciding whether that record IS the house a
client's lead refers to, so `identity.verified` was False for every row.
`POST /api/crm/houses/{lead_id}/record-match` is that decision: one
append-only row in property_match_decisions (0168) per decision, a reversal
being a new row naming the head it supersedes. The pairing is the exact parcel
key Houses already uses (lead.parcel_id/state = the record's). The client is
read from the lead under FOR UPDATE, never taken from the request. The reason
often names an owner, so it is stored only sealed under the lead's own tenant
key; the audit ledger gets ids and the decision only.

Driven with a fake connection; tests/test_property_match_decisions_live.py
proves the same under FORCE RLS as the application login.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import asyncpg
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import property_match_api
import sealed_text
from audit_ledger import AuditCategory
from tenancy import Role, TenantContext

TENANT_ID = "11111111-1111-1111-1111-111111111111"
# A platform admin works from their own context tenant; the lead may not be in it.
OTHER_TENANT = uuid.UUID("22222222-2222-2222-2222-222222222222")
LEAD = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
RECORD = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
CLIENT = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
OTHER_CLIENT = uuid.UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
HEAD = uuid.UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee")
DECISION = uuid.UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")
OWNER = TenantContext(agent_id="owner@brokerage.test", tenant_id=TENANT_ID, role=Role.BROKER_OWNER)
AGENT = TenantContext(agent_id="agent@brokerage.test", tenant_id=TENANT_ID, role=Role.AGENT)
ADMIN = TenantContext(agent_id="ops@platform.test", tenant_id=TENANT_ID, role=Role.PLATFORM_ADMIN)
DECIDED_AT = datetime(2026, 10, 17, 16, 0, tzinfo=timezone.utc)
REFRESHED_AT = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
MASTER = "record-match-test-master-key-0123456789abcdef"
# What a real broker owner writes: the owner's name and what they compared.
REASON = "Deed names Jane Q. Homeowner at 88 Oak St, parcel DE-NCC-0142 checked"

_UNSET = object()


def _lead(**overrides):
    return {"tenant_id": uuid.UUID(TENANT_ID), "parcel_id": "DE-NCC-0142", "state": "DE",
            "seller_client_id": CLIENT, **overrides}


def _record(**overrides):
    return {"id": RECORD, "parcel_id": "DE-NCC-0142", "state": "DE",
            "record_refreshed_at": REFRESHED_AT, **overrides}


def _head(decision="confirmed", client=CLIENT):
    return {"id": HEAD, "decision": decision, "client_id": client}


class _Conn:
    """The lead (None = invisible under RLS), the record (None = unknown), the
    current head of the pair's chain (None = undecided), and the INSERT."""

    def __init__(self, *, lead=_UNSET, record=_UNSET, head=None, unique_violation=False):
        self.lead = _lead() if lead is _UNSET else lead
        self.record = _record() if record is _UNSET else record
        self.head = head
        self.unique_violation = unique_violation
        self.queries: list[tuple[str, tuple]] = []

    async def fetchrow(self, query, *args):
        self.queries.append((query, args))
        flat = _flat(query)
        if "FROM leads" in flat and "FOR UPDATE" in flat:
            return self.lead
        if "FROM public_property_records" in flat:
            return self.record
        if flat.startswith("INSERT INTO property_match_decisions"):
            if self.unique_violation:
                raise asyncpg.UniqueViolationError(
                    'duplicate key value violates unique constraint "uq_pmd_successor"')
            return {"id": DECISION, "decided_at": DECIDED_AT}
        if "FROM property_match_decisions" in flat:
            return self.head
        raise AssertionError(f"unexpected fetchrow: {query}")

    async def fetch(self, query, *args):
        self.queries.append((query, args))
        raise AssertionError(f"unexpected fetch: {query}")

    async def fetchval(self, query, *args):
        self.queries.append((query, args))
        raise AssertionError(f"unexpected fetchval: {query}")

    async def execute(self, query, *args):
        self.queries.append((query, args))
        raise AssertionError(f"unexpected execute: {query}")


class _Ledger:
    def __init__(self):
        self.calls: list[dict] = []

    async def record(self, **kwargs):
        self.calls.append(kwargs)
        return kwargs


def _body(**overrides):
    fields = {"public_record_id": str(RECORD), "decision": "confirmed",
              "evidence": ["parcel_number_checked", "deed_or_title"], "reason": REASON,
              "supersedes_id": None, **overrides}
    return property_match_api.RecordMatchDecision(**fields)


def _decide(monkeypatch, ctx, *, body=None, conn=None, master=MASTER):
    if master:
        monkeypatch.setenv("ORACLE_ENCRYPTION_MASTER_KEY", master)
    else:
        monkeypatch.delenv("ORACLE_ENCRYPTION_MASTER_KEY", raising=False)
    conn = conn or _Conn()
    ledger = _Ledger()

    @asynccontextmanager
    async def fake_tx(received):
        assert received == ctx
        yield conn

    monkeypatch.setattr(property_match_api, "tenant_tx", fake_tx)
    monkeypatch.setattr(property_match_api, "ledger", ledger)
    try:
        result = asyncio.run(property_match_api.decide_record_match(
            lead_id=LEAD, body=body or _body(), ctx=ctx))
    except (HTTPException, sealed_text.SealError) as exc:
        return exc, conn, ledger
    return result, conn, ledger


def _flat(query: str) -> str:
    return re.sub(r"\s+", " ", query).strip()


def _inserts(conn) -> list:
    return [(q, a) for q, a in conn.queries if _flat(q).startswith("INSERT INTO property_match_decisions")]


# ── who may decide ─────────────────────────────────────────────────────────

def test_an_agent_cannot_decide_and_nothing_runs(monkeypatch):
    error, conn, ledger = _decide(monkeypatch, AGENT)

    assert isinstance(error, HTTPException) and error.status_code == 403
    assert conn.queries == [] and ledger.calls == []


def test_a_broker_owner_confirms_a_linked_houses_record(monkeypatch):
    result, conn, ledger = _decide(monkeypatch, OWNER)

    assert result["decision_id"] == str(DECISION)
    assert result["decision"] == "confirmed"
    assert result["decided_at"] == DECIDED_AT.isoformat()
    assert result["identity"] == {
        "verified": True,
        "confirmation": {"state": "confirmed", "head_id": str(DECISION), "decided_at": DECIDED_AT.isoformat()},
        "confirmable": True,
    }
    [(lock, lock_args), (record, record_args), (head, head_args), (insert, args)] = conn.queries
    # The lead is read and locked first: RLS decides whether the caller sees
    # it, its client is the one decided on, and its tenant names the key.
    assert _flat(lock) == "SELECT tenant_id,parcel_id,state,seller_client_id FROM leads WHERE id=$1::uuid FOR UPDATE"
    assert lock_args == (str(LEAD),)
    assert record_args == (str(RECORD),)
    assert "NOT EXISTS (SELECT 1 FROM property_match_decisions n WHERE n.supersedes_id=h.id)" in _flat(head)
    assert head_args == (str(LEAD), str(RECORD))
    # The client comes from the lead, never from the request.
    assert args[:8] == (uuid.UUID(TENANT_ID), str(LEAD), str(RECORD), CLIENT, "confirmed", None,
                        ["parcel_number_checked", "deed_or_title"], REFRESHED_AT)
    assert "decided_by" not in _flat(insert) and "decided_role" not in _flat(insert)


def test_a_platform_admin_passes_the_gate(monkeypatch):
    result, _conn, _ledger = _decide(monkeypatch, ADMIN)

    assert result["decision"] == "confirmed"
    # The identity is what Houses would show this caller: no form (below).
    assert result["identity"]["confirmable"] is False


@pytest.mark.parametrize(("ctx", "offered"), [(OWNER, True), (AGENT, False), (ADMIN, False)])
def test_houses_offers_the_decision_form_to_a_broker_owner_only(ctx, offered):
    """A platform admin's RLS spans every tenant, so the lead Houses puts on a
    card is the newest one for that parcel in any brokerage, not one the
    operator chose: Houses never offers them the form. They decide by an
    explicit lead id, which the route still allows (above)."""
    assert property_match_api.can_decide(ctx) is offered


@pytest.mark.parametrize("reason", ["", "        ", "7 chars"])
def test_a_short_reason_is_a_422_with_nothing_written(monkeypatch, reason):
    error, conn, ledger = _decide(monkeypatch, OWNER, body=_body(reason=reason))

    assert isinstance(error, HTTPException) and error.status_code == 422
    assert conn.queries == [] and ledger.calls == []


# ── refusals that write nothing ────────────────────────────────────────────

def test_a_house_the_caller_cannot_see_is_a_404(monkeypatch):
    error, conn, ledger = _decide(monkeypatch, OWNER, conn=_Conn(lead=None))

    assert isinstance(error, HTTPException) and error.status_code == 404
    assert len(conn.queries) == 1 and ledger.calls == []


def test_a_house_with_no_linked_client_is_a_409(monkeypatch):
    error, conn, ledger = _decide(monkeypatch, OWNER, conn=_Conn(lead=_lead(seller_client_id=None)))

    assert isinstance(error, HTTPException) and error.status_code == 409
    assert "Link this house to a client" in error.detail
    assert _inserts(conn) == [] and ledger.calls == []


def test_an_unknown_record_is_a_404(monkeypatch):
    error, conn, ledger = _decide(monkeypatch, OWNER, conn=_Conn(record=None))

    assert isinstance(error, HTTPException) and error.status_code == 404
    assert _inserts(conn) == [] and ledger.calls == []


@pytest.mark.parametrize("record", [_record(parcel_id="DE-NCC-0143"), _record(state="MD")])
def test_a_record_on_another_parcel_is_a_422(monkeypatch, record):
    error, conn, ledger = _decide(monkeypatch, OWNER, conn=_Conn(record=record))

    assert isinstance(error, HTTPException) and error.status_code == 422
    assert "this house's parcel" in error.detail
    assert _inserts(conn) == [] and ledger.calls == []


# ── transitions ────────────────────────────────────────────────────────────

def test_a_decision_on_a_stale_head_is_a_409(monkeypatch):
    """The person decided against the head they saw; someone else has since
    decided, so theirs would silently overwrite a decision they never read."""
    error, conn, ledger = _decide(monkeypatch, OWNER, conn=_Conn(head=_head("rejected")))

    assert isinstance(error, HTTPException) and error.status_code == 409
    assert "someone else" in error.detail and "reload" in error.detail.lower()
    assert _inserts(conn) == [] and ledger.calls == []


def test_a_head_the_person_did_not_see_is_a_409_even_when_naming_one(monkeypatch):
    error, conn, _ledger = _decide(monkeypatch, OWNER, conn=_Conn(head=None),
                                   body=_body(supersedes_id=str(HEAD)))

    assert isinstance(error, HTTPException) and error.status_code == 409
    assert _inserts(conn) == []


def test_a_concurrent_decision_on_the_same_head_is_a_409(monkeypatch):
    """Two decisions that read the same head both pass the check; the unique
    index on supersedes_id lets one land and refuses the other (23505)."""
    error, conn, ledger = _decide(monkeypatch, OWNER, conn=_Conn(unique_violation=True))

    assert isinstance(error, HTTPException) and error.status_code == 409
    assert len(_inserts(conn)) == 1 and ledger.calls == []


def test_withdrawing_with_nothing_decided_is_a_409(monkeypatch):
    error, conn, _ledger = _decide(monkeypatch, OWNER, body=_body(decision="withdrawn", evidence=[]))

    assert isinstance(error, HTTPException) and error.status_code == 409
    assert _inserts(conn) == []


def test_withdrawing_a_withdrawn_head_is_a_409(monkeypatch):
    error, conn, _ledger = _decide(monkeypatch, OWNER, conn=_Conn(head=_head("withdrawn")),
                                   body=_body(decision="withdrawn", evidence=[], supersedes_id=str(HEAD)))

    assert isinstance(error, HTTPException) and error.status_code == 409
    assert _inserts(conn) == []


def test_re_confirming_a_current_confirmation_is_a_409(monkeypatch):
    error, conn, _ledger = _decide(monkeypatch, OWNER, conn=_Conn(head=_head("confirmed")),
                                   body=_body(supersedes_id=str(HEAD)))

    assert isinstance(error, HTTPException) and error.status_code == 409
    assert "already confirmed" in error.detail
    assert _inserts(conn) == []


def test_an_out_of_date_confirmation_may_be_confirmed_again_for_the_current_client(monkeypatch):
    """The head confirmed this record for the house's previous client; the
    house is now another client's, so this is a new decision, not a repeat."""
    result, conn, ledger = _decide(monkeypatch, OWNER, conn=_Conn(head=_head("confirmed", OTHER_CLIENT)),
                                   body=_body(supersedes_id=str(HEAD)))

    assert result["identity"]["confirmation"]["state"] == "confirmed"
    [(_insert, args)] = _inserts(conn)
    assert args[3] == CLIENT and args[5] == str(HEAD)
    assert ledger.calls[0]["metadata"]["supersedes_id"] == str(HEAD)


def test_a_broker_owner_withdraws_a_decision_back_to_undecided(monkeypatch):
    result, conn, ledger = _decide(monkeypatch, OWNER, conn=_Conn(head=_head("rejected")),
                                   body=_body(decision="withdrawn", evidence=[], supersedes_id=str(HEAD)))

    assert result["identity"]["verified"] is False
    assert result["identity"]["confirmation"]["state"] == "none"
    assert ledger.calls[0]["action"] == "property_match_withdrawn"
    [(_insert, args)] = _inserts(conn)
    assert args[4:6] == ("withdrawn", str(HEAD))


def test_a_rejection_is_recorded_and_never_verifies(monkeypatch):
    result, _conn, ledger = _decide(monkeypatch, OWNER, body=_body(decision="rejected", evidence=[]))

    assert result["identity"]["verified"] is False
    assert result["identity"]["confirmation"]["state"] == "rejected"
    assert ledger.calls[0]["action"] == "property_match_rejected"


def test_confirming_without_evidence_is_a_422(monkeypatch):
    error, conn, ledger = _decide(monkeypatch, OWNER, body=_body(evidence=[]))

    assert isinstance(error, HTTPException) and error.status_code == 422
    assert conn.queries == [] and ledger.calls == []


def test_the_client_is_never_taken_from_the_request():
    with pytest.raises(ValidationError):
        _body(client_id=str(OTHER_CLIENT))


@pytest.mark.parametrize("decision", ["verified", "approved", "dismissed", ""])
def test_only_confirmed_rejected_or_withdrawn_is_a_decision(decision):
    with pytest.raises(ValidationError):
        _body(decision=decision)


@pytest.mark.parametrize("evidence", [["gut_feeling"], ["tax_bill", "tax_bill"]])
def test_evidence_is_a_known_kind_named_once(evidence):
    with pytest.raises(ValidationError):
        _body(evidence=evidence)


# ── sealing and audit ──────────────────────────────────────────────────────

def test_the_reason_is_written_only_sealed_under_the_leads_tenant_key(monkeypatch):
    """CONTRACT C5.1: the reason may name an owner, so 0168 has no plaintext
    column for it and the INSERT seals it."""
    _result, conn, _ledger = _decide(monkeypatch, OWNER)

    [(insert, args)] = _inserts(conn)
    assert "pgp_sym_encrypt($9::text,$10::text,'s2k-mode=1')" in _flat(insert)
    assert args[8] == REASON
    assert args[9] == sealed_text.key_for(TENANT_ID)


def test_a_platform_admins_decision_is_sealed_and_audited_in_the_leads_tenant(monkeypatch):
    """RLS lets a platform admin decide another tenant's house. That tenant's
    trail records it and its key seals the reason: never the admin's context."""
    conn = _Conn(lead=_lead(tenant_id=OTHER_TENANT))
    _result, conn, ledger = _decide(monkeypatch, ADMIN, conn=conn)

    [(_insert, args)] = _inserts(conn)
    assert args[0] == OTHER_TENANT
    assert args[9] == sealed_text.key_for(OTHER_TENANT) != sealed_text.key_for(TENANT_ID)
    [call] = ledger.calls
    assert call["tenant_id"] == str(OTHER_TENANT)


def test_without_the_master_key_nothing_is_decided(monkeypatch):
    error, conn, ledger = _decide(monkeypatch, OWNER, master="")

    assert isinstance(error, sealed_text.SealError)
    assert _inserts(conn) == [] and ledger.calls == []


def test_the_audit_record_carries_decision_ids_and_the_decision_only(monkeypatch, caplog):
    """The tenant's ledger is readable by every agent of the tenant (0005's
    audit_ledger_select; the portfolio summary's activity pulse returns its
    metadata), while 0168 hides a decision from an agent who cannot see the
    house. So the entry names the decision and nothing that locates it: no
    lead, no client, and no public record, whose id resolves to a street
    address in the global catalog. The table row keeps those under RLS."""
    with caplog.at_level(logging.DEBUG):
        result, _conn, ledger = _decide(monkeypatch, OWNER)

    [call] = ledger.calls
    assert call["category"] is AuditCategory.USER_STATE_CHANGE
    assert call["action"] == "property_match_confirmed"
    assert call["tenant_id"] == TENANT_ID
    assert call["user_id"] == OWNER.agent_id
    assert call["target_id"] == str(DECISION)
    assert call["metadata"] == {"decision_id": str(DECISION), "decision": "confirmed", "supersedes_id": None}
    for located in (str(LEAD), str(RECORD), str(CLIENT)):
        assert located not in json.dumps(call, default=str)
    # Nothing a person typed, and no owner name, reaches the ledger, a log
    # line or the response.
    for blob in (json.dumps(call, default=str), caplog.text, json.dumps(result, default=str)):
        for text in (REASON, "Jane", "Homeowner", "88 Oak"):
            assert text not in blob
    # Nor the evidence kinds or the actor in the response: ids, state, time.
    assert "evidence" not in result and "decided_by" not in json.dumps(result)


def test_no_statement_carries_a_tenant_predicate(monkeypatch):
    """Tenancy is RLS's job, never a duplicated predicate here."""
    for conn in (_Conn(), _Conn(head=_head("rejected"))):
        _decide(monkeypatch, OWNER, conn=conn,
                body=_body(supersedes_id=str(HEAD)) if conn.head else None)
        for query, _args in conn.queries:
            assert not re.search(r"tenant_id\s*=", query), query


def test_the_brokerage_export_opens_the_sealed_reason():
    """bytea is never exported, so a sealed column the export cannot open
    would silently vanish from the brokerage's own export."""
    import privacy_export

    assert privacy_export.DECRYPT["property_match_decisions"] == {"reason_ciphertext": "sealed_reason"}


def test_0168_keeps_the_reason_sealed_and_no_address_fingerprint():
    sql = (Path(__file__).resolve().parents[1] / "db" / "migrations"
           / "0168_property_match_decisions.sql").read_text()
    body = "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--")).lower()
    assert re.search(r"reason_ciphertext\s+bytea\s+not null", body)
    assert not re.search(r"\breason\s+(text|varchar|character)", body)
    # Drift under the same record id is an owner decision (a keyed HMAC if
    # ever); an unkeyed hash of a home address is effectively plaintext.
    for fingerprint in (r"\baddress\b", r"\bowner_name\b", r"\bzip(_code)?\b", r"\bcity\b",
                        r"\bsha256\b", r"\bdigest\s*\(", r"\bmd5\s*\(", r"\bhmac\s*\("):
        assert not re.search(fingerprint, body), fingerprint
