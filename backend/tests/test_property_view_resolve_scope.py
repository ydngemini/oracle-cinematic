"""Property View address resolve + subject creation stay inside the acting tenant.

Found by the Mission 3 browser journey (scripts/test-mission3-journeys-playwright.py):
every address lookup sat on "Locating…" for 30 s and the resolve call ended in a
500. Under the `app_is_platform_admin() OR tenant_id = app_current_tenant()`
policy the queries had no narrowing predicate, so `address ILIKE '%…%' ORDER BY
created_at DESC LIMIT 10` walked every brokerage's leads (millions on the dev
database) until command_timeout. Media attaches to the record of the tenant
being worked in, so the explicit tenant scope is the intended semantics, not a
duplicate of the RLS predicate.

The resolve is a substring search, so "12 Main St" also finds "112 Main St".
Each candidate says how it matched (T11.03, F11.04, AC11.04): only a record
whose address has the same words and names the same state is an
exact_address, everything else is a possible_match, and none is verified.
"""
from __future__ import annotations

import asyncio
import hashlib
import uuid
from contextlib import asynccontextmanager

import pytest
from fastapi import HTTPException

import property_view_api
from tenancy import Role, TenantContext


class _Conn:
    """`leads`/`listings` are what the resolve's ILIKE returns; `by_parcel`
    maps a synthetic parcel key to the visible lead holding it."""

    def __init__(self, leads=(), listings=(), by_parcel=None, insert_ok=True):
        self.calls: list[tuple[str, tuple]] = []
        self.leads = list(leads)
        self.listings = list(listings)
        self.by_parcel = dict(by_parcel or {})
        self.insert_ok = insert_ok

    async def fetch(self, query, *args):
        self.calls.append((" ".join(query.split()), args))
        if "FROM leads" in query:
            return self.leads
        if "FROM listings" in query:
            return self.listings
        return []

    async def fetchrow(self, query, *args):
        self.calls.append((" ".join(query.split()), args))
        return self.by_parcel.get(args[0])

    async def fetchval(self, query, *args):
        self.calls.append((" ".join(query.split()), args))
        if query.lstrip().startswith("SELECT"):
            return None
        return uuid.uuid4() if self.insert_ok else None


def _install(monkeypatch, conn=None):
    conn = conn or _Conn()

    @asynccontextmanager
    async def tx(_ctx):
        yield conn

    monkeypatch.setattr(property_view_api, "tenant_tx", tx)
    return conn


def _ctx() -> TenantContext:
    return TenantContext(agent_id="owner@example.test", tenant_id=str(uuid.uuid4()), role=Role.BROKER_OWNER)


def _legacy_key(address: str) -> str:
    """The key create_subject wrote before the state was part of it."""
    return f"pv:{hashlib.sha256(address.lower().encode()).hexdigest()[:32]}"


def _lead(address, state="DE", motivation_score=0):
    return {"id": uuid.uuid4(), "address": address, "state": state, "motivation_score": motivation_score}


def _listing(address):
    return {"id": uuid.uuid4(), "address": address, "price": None, "status": "active"}


def _resolve(monkeypatch, typed, *, leads=(), listings=()):
    _install(monkeypatch, _Conn(leads=leads, listings=listings))
    return asyncio.run(property_view_api.resolve_subject(address=typed, ctx=_ctx()))


def test_resolve_scopes_leads_and_listings_to_the_acting_tenant(monkeypatch):
    conn = _install(monkeypatch)
    ctx = _ctx()
    out = asyncio.run(property_view_api.resolve_subject(address="100 W 10th St", ctx=ctx))
    assert out == {"leads": [], "listings": []}
    tables = {"leads": False, "listings": False}
    for query, args in conn.calls:
        for table in tables:
            if f"FROM {table}" in query:
                assert "tenant_id = $2::uuid AND address ILIKE $1" in query, query
                assert args == ("%100 W 10th St%", ctx.tenant_id)
                tables[table] = True
    assert all(tables.values()), tables


# --- resolve: how each candidate matched (F11.04) ----------------------------

POSSIBLE_CONTAINS = {"status": "possible_match", "basis": "address_contains", "verified": False}
EXACT = {"status": "exact_address", "basis": "address_exact", "verified": False}


