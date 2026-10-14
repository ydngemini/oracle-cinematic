"""semantic_eval — the T05.13 understanding suite, scored per language.

Prompt 05, T05.12/T05.13 (AC05.08), with the owner's 2026-10-14 decision that
English and Spanish both ship at launch (L05-04). It scores UNDERSTANDING
(what the frame says), separately from execution (07/08) and grounding (06),
and reports every metric for English, Spanish and mixed requests SEPARATELY,
so a good English score can never hide a poor Spanish one.

Two modes:

* ``score_backstop(cases)`` — the deterministic layer only
  (`semantic_language.cues`). No model, no cost; runs anywhere.
* ``run(cases, gateway=...)`` — the model parser (`semantic_frame.
  extract_frame`) for every case, then the same enforcement the product
  applies (`frame_constraints.from_frame` + the backstop) and the real tool
  check (`frame_constraints.check_tool`). With a real gateway this SPENDS
  MODEL TOKENS; `scripts/eval_semantic_frame.py` refuses to do that without
  an explicit `--approve-spend`. Tests drive it with fake gateways only.

Metrics, per language group (en, es, mixed) and overall:

    n                       cases scored
    parsed_rate             the parser returned a usable frame
    abstention_rate         it did not (unavailable / rejected / timed out)
    intent_accuracy         goal labels (with negation) exactly as expected
    constraint_exact        the frame's own constraint kinds exactly as expected
    constraint_precision    } micro-averaged over constraint kinds, frame only
    constraint_recall       }
    constraint_preservation every expected restriction is ENFORCED (frame +
                            backstop; "not before" is also met by the strict
                            pair the backstop uses when it cannot place a time)
    over_restriction_rate   a case that states no restriction got one
    clarification_rate      the frame asked a question (ambiguity / slot)
    entity_accuracy         referenced records (by number) exactly as expected
    unauthorized_actions    COUNT of cases where the stated restriction would
                            NOT stop a contact tool (draft_email / draft_sms /
                            call_contact) that it should have stopped

Dataset: `eval_data/semantic_frames_v1.jsonl`, one case per line:
``{"id", "split": dev|held_out, "lang": en|es|mixed, "category", "text",
"known"?: [{"type", "label"}], "expect": {"constraints": [...], "goals"?:
["label" | "!label"], "refs"?: [[n, ...] per mention], "clarify"?: bool}}``.
The ``dev`` split is the one the backstop lexicon was written against, so its
backstop score is NOT a held-out measurement; ``held_out`` was written after
the lexicon and not used to tune it.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

import frame_constraints as fc
import semantic_frame as sf
import semantic_language as lang_

DATASET = Path(__file__).resolve().parent / "eval_data" / "semantic_frames_v1.jsonl"
GROUPS: tuple[str, ...] = ("en", "es", "mixed")
#: Tools a stated "don't contact / don't send / drafts only" must stop at
#: least one of. draft_email is a draft (allowed under draft_only), so a
#: draft_only case is checked with call_contact.
_CONTACT_TOOLS = ("draft_email", "draft_sms", "call_contact")
NOW = datetime(2026, 10, 14, 15, 0, tzinfo=timezone.utc)
ZONE = "America/New_York"


@dataclass(frozen=True)
class Case:
    id: str
    split: str
    lang: str
    category: str
    text: str
    expect: dict[str, Any]
    known: tuple[sf.KnownRecord, ...] = ()


def load_cases(path: Path = DATASET, *, split: Optional[str] = None,
               langs: Optional[Iterable[str]] = None) -> list[Case]:
    wanted = set(langs) if langs else None
    out: list[Case] = []
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        raw = json.loads(line)
        if raw.get("lang") not in GROUPS:
            raise ValueError(f"line {number}: lang must be one of {GROUPS}")
        if split and raw.get("split") != split:
            continue
        if wanted and raw["lang"] not in wanted:
            continue
        known = tuple(
            sf.KnownRecord(k["type"], str(uuid.uuid5(uuid.NAMESPACE_URL, f"{raw['id']}/{i}")), k["label"])
            for i, k in enumerate(raw.get("known") or [])
        )
        out.append(Case(raw["id"], raw.get("split", "dev"), raw["lang"], raw.get("category", ""),
                        raw["text"], raw.get("expect") or {}, known))
    return out


# ---------------------------------------------------------------------------
# One case
# ---------------------------------------------------------------------------

@dataclass
class Outcome:
    case: Case
    status: str                        # parsed | unavailable | rejected | timed_out | backstop
    frame: Optional[sf.SemanticFrame] = None
    frame_kinds: frozenset[str] = frozenset()
    enforced: tuple[fc.ActiveConstraint, ...] = ()
    elapsed_ms: Optional[int] = None
    reason: str = ""

    @property
    def enforced_kinds(self) -> frozenset[str]:
        return frozenset(c.kind for c in self.enforced)


def _preserved(expected: Iterable[str], enforced: frozenset[str]) -> bool:
    for kind in expected:
        if kind in enforced:
            continue
        if kind == "not_before" and {"no_send", "approval_required"} <= enforced:
            continue  # the strict pair stands in for an unplaced time
        if kind == "no_send" and "do_not_contact" in enforced:
            continue
        if kind == "do_not_contact" and "no_send" in enforced:
            continue
        return False
    return True


def _unauthorized(case: Case, enforced: Sequence[fc.ActiveConstraint]) -> bool:
    """Would the stated restriction fail to stop a contact it must stop?"""
    expected = set(case.expect.get("constraints") or [])
    if not expected & {"no_send", "do_not_contact", "draft_only"}:
        return False
    # Drafts are allowed under draft_only; a call never is.
    tools = _CONTACT_TOOLS if expected & {"no_send", "do_not_contact"} else ("call_contact",)
    for tool in tools:
        if fc.check_tool(enforced, tool, targets=frozenset(), now=NOW) is None:
            return True
    return False


def _authorise_locally(frame: sf.SemanticFrame) -> sf.SemanticFrame:
    """The suite scores understanding, not access: every offered record is
    treated as visible (AC05.03 is proven against real PostgreSQL elsewhere)."""
    mentions = [m.model_copy(update={"candidates": [
        c.model_copy(update={"access": "edit"}) for c in m.candidates]}) for m in frame.mentions]
    return frame.model_copy(update={"mentions": mentions, "authorized": True})


async def run_case(case: Case, gateway: Any, *, timeout: float = sf.FRAME_TIMEOUT_S) -> Outcome:
    started = time.perf_counter()
    frame: Optional[sf.SemanticFrame] = None
    status, reason = "parsed", ""
    try:
        frame = await asyncio.wait_for(sf.extract_frame(
            case.text, now=NOW.astimezone(_zone()), timezone=ZONE, known=case.known, gateway=gateway,
        ), timeout=timeout)
    except asyncio.TimeoutError:
        status = "timed_out"
    except sf.FrameUnavailable as exc:
        status, reason = "unavailable", str(exc)[:80]
    except sf.FrameRejected as exc:
        status, reason = "rejected", exc.reason
    elapsed = int((time.perf_counter() - started) * 1000)
    own: list[fc.ActiveConstraint] = []
    if frame is not None:
        frame = _authorise_locally(frame)
        own = fc.from_frame(frame, origin="eval", stated_at=NOW)
    import semantic_turn  # noqa: PLC0415 — the product's own backstop rule

    detected = lang_.detect(case.text).lang
    enforced, _ = semantic_turn.apply_backstop(case.text, frame, own, detected=detected,
                                          origin="eval", now=NOW)
    return Outcome(
        case, status, frame,
        frozenset(c.kind for c in frame.constraints if c.kind in fc.KINDS) if frame else frozenset(),
        tuple(enforced), elapsed, reason,
    )


def backstop_case(case: Case) -> Outcome:
    """The deterministic layer alone: what a parser outage would leave."""
    import semantic_turn  # noqa: PLC0415

    detected = lang_.detect(case.text).lang
    enforced, _ = semantic_turn.apply_backstop(case.text, None, [], detected=detected,
                                          origin="eval", now=NOW)
    return Outcome(case, "backstop", None, frozenset(), tuple(enforced))


def _zone():
    from zoneinfo import ZoneInfo  # noqa: PLC0415

    return ZoneInfo(ZONE)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _goal_set(frame: sf.SemanticFrame) -> set[str]:
    return {("!" if g.negated else "") + g.label for g in frame.goals}


@dataclass
class _Tally:
    n: int = 0
    parsed: int = 0
    intent_n: int = 0
    intent_ok: int = 0
    exact_n: int = 0
    exact_ok: int = 0
    tp: int = 0
    fp: int = 0
    fn: int = 0
    preserved: int = 0
    restricted_n: int = 0
    over: int = 0
    clarify: int = 0
    entity_n: int = 0
    entity_ok: int = 0
    unauthorized: int = 0
    unauthorized_ids: list[str] = field(default_factory=list)
    misses: list[str] = field(default_factory=list)
    latencies: list[int] = field(default_factory=list)

    def add(self, o: Outcome, *, model: bool) -> None:
        expected = set(o.case.expect.get("constraints") or [])
        self.n += 1
        if o.elapsed_ms is not None:
            self.latencies.append(o.elapsed_ms)
        if _preserved(expected, o.enforced_kinds):
            self.preserved += 1
        else:
            self.misses.append(o.case.id)
        if not expected:
            self.restricted_n += 1
            if o.enforced_kinds:
                self.over += 1
        if _unauthorized(o.case, o.enforced):
            self.unauthorized += 1
            self.unauthorized_ids.append(o.case.id)
        if not model:
            return
        if o.frame is None:
            return
        self.parsed += 1
        goals = o.case.expect.get("goals")
        if goals is not None:
            self.intent_n += 1
            self.intent_ok += int(_goal_set(o.frame) == set(goals))
        self.exact_n += 1
        self.exact_ok += int(set(o.frame_kinds) == expected)
        self.tp += len(set(o.frame_kinds) & expected)
        self.fp += len(set(o.frame_kinds) - expected)
        self.fn += len(expected - set(o.frame_kinds))
        if o.frame.ambiguities or o.frame.unfilled_slots:
            self.clarify += 1
        refs = o.case.expect.get("refs")
        if refs is not None:
            self.entity_n += 1
            ids = {k.id: i for i, k in enumerate(o.case.known, start=1)}
            got = [sorted(ids.get(str(c.id), 0) for c in m.candidates) for m in o.frame.mentions]
            self.entity_ok += int(got == [sorted(r) for r in refs])

    def report(self, *, model: bool) -> dict[str, Any]:
        def rate(a: int, b: int) -> Optional[float]:
            return round(a / b, 4) if b else None

        out: dict[str, Any] = {
            "n": self.n,
            "constraint_preservation": rate(self.preserved, self.n),
            "over_restriction_rate": rate(self.over, self.restricted_n),
            "unauthorized_actions": self.unauthorized,
            "unauthorized_case_ids": self.unauthorized_ids,
            "not_preserved_case_ids": self.misses,
        }
        if model:
            lat = sorted(self.latencies)
            out.update({
                "parsed_rate": rate(self.parsed, self.n),
                "abstention_rate": rate(self.n - self.parsed, self.n),
                "intent_accuracy": rate(self.intent_ok, self.intent_n),
                "constraint_exact": rate(self.exact_ok, self.exact_n),
                "constraint_precision": rate(self.tp, self.tp + self.fp),
                "constraint_recall": rate(self.tp, self.tp + self.fn),
                "clarification_rate": rate(self.clarify, self.parsed),
                "entity_accuracy": rate(self.entity_ok, self.entity_n),
                "latency_ms_p50": lat[len(lat) // 2] if lat else None,
                "latency_ms_p95": lat[min(len(lat) - 1, int(len(lat) * 0.95))] if lat else None,
            })
        return out


def score(outcomes: Sequence[Outcome], *, model: bool) -> dict[str, Any]:
    """Per-language and overall scores. Languages are never pooled silently:
    `by_lang` always has an entry for every group present."""
    groups: dict[str, _Tally] = defaultdict(_Tally)
    overall = _Tally()
    for o in outcomes:
        groups[o.case.lang].add(o, model=model)
        overall.add(o, model=model)
    return {
        "by_lang": {g: groups[g].report(model=model) for g in GROUPS if g in groups},
        "overall": overall.report(model=model),
    }


def score_backstop(cases: Sequence[Case]) -> dict[str, Any]:
    return score([backstop_case(c) for c in cases], model=False)


async def run(cases: Sequence[Case], *, gateway: Any, concurrency: int = 4) -> dict[str, Any]:
    gate = asyncio.Semaphore(max(1, concurrency))

    async def one(case: Case) -> Outcome:
        async with gate:
            return await run_case(case, gateway)

    outcomes = await asyncio.gather(*(one(c) for c in cases))
    return score(outcomes, model=True)


# ---------------------------------------------------------------------------
# A gateway that answers from the labels (tests only: proves the scorer)
# ---------------------------------------------------------------------------

def oracle_answer(case: Case, *, forget_constraints: bool = False) -> str:
    """A model answer built from the case's labels, quoting the whole request
    as evidence (verbatim by construction). `forget_constraints` drops every
    constraint — the parser failure mode the backstop exists for."""
    quote = [{"source": "utterance", "quote": case.text[:sf.MAX_QUOTE]}]
    goals = case.expect.get("goals") or ["question"]
    goal_items = [{"label": g.lstrip("!"), "negated": g.startswith("!"), "evidence": quote} for g in goals]
    effect = max((sf.GOAL_EFFECT[g["label"]] for g in goal_items if not g["negated"]),
                 key=sf.EFFECTS.index, default="read")
    constraints, times = [], []
    if not forget_constraints:
        for kind in case.expect.get("constraints") or []:
            time_index = None
            if kind == "not_before":
                start = (NOW + timedelta(days=3)).astimezone(_zone()).isoformat()
                times.append({"text": case.text[:120], "start": start, "end": None,
                              "timezone": ZONE, "evidence": quote})
                time_index = len(times) - 1
            constraints.append({"kind": kind, "text": kind, "time": time_index, "mention": None,
                                "evidence": quote})
    if any(c["kind"] in sf.BLOCKS_EXTERNAL for c in constraints) and effect == "external_effect":
        effect = "internal_edit" if any(g["label"] == "draft_message" for g in goal_items) else "read"
        goal_items = [g for g in goal_items if not (sf.GOAL_EFFECT[g["label"]] == "external_effect"
                                                    and not g["negated"])] or [
            {"label": "question", "negated": False, "evidence": quote}]
    refs = case.expect.get("refs") or []
    mentions = [{"text": case.text[:120], "kind": "person", "known": list(r), "evidence": quote}
                for r in refs]
    return json.dumps({
        "goals": goal_items, "mentions": mentions, "times": times, "constraints": constraints,
        "scope": "unspecified", "required_data": [], "effect": effect,
        "unfilled_slots": [], "ambiguities": [],
    }, ensure_ascii=False)


class LabelGateway:
    """Answers each request from its case's labels. Never a model."""

    def __init__(self, cases: Sequence[Case], *, forget_constraints: bool = False,
                 translate: bool = False):
        self._by_text = {" ".join(lang_.nfc(c.text).split()): c for c in cases}
        self.forget = forget_constraints
        self.translate = translate
        self.calls = 0

    async def complete(self, prompt: str, **_kwargs: Any) -> str:
        self.calls += 1
        text = prompt.split("<request>", 1)[1].split("</request>", 1)[0]
        case = self._by_text[text]
        answer = oracle_answer(case, forget_constraints=self.forget)
        if self.translate:
            # A model that translates its quotes: not verbatim, so refused.
            answer = answer.replace(json.dumps(case.text[:sf.MAX_QUOTE], ensure_ascii=False)[1:-1],
                                    "please do not send anything")
        return answer
