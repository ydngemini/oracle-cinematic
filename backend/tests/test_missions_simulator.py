"""A simulation must not read like a forecast.

This is the screen someone reads before letting software contact their clients
under their licence. The arithmetic matters less than the labelling: a point
estimate here is a rate the agent can falsify from memory, and a feature that
gets falsified once loses its credibility permanently.
"""

from __future__ import annotations

import inspect

import pytest

from missions import costs, simulator


def _actions(*channels):
    return [{"channel": c} for c in channels]


class TestDeterminism:
    def test_the_same_inputs_always_produce_the_same_simulation(self):
        """Nothing here may call a model or read a clock. A person deciding
        whether to launch has to be able to look twice."""
        mission = {"budget_cents": 1000}
        candidates = [{"score": 0.9}, {"score": 0.2}]
        first = simulator.simulate(mission, candidates, _actions("sms", "email"))
        second = simulator.simulate(mission, candidates, _actions("sms", "email"))
        assert first == second

    def test_no_model_is_reachable_from_the_simulator(self):
        source = inspect.getsource(simulator) + inspect.getsource(costs)
        for forbidden in ("llm_gateway", "litellm", "complete(", "acompletion"):
            assert forbidden not in source, forbidden


class TestHonesty:
    def test_the_expected_result_is_never_calibrated_here(self):
        """Calibration is the evaluator's job once real outcomes exist.
        Claiming it in the simulator would be the lie."""
        out = simulator.simulate({}, [{"score": 0.9}], _actions("sms"))
        assert out["expected"]["calibrated"] is False
        assert "calibrated\": True" not in inspect.getsource(simulator)

    def test_the_result_is_a_range_not_a_number(self):
        out = simulator.simulate({}, [{"score": 0.9}], _actions("sms", "sms", "sms"))
        expected = out["expected"]
        assert expected["rate_low"] < expected["rate_high"], "a point estimate"
        assert expected["replies_low"] <= expected["replies_high"]
        # Wide by construction: the width is the message.
        assert expected["rate_high"] - expected["rate_low"] > 0.1

    def test_the_caveat_is_prose_and_says_it_is_not_a_forecast(self):
        """`calibrated: false` is for code. This sentence is what the person
        actually reads before pressing launch."""
        out = simulator.simulate({}, [{"score": 0.9}], _actions("sms"))
        caveat = out["caveat"]
        assert "not a forecast" in caveat
        assert "published rates" in caveat.lower()
        assert len(caveat.split()) > 20, "a label, not an explanation"

    def test_an_empty_book_says_so_rather_than_predicting_zero(self):
        """"Nothing matched" is a fact about the book. Dressing it as a
        prediction of zero results would be a different, wrong claim."""
        out = simulator.simulate({}, [], [])
        assert "Nothing matched" in out["caveat"]
        assert "prediction" in out["caveat"] or "not a prediction" in out["caveat"]

    def test_recorded_outcomes_change_the_sentence_without_claiming_a_fit(self):
        out = simulator.simulate({}, [{"score": 0.9}], _actions("sms"), outcomes_observed=7)
        assert "7 outcomes" in out["caveat"]
        assert "not enough to fit against" in out["caveat"]
        assert out["expected"]["calibrated"] is False


class TestCost:
    def test_channels_are_counted_never_averaged(self):
        out = costs.cost_of(_actions("sms", "sms", "voice", "email"))
        by = {row["channel"]: row for row in out["by_channel"]}
        assert by["sms"]["count"] == 2
        assert by["sms"]["cents"] == 2 * costs.unit_cost_cents("sms")
        assert by["voice"]["count"] == 1

    def test_a_free_channel_still_costs_attention(self):
        """Email is inside the plan's volume, so it costs nothing to send and
        several minutes to deal with. Reporting only money would make a
        thousand emails look free."""
        out = costs.cost_of(_actions("email", "email"))
        assert out["total_cents"] == 0
        assert out["total_minutes"] > 0

    def test_the_basis_travels_with_the_numbers(self):
        out = costs.cost_of(_actions("sms"))
        assert "not this brokerage's own rates" in out["basis"]
        assert simulator.simulate({}, [{"score": 1}], _actions("sms"))["cost"]["basis"]

    @pytest.mark.parametrize("channel", ["email", "sms", "voice", "task"])
    def test_every_channel_the_schema_allows_is_priced(self, channel):
        """A channel with no price silently costs zero, which would let a
        mission plan unlimited voice calls inside any budget."""
        assert channel in costs.CHANNEL_UNIT_COSTS_CENTS
        assert channel in costs.ACTION_MINUTES


class TestBudget:
    def test_a_plan_over_budget_is_flagged_not_trimmed(self):
        """Trimming silently would answer a question nobody asked. The person
        decides whether to raise the budget or shrink the mission."""
        out = simulator.simulate({"budget_cents": 1}, [{"score": 1}], _actions("voice", "voice"))
        assert out["cost"]["within_budget"] is False
        assert out["actions"]["planned"] == 2, "the plan must not be silently cut"

    def test_no_budget_means_unbounded_not_zero(self):
        out = simulator.simulate({"budget_cents": 0}, [{"score": 1}], _actions("voice"))
        assert out["cost"]["within_budget"] is True
