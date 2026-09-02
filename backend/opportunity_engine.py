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


async def perception_coverage(ctx: TenantContext) -> dict[str, Any]:
    """What this engine can and cannot currently see.

    Reported to the UI rather than hidden. A feed that says "no behavioural
    opportunities" when the truth is "no behavioural data is being captured"
    teaches the agent to distrust the product the first time they find out.
    """
    async with tenant_tx(ctx) as conn:
        rows = await conn.fetch(
            """
            SELECT interaction_type, count(*)::int AS n
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
        "by_type": by_type,
        "clients": clients or 0,
        "clients_with_intent_model": scored or 0,
        # The behavioural detectors need a stream, not a trickle. Naming the
        # threshold means the UI can explain the silence instead of implying
        # there was nothing to find.
        "behavioural_detectors_active": total >= 50,
        "note": (
            "Behavioural detectors stay silent until interaction_logs carries a "
            "real stream. The schema supports it (portal_view, email, message); "
            "the volume is not there yet."
        ),
    }


async def _contract_deadline_opportunities(conn, ctx: TenantContext) -> list[Opportunity]:
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
            subject=r["client_name"] or r["address"] or "Unnamed record",
            subject_id=r["client_id"] or r["id"],
            headline=f"Contract expires in {days} day{'s' if days != 1 else ''}",
            why=(
                f"{r['address'] or 'This property'} has a contract expiring "
                f"{r['contract_expires_at'].date().isoformat()} and its status is "
                f"{r['dossier_status'] or 'unset'}."
            ),
            recommended_action="Confirm the closing timeline with all parties today.",
            confidence=confidence,
            evidence=[Evidence(
                label="Contract expiry",
                value=r["contract_expires_at"].date().isoformat(),
                source="leads.contract_expires_at",
            )],
            # A deadline needs a judgement call about the deal, not a template.
            safe_to_automate=False,
        ))
    return out


async def _intent_model_opportunities(conn, ctx: TenantContext) -> list[Opportunity]:
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
        action_text = first.get("action") if isinstance(first, dict) else str(first)
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
            label="Lead score",
            value=str(r["lead_score"] if r["lead_score"] is not None else "unscored"),
            source="clients.lead_score",
        )]
        if r["summary"]:
            evidence.append(Evidence(
                label="Model summary", value=str(r["summary"])[:200],
                source="client_ai_state.summary",
            ))
        for gap in (gaps or [])[:3]:
            evidence.append(Evidence(
                label="Unknown", value=str(gap)[:120], source="client_ai_state.data_gaps",
            ))
        out.append(Opportunity(
            kind="next_best_action",
            subject=r["full_name"] or "Client",
            subject_id=r["client_id"],
            headline=str(action_text or "Recommended follow-up")[:120],
            why=(r["summary"] or "The maintained intent model for this client "
                 "recommends this as the next step.")[:280],
            recommended_action=str(action_text or "Follow up")[:200],
            confidence=round(confidence, 2),
            evidence=evidence,
            safe_to_automate=False,
        ))
    return out


async def _distress_opportunities(conn, ctx: TenantContext) -> list[Opportunity]:
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
            headline=f"Motivation score {score} — untouched",
            why=(
                f"This record scores {score}/100 on the public-record motivation "
                f"model and is still marked '{r['dossier_status']}'. Nobody has "
                "worked it."
            ),
            recommended_action="Review the dossier and decide whether to source it.",
            confidence=round(confidence, 2),
            evidence=[
                Evidence(label="Motivation score", value=str(score),
                         source="leads.motivation_score"),
                Evidence(label="Last updated",
                         value=r["updated_at"].date().isoformat() if r["updated_at"] else "unknown",
                         source="leads.updated_at"),
            ],
            safe_to_automate=False,
        ))
    return out


#: Detectors run in order; each is independently allowed to find nothing.
_DETECTORS = (
    _contract_deadline_opportunities,
    _intent_model_opportunities,
    _distress_opportunities,
)


async def scan(ctx: TenantContext) -> dict[str, Any]:
    """One pass over everything this tenant can currently be told about.

    A detector that raises is logged and skipped rather than failing the scan:
    the feed is a read surface, and one broken detector should cost its own
    cards, not the whole morning briefing.
    """
    found: list[Opportunity] = []
    failed: list[str] = []
    async with tenant_tx(ctx) as conn:
        for detector in _DETECTORS:
            try:
                found.extend(await detector(conn, ctx))
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
