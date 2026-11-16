"""NQ-13 (audit 17.3): model accuracy must also be reported over every case.

`intent_accuracy` and `constraint_exact` are parsed-only (the accumulator
returns early when the model produced no frame), so a model that abstains on
the hard cases still looks accurate. `intent_accuracy_all` and
`constraint_exact_all` use every case that carries the expectation as the
denominator and score an abstention as wrong. The parsed-only keys keep their
meaning.
"""

from __future__ import annotations

import semantic_eval
import semantic_frame as sf


def _case(n: int) -> semantic_eval.Case:
    return semantic_eval.Case(
        id=f"nq13-{n}", split="held_out", lang="en", category="nq13",
        text=f"What is on my calendar tomorrow, request {n}?",
        expect={"constraints": [], "goals": ["question"]},
    )


def _parsed(case: semantic_eval.Case) -> semantic_eval.Outcome:
    frame = sf.frame_from_model(semantic_eval.oracle_answer(case), case.text, case.known)
    return semantic_eval.Outcome(case, "parsed", frame, frozenset())


def _abstained(case: semantic_eval.Case) -> semantic_eval.Outcome:
    return semantic_eval.Outcome(case, "unavailable", None, frozenset(), reason="no frame")


def test_abstentions_count_against_accuracy_over_all_cases():
    outcomes = [_parsed(_case(1)), _parsed(_case(2)), _abstained(_case(3)), _abstained(_case(4))]

    overall = semantic_eval.score(outcomes, model=True)["overall"]

    # Existing keys: parsed-only, unchanged.
    assert overall["n"] == 4
    assert overall["parsed_rate"] == 0.5
    assert overall["intent_accuracy"] == 1.0
    assert overall["constraint_exact"] == 1.0
    # New keys: every case with the expectation, abstentions scored wrong.
    assert overall["intent_accuracy_all"] == 0.5
    assert overall["constraint_exact_all"] == 0.5