def test_a_substring_hit_is_a_possible_match_not_the_typed_address(monkeypatch):
    # "12 Main St" is inside "112 Main St": the ILIKE finds the other house.
    out = _resolve(monkeypatch, "12 Main St, Dover, DE 19901",
                   leads=[_lead("112 Main St, Dover, DE 19901")],
                   listings=[_listing("112 Main St, Dover, DE 19901")])
    assert out["leads"][0]["identity"] == POSSIBLE_CONTAINS
    assert out["listings"][0]["identity"] == POSSIBLE_CONTAINS


def test_the_same_words_in_the_named_state_are_an_exact_address_never_verified(monkeypatch):
    # Case, commas and spacing are not words; the record is the typed address.
    out = _resolve(monkeypatch, "1 Main St, Dover, DE 19901",
                   leads=[_lead("1 MAIN ST,  Dover, DE 19901", state="DE")],
                   listings=[_listing("1 Main St, Dover, DE 19901")])
    assert out["leads"][0]["identity"] == EXACT
    assert out["listings"][0]["identity"] == EXACT


def test_the_same_street_text_in_another_state_is_only_a_possible_match(monkeypatch):
    # The lead's own state column contradicts the state the address names.
    out = _resolve(monkeypatch, "1 Main St, Dover, DE", leads=[_lead("1 Main St, Dover, DE", state="PA")])
    assert out["leads"][0]["identity"] == {
        "status": "possible_match", "basis": "address_exact", "verified": False}


def test_an_address_that_names_no_state_is_never_an_exact_address(monkeypatch):
    # "100 Main St" exists in most towns; nothing typed says which one this is.
    out = _resolve(monkeypatch, "100 Main St",
                   leads=[_lead("100 Main St", state="DE")], listings=[_listing("100 Main St")])
    assert out["leads"][0]["identity"]["status"] == "possible_match"
    assert out["listings"][0]["identity"]["status"] == "possible_match"


def test_an_address_that_names_a_state_but_no_town_is_never_an_exact_address(monkeypatch):
    # The state prompt steers agents to "100 Main St, DE": a 100 Main St is in
    # Milford and in Dover, and the state column agrees for both.
    out = _resolve(monkeypatch, "100 Main St, DE",
                   leads=[_lead("100 Main St, DE", state="DE")], listings=[_listing("100 Main St, DE")])
    possible_same_words = {"status": "possible_match", "basis": "address_exact", "verified": False}
    assert out["leads"][0]["identity"] == possible_same_words
    assert out["listings"][0]["identity"] == possible_same_words


@pytest.mark.parametrize("typed", ["1 Main St, Dover, DE", "1 Main St, DE 19901"])
def test_a_city_or_a_zip_names_the_town(monkeypatch, typed):
    out = _resolve(monkeypatch, typed, leads=[_lead(typed, state="DE")], listings=[_listing(typed)])
    assert out["leads"][0]["identity"] == EXACT
    assert out["listings"][0]["identity"] == EXACT


def test_a_split_number_is_not_the_same_words(monkeypatch):
    # Compacted, "15 1/2 Main St" and "151 2 Main St" are one string; as words they differ.
    out = _resolve(monkeypatch, "151 2 Main St, Dover, DE", leads=[_lead("15 1/2 Main St, Dover, DE")])
    assert out["leads"][0]["identity"]["status"] == "possible_match"


# --- create_subject: the synthetic key and what a reuse claims --------------

def test_existing_subject_lookup_uses_the_tenant_parcel_key(monkeypatch):
    conn = _install(monkeypatch)
    ctx = _ctx()
    body = property_view_api.CreateSubject(address="100 W 10th St, Wilmington, DE", state="de")
    out = asyncio.run(property_view_api.create_subject(body=body, ctx=ctx))
    assert out["created"] is True
    lookup, args = conn.calls[0]
    assert lookup.startswith("SELECT id, state, address FROM leads WHERE tenant_id = $2::uuid AND parcel_id = $1")
    assert args[1] == ctx.tenant_id and args[0].startswith("pv:")


def _inserted_parcel(conn) -> str:
    inserts = [args for query, args in conn.calls if query.startswith("INSERT INTO leads")]
    assert len(inserts) == 1, conn.calls
    return inserts[0][1]


