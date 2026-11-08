"""The evidence contract (T06.02, AC06.02, L06-03): a bundle has a schema
version, every fact has a source and a C6 class, a failed source is an
unknown and never a fact, and beliefs read in as facts without losing their
provenance, expiry or disputes (T06.04)."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import pytest

import evidence as ev
import source_status as ss

T0 = "2026-09-01T12:00:00+00:00"


def _fact(**kw):
    base = dict(id="f1", subject="client:c1", predicate="max_budget", value=650000,
                evidence_class=ev.PROVISIONAL, sources=("beliefs",), observed_at=T0,
                sensitivity="ai_memory", audience="client:c1")
    return ev.EvidenceFact(**{**base, **kw})


def _belief(id_, predicate, value, status="reported", state="active", kind="sms",
            ref="msg-1", quote=None, valid_until=None, pinned=False):
    return {"id": id_, "subject_type": "client", "subject_id": "c1", "predicate": predicate,
            "value": value, "status": status, "confidence": 0.7, "stored_confidence": 0.8,
            "source": {"kind": kind, "ref": ref, "quote": quote},
            "learned_at": T0, "recorded_at": T0, "valid_until": valid_until,
            "revision_state": state, "pinned": pinned, "age_days": 10.0}


# ── the fact ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("bad, message", [
    (dict(evidence_class="confident"), "C6 class"),
    (dict(sources=()), "no source"),
    (dict(subject="c1"), "subject"),
    (dict(sensitivity="secretish"), "sensitivity"),
    (dict(observed_at=None), "observation time"),
    (dict(observed_at="2026-09-01T12:00:00"), "timezone-aware"),
    (dict(evidence_class=ev.FACT), "citation"),
    (dict(disputed=True), "conflicts with"),
    (dict(disputed=True, conflicts=("f1",)), "itself"),
])
def test_a_fact_that_breaks_the_contract_is_refused(bad, message):
    with pytest.raises(ev.EvidenceError, match=message):
        _fact(**bad)


def test_a_cited_fact_is_a_fact():
    f = _fact(evidence_class=ev.FACT, citations=(ev.Citation("public_record", ref="parcel-9"),))
    assert f.evidence_class == ev.FACT and f.observed_at == T0


def test_there_is_no_single_confidence_number():
    names = {f.name for f in ev.EvidenceFact.__dataclass_fields__.values()}
    assert not {"confidence", "score", "probability"} & names


# ── the bundle ──────────────────────────────────────────────────────────────

def test_a_fact_may_cite_only_a_checked_source():
    with pytest.raises(ev.EvidenceError, match="not checked"):
        ev.EvidenceBundle(sources={"beliefs": ss.unavailable()}, facts=(_fact(),))
    with pytest.raises(ev.EvidenceError, match="absent"):
        ev.EvidenceBundle(sources={}, facts=(_fact(),))


def test_every_source_carries_a_c6_status():
    with pytest.raises(ev.EvidenceError, match="C6 status"):
        ev.EvidenceBundle(sources={"beliefs": {"status": "ok"}})


def test_a_failed_required_source_is_an_unknown_and_degrades_the_bundle():
    b = ev.EvidenceBundle(sources={
        "beliefs": ss.checked(matched=1),
        "calls": {**ss.unavailable(ss.NOT_CONFIGURED)},
        "files": ss.not_applicable(ss.NOT_CONFIGURED),
    }, facts=(_fact(),))
    assert b.degraded is True
    assert b.unknowns == [{"source": "calls", "status": "unavailable",
                           "reason": "not_configured", "required": True}]


def test_conflicts_must_point_inside_the_bundle_and_ids_are_unique():
    with pytest.raises(ev.EvidenceError, match="not in the bundle"):
        ev.EvidenceBundle(sources={"beliefs": ss.checked()},
                          facts=(_fact(disputed=True, conflicts=("f9",)),))
    with pytest.raises(ev.EvidenceError, match="unique"):
        ev.EvidenceBundle(sources={"beliefs": ss.checked()}, facts=(_fact(), _fact()))


def test_the_bundle_is_versioned_and_round_trips():
    b = ev.EvidenceBundle(sources={"beliefs": ss.checked(matched=1)},
                          facts=(_fact(citations=(ev.Citation("sms", ref="m1"),)),))
    data = json.loads(json.dumps(b.to_dict()))
    assert data["schema_version"] == ev.SCHEMA_VERSION == "evidence/1"
    assert data["degraded"] is False and data["unknowns"] == []
    again = ev.from_dict(data)
    assert again.facts == b.facts and again.sources == b.sources
    with pytest.raises(ev.EvidenceError, match="unsupported schema"):
        ev.from_dict({**data, "schema_version": "evidence/2"})


# ── the beliefs adapter (T06.04) ────────────────────────────────────────────

def _payload(*beliefs):
    by = {}
    for b in beliefs:
        by.setdefault(b["predicate"], []).append(b)
    return {"subject_type": "client", "subject_id": "c1", "beliefs": by, "disputes": []}


def test_beliefs_keep_their_provenance_grade_and_expiry():
    facts = {f.id: f for f in ev.facts_from_beliefs(_payload(
        _belief("1", "pre_approval", True, status="confirmed", kind="form", ref="doc-7",
                valid_until="2026-12-01T00:00:00+00:00"),
        _belief("2", "max_budget", 650000, status="reported", quote="we can do 650"),
        _belief("3", "timeline", "spring", status="inference", kind="model", ref=None),
        _belief("4", "prefers_area", "Reston", status="hypothesis"),
        _belief("5", "household_size", 4, status="confirmed", kind="model", ref="run-1"),
        _belief("6", "objection", "price", state="retracted"),
    ))}
    assert set(facts) == {"belief:1", "belief:2", "belief:3", "belief:4", "belief:5"}, "history is not evidence"
    assert facts["belief:1"].evidence_class == ev.FACT
    assert facts["belief:1"].valid_until == "2026-12-01T00:00:00+00:00"
    assert facts["belief:1"].refresh == "reading halves every 90 days"
    assert facts["belief:2"].evidence_class == ev.PROVISIONAL, "said is not verified"
    assert facts["belief:2"].sensitivity == "communication_content", "the client's own words"
    assert facts["belief:3"].evidence_class == ev.INFERRED
    assert facts["belief:4"].evidence_class == ev.PROVISIONAL
    assert facts["belief:5"].evidence_class == ev.INFERRED, "a model output is never a fact"
    assert all(f.sources == ("beliefs",) and f.audience == "client:c1" for f in facts.values())
    assert facts["belief:2"].basis == "reported"


def test_a_confirmed_belief_without_a_ref_is_not_a_fact():
    (f,) = ev.facts_from_beliefs(_payload(_belief("1", "pre_approval", True, status="confirmed",
                                                  kind="agent_entry", ref=None)))
    assert f.evidence_class == ev.PROVISIONAL


def test_disputed_beliefs_name_each_other():
    facts = {f.id: f for f in ev.facts_from_beliefs(_payload(
        _belief("a", "prefers_area", "Ashburn", state="disputed"),
        _belief("b", "prefers_area", "Reston", state="disputed"),
        _belief("c", "timeline", "spring", state="disputed"),  # partner retracted since
    ))}
    assert facts["belief:a"].disputed and facts["belief:a"].conflicts == ("belief:b",)
    assert facts["belief:b"].conflicts == ("belief:a",)
    assert not facts["belief:c"].disputed and facts["belief:c"].conflicts == ()
    bundle = ev.EvidenceBundle(sources={"beliefs": ss.checked()}, facts=tuple(facts.values()))
    assert len(bundle.facts) == 3


def test_a_failed_belief_read_is_an_unknown_not_an_empty_set(monkeypatch):
    import belief_store

    async def down(ctx, subject_type, subject_id):
        raise asyncio.TimeoutError()

    async def two(ctx, subject_type, subject_id):
        return _payload(_belief("1", "max_budget", 1), _belief("2", "timeline", "now"))

    monkeypatch.setattr(belief_store, "beliefs_about", down)
    b = asyncio.run(ev.beliefs_bundle(None, "client", "c1"))
    assert b.facts == () and b.degraded is True
    assert b.unknowns[0]["status"] == ss.TIMED_OUT

    monkeypatch.setattr(belief_store, "beliefs_about", two)
    b = asyncio.run(ev.beliefs_bundle(None, "client", "c1"))
    assert len(b.facts) == 2 and b.degraded is False
    assert b.sources["beliefs"]["matched"] == 2
    assert datetime.fromisoformat(b.compiled_at) <= datetime.now(timezone.utc)
