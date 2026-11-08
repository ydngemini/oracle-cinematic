"""The Opportunity Engine — what deserves attention, why, and what to do next.

The product promise this serves: *Neoh knows who needs attention, why they need
attention, what should happen next, and can do it for you.* Everything here
exists to make the first three answerable with evidence, so that the fourth is
something an agent can safely authorise.

Design rules, in priority order:

1. **No opportunity without evidence.** Every card carries the rows it was
   derived from. A card the agent cannot audit is a claim, not a finding, and
   this codebase already refuses that shape elsewhere (draft_contract names
   unrecorded terms rather than inventing them; the intelligence API resolves
   every citation against `source_records`). An engine that hallucinated
   "Sarah viewed this three times" would be worse than no engine, because the
   agent would act on it.

2. **Confidence is explicit and bounded by the weakest input.** "Sarah is
   buying in 30 days" is not something this system can know. "60% — one
   behavioural signal, no lender contact on file" is.

3. **Silence about what cannot be seen.** `interaction_logs` currently holds
   four rows across three types, so behavioural detectors return nothing rather
   than something weak. `perception_coverage()` reports that gap to the UI as a
   first-class fact, because an empty feed caused by missing capture and an
   empty feed caused by a quiet week are different situations and an agent
   deciding whether to trust this product needs to tell them apart.

4. **Read what already exists.** `client_ai_state` carries score_breakdown,
   normalized_preferences, next_actions, data_gaps, evidence and
   property_candidates — most of an intent vector, maintained by
   client_ai_automation. Detectors consume it rather than recomputing a second,
   divergent opinion.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import semantic_language as lang_
from db.connection import tenant_tx
from tenancy import TenantContext

logger = logging.getLogger("oracle.opportunity_engine")

#: Below this, an opportunity is noise. Surfacing a 20%-confidence guess costs
#: more trust than the occasional missed lead earns.
MIN_CONFIDENCE = 0.45

#: Ceiling on a single scan. The feed is read at a glance; a hundred cards is
#: the same as none.
MAX_OPPORTUNITIES = 25


@dataclass
class Evidence:
    """One auditable fact behind an opportunity.

    `source` names the table or system the value came from so the agent can go
    and look. `as_of` is when the fact was true, which is not the same as when
    it was read — the market data in this system runs ~81 days behind its
    publisher, and a card that hid that would misrepresent its own freshness.
    """

    label: str
    value: str
    source: str
    as_of: Optional[str] = None


@dataclass
class Opportunity:
    kind: str
    subject: str
    subject_id: Optional[str]
    headline: str
    why: str
    recommended_action: str
    #: 0-1. Never 1.0: this system infers, and a certainty it cannot have would
    #: be a lie told in a number.
    confidence: float
    #: Rough dollar consequence of acting, for ranking only. None where the
    #: engine has no honest basis for one, which is most of the time.
    value_signal: Optional[float] = None
    evidence: list[Evidence] = field(default_factory=list)
    #: True only where acting is reversible AND needs no human judgement, so
    #: "Handle all safe opportunities" cannot send an irreversible thing.
    safe_to_automate: bool = False
    #: When this stops being actionable, ISO-8601. Only ever set from a date
    #: that exists in the database — the Command Center files an opportunity
    #: with no deadline under "Watching" rather than inventing urgency for it,
    #: because a fabricated deadline trains the agent to ignore the real ones.
    deadline: Optional[str] = None
    #: Shape of the work, for costing in expected_value. Not the action itself.
    action_type: str = "call"
    #: What subject_id points at. The distress detector emits leads.id, the
    #: contract detector emits a client id when the lead names one and the
    #: lead id otherwise — and the feed used to post every decision to the
    #: twin as subject_type='client'. Every lead-anchored decision was filed
    #: under a type it was not, and Outcome Memory's join could never reach
    #: it. Stated per card because it cannot be inferred from the id.
    subject_type: str = "client"

    def score(self) -> float:
        """Ranking utility. Confidence gates it; value only breaks ties.

        Deliberately not value-dominant: a $2M deal at 30% confidence should not
        outrank a $300K deal at 90%, because the agent's scarcest resource is
        attention spent on something real.
        """
        base = self.confidence
        if self.value_signal:
            base *= 1.0 + min(self.value_signal / 1_000_000.0, 0.5)
        return round(base, 4)

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["score"] = self.score()
        return out


# ── card language (L05-06) ──────────────────────────────────────────────────
# Every sentence a card says comes from the en/es catalogue; `lang` None or
# "en" is the English the cards always had. Record content (names,
# addresses, model-written suggestions and summaries, stated needs) is shown
# as stored in either language.

def _say(key: str, lang: Optional[str], **values: Any) -> str:
    return lang_.t(key, lang, **values)


def _known(stored: Any, keys: tuple[str, ...], lang: Optional[str]) -> Any:
    """`stored` in `lang` when it is exactly one of the catalogue's English
    sentences, else as stored: a producer's fixed text is translated, and
    anything a model or a person wrote is not guessed at."""
    if lang_.lang_or_default(lang) == "en":
        return stored
    for key in keys:
        entry = lang_.MESSAGES.get(key)
        if entry and entry["en"] == stored:
            return entry[lang_.lang_or_default(lang)]
    return stored


def _status_word(status: Any, lang: Optional[str]) -> str:
    """A leads.dossier_status value: as stored in English, named in Spanish."""
    key = f"dossier.{status}"
    if lang_.lang_or_default(lang) == "en" or key not in lang_.MESSAGES:
        return str(status)
    return lang_.t(key, lang)


#: NOTE ON SCOPING. Queries in this module carry `tenant_id = $1::uuid` bound
#: to ctx.tenant_id. That is NOT the RLS-duplication anti-pattern fixed in
#: belief_store and intent_states — it is a BUSINESS predicate. A briefing is
#: one tenant's briefing: a platform admin, whom RLS lets read every tenant,
#: must still be briefed about their own, not handed a pooled scan of the
#: whole platform. agent_twin.policy() keeps its per-agent filter for the same
#: reason. Do not "simplify" these away.

async def perception_coverage(ctx: TenantContext) -> dict[str, Any]:
    """What this engine can and cannot currently see.

    Reported to the UI rather than hidden. A feed that says "no behavioural
    opportunities" when the truth is "no behavioural data is being captured"
    teaches the agent to distrust the product the first time they find out.
    """
    async with tenant_tx(ctx) as conn:
        rows = await conn.fetch(
            """
            SELECT interaction_type, count(*)::int AS n,
                   count(*) FILTER (WHERE client_id IS NULL)::int AS unattributed,
                   count(*) FILTER (
                       WHERE actor_role IN ('buyer', 'seller')
                   )::int AS client_originated
              FROM interaction_logs
             WHERE tenant_id = $1::uuid
             GROUP BY 1
            """,
            ctx.tenant_id,
        )
        clients = await conn.fetchval(
            "SELECT count(*)::int FROM clients WHERE tenant_id = $1::uuid", ctx.tenant_id
        )
        scored = await conn.fetchval(
            "SELECT count(*)::int FROM client_ai_state WHERE tenant_id = $1::uuid",
            ctx.tenant_id,
        )
        # A scored record with no street address cannot become a card an agent
        # can act on. Measured on this corpus: 5,252 records score >= 80 and
        # NONE of them carries an address, so the distress detector correctly
        # returns nothing. That is a data-acquisition gap, not a quiet week, and
        # reporting the number is the difference between "no opportunities" and
        # "5,252 opportunities you cannot reach" — which are different problems
        # with different owners.
        blocked = await conn.fetchrow(
            """
            SELECT count(*)::int AS scored,
                   count(*) FILTER (
                       WHERE address IS NOT NULL AND btrim(address) <> ''
                   )::int AS actionable
              FROM leads
             WHERE tenant_id = $1::uuid AND motivation_score >= 80
            """,
            ctx.tenant_id,
        )

    by_type = {r["interaction_type"]: r["n"] for r in rows}
    total = sum(by_type.values())

    # Two different reasons a captured event still fails to become intent, and
    # they have different owners, so they are counted apart.
    #
    # UNATTRIBUTED: the row is anchored to a parcel but names no person, which
    # happens when a portal link was opened before its lead was tied to a
    # client. 0096 stamps client_id at write time and backfilled what existed;
    # anything left is a CRM linkage gap somebody can go and fix.
    #
    # AGENT-ORIGINATED: the row is the brokerage's own activity. It is
    # deliberately excluded from every intent reading — counting it would
    # measure how busy the agent has been and report it as how interested the
    # client is — so it inflates the raw signal count without ever moving a
    # score. Reporting the split stops "we have 400 events and no intent" from
    # looking like a broken model.
    unattributed = sum(r["unattributed"] for r in rows)
    client_originated = sum(r["client_originated"] for r in rows)
    # Named distinctly on purpose: `scored` already means "clients with an
    # intent model" ten lines up, and reusing it reported 5,252 clients where
    # there are five.
    motivated_total = (blocked["scored"] if blocked else 0) or 0
    motivated_actionable = (blocked["actionable"] if blocked else 0) or 0
    return {
        "high_motivation_scored": motivated_total,
        "high_motivation_actionable": motivated_actionable,
        "high_motivation_unreachable": max(0, motivated_total - motivated_actionable),
        "interaction_signals": total,
        "client_originated_signals": client_originated,
        "agent_originated_signals": total - client_originated,
        "unattributed_signals": unattributed,
        "by_type": by_type,
        "clients": clients or 0,
        "clients_with_intent_model": scored or 0,
        # The behavioural detectors need a stream, not a trickle. Naming the
        # threshold means the UI can explain the silence instead of implying
        # there was nothing to find.
        "behavioural_detectors_active": client_originated >= 50,
        "note": (
            "Behavioural detectors stay silent until interaction_logs carries a "
            "real stream. The schema supports it (portal_view, email, message); "
            "the volume is not there yet."
        ),
    }


async def _contract_deadline_opportunities(conn, ctx: TenantContext,
                                           lang: Optional[str] = None) -> list[Opportunity]:
    """Contracts running out of time.

    A date is the one signal that needs no behavioural capture to be true, and
    a missed contract deadline is the most expensive thing on this list.
    """
    rows = await conn.fetch(
        """
        SELECT l.id::text, l.address, l.state, l.contract_expires_at, l.dossier_status,
               c.full_name AS client_name, c.id::text AS client_id
          FROM leads l
          LEFT JOIN clients c ON c.id = l.seller_client_id AND c.tenant_id = l.tenant_id
         WHERE l.tenant_id = $1::uuid
           AND l.contract_expires_at IS NOT NULL
           AND l.contract_expires_at BETWEEN now() AND now() + interval '21 days'
         ORDER BY l.contract_expires_at ASC
         LIMIT 10
        """,
        ctx.tenant_id,
    )
    out: list[Opportunity] = []
    for r in rows:
        days = max(0, (r["contract_expires_at"] - datetime.now(timezone.utc)).days)
        # A date in the database is a fact, so confidence is high — but not 1.0,
        # because the date itself can be stale or wrongly entered.
        confidence = 0.95 if days <= 7 else 0.8
        out.append(Opportunity(
            kind="contract_deadline",
            subject=r["client_name"] or r["address"] or _say("opp.contract.subject", lang),
            subject_id=r["client_id"] or r["id"],
            subject_type="client" if r["client_id"] else "lead",
            headline=_say("opp.contract.headline_one" if days == 1 else "opp.contract.headline_many",
                          lang, days=days),
            why=_say(
                "opp.contract.why", lang,
                address=r["address"] or _say("opp.contract.this_property", lang),
                date=r["contract_expires_at"].date().isoformat(),
                status=(_status_word(r["dossier_status"], lang) if r["dossier_status"]
                        else _say("opp.contract.unset", lang)),
            ),
            recommended_action=_say("opp.contract.action", lang),
            confidence=confidence,
            evidence=[Evidence(
                label=_say("opp.contract.evidence", lang),
                value=r["contract_expires_at"].date().isoformat(),
                source="leads.contract_expires_at",
            )],
            # A deadline needs a judgement call about the deal, not a template.
            safe_to_automate=False,
            deadline=r["contract_expires_at"].isoformat(),
            action_type="review",
        ))
    return out



def gap_label(gap: Any) -> str:
    """The human sentence for one `data_gaps` entry.

    Entries are {"code": ..., "label": ...} objects. An earlier version passed
    the whole dict through str(), which put a Python repr on the agent's screen
    — braces, single quotes and all — under a heading of "Unknown". The label is
    the sentence written for a person; the code is only the machine handle, and
    is a poor but survivable fallback when a producer omits the label.
    """
    if isinstance(gap, dict):
        text = gap.get("label") or gap.get("code") or ""
    elif gap is None:
        text = ""
    else:
        text = str(gap)
    return str(text)[:120]


async def _intent_model_opportunities(conn, ctx: TenantContext,
                                      lang: Optional[str] = None) -> list[Opportunity]:
    """Clients whose maintained intent model already asked for something.

    client_ai_automation writes next_actions and data_gaps per client. Those are
    a standing recommendation nobody currently surfaces anywhere the agent
    looks, so this is closer to plumbing than inference — which is exactly why
    it can carry high confidence.
    """
    rows = await conn.fetch(
        """
        SELECT s.client_id::text, s.next_actions, s.data_gaps, s.summary,
               s.score_breakdown, c.full_name, c.lead_score, c.stage
          FROM client_ai_state s
          JOIN clients c ON c.id = s.client_id AND c.tenant_id = s.tenant_id
         WHERE s.tenant_id = $1::uuid
           AND s.enabled
           AND jsonb_array_length(coalesce(s.next_actions, '[]'::jsonb)) > 0
         LIMIT 15
        """,
        ctx.tenant_id,
    )
    out: list[Opportunity] = []
    for r in rows:
        actions = r["next_actions"]
        if isinstance(actions, str):
            try:
                actions = json.loads(actions)
            except json.JSONDecodeError:
                actions = []
        if not actions:
            continue
        first = actions[0]
        # client_ai_automation writes {code, title, reason}; reading only
        # "action" turned every one of these cards into "Recommended
        # follow-up" / "Follow up" whatever the model had actually said.
        action_text = (
            (first.get("title") or first.get("action")) if isinstance(first, dict) else str(first)
        )
        if isinstance(first, dict) and first.get("code"):
            # Only the producer's fixed titles, by code; a model's own
            # suggestion ("model-suggestion") has no catalogue key.
            action_text = _known(action_text, (f"nba.{first['code']}",), lang)
        gaps = r["data_gaps"]
        if isinstance(gaps, str):
            try:
                gaps = json.loads(gaps)
            except json.JSONDecodeError:
                gaps = []
        # Every unanswered question about a client is a reason to trust the
        # recommendation less. Stated, not hidden.
        confidence = max(0.5, 0.9 - 0.1 * len(gaps or []))
        evidence = [Evidence(
            label=_say("opp.nba.lead_score", lang),
            value=str(r["lead_score"] if r["lead_score"] is not None else _say("opp.nba.unscored", lang)),
            source="clients.lead_score",
        )]
        if r["summary"]:
            evidence.append(Evidence(
                label=_say("opp.nba.summary", lang), value=str(r["summary"])[:200],
                source="client_ai_state.summary",
            ))
        for gap in (gaps or [])[:3]:
            text = gap_label(gap)
            if not text:
                continue
            code = gap.get("code") if isinstance(gap, dict) else None
            evidence.append(Evidence(
                label=_say("opp.nba.not_known", lang),
                value=_known(text, (f"gap.{code}", f"gap.{code}.timed_out",
                                    f"gap.{code}.unavailable"), lang) if code else text,
                source="client_ai_state.data_gaps",
            ))
        out.append(Opportunity(
            kind="next_best_action",
            subject=r["full_name"] or _say("opp.nba.subject", lang),
            subject_id=r["client_id"],
            headline=str(action_text or _say("opp.nba.headline", lang))[:120],
            why=(r["summary"] or _say("opp.nba.why", lang))[:280],
            recommended_action=str(action_text or _say("opp.nba.action", lang))[:200],
            confidence=round(confidence, 2),
            evidence=evidence,
            safe_to_automate=False,
        ))
    return out


async def _distress_opportunities(conn, ctx: TenantContext,
                                  lang: Optional[str] = None) -> list[Opportunity]:
    """High-motivation property records — the one signal that exists at scale.

    8.47M leads carry a motivation_score. This is the detector with real data
    behind it today, and it is property-side rather than person-side precisely
    because the behavioural stream is not there yet.
    """
    # Two constraints that decide whether this detector is useful or noise.
    #
    # An address is required. A card reading "Parcel in NH" with no street is
    # not something an agent can act on, and 8.47M rows contain plenty of them —
    # the first version of this query returned eight identical unactionable
    # cards, which is worse than returning none.
    #
    # DISTINCT ON (state) spreads the result. Motivation scores cluster hard, so
    # a plain ORDER BY score returns the same neighbourhood eight times. One per
    # state gives the agent eight decisions instead of one decision repeated.
    rows = await conn.fetch(
        """
        SELECT DISTINCT ON (state)
               id::text, address, state, motivation_score, dossier_status, updated_at
          FROM leads
         WHERE tenant_id = $1::uuid
           AND motivation_score >= 80
           AND dossier_status IN ('draft', 'new')
           AND address IS NOT NULL
           AND btrim(address) <> ''
         ORDER BY state, motivation_score DESC, updated_at DESC
         LIMIT 8
        """,
        ctx.tenant_id,
    )
    out: list[Opportunity] = []
    for r in rows:
        score = int(r["motivation_score"] or 0)
        # A model score, not an observation. Capped well below the deadline
        # detector because nobody has confirmed anything about this owner.
        confidence = min(0.75, 0.4 + score / 200.0)
        out.append(Opportunity(
            kind="distress_signal",
            subject=f"{r['address']}, {r['state']}",
            subject_id=r["id"],
            subject_type="lead",
            headline=_say("opp.distress.headline", lang, score=score),
            why=_say("opp.distress.why", lang, score=score,
                     status=_status_word(r["dossier_status"], lang)),
            recommended_action=_say("opp.distress.action", lang),
            confidence=round(confidence, 2),
            evidence=[
                Evidence(label=_say("opp.distress.score", lang), value=str(score),
                         source="leads.motivation_score"),
                Evidence(label=_say("opp.distress.updated", lang),
                         value=(r["updated_at"].date().isoformat() if r["updated_at"]
                                else _say("opp.distress.unknown", lang)),
                         source="leads.updated_at"),
            ],
            safe_to_automate=False,
        ))
    return out


async def _listing_buyer_opportunities(conn, ctx: TenantContext,
                                       lang: Optional[str] = None) -> list[Opportunity]:
    """One of the brokerage's own active listings fits one of its buyers.

    The most ordinary opportunity in residential brokerage — "123 Main St may
    fit Sarah" — and the engine had no detector for it: matching existed only
    on the MLS detail page. Deterministic (buyer_matching), so every card says
    which recorded criteria agree and which homes the buyer was actually
    shown; nothing is inferred and no model is called.

    Only STRONG fits (two or more criteria agree, none conflict) become a
    card. Confidence is the share of the four criteria that agree, capped well
    below certainty, because recorded preferences are what a buyer said, not
    what they will buy.
    """
    # Its own savepoint: scan() runs every detector in ONE transaction, and a
    # failed statement here would otherwise abort it for the detectors after.
    async with conn.transaction():
        return await _listing_buyer_cards(conn, ctx, lang)


def _fit_notes(match: dict[str, Any], lang: Optional[str]) -> list[str]:
    """The matched criteria as sentences. English is buyer_matching's own
    note; another language is said from the note's key and values, and a
    note without them (an older producer) stays as written."""
    notes = list(match.get("matched_criteria") or [])
    if lang_.lang_or_default(lang) == "en":
        return notes
    detail = match.get("matched_detail") or []
    out = []
    for i, note in enumerate(notes):
        item = detail[i] if i < len(detail) and isinstance(detail[i], dict) else {}
        key = f"fit.{item.get('key')}"
        out.append(_say(key, lang, **(item.get("params") or {}))
                   if item.get("key") and key in lang_.MESSAGES else note)
    return out


async def _listing_buyer_cards(conn, ctx: TenantContext,
                               lang: Optional[str] = None) -> list[Opportunity]:
    import buyer_matching

    listings = await conn.fetch(
        """
        SELECT l.id::text AS listing_id, l.lead_id::text AS lead_id
          FROM listings l
         WHERE l.tenant_id = $1::uuid AND l.status = 'active' AND l.lead_id IS NOT NULL
         ORDER BY l.updated_at DESC
         LIMIT 10
        """,
        ctx.tenant_id,
    )
    out: list[Opportunity] = []
    for row in listings:
        found = await buyer_matching.property_buyer_matches(
            conn, ctx, listing_id=row["listing_id"], limit=3,
        )
        if not found:
            continue
        strong = [m for m in found["matches"] if m["verdict"] == buyer_matching.STRONG]
        if not strong:
            continue
        best = strong[0]
        prop = found["property"]
        place = buyer_matching.parse_us_address(prop.get("address") or "")
        where = (f"{place['street']}, {place['city']}" if place
                 else str(prop.get("address") or _say("opp.match.this_listing", lang)))
        first_name = (best["name"] or _say("opp.match.buyer", lang)).split()[0]
        if lang_.lang_or_default(lang) != "en":
            unnamed = not best["name"] or best["name"] == lang_.MESSAGES["opp.match.unnamed"]["en"]
            best = {**best, "name": _known(best["name"], ("opp.match.unnamed",), lang)}
            if unnamed:
                first_name = _say("opp.match.buyer", lang)
        fits = _fit_notes(best, lang)
        evidence = [
            Evidence(label=_say("opp.match.fits", lang), value=note, source="clients.preferences")
            for note in fits
        ]
        for need in best["stated_needs"][:2]:
            evidence.append(Evidence(label=_say("opp.match.wants", lang), value=need,
                                     source="clients.preferences"))
        for shown in best["recent_showings"][:2]:
            evidence.append(Evidence(
                label=_say("opp.match.shown", lang),
                value=f"{shown['address']}" + (f" — {shown['outcome']}" if shown.get("outcome") else ""),
                source="showings", as_of=shown.get("shown_at"),
            ))
        others = len(strong) - 1
        out.append(Opportunity(
            kind="listing_buyer_match",
            subject=where,
            subject_id=prop.get("lead_id") or row["lead_id"],
            subject_type="lead",
            headline=_say("opp.match.headline", lang, name=best["name"]),
            why=(
                _say("opp.match.why", lang, name=best["name"], n=best["matched_signals"],
                     criteria="; ".join(fits))
                + (_say("opp.match.others_one" if others == 1 else "opp.match.others_many",
                        lang, n=others) if others else ".")
            )[:280],
            recommended_action=_say("opp.match.action", lang, first=first_name),
            confidence=round(min(0.8, 0.2 * best["matched_signals"] + 0.15), 2),
            evidence=evidence,
            safe_to_automate=False,
            action_type="call",
        ))
    return out


#: Detectors run in order; each is independently allowed to find nothing.
_DETECTORS = (
    _contract_deadline_opportunities,
    _intent_model_opportunities,
    _listing_buyer_opportunities,
    _distress_opportunities,
)


async def scan(ctx: TenantContext, lang: Optional[str] = None) -> dict[str, Any]:
    """One pass over everything this tenant can currently be told about.

    `lang` is the language the cards are written in (en/es; None is English).
    It changes sentences only: which cards exist, their order, confidence and
    evidence rows are the same in every language.

    A detector that raises is logged and skipped rather than failing the scan:
    the feed is a read surface, and one broken detector should cost its own
    cards, not the whole morning briefing.
    """
    found: list[Opportunity] = []
    failed: list[str] = []
    async with tenant_tx(ctx) as conn:
        for detector in _DETECTORS:
            try:
                found.extend(await detector(conn, ctx, lang))
            except Exception:  # noqa: BLE001 - see docstring
                logger.exception("Opportunity detector failed: %s", detector.__name__)
                failed.append(detector.__name__)

    kept = [o for o in found if o.confidence >= MIN_CONFIDENCE]
    kept.sort(key=lambda o: o.score(), reverse=True)
    kept = kept[:MAX_OPPORTUNITIES]

    coverage = await perception_coverage(ctx)
    return {
        "opportunities": [o.as_dict() for o in kept],
        "count": len(kept),
        "suppressed_low_confidence": len(found) - len([o for o in found if o.confidence >= MIN_CONFIDENCE]),
        "safe_to_automate": len([o for o in kept if o.safe_to_automate]),
        "perception": coverage,
        "detectors_failed": failed,
        "scanned_at": datetime.now(timezone.utc).isoformat(),
    }