def test_the_same_street_in_another_state_gets_its_own_record(monkeypatch):
    # A Delaware lead holds the key for "100 Main St". Typing that street for a
    # Pennsylvania house returned the Delaware lead as "Matched an existing record".
    de_lead = _lead("100 Main St", state="DE")
    conn = _install(monkeypatch, _Conn(by_parcel={_legacy_key("100 Main St"): de_lead}))
    body = property_view_api.CreateSubject(address="100 Main St", state="PA")
    out = asyncio.run(property_view_api.create_subject(body=body, ctx=_ctx()))
    assert out["created"] is True
    assert out["lead_id"] != str(de_lead["id"])
    parcel = _inserted_parcel(conn)
    assert parcel.startswith("pv:") and parcel != _legacy_key("100 Main St")


def test_a_new_address_keeps_the_existing_key(monkeypatch):
    # Rows already written under the address-only key keep being found, and a
    # hidden one (a colleague's client's house, 0143) still conflicts.
    conn = _install(monkeypatch)
    body = property_view_api.CreateSubject(address="100 Main St, Dover, DE", state="DE")
    asyncio.run(property_view_api.create_subject(body=body, ctx=_ctx()))
    assert _inserted_parcel(conn) == _legacy_key("100 Main St, Dover, DE")


def test_a_hidden_record_for_the_address_is_still_a_conflict(monkeypatch):
    _install(monkeypatch, _Conn(insert_ok=False))
    body = property_view_api.CreateSubject(address="100 Main St, Dover, DE", state="DE")
    with pytest.raises(HTTPException) as e:
        asyncio.run(property_view_api.create_subject(body=body, ctx=_ctx()))
    assert e.value.status_code == 409


def test_reusing_a_record_says_how_it_matched(monkeypatch):
    typed = "100 Main St, Dover, DE 19901"
    lead = _lead(typed, state="DE")
    _install(monkeypatch, _Conn(by_parcel={_legacy_key(typed): lead}))
    out = asyncio.run(property_view_api.create_subject(
        body=property_view_api.CreateSubject(address=typed, state="DE"), ctx=_ctx()))
    assert out == {"lead_id": str(lead["id"]), "created": False, "address": typed, "identity": EXACT}


def test_reusing_a_record_for_an_address_that_names_no_state_is_a_possible_match(monkeypatch):
    # Same street text, same state column, but the typed address names no town
    # or state of its own: another "100 Main St" in Delaware has the same key.
    lead = _lead("100 Main St", state="DE")
    _install(monkeypatch, _Conn(by_parcel={_legacy_key("100 Main St"): lead}))
    out = asyncio.run(property_view_api.create_subject(
        body=property_view_api.CreateSubject(address="100 Main St", state="DE"), ctx=_ctx()))
    assert out["lead_id"] == str(lead["id"]) and out["created"] is False
    assert out["identity"] == {"status": "possible_match", "basis": "address_exact", "verified": False}


def test_reusing_a_record_for_an_address_that_names_no_town_is_a_possible_match(monkeypatch):
    # The Milford lead was created as "100 Main St, DE". The same text typed
    # for a Dover house hits its key; it is not "Matched an existing record".
    lead = _lead("100 Main St, DE", state="DE")
    _install(monkeypatch, _Conn(by_parcel={_legacy_key("100 Main St, DE"): lead}))
    out = asyncio.run(property_view_api.create_subject(
        body=property_view_api.CreateSubject(address="100 Main St, DE", state="DE"), ctx=_ctx()))
    assert out["lead_id"] == str(lead["id"]) and out["created"] is False
    assert out["identity"] == {"status": "possible_match", "basis": "address_exact", "verified": False}


def test_a_record_found_by_key_whose_address_changed_is_a_possible_match(monkeypatch):
    # The key was hashed from the address at creation; the row's address can
    # have been edited since, so the label reads the row, not the key.
    lead = _lead("102 Main St, Dover, DE", state="DE")
    _install(monkeypatch, _Conn(by_parcel={_legacy_key("100 Main St, Dover, DE"): lead}))
    out = asyncio.run(property_view_api.create_subject(
        body=property_view_api.CreateSubject(address="100 Main St, Dover, DE", state="DE"), ctx=_ctx()))
    assert out["identity"] == {"status": "possible_match", "basis": "subject_key", "verified": False}
