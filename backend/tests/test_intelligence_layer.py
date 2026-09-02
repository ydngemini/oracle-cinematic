"""The intelligence layer's honesty rules.

Every test here pins a property that, if it broke, would produce output that
looks right and is wrong — which is the only failure mode that matters for a
surface an agent acts on without checking.

The recurring theme is the distinction between *no evidence* and *negative
evidence*. A client nobody has watched and a client who has gone cold produce
the same zero under a naive scorer and need opposite responses. Several of
these tests exist only to keep that distinction alive through refactors.
"""

from datetime import datetime, timedelta, timezone

import pytest

import autonomy
import belief_store
import expected_value
import intent_states


NOW = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)


# ── belief decay ────────────────────────────────────────────────────────────

def test_confidence_decays_with_age():
    """The same claim is worth less six months later, without being rewritten."""
    fresh = belief_store.effective_confidence(
        0.9, "prefers_area", NOW - timedelta(days=1), now=NOW)
    stale = belief_store.effective_confidence(
        0.9, "prefers_area", NOW - timedelta(days=180), now=NOW)
    assert fresh > stale
    assert stale < 0.3, "a six-month-old area preference should not still read as strong"


def test_predicates_decay_at_different_rates():
    """A timeline rots faster than a financing type, because it does."""
    age = timedelta(days=90)
    timeline = belief_store.effective_confidence(0.9, "timeline", NOW - age, now=NOW)
    financing = belief_store.effective_confidence(0.9, "financing_type", NOW - age, now=NOW)
    assert financing > timeline * 2


def test_decayed_belief_never_reaches_zero():
    """Something once said is never evidence of nothing.

    It stops outranking fresh evidence; it does not become false. A floor of
    exactly zero would let a stale belief silently vanish from a weighted sum
    instead of being visibly outvoted.
    """
    ancient = belief_store.effective_confidence(
        0.9, "timeline", NOW - timedelta(days=3650), now=NOW)
    assert ancient == belief_store.CONFIDENCE_FLOOR
    assert ancient > 0


def test_pinned_belief_does_not_decay():
    """The agent corrected it. Re-asking every 90 days would undo the correction."""
    assert belief_store.effective_confidence(
        0.9, "timeline", NOW - timedelta(days=400), now=NOW, pinned=True) == 0.9


def test_expired_belief_drops_immediately_not_gradually():
    """A pre-approval that ran out is dead, not merely old.

    Expiry is a fact about the claim; decay is a guess about its freshness.
    Smoothing the first with the second would leave an expired letter reading
    as 70% true the day after it lapsed.
    """
    just_expired = belief_store.effective_confidence(
        0.95, "pre_approval", NOW - timedelta(days=1), now=NOW,
        valid_until=NOW - timedelta(hours=1))
    assert just_expired == belief_store.CONFIDENCE_FLOOR


def test_dispute_question_names_both_sides_and_their_sources():
    """The output must be answerable by a human in one phone call."""
    older = {"value": "Ashburn", "age_days": 92.0, "source": {"kind": "call"},
             "learned_at": "2026-06-02T00:00:00+00:00", "confidence": 0.5,
             "status": "reported"}
    newer = {"value": "Reston", "age_days": 3.0, "source": {"kind": "behaviour"},
             "learned_at": "2026-08-30T00:00:00+00:00", "confidence": 0.7,
             "status": "inference"}
    question = belief_store._dispute_question("prefers_area", [newer, older])
    assert "Ashburn" in question and "Reston" in question
    assert "call" in question and "behaviour" in question
    # A verdict would be a claim the system cannot support; a question is not.
    assert "?" not in question or "confirming" in question


# ── observed vs unobserved ──────────────────────────────────────────────────

def test_no_behaviour_is_unobserved_not_zero():
    """The single most important line in this file.

    A client with no captured behaviour must never render as low intent. One
    needs the portal instrumented; the other needs a nurture campaign.
    """
    reading = intent_states._observed_reading({}, None)
    assert reading.evidence_state == "unobserved"
    assert reading.score is None, "a missing signal must not be reported as a 0"
    assert "capture gap" in reading.basis


