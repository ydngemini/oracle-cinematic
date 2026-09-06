"""Outcomes reach the valuation — but only once there are enough of them.

`calibrated` was a hardcoded False for the life of expected_value, and the
docstring named agent_decisions plus closed transactions as the fitting data.
That data exists now. These tests pin the thresholds that decide when a fitted
number is allowed to replace a stated prior, because the failure mode is a
figure that looks fitted and is noise wearing a decimal point.
"""

from __future__ import annotations

import expected_value as ev


def _cal(**per_kind):
    return ev.Calibration(per_kind=per_kind, total_outcomes=sum(v[1] for v in per_kind.values()))


def test_no_calibration_means_the_priors_and_an_honest_flag():
    valued = ev.value_of(kind="contract_deadline", confidence=0.9, deal_value=400_000)
    assert valued.calibrated is False
    assert valued.uplift == ev.TIMING_UPLIFT["contract_deadline"]
    assert any("uncalibrated" in line for line in valued.basis)


def test_too_few_outcomes_keeps_the_prior():
    """n=12 with a tight-looking interval is still below the bar. The interval
    at n=12 is not tight; it only looks that way as a point estimate."""
    cal = _cal(contract_deadline=(0.60, 12, 0.35, 0.82))
    valued = ev.value_of(kind="contract_deadline", confidence=0.9, deal_value=400_000, calibration=cal)
    assert valued.calibrated is False
    assert valued.uplift == ev.TIMING_UPLIFT["contract_deadline"]


def test_a_wide_interval_keeps_the_prior_even_at_high_n():
    """Enough rows does not mean enough signal. A ±0.25 interval is still a guess."""
    cal = _cal(contract_deadline=(0.40, 80, 0.10, 0.70))
    valued = ev.value_of(kind="contract_deadline", confidence=0.9, deal_value=400_000, calibration=cal)
    assert valued.calibrated is False


def test_enough_tight_outcomes_replace_the_prior_and_say_so():
    cal = _cal(contract_deadline=(0.32, 45, 0.22, 0.44))
    valued = ev.value_of(kind="contract_deadline", confidence=0.9, deal_value=400_000, calibration=cal)
    assert valued.calibrated is True
    assert valued.uplift == 0.32
    fitted_line = next(line for line in valued.basis if "fitted" in line)
    assert "45" in fitted_line
    assert "not a causal estimate" in fitted_line, "a difference in rates must not read as causation"


def test_thresholds_are_ordered_sensibly():
    assert ev.MIN_OUTCOMES_PER_KIND_FOR_UPLIFT >= 30
    assert ev.MIN_OUTCOMES_FOR_PORTFOLIO >= ev.MIN_OUTCOMES_PER_KIND_FOR_UPLIFT
    assert 0 < ev.MAX_INTERVAL_WIDTH < 0.5


def test_portfolio_is_calibrated_only_when_every_action_is():
    """One prior in the sum makes the total a mixed figure, and a mixed figure
    labelled calibrated is the exact false precision this module refuses."""
    cal = _cal(contract_deadline=(0.32, 45, 0.22, 0.44))
    cal.total_outcomes = 120
    fitted = ev.value_of(kind="contract_deadline", confidence=0.9, deal_value=400_000, calibration=cal)
    prior = ev.value_of(kind="next_best_action", confidence=0.7, deal_value=400_000, calibration=cal)
    mixed = ev.portfolio([fitted, prior], calibration=cal)
    assert mixed["calibrated"] is False
    assert "priors" in mixed["caveat"]
    pure = ev.portfolio([fitted], calibration=cal)
    assert pure["calibrated"] is True
    assert "not causal" in pure["caveat"]


def test_portfolio_counts_toward_the_threshold_out_loud():
    """'12 recorded, 38 more before fitted' — a stage, not a shrug."""
    cal = ev.Calibration(total_outcomes=12)
    out = ev.portfolio([], calibration=cal)
    assert out["outcomes_observed"] == 12
    assert out["outcomes_needed"] == ev.MIN_OUTCOMES_FOR_PORTFOLIO - 12
    assert "38 more" in out["caveat"]


def test_portfolio_with_no_calibration_keeps_the_original_caveat_verbatim():
    out = ev.portfolio([])
    assert out["calibrated"] is False
    assert "no outcomes have been recorded yet" in out["caveat"]


def test_uplift_is_reported_relative_to_the_base_rate():
    """The stored tuple already has the base rate subtracted; the interval is
    shifted by the same constant so the width the threshold checks is the
    width of the acted-on rate, not something narrower."""
    cal = _cal(k=(0.10, 40, -0.05, 0.25))
    uplift, fitted = cal.uplift_for("k")
    assert fitted and uplift == 0.10
