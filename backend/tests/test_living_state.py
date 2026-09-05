"""living_state.derive is pure, and every state is a recorded fact."""
from datetime import datetime, timedelta, timezone

import pytest

from living_state import (
    AFTER_CALL_MINUTES, CALLING_STALE_MINUTES, CLOSED_RECENT_DAYS, DORMANT_DAYS,
    ENGAGED_DAYS, STATES, LivingFacts, derive,
)

NOW = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)


def ago(**kw):
    return NOW - timedelta(**kw)


def test_no_facts_is_dormant_and_says_so():
    out = derive(LivingFacts(), NOW)
    assert out["state"] == "dormant"
    assert out["last_activity_at"] is None
    assert out["transaction"] is None


@pytest.mark.parametrize("days,expected", [
    (0, "engaged"), (ENGAGED_DAYS, "engaged"), (ENGAGED_DAYS + 1, "quiet"),
    (DORMANT_DAYS, "quiet"), (DORMANT_DAYS + 1, "dormant"),
])
def test_recency_thresholds_are_inclusive_at_the_edge(days, expected):
    assert derive(LivingFacts(last_activity_at=ago(days=days)), NOW)["state"] == expected


def test_under_contract_beats_recency():
    f = LivingFacts(last_activity_at=ago(days=100), transaction_status="under_contract",
                    transaction_id="t1", closing_deadline=NOW + timedelta(days=30))
    out = derive(f, NOW)
    assert out["state"] == "under_contract"
    assert out["transaction"]["closing_deadline"].startswith("2026-10-05")


def test_closed_is_a_state_only_while_recent():
    recent = LivingFacts(transaction_status="closed", closed_at=ago(days=3))
    assert derive(recent, NOW)["state"] == "closed"
    old = LivingFacts(transaction_status="closed", closed_at=ago(days=CLOSED_RECENT_DAYS + 1))
    assert derive(old, NOW)["state"] == "dormant"  # the transaction is still reported
    assert derive(old, NOW)["transaction"]["status"] == "closed"


def test_a_live_call_wins_over_everything():
    f = LivingFacts(transaction_status="under_contract", call_state="in_progress",
                    call_started_at=ago(minutes=2))
    out = derive(f, NOW)
    assert out["state"] == "calling"
    assert out["since"] == ago(minutes=2).isoformat()


def test_a_call_intent_that_never_completed_goes_stale():
    f = LivingFacts(call_state="ringing", call_started_at=ago(minutes=CALLING_STALE_MINUTES + 1))
    assert derive(f, NOW)["state"] == "dormant"


def test_after_call_expires():
    fresh = LivingFacts(call_state="completed", call_completed_at=ago(minutes=5))
    assert derive(fresh, NOW)["state"] == "after_call"
    stale = LivingFacts(call_state="completed", call_completed_at=ago(minutes=AFTER_CALL_MINUTES + 1))
    assert derive(stale, NOW)["state"] == "dormant"


def test_vocabulary_is_closed_and_every_state_is_reachable():
    reached = {
        derive(LivingFacts(), NOW)["state"],
        derive(LivingFacts(last_activity_at=ago(days=20)), NOW)["state"],
        derive(LivingFacts(last_activity_at=ago(days=1)), NOW)["state"],
        derive(LivingFacts(transaction_status="closed", closed_at=ago(days=1)), NOW)["state"],
        derive(LivingFacts(transaction_status="under_contract"), NOW)["state"],
        derive(LivingFacts(call_completed_at=ago(minutes=1)), NOW)["state"],
        derive(LivingFacts(call_state="ringing", call_started_at=ago(minutes=1)), NOW)["state"],
    }
    assert reached == set(STATES)
