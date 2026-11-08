"""client_contact_index values (0156, audit A01, L17-23): the same keyed
hashes and name tokens agent_contacts uses (contact_truth), computed in the
app, never containing the plaintext."""

from __future__ import annotations

import pytest

import client_contact_index as cci
import contact_truth

T1 = "11111111-1111-1111-1111-111111111111"
T2 = "22222222-2222-2222-2222-222222222222"


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("ORACLE_ENCRYPTION_MASTER_KEY", "ab" * 32)


def test_values_match_the_agent_contacts_scheme():
    v = cci.index_values(T1, "Sarah  Johnson", " Sarah.J@Example.COM ", "(302) 555-0101")
    assert v["email_hash"] == contact_truth.lookup_hash(T1, "email", "sarah.j@example.com")
    assert v["phone_hash"] == contact_truth.lookup_hash(T1, "phone", "+13025550101")
    assert v["tokens"] == contact_truth.name_search_tokens(T1, "Sarah Johnson")
    assert not v["email_unusable"] and not v["phone_unusable"]
    # A word-prefix query for "sar joh" is answerable from the tokens.
    assert set(contact_truth.name_query_tokens(T1, "sar joh")) <= set(v["tokens"])


def test_no_plaintext_and_tenant_separated():
    v1 = cci.index_values(T1, "Sarah Johnson", "sarah@example.com", "+13025550101")
    v2 = cci.index_values(T2, "Sarah Johnson", "sarah@example.com", "+13025550101")
    blob = repr(v1).lower()
    for plain in ("sarah", "johnson", "example", "5550101"):
        assert plain not in blob
    assert v1["email_hash"] != v2["email_hash"] and v1["phone_hash"] != v2["phone_hash"]
    assert not set(v1["tokens"]) & set(v2["tokens"])


def test_values_that_cannot_be_normalised_are_flagged_not_guessed():
    v = cci.index_values(T1, "", "not-an-email", "555-0101")
    assert v == {"email_hash": None, "phone_hash": None, "tokens": [],
                 "email_unusable": True, "phone_unusable": True}
    empty = cci.index_values(T1, None, None, "  ")
    assert empty == {"email_hash": None, "phone_hash": None, "tokens": [],
                     "email_unusable": False, "phone_unusable": False}


def test_no_key_fails_closed(monkeypatch):
    monkeypatch.delenv("ORACLE_ENCRYPTION_MASTER_KEY")
    with pytest.raises(contact_truth.ContactTruthConfigError):
        cci.index_values(T1, "Sarah", "sarah@example.com", None)


def test_the_periodic_task_skips_without_a_key(monkeypatch):
    import asyncio

    from data_integrations import periodic

    monkeypatch.delenv("ORACLE_ENCRYPTION_MASTER_KEY")
    assert asyncio.run(periodic._client_contact_index_task()) == {
        "skipped": "ORACLE_ENCRYPTION_MASTER_KEY is not set"}


def test_statements_are_literals_that_name_no_plaintext_output():
    """The filler reads the plaintext (it must hash it) but writes only the
    index columns; its output never prints a value."""
    import inspect

    src = inspect.getsource(cci)
    assert "full_name" not in cci._UPSERT and "email," not in cci._UPSERT.split("SELECT")[0]
    for line in src.splitlines():
        if "print(" in line:
            assert "full_name" not in line and "email\"]" not in line and "phone\"]" not in line, line
