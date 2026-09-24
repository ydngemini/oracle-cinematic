"""Buyer matching — evidence first, and unknown is never "no".

The two rules these exist to hold:

  §24  No unexplained percentage. A match is reasons a person can check
       against the record. A number nobody can verify is abandoned the first
       time it is wrong, and takes every later number with it.

  §25  Missing preference data must not read as "not interested". A buyer
       whose budget nobody wrote down is not a buyer who cannot afford the
       house, and dropping them silently is how a brokerage never calls the
       person who would have bought it.
"""

from __future__ import annotations

import asyncio

import pytest

from buyer_matching import (
    INSUFFICIENT, MISMATCH, POSSIBLE, STRONG,
    buyers_for_listing, extract_preferences, match_listing_to_buyer, rank_matches,
)

LISTING = {
    "city": "Wilmington", "zip_code": "19801", "list_price": 485000,
    "beds": 4, "property_type": "Single Family",
}


def buyer(name="Buyer", **prefs):
    return {"id": name.lower(), "full_name": name, "preferences": prefs}


# ---------------------------------------------------------------------------
# Reading real, messy preference data
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("key", ["target_zips", "zips", "zip_codes", "target_zip_codes"])
def test_every_live_spelling_of_target_zips_is_read(key):
    """Live data carries both `target_zips` and `zips`. Reading one spelling
    silently halves the candidate pool."""
    assert extract_preferences({key: ["19801"]}).zips == ["19801"]


@pytest.mark.parametrize("key", ["budget_max", "budget", "max_price", "price_max"])
def test_every_live_spelling_of_budget_is_read(key):
    assert extract_preferences({key: 525000}).budget_max == 525000


def test_money_written_as_a_string_is_understood():
    assert extract_preferences({"budget": "$525,000"}).budget_max == 525000


def test_a_comma_separated_string_of_zips_is_understood():
    assert extract_preferences({"zips": "19801, 19802"}).zips == ["19801", "19802"]


def test_garbage_preferences_do_not_explode():
    for junk in (None, "", "not json", [], 42, {"budget": "abc"}):
        extract_preferences(junk)


def test_zips_are_truncated_to_five_digits():
    assert extract_preferences({"zips": ["19801-1234"]}).zips == ["19801"]


# ---------------------------------------------------------------------------
# Verdicts
# ---------------------------------------------------------------------------

def test_two_agreeing_signals_is_a_strong_match():
    m = match_listing_to_buyer(LISTING, buyer("Sarah", target_zips=["19801"], budget_max=525000, beds=3))
    assert m.verdict == STRONG
    assert "looking in 19801" in m.evidence
    assert any("525,000" in e for e in m.evidence)


def test_one_signal_is_possible_not_strong():
    """The spec's own example: 'Marcus Lee — location match, budget unknown'."""
    m = match_listing_to_buyer(LISTING, buyer("Marcus", cities=["Wilmington"]))
    assert m.verdict == POSSIBLE
    assert m.matched_signals == 1
    assert any("no budget recorded" in u for u in m.unknowns)


def test_a_buyer_with_no_recorded_preferences_is_insufficient_data_not_a_mismatch():
    m = match_listing_to_buyer(LISTING, buyer("Ursula"))
    assert m.verdict == INSUFFICIENT
    assert m.conflicts == []


def test_only_a_real_conflict_produces_a_mismatch():
    m = match_listing_to_buyer(LISTING, buyer("Dana", zips=["19901"], budget=300000))
    assert m.verdict == MISMATCH
    assert len(m.conflicts) == 2


def test_a_near_miss_on_budget_is_surfaced_not_rejected():
    """Agents routinely stretch a buyer a few percent; silently excluding them
    hides a real opportunity."""
    m = match_listing_to_buyer(LISTING, buyer("Steve", target_zips=["19801"], budget_max=450000))
    assert m.verdict == STRONG
    assert any("over their" in e for e in m.evidence)


def test_a_budget_far_below_is_a_genuine_conflict():
    m = match_listing_to_buyer(LISTING, buyer("Low", target_zips=["19801"], budget_max=200000))
    assert m.verdict == MISMATCH


def test_bedrooms_below_requirement_conflict():
    small = {**LISTING, "beds": 2}
    m = match_listing_to_buyer(small, buyer("Bea", target_zips=["19801"], beds=4))
    assert m.verdict == MISMATCH


def test_a_listing_missing_a_field_is_unknown_not_a_conflict():
    """The listing's gaps must not be held against the buyer either."""
    priceless = {**LISTING, "list_price": None}
    m = match_listing_to_buyer(priceless, buyer("Pat", target_zips=["19801"], budget_max=100))
    assert m.verdict != MISMATCH
    assert any("no price" in u for u in m.unknowns)


def test_every_match_carries_readable_evidence_and_no_score():
    m = match_listing_to_buyer(LISTING, buyer("Sarah", target_zips=["19801"], budget_max=525000))
    assert m.evidence and all(isinstance(e, str) and e for e in m.evidence)
    assert not hasattr(m, "percent")
    assert not hasattr(m, "probability")


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------

def test_ranking_puts_strong_first_and_keeps_unknowns():
    people = [buyer("Ursula"), buyer("Dana", zips=["19901"], budget=300000),
              buyer("Marcus", cities=["Wilmington"]),
              buyer("Sarah", target_zips=["19801"], budget_max=525000, beds=3)]
    ranked = rank_matches([match_listing_to_buyer(LISTING, p) for p in people])
    assert [m.name for m in ranked] == ["Sarah", "Marcus", "Ursula"]


def test_mismatches_are_excluded_by_default_but_available():
    people = [buyer("Dana", zips=["19901"], budget=300000)]
    matches = [match_listing_to_buyer(LISTING, p) for p in people]
    assert rank_matches(matches) == []
    assert len(rank_matches(matches, include_mismatches=True)) == 1


# ---------------------------------------------------------------------------
# The query
# ---------------------------------------------------------------------------

class Conn:
    def __init__(self, rows):
        self.rows = rows
        self.queries = []

    async def fetch(self, query, *args):
        self.queries.append(query)
        return self.rows


def test_the_query_leans_on_rls_rather_than_a_hand_written_tenant_predicate():
    """0109's lesson: reproducing half a policy by hand is how rows go missing
    for a platform admin. clients is FORCE RLS'd, so tenant_tx scopes it."""
    conn = Conn([])
    asyncio.run(buyers_for_listing(conn, LISTING))
    q = conn.queries[0]
    assert "FROM clients" in q
    assert "tenant_id" not in q
    assert "archived_at IS NULL" in q
    assert "LIMIT" in q          # never an unbounded scan


def test_sellers_are_not_offered_as_buyers():
    conn = Conn([])
    asyncio.run(buyers_for_listing(conn, LISTING))
    assert "client_type" in conn.queries[0]


def test_results_are_capped():
    rows = [{"id": str(i), "full_name": f"B{i}",
             "preferences": {"target_zips": ["19801"], "budget_max": 525000}}
            for i in range(60)]
    out = asyncio.run(buyers_for_listing(Conn(rows), LISTING, limit=25))
    assert len(out) == 25
