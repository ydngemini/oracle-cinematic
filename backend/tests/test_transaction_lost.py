"""A deal that dies is a fact, not a cancelled row.

'cancelled' existed and carried nothing — no timestamp, no reason, no value —
so a lost deal was indistinguishable from an abandoned draft and Outcome
Memory had no negative signal to learn from. These tests pin the rules that
make 'lost' mean something.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

import portfolio_api as pa


def test_only_a_live_deal_can_be_lost():
    """A closed deal is not lost; a cancelled one was never a deal.

    Letting 'closed' → 'lost' through would let a won deal be re-filed as a
    loss after the fact, which is precisely the kind of history rewrite the
    outcome journal exists to refuse.
    """
    assert pa.lose_transition_allowed("active")
    assert pa.lose_transition_allowed("under_contract")
    assert not pa.lose_transition_allowed("closed")
    assert not pa.lose_transition_allowed("cancelled")
    assert not pa.lose_transition_allowed("lost")
    assert not pa.lose_transition_allowed(None)


def test_reason_code_vocabulary_matches_the_database_check():
    """The Literal on the request model and the CHECK in 0098 must agree, or
    a code the API accepts is one the database rejects — a 500 in place of a
    422, with the reason lost in the traceback."""
    from pathlib import Path

    sql = (Path(__file__).resolve().parents[1] / "db" / "migrations"
           / "0098_outcome_memory.sql").read_text()
    for code in pa.LOST_REASON_CODES:
        assert f"'{code}'" in sql, f"{code} accepted by the API but absent from the CHECK"
    # And nothing in the CHECK that the API cannot send. Split on the
    # definition, not the name — the name also appears in the IF NOT EXISTS
    # guard that precedes it.
    check = sql.split("ADD CONSTRAINT transactions_lost_reason_chk")[1].split("NOT VALID")[0]
    for token in ("price", "financing", "inspection", "competing_offer",
                  "client_withdrew", "listing_expired", "other"):
        assert token in check


def test_request_requires_a_coded_reason_and_refuses_free_text_alone():
    """Free text is not aggregatable. The code is what the evaluator counts;
    the text is the agent's own words alongside it, never instead of it."""
    with pytest.raises(ValidationError):
        pa.TransactionLose(version=1, reason="they went with someone else")
    with pytest.raises(ValidationError):
        pa.TransactionLose(version=1, reason_code="vibes")
    body = pa.TransactionLose(version=1, reason_code="competing_offer",
                              reason="  Went with a cash buyer.  ")
    assert body.reason == "Went with a cash buyer."


def test_request_forbids_unknown_fields():
    """extra='forbid', the house style: a client that sends lost_at itself must
    be told, not silently ignored — that timestamp is the server's to set."""
    with pytest.raises(ValidationError):
        pa.TransactionLose(version=1, reason_code="price", lost_at="2026-09-01")


def test_close_records_the_outcome_and_the_usage_metric():
    """Both receipts sit inside close_transaction, after the cascade, on the
    caller's connection. Static check: the calls exist and name the metric that
    0067 allowed and nothing ever emitted."""
    import inspect

    source = inspect.getsource(pa.close_transaction)
    assert 'outcome_kind="transaction_closed"' in source
    assert 'metric="transaction_closed"' in source
    assert "conn=conn" in source, "receipts must ride the caller's transaction via SAVEPOINT"


def test_lose_records_a_negative_outcome_with_its_reason():
    import inspect

    source = inspect.getsource(pa.lose_transaction)
    assert 'outcome_kind="transaction_lost"' in source
    assert '"reason_code": body.reason_code' in source
    # The docstring names the helper to explain why it is NOT called; the
    # assertion has to look for the call, not the name.
    assert "await _move_related_stages(" not in source, \
        "lose must not cascade stages — that is a separate decision about two other tables"