def test_two_clicks_is_not_a_pattern():
    """Below the evidence threshold we decline to score rather than guess."""
    reading = intent_states._observed_reading({"listing_view": 2}, NOW)
    assert reading.evidence_state == "weak"
    assert reading.score is None
    assert reading.signals == {"listing_view": 2}, "the raw counts still show"


def test_showing_request_outweighs_idle_browsing():
    """Asking to stand inside a house beats any amount of scrolling."""
    browsing = intent_states._observed_reading({"listing_view": 12}, NOW)
    serious = intent_states._observed_reading(
        {"listing_view": 4, "showing_request": 1, "calculator_use": 1}, NOW)
    assert serious.score > browsing.score


def test_unfavorite_is_counted_as_negative_evidence():
    """A scorer that only ever adds will call everyone hot by March."""
    plain = intent_states._observed_reading(
        {"listing_view": 6, "listing_favorite": 2}, NOW)
    withdrawn = intent_states._observed_reading(
        {"listing_view": 6, "listing_favorite": 2, "listing_unfavorite": 3}, NOW)
    assert withdrawn.score < plain.score


def test_state_distribution_is_empty_without_evidence():
    """A uniform prior dressed as a prediction is the worst possible output.

    It looks like knowledge and contains none, and it would appear on every
    client in the book identically.
    """
    unobserved = intent_states._observed_reading({}, None)
    declared = intent_states._declared_reading(None, None, None)
    assert intent_states._state_distribution({}, declared, unobserved) == []


def test_no_state_claims_near_certainty():
    """Nobody's position in their own head is 95% knowable from click data."""
    counts = {"showing_request": 40}
    observed = intent_states._observed_reading(counts, NOW)
    declared = intent_states._declared_reading(None, None, None)
    for entry in intent_states._state_distribution(counts, declared, observed):
        assert entry["probability"] <= 0.85


# ── declared vs observed reconciliation ─────────────────────────────────────

def test_behaviour_ahead_of_words_is_called_out():
    """The sellable insight: she says six months, she acts like thirty days."""
    declared = intent_states.IntentReading(0.3, "observed", "said six months")
    observed = intent_states.IntentReading(0.8, "observed", "lots of activity")
    result = intent_states._reconcile(declared, observed)
    assert result["verdict"] == "behaviour_ahead"
    assert result["latent_score"] > declared.score


def test_cannot_compare_when_nothing_is_observed():
    """With no behaviour there is no contradiction to report, and saying there
    is one would be inventing the product's headline feature."""
    declared = intent_states.IntentReading(0.6, "observed", "said soon")
    observed = intent_states.IntentReading(None, "unobserved", "nothing captured")
    result = intent_states._reconcile(declared, observed)
    assert result["verdict"] == "cannot_compare"
    assert result["confidence"] <= 0.3


def test_agreement_raises_confidence_above_conflict():
    """Two independent readings agreeing is itself evidence."""
    agree = intent_states._reconcile(
        intent_states.IntentReading(0.6, "observed", ""),
        intent_states.IntentReading(0.65, "observed", ""))
    clash = intent_states._reconcile(
        intent_states.IntentReading(0.2, "observed", ""),
        intent_states.IntentReading(0.8, "observed", ""))
    assert agree["confidence"] > clash["confidence"]


def test_unobserved_client_is_offered_the_instrumentation_lever():
    """The prescription for 'we cannot see them' is not 'call them again'."""
    observed = intent_states._observed_reading({}, None)
    levers = intent_states._levers({}, observed, None)
    assert [l["gap"] for l in levers] == ["unobserved"]


# ── expected value ──────────────────────────────────────────────────────────

def test_declines_to_value_without_any_basis():
    """No deal value and no market median means no number, not a guess.

    A hole in a ranked list beats a fabricated figure sorting above a real one.
    """
    assert expected_value.value_of(
        kind="contract_deadline", confidence=0.9,
        deal_value=None, market_median=None) is None


