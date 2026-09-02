"""The Opportunity Engine's honesty rules.

This surface tells an agent what to do next, so its failure mode is not a
crash — it is a confident, well-formatted card that nobody can check. These
tests pin the properties that keep it a finding rather than a claim.

The engine is deliberately narrower than the product vision it serves. The
vision's perception layer wants listing views, favourites and search activity;
`interaction_logs` currently holds four rows. Rather than synthesise a
behavioural signal that does not exist, the behavioural detectors stay silent
and `perception_coverage()` reports the gap. An empty feed because it was a
quiet week and an empty feed because nothing is being captured are different
situations, and an agent deciding whether to trust this thing has to be able to
tell them apart.
"""

import asyncio

import pytest

import opportunity_engine as oe
from tenancy import Role, TenantContext

TENANT = "00000000-0000-0000-0000-000000000000"


def _ctx() -> TenantContext:
    return TenantContext(agent_id="probe", tenant_id=TENANT, role=Role.AGENT)


def _opportunity(**kw):
    base = dict(
        kind="test", subject="Subject", subject_id="1", headline="H",
        why="W", recommended_action="A", confidence=0.8,
    )
    base.update(kw)
    return oe.Opportunity(**base)


# ── ranking ─────────────────────────────────────────────────────────────────

def test_confidence_outranks_deal_size():
    """A big uncertain deal must not bury a small certain one.

    The agent's scarcest resource is attention spent on something real, so
    value only breaks ties — it never dominates. A value-weighted ranking would
    put a $2M maybe above a $300K certainty, which is how a feed teaches
    someone to stop reading it.
    """
    small_sure = _opportunity(confidence=0.9, value_signal=300_000)
    big_maybe = _opportunity(confidence=0.3, value_signal=2_000_000)
    assert small_sure.score() > big_maybe.score()


def test_value_still_breaks_a_tie():
    at_same_confidence = _opportunity(confidence=0.8, value_signal=900_000)
    without_value = _opportunity(confidence=0.8, value_signal=None)
    assert at_same_confidence.score() > without_value.score()


# ── honesty ─────────────────────────────────────────────────────────────────

def test_low_confidence_findings_are_withheld_not_shown():
    """Below the floor, silence beats a guess.

    A wrong card costs more trust than a missed lead earns, and the count of
    what was withheld is reported so the suppression is visible rather than
    silent.
    """
    assert oe.MIN_CONFIDENCE >= 0.4, "the floor must be meaningful"


def test_the_feed_is_capped_so_it_stays_readable():
    """A hundred cards is the same as none."""
    assert 1 <= oe.MAX_OPPORTUNITIES <= 50


def test_evidence_carries_its_source():
    """A value with no provenance is not evidence, it is an assertion."""
    e = oe.Evidence(label="Motivation score", value="89", source="leads.motivation_score")
    assert e.source and "." in e.source, "evidence must name the table it came from"


def test_an_opportunity_serialises_with_its_score_and_evidence():
    o = _opportunity(evidence=[oe.Evidence("L", "V", "leads.x")])
    payload = o.as_dict()
    assert payload["score"] == o.score()
    assert payload["evidence"][0]["source"] == "leads.x"
    assert payload["safe_to_automate"] is False, "automation must be opt-in per detector"


# ── resilience ──────────────────────────────────────────────────────────────

def test_one_broken_detector_does_not_lose_the_whole_briefing(monkeypatch):
    """The feed is a read surface; a broken detector costs its own cards only.

    Failing the entire scan would turn one bad query into a blank morning
    briefing, which reads to the agent as "nothing is happening" — the single
    most damaging thing this surface can say incorrectly.
    """
    async def exploding(conn, ctx):
        raise RuntimeError("detector is broken")

    async def working(conn, ctx):
        return [_opportunity(kind="worked", confidence=0.9)]

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def fake_tx(_ctx):
        yield object()

    monkeypatch.setattr(oe, "_DETECTORS", (exploding, working))
    monkeypatch.setattr(oe, "tenant_tx", fake_tx)
    monkeypatch.setattr(oe, "perception_coverage", lambda ctx: _async({}))

    result = asyncio.run(oe.scan(_ctx()))
    assert result["count"] == 1, "the working detector's card must survive"
    assert "exploding" in result["detectors_failed"], "the failure must be reported, not hidden"


async def _async(value):
    return value


# ── the perception gap ──────────────────────────────────────────────────────

def test_perception_reports_the_behavioural_gap_rather_than_implying_silence():
    """Asserted against the source: this is a promise about what the UI is told.

    `behavioural_detectors_active` exists so the feed can say "nothing is being
    captured" instead of "nothing was found". Removing it would leave the two
    indistinguishable, and the failure would be invisible until an agent
    discovered it themselves.
    """
    import inspect

    src = inspect.getsource(oe.perception_coverage)
    assert "behavioural_detectors_active" in src
    assert "high_motivation_unreachable" in src, (
        "records that score highly but carry no address cannot become an "
        "actionable card, and that count is a data-acquisition gap the agent "
        "should see rather than a quiet week"
    )


def test_data_gap_evidence_renders_the_label_not_the_dict():
    """`data_gaps` holds {"code", "label"} objects.

    Stringifying the whole object put a Python repr on the agent's screen —
    braces, single quotes and all — under a heading of "Unknown". Caught by
    looking at the rendered page, not by any test that existed at the time.
    """
    rendered = oe.gap_label({"code": "contact-history", "label": "No recorded contact"})
    assert rendered == "No recorded contact"
    assert "{" not in rendered and "'" not in rendered


def test_data_gap_falls_back_to_code_then_to_nothing():
    """A producer that omits the label should degrade, not print a dict."""
    assert oe.gap_label({"code": "source"}) == "source"
    assert oe.gap_label({}) == ""
    assert oe.gap_label(None) == ""
    assert oe.gap_label("plain string") == "plain string"


# ── subject typing ──────────────────────────────────────────────────────────

def test_every_opportunity_states_what_its_subject_id_points_at():
    """The feed posts decisions to the twin keyed on (subject_type, subject_id).

    The distress detector emits leads.id and the contract detector emits a
    client id or a lead id depending on the row — and the frontend used to
    hardcode 'client'. Every lead-anchored decision was filed under the wrong
    type, and Outcome Memory's join could never reach it.
    """
    assert _opportunity().as_dict()["subject_type"] == "client"
    assert _opportunity(subject_type="lead").as_dict()["subject_type"] == "lead"


def test_lead_anchored_detectors_say_so():
    """Static check, because the detectors need a connection to run. The
    assignment has to be in the source, per detector, not inferred."""
    import inspect

    distress = inspect.getsource(oe._distress_opportunities)
    assert 'subject_type="lead"' in distress

    contract = inspect.getsource(oe._contract_deadline_opportunities)
    assert 'subject_type="client" if r["client_id"] else "lead"' in contract

    nba = inspect.getsource(oe._intent_model_opportunities)
    assert "subject_type=" not in nba, "next_best_action is always a client; the default holds"
