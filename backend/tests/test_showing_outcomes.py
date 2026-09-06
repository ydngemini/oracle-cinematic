"""A showing could be logged but never resolved.

`create_showing` inserted a row with whatever outcome the agent knew at the
time — almost always 'pending', because it is logged before the buyer has
reacted — and nothing in the codebase could ever change it. Every showing in
the deployment would have read 'pending' forever, and the one exposure record
the whole pipeline reads would have taught Outcome Memory nothing.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

import crm


def test_outcome_map_covers_every_resolved_state_and_nothing_else():
    """'pending' is the absence of a result, not a result. It must map to
    nothing, and every other database-accepted outcome must map to something."""
    resolved = set(crm.SHOWING_OUTCOMES) - {"pending"}
    assert set(crm.SHOWING_OUTCOME_KINDS) == resolved
    assert "pending" not in crm.SHOWING_OUTCOME_KINDS


def test_an_offer_is_also_a_showing_that_was_held():
    """"They came" and "they bid" are different denominators. Recording only
    offer_made would make the showing→offer rate undefined for exactly the
    showings that produced offers."""
    assert crm.SHOWING_OUTCOME_KINDS["offer_made"] == ("showing_held", "offer_made")
    assert crm.SHOWING_OUTCOME_KINDS["passed"] == ("showing_held",)


def test_a_no_show_is_negative_and_never_held():
    assert crm.SHOWING_OUTCOME_KINDS["no_show"] == ("no_show",)


def test_every_mapped_kind_is_one_outcome_memory_accepts():
    import outcome_memory

    for kinds in crm.SHOWING_OUTCOME_KINDS.values():
        for kind in kinds:
            assert kind in outcome_memory.OUTCOME_KINDS, kind


def test_showing_outcomes_mirror_the_database_check():
    """The tuple, the request Literal, and chk_showing_outcome (0012) must
    agree or the API accepts what the database rejects."""
    from pathlib import Path

    sql = (Path(__file__).resolve().parents[1] / "db" / "migrations"
           / "0012_agent_crm.sql").read_text()
    check = sql.split("chk_showing_outcome CHECK (outcome IN")[1].split("))")[0]
    for outcome in crm.SHOWING_OUTCOMES:
        assert f"'{outcome}'" in check, outcome


def test_update_refuses_unknown_outcomes_and_unknown_fields():
    with pytest.raises(ValidationError):
        crm.ShowingUpdate(outcome="loved_it")
    with pytest.raises(ValidationError):
        crm.ShowingUpdate(outcome="interested", client_id="x")
    body = crm.ShowingUpdate(outcome="interested", feedback="  Liked the kitchen.  ")
    assert body.feedback == "Liked the kitchen."


def test_update_path_exists_and_records_outcomes():
    """The whole point of the commit: there is now an UPDATE on showings, and
    it hands the resolved row to Outcome Memory."""
    import inspect

    source = inspect.getsource(crm.update_showing)
    assert "UPDATE showings" in source
    assert "_record_showing_outcomes" in source
    # COALESCE keeps feedback and shown_at when the caller omits them — a
    # resolve must not blank what the agent wrote when they logged it.
    assert "COALESCE($3, feedback)" in source