def test_low_confidence_is_discounted_faster_than_linearly():
    """Half the confidence must be worth less than half the value.

    Acting on a shaky inference also costs credibility with the client, which a
    linear discount does not capture.
    """
    certain = expected_value.value_of(
        kind="contract_deadline", confidence=0.98, deal_value=400_000)
    coin_flip = expected_value.value_of(
        kind="contract_deadline", confidence=0.5, deal_value=400_000)
    assert coin_flip.expected_value < certain.expected_value * 0.5


def test_cost_of_the_action_is_subtracted():
    """A two-hour review and a two-minute text are not the same ask."""
    quick = expected_value.value_of(
        kind="intent_next_action", confidence=0.7,
        deal_value=400_000, action_type="text")
    slow = expected_value.value_of(
        kind="intent_next_action", confidence=0.7,
        deal_value=400_000, action_type="meeting")
    assert quick.expected_value > slow.expected_value


def test_every_valuation_admits_it_is_uncalibrated():
    """No closed deals have been recorded, so nothing here is fitted.

    If this ever flips to True it must be because a fitting step exists, not
    because the flag was convenient to remove.
    """
    valued = expected_value.value_of(
        kind="contract_deadline", confidence=0.9, deal_value=400_000)
    assert valued.calibrated is False
    assert valued.basis, "the inputs must be inspectable, not just the total"
    assert expected_value.portfolio([valued])["calibrated"] is False


def test_median_fallback_says_it_is_a_stand_in():
    """An agent must be able to see the number came from a market typical."""
    valued = expected_value.value_of(
        kind="intent_next_action", confidence=0.7,
        deal_value=None, market_median=450_000)
    assert any("stand-in" in line for line in valued.basis)


# ── autonomy ceilings ───────────────────────────────────────────────────────

def test_consequential_categories_cannot_be_automated():
    """Mirrors the CHECK constraint in 0095. If these ever diverge, the UI will
    offer a level the database rejects."""
    for category in autonomy.OBSERVE_ONLY:
        assert autonomy.ceiling_for(category) == "observe"
        assert autonomy.permitted_levels(category) == ["observe"]


def test_outbound_contact_tops_out_at_assist():
    """Neoh can draft; the agent's licence signs."""
    for category in autonomy.ASSIST_MAX:
        assert autonomy.ceiling_for(category) == "assist"
        assert "autopilot" not in autonomy.permitted_levels(category)


def test_every_category_has_a_default_and_a_label():
    """A category with no default silently becomes a KeyError at read time."""
    for category in autonomy.CATEGORIES:
        assert category in autonomy.DEFAULTS
        assert autonomy.CATEGORIES[category]["label"]


def test_defaults_never_exceed_their_own_ceiling():
    """The shipped configuration must itself be legal."""
    for category, level in autonomy.DEFAULTS.items():
        ceiling = autonomy.ceiling_for(category)
        assert autonomy.LEVELS.index(level) <= autonomy.LEVELS.index(ceiling)


def test_only_research_is_automatic_by_default():
    """A default that acts is a default nobody consented to. Research reads
    only and contacts no one, which is why it is the sole exception."""
    automatic = [c for c, l in autonomy.DEFAULTS.items() if l == "autopilot"]
    assert automatic == ["research"]


def test_unscored_lead_is_not_a_declared_zero():
    """`lead_score` defaults to 0 on every new client.

    Reading that as "declared intent: 0%" invents a position the client never
    took, and the reconciliation then reports a dramatic contradiction between
    a real behavioural reading and a number nobody chose.
    """
    unscored = intent_states._declared_reading(stage="lead", lead_score=0, timeline_belief=None)
    assert unscored.score is None

    scored = intent_states._declared_reading(stage="lead", lead_score=40, timeline_belief=None)
    assert scored.score == 0.4


def test_unscored_client_with_behaviour_reports_behaviour_only():
    """Not 'their words disagree with their actions' — they have said nothing."""
    declared = intent_states._declared_reading("lead", 0, None)
    observed = intent_states._observed_reading(
        {"listing_view": 4, "showing_request": 1, "calculator_use": 1}, NOW)
    assert intent_states._reconcile(declared, observed)["verdict"] == "behaviour_only"
