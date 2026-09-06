"""The generative UI generates arrangement, never content.

Everything in neoh_render is a pure transform, so the wording that matters —
the caveat on an uncertain number, the three different reasons a call queue can
be empty — is pinned here without a database.
"""

from __future__ import annotations

import pytest

import neoh_render as render


def _briefing(opportunities, *, portfolio=None, **extra):
    return {
        "attention": {
            "opportunities": opportunities,
            "portfolio": portfolio if portfolio is not None else {
                "total": 0, "caveat": "Priors, not this brokerage's outcomes.", "calibrated": False,
            },
        },
        **extra,
    }


def _opportunity(subject, ev, *, action_type="call", **extra):
    return {
        "kind": "follow_up", "subject": subject, "subject_id": f"id-{subject}",
        "subject_type": "client", "headline": f"{subject} went quiet",
        "why": "no contact in 21 days", "recommended_action": "Call them",
        "confidence": 0.6, "action_type": action_type,
        "economics": {"expected_value": ev}, **extra,
    }


class TestVocabulary:
    def test_a_block_outside_the_vocabulary_cannot_be_built(self):
        """The closed list is enforced where blocks are made, not where they
        are drawn — the frontend skipping an unknown primitive is the second
        line of defence, not the first."""
        with pytest.raises(ValueError):
            render.block("freeform_html", content="<h1>anything</h1>")
        assert render.block("metric", label="x")["primitive"] == "metric"

    def test_every_renderer_emits_only_known_primitives(self):
        out = render.who_to_call(_briefing([_opportunity("Sarah", 900.0)]))
        assert all(b["primitive"] in render.PRIMITIVES for b in out["blocks"])


class TestWhoToCall:
    def test_it_ranks_by_the_briefings_own_expected_value(self):
        """Ranking twice in one product would put the same card in two places."""
        out = render.who_to_call(_briefing([
            _opportunity("Marcus", 120.0),
            _opportunity("Sarah", 980.0),
            _opportunity("Dana", 400.0),
        ]))
        queue = next(b for b in out["blocks"] if b["primitive"] == "call_queue")
        assert [i["subject"] for i in queue["props"]["items"]] == ["Sarah", "Dana", "Marcus"]
        assert [i["rank"] for i in queue["props"]["items"]] == [1, 2, 3]
        assert out["spoken"].startswith("Sarah first")

    def test_the_queue_has_a_ceiling(self):
        out = render.who_to_call(_briefing([
            _opportunity(f"P{i}", float(i)) for i in range(20)
        ]))
        queue = next(b for b in out["blocks"] if b["primitive"] == "call_queue")
        assert len(queue["props"]["items"]) == render.MAX_QUEUE

    def test_an_opportunity_you_cannot_reach_out_on_is_not_in_a_call_queue(self):
        out = render.who_to_call(_briefing([
            _opportunity("Paperwork", 500.0, action_type="task"),
        ]))
        assert not any(b["primitive"] == "call_queue" for b in out["blocks"])

    def test_the_uncertainty_travels_as_a_prop_not_as_prose(self):
        """A number this uncertain shown bare is the false precision the
        expected-value module exists to refuse, so the caveat is a prop the
        renderer cannot drop by forgetting to concatenate it."""
        out = render.who_to_call(_briefing(
            [_opportunity("Sarah", 900.0)],
            portfolio={"total": 900.0, "caveat": "Priors, not outcomes.", "calibrated": False},
        ))
        metric = next(b for b in out["blocks"] if b["primitive"] == "metric")
        assert metric["props"]["caveat"] == "Priors, not outcomes."
        assert metric["props"]["calibrated"] is False

    def test_the_three_reasons_for_an_empty_queue_read_differently(self):
        """"Nobody to call" is a fact; "a detector broke" is an incomplete
        answer; "held back as too uncertain" is a third thing. Rendering them
        with one sentence would teach the agent to distrust all three."""
        quiet = render.who_to_call(_briefing([]))["spoken"]
        broken = render.who_to_call(_briefing([], detectors_failed=["distress"]))["spoken"]
        shy = render.who_to_call(_briefing([], suppressed_low_confidence=4))["spoken"]

        assert len({quiet, broken, shy}) == 3
        assert "detector" in broken and "incomplete" in broken
        assert "4" in shy and "uncertain" in shy
        assert "detector" not in quiet and "uncertain" not in quiet


class TestPerson:
    def test_a_contradiction_is_surfaced_as_the_question_that_resolves_it(self):
        out = render.person(
            {"id": "c1", "full_name": "Sarah Chen", "client_type": "buyer", "stage": "active"},
            {
                "latent": {"summary": "Says browsing, acts ready.", "confidence": 0.62},
                "disputes": [{"question": "Is the budget 400k or 550k?", "predicate": "budget"}],
            },
            [{"label": "Called", "at": None, "done": True}],
        )
        assert out["spoken"] == "Says browsing, acts ready."
        evidence = next(b for b in out["blocks"] if b["primitive"] == "evidence")
        assert evidence["props"]["items"][0]["label"] == "Is the budget 400k or 550k?"

    def test_more_than_one_match_is_a_question_not_a_guess(self):
        out = render.person_choices(
            [{"id": "a", "label": "Sarah Chen"}, {"id": "b", "label": "Sarah Chen"}], "sarah",
        )
        block = out["blocks"][0]
        assert block["primitive"] == "comparison"
        assert len(block["props"]["options"]) == 2
        assert "2 people match" in out["spoken"]


class TestDeal:
    def test_the_blocker_is_the_earliest_open_milestone(self):
        out = render.deal_blocker(
            {"id": "d1", "property_address": "12 Main St", "status": "under_contract"},
            [
                {"title": "Appraisal", "due_at": "2026-10-01", "completed_at": None},
                {"title": "Inspection", "due_at": "2026-09-05", "completed_at": None},
                {"title": "Offer", "due_at": "2026-08-01", "completed_at": "2026-08-01"},
            ],
        )
        assert "Inspection" in out["spoken"]
        deal = next(b for b in out["blocks"] if b["primitive"] == "deal")
        assert deal["props"]["open_count"] == 2 and deal["props"]["total_count"] == 3

    def test_no_milestones_is_not_the_same_as_all_done(self):
        """One is a gap in the file, the other is a fact about the deal."""
        empty = render.deal_blocker({"id": "d", "property_address": "12 Main"}, [])
        done = render.deal_blocker(
            {"id": "d", "property_address": "12 Main"},
            [{"title": "Closing", "completed_at": "2026-08-01"}],
        )
        assert "gap in the file" in empty["spoken"]
        assert "every milestone is done" in done["spoken"].lower()
        assert empty["spoken"] != done["spoken"]


class TestProperties:
    def test_an_empty_shortlist_explains_itself(self):
        out = render.properties_for("Sarah", [])
        assert "shortlist" in out["spoken"] and out["blocks"] == []

    def test_the_shortlist_is_capped_and_priced(self):
        out = render.properties_for("Sarah", [
            {"id": str(i), "address": f"{i} Main St", "price": 350000 + i} for i in range(9)
        ])
        options = out["blocks"][0]["props"]["options"]
        assert len(options) == render.MAX_COMPARISON
        assert "$350,000" in options[0]["detail"]


class TestFallthrough:
    def test_a_miss_is_a_fallthrough_never_an_empty_panel(self):
        out = render.fallthrough("no pattern matched")
        assert out["fallthrough"] is True
        assert out["blocks"] == [] and out["intent"] is None
