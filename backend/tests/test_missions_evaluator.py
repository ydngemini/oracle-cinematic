"""The discipline of not answering too early.

A mission with eleven emails and nine texts CAN produce two percentages, and
they will differ, and an agent reading them will change their strategy. At
those volumes the difference is noise. These tests pin the three things that
prevent the product teaching something false: intervals instead of points, a
minimum before channels may be compared, and a sentence that says so.
"""

from __future__ import annotations

import inspect

import pytest

from missions import evaluator, learning


def channel(name, positive, measured, *, suppressed=0):
    return evaluator._channel_row({
        "channel": name, "attempted": measured, "with_outcome": measured,
        "positive": positive, "suppressed": suppressed,
    })


class TestIntervalsNotPoints:
    def test_a_rate_is_always_an_interval(self):
        row = channel("sms", 3, 3)
        assert row["rate"] == 1.0
        assert row["low"] < 1.0, "3/3 must not be reported as certainty"
        assert row["low"] == pytest.approx(0.44, abs=0.02)

    def test_no_outcomes_means_no_rate_rather_than_zero(self):
        """0/0 is not a nought-percent success rate; it is no information."""
        row = channel("email", 0, 0)
        assert row["rate"] is None and row["low"] is None
        assert row["enough_to_judge"] is False


class TestComparison:
    def test_thin_evidence_says_so_and_shows_the_counts(self):
        """The honest third answer: neither silence (which looks broken) nor a
        verdict (which would be false)."""
        out = evaluator.compare_channels([channel("email", 4, 11), channel("sms", 2, 9)])
        assert out["verdict"] == "not_enough_evidence"
        assert out["changed"] is False
        assert "email 4/11" in out["sentence"] and "sms 2/9" in out["sentence"]
        assert out["needed_per_channel"] == evaluator.MIN_PER_ARM

    def test_a_big_apparent_gap_below_the_threshold_still_says_nothing(self):
        """8/10 against 1/10 is a 70-point gap and still noise. This is the
        case the whole gate exists for."""
        out = evaluator.compare_channels([channel("sms", 8, 10), channel("email", 1, 10)])
        assert out["changed"] is False
        assert out["verdict"] == "not_enough_evidence"

    def test_overlapping_intervals_are_a_result_not_a_gap(self):
        """Enough data, still indistinguishable. Saying so stops a coin-flip
        difference being read as a finding."""
        out = evaluator.compare_channels([channel("sms", 11, 25), channel("email", 9, 25)])
        assert out["verdict"] == "indistinguishable"
        assert out["changed"] is False
        assert "overlap" in out["sentence"]

    def test_a_real_separation_is_reported_with_its_evidence(self):
        out = evaluator.compare_channels([channel("sms", 30, 60), channel("email", 3, 60)])
        assert out["verdict"] == "separated"
        assert out["changed"] is True and out["winner"] == "sms"
        assert "do not overlap" in out["sentence"]
        assert out["evidence"]["winner"]["measured"] == 60
        assert out["evidence"]["runner_up"]["channel"] == "email"

    def test_one_strong_channel_alone_proves_nothing(self):
        """There is nothing to be better than."""
        out = evaluator.compare_channels([channel("sms", 40, 60)])
        assert out["changed"] is False

    def test_no_channels_at_all_is_stated_plainly(self):
        out = evaluator.compare_channels([])
        assert "No outcomes recorded yet" in out["sentence"]


class TestAttribution:
    def test_credit_is_last_touch_inside_a_window_and_idempotent(self):
        source = inspect.getsource(evaluator.attach_outcomes)
        assert "outcome_event_id IS NULL" in source, "must not re-attribute"
        assert "ORDER BY o.occurred_at" in source and "LIMIT 1" in source
        assert "$2 || ' days'" in source
        assert evaluator.CREDIT_WINDOW_DAYS == 14

    def test_the_goal_number_says_it_is_not_a_causal_claim(self):
        goal = evaluator._goal({"target_count": 3}, [channel("sms", 2, 20)])
        assert goal["achieved"] == 2 and goal["target"] == 3
        assert "not outcomes it is proven to have caused" in goal["caveat"]


class TestLearningBoundary:
    def test_nothing_is_fitted(self):
        """The stopping point is deliberate and must stay legible."""
        state = learning.what_is_not_fitted()
        assert state["fitted_models"] == []
        assert state["method"] == "ordered rules over measured outcomes"
        names = {m["model"] for m in state["not_built"]}
        assert names == {"logistic propensity", "uplift", "contextual bandit"}
        for entry in state["not_built"]:
            assert entry["needs"], "each unbuilt model must state its volume"

    def test_no_model_is_imported_anywhere_in_the_learning_path(self):
        source = inspect.getsource(learning) + inspect.getsource(evaluator)
        for forbidden in ("sklearn", "torch", "numpy", "llm_gateway", "litellm"):
            assert forbidden not in source, forbidden

    def test_a_mission_with_nothing_to_say_recommends_nothing(self):
        """No recommendation is the correct output for a mission that has not
        learned anything. Inventing one is the whole mistake."""
        assert learning.recommendations({
            "channels": [channel("sms", 1, 4)],
            "comparison": {"verdict": "not_enough_evidence", "changed": False},
        }) == []

    def test_a_separation_becomes_one_auditable_rule(self):
        comparison = evaluator.compare_channels(
            [channel("sms", 30, 60), channel("email", 3, 60)])
        out = learning.recommendations({"channels": [], "comparison": comparison})
        assert len(out) == 1
        assert out[0]["rule"] == "prefer_separated_channel"
        assert "30/60" in out[0]["because"], "the rule must carry its evidence"

    def test_opting_out_is_acted_on_at_a_sample_size_a_rate_would_not_justify(self):
        """A reply rate needs 20. Suppression is different in kind: each one is
        a person asking to be left alone."""
        out = learning.recommendations({
            "channels": [channel("sms", 1, 12, suppressed=3)],
            "comparison": {"verdict": "not_enough_evidence", "changed": False},
        })
        assert [r["rule"] for r in out] == ["suppression_alarm"]
        assert "pause sms" in out[0]["action"]
        assert learning.MIN_FOR_SUPPRESSION < evaluator.MIN_PER_ARM

    def test_one_opt_out_in_three_sends_is_not_a_pattern(self):
        assert learning.recommendations({
            "channels": [channel("sms", 0, 3, suppressed=1)],
            "comparison": {"verdict": "not_enough_evidence", "changed": False},
        }) == []
