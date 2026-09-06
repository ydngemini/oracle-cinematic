"""neoh_render — service results in, UI primitives out. Pure.

The model never emits UI. It cannot: nothing in this module calls a model, and
the only thing the /api/neoh/ask path returns is a list of
`{primitive, props}` drawn from a CLOSED vocabulary the frontend registry
knows how to draw. A model that could emit markup could emit anything —
a fabricated number in a heading is indistinguishable from a real one once
it is rendered — so the generative half of the UI generates *arrangement*,
never content and never markup.

Every function here is a pure transform of already-fetched data. That is what
makes the wording testable without a database, and it is why the caveats
below cannot be dropped in passing: they are returned as props, not written
into prose somewhere downstream.
"""

from __future__ import annotations

from typing import Any, Optional

#: The whole vocabulary. A primitive the registry cannot draw is a bug here,
#: not a blank panel there — the frontend logs an unknown primitive and skips
#: it, and this list is what the two sides agree on.
PRIMITIVES = (
    "person", "property", "deal",
    "opportunity", "call_queue", "comparison",
    "timeline", "evidence", "metric",
    "approval", "receipt", "mission",
)

#: Ranked lists get a ceiling. "Who should I call" answered with forty names
#: is the six-tab shell again in one panel.
MAX_QUEUE = 5
MAX_COMPARISON = 3


def block(primitive: str, **props: Any) -> dict[str, Any]:
    if primitive not in PRIMITIVES:
        raise ValueError(f"unknown primitive {primitive!r}")
    return {"primitive": primitive, "props": props}


def answer(
    intent: str, spoken: str, blocks: list[dict[str, Any]],
) -> dict[str, Any]:
    """One answer: a sentence a person could read aloud, then the blocks.

    `spoken` is never the whole answer and never restates the blocks — it says
    what was found and, where it matters, what it is not.
    """
    return {"intent": intent, "spoken": spoken, "blocks": blocks, "fallthrough": False}


def fallthrough(reason: str) -> dict[str, Any]:
    """No intent matched. The frontend sends the text to the model instead.

    This is the honest default: a question this module does not understand
    must reach something that might, not produce an empty panel.
    """
    return {"intent": None, "spoken": "", "blocks": [], "fallthrough": True, "reason": reason}


# ---------------------------------------------------------------------------
# Intent renderers
# ---------------------------------------------------------------------------

def who_to_call(briefing: dict[str, Any]) -> dict[str, Any]:
    """The product's core question, rendered as a queue rather than a paragraph.

    Ordering is the briefing's own expected-value ordering — the same numbers
    the Command Center shows, because the same card must not be ranked two
    ways in one product.
    """
    attention = briefing.get("attention") or {}
    opportunities = [o for o in (attention.get("opportunities") or []) if o]
    callable_now = [
        o for o in opportunities
        if (o.get("action_type") or "call") in ("call", "sms", "email")
    ]
    callable_now.sort(key=_expected_value, reverse=True)
    queue = callable_now[:MAX_QUEUE]

    if not queue:
        return answer(
            "who_to_call",
            _nobody_sentence(briefing),
            [block("metric", **_portfolio_metric(attention))] if attention.get("portfolio") else [],
        )

    spoken = (
        f"{queue[0]['subject']} first."
        if len(queue) == 1
        else f"{queue[0]['subject']} first, then {len(queue) - 1} more."
    )
    return answer(
        "who_to_call",
        spoken,
        [
            block("call_queue", items=[_queue_item(o, i) for i, o in enumerate(queue)]),
            block("metric", **_portfolio_metric(attention)),
        ],
    )


def person(record: dict[str, Any], intent: Optional[dict[str, Any]],
           timeline: list[dict[str, Any]]) -> dict[str, Any]:
    """One person: who they are, what Neoh reads, what actually happened."""
    name = record.get("full_name") or "This person"
    latent = (intent or {}).get("latent") or {}
    summary = latent.get("summary")
    disputes = (intent or {}).get("disputes") or []

    blocks = [block(
        "person",
        id=str(record.get("id")),
        name=name,
        subtitle=" · ".join(x for x in (record.get("client_type"), record.get("stage")) if x),
        read=summary,
        confidence=latent.get("confidence"),
        href=f"/p/{record.get('id')}",
    )]
    if timeline:
        blocks.append(block("timeline", items=timeline[:5]))
    if disputes:
        # A contradiction is the most useful thing on this panel: it is the one
        # item a single phone call can resolve.
        blocks.append(block("evidence", items=[
            {"label": d.get("question") or "Two records disagree", "detail": d.get("predicate")}
            for d in disputes[:3]
        ]))
    return answer("show_person", summary or f"{name}.", blocks)


def person_choices(matches: list[dict[str, Any]], query: str) -> dict[str, Any]:
    """More than one match is a question, not a guess."""
    # The count is of what MATCHED; the list is capped. Saying "6 match" above
    # five cards without a word about the sixth is a small lie of arrangement.
    shown = len(matches[:MAX_QUEUE])
    more = "" if shown == len(matches) else f" Showing the first {shown}."
    return answer(
        "show_person",
        f"{len(matches)} people match “{query}”.{more}",
        [block("comparison", title="Which one?", options=[
            {
                "id": str(m.get("id")),
                "label": m.get("label") or m.get("full_name") or "Unnamed",
                "detail": m.get("sublabel"),
                "href": m.get("href") or f"/p/{m.get('id')}",
            }
            for m in matches[:MAX_QUEUE]
        ])],
    )


def properties_found(matches: list[dict[str, Any]], query: str) -> dict[str, Any]:
    """"Show 412 Delaware" is a property question wearing a person's grammar.

    The same words open a person or a house depending on what exists, so the
    resolver tries people first and lands here when the text looks like an
    address and no person matched.
    """
    if len(matches) == 1:
        hit = matches[0]
        return answer(
            "show_property",
            hit.get("label") or query,
            [block(
                "property",
                id=str(hit.get("id")),
                address=hit.get("label"),
                detail=hit.get("sublabel"),
                href=hit.get("href") or f"/property/{hit.get('id')}",
            )],
        )
    return answer(
        "show_property",
        f"{len(matches)} properties match “{query}”.",
        [block("comparison", title="Which one?", options=[
            {
                "id": str(m.get("id")),
                "label": m.get("label"),
                "detail": m.get("sublabel"),
                "href": m.get("href") or f"/property/{m.get('id')}",
            }
            for m in matches[:MAX_QUEUE]
        ])],
    )


def properties_for(name: str, candidates: list[dict[str, Any]]) -> dict[str, Any]:
    """Candidates the automation already maintains — not a new matcher.

    Inventing a second ranking here would give the same client two different
    "best" houses depending on which surface asked.
    """
    if not candidates:
        return answer(
            "properties_for",
            f"Nothing is on {name}'s shortlist yet. It fills in as they browse "
            f"and as the criteria on their record get specific enough to match on.",
            [],
        )
    shown = candidates[:MAX_COMPARISON]
    return answer(
        "properties_for",
        f"{len(shown)} on {name}'s shortlist.",
        [block("comparison", title=f"For {name}", options=[
            {
                "id": str(c.get("id") or c.get("property_id") or ""),
                "label": c.get("address") or "Address unavailable",
                "detail": _property_detail(c),
                "href": f"/property/{c.get('id') or c.get('property_id')}" if (c.get("id") or c.get("property_id")) else None,
            }
            for c in shown
        ])],
    )


def deal_blocker(transaction: dict[str, Any], milestones: list[dict[str, Any]]) -> dict[str, Any]:
    """What is holding a deal up is a fact on a row, not an opinion."""
    address = transaction.get("property_address") or "This deal"
    open_ones = [
        m for m in milestones
        if not m.get("completed_at") and m.get("status") not in ("completed", "skipped")
    ]
    open_ones.sort(key=lambda m: (m.get("due_at") or "9999"))

    if not milestones:
        spoken = (
            f"{address} has no milestones recorded, so nothing here can say what "
            f"is next. That is a gap in the file, not a clear runway."
        )
    elif not open_ones:
        spoken = f"Nothing is holding {address} up — every milestone is done."
    else:
        nxt = open_ones[0]
        title = nxt.get("title") or (nxt.get("milestone_type") or "").replace("_", " ") or "the next milestone"
        spoken = f"{title.strip().capitalize()} is the earliest thing still open on {address}."

    blocks = [block(
        "deal",
        id=str(transaction.get("id")),
        address=address,
        status=transaction.get("status"),
        open_count=len(open_ones),
        total_count=len(milestones),
        href=f"/deal/{transaction.get('id')}",
    )]
    if milestones:
        blocks.append(block("timeline", items=[
            {
                "label": m.get("title") or (m.get("milestone_type") or "").replace("_", " "),
                "at": m.get("due_at"),
                "done": bool(m.get("completed_at")),
            }
            for m in (open_ones or milestones)[:5]
        ]))
    return answer("deal_blocker", spoken, blocks)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _expected_value(opportunity: dict[str, Any]) -> float:
    economics = opportunity.get("economics") or {}
    try:
        return float(economics.get("expected_value") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _queue_item(opportunity: dict[str, Any], index: int) -> dict[str, Any]:
    subject_type = opportunity.get("subject_type") or "client"
    subject_id = opportunity.get("subject_id")
    return {
        "rank": index + 1,
        "subject": opportunity.get("subject"),
        "subject_id": subject_id,
        "subject_type": subject_type,
        "headline": opportunity.get("headline"),
        "why": opportunity.get("why"),
        "action": opportunity.get("recommended_action"),
        "action_type": opportunity.get("action_type") or "call",
        "confidence": opportunity.get("confidence"),
        "kind": opportunity.get("kind"),
        "deadline": opportunity.get("deadline"),
        "href": f"/p/{subject_id}" if subject_type == "client" and subject_id else None,
    }


def _portfolio_metric(attention: dict[str, Any]) -> dict[str, Any]:
    portfolio = attention.get("portfolio") or {}
    return {
        "label": "Expected value of acting on all of it",
        "value": portfolio.get("total"),
        "unit": "currency",
        # The caveat travels as a prop so it cannot be dropped in rendering.
        # A number this uncertain shown bare is the false precision the
        # expected-value module exists to refuse.
        "caveat": portfolio.get("caveat"),
        "calibrated": portfolio.get("calibrated", False),
    }


def _nobody_sentence(briefing: dict[str, Any]) -> str:
    """An empty queue has three different causes and they must not read alike."""
    suppressed = briefing.get("suppressed_low_confidence") or 0
    failed = briefing.get("detectors_failed") or []
    if failed:
        return (
            f"No one to call — but {len(failed)} detector"
            f"{'s' if len(failed) != 1 else ''} failed this pass, so this is "
            f"an incomplete answer rather than a quiet day."
        )
    if suppressed:
        return (
            f"No one worth calling yet. {suppressed} possibilit"
            f"{'ies were' if suppressed != 1 else 'y was'} found but held back "
            f"as too uncertain to put a name to."
        )
    return "No one to call right now. Nothing in the book is asking for you today."


def _property_detail(candidate: dict[str, Any]) -> Optional[str]:
    bits = []
    price = candidate.get("price") or candidate.get("list_price")
    if price:
        try:
            bits.append(f"${float(price):,.0f}")
        except (TypeError, ValueError):
            pass
    for key in ("beds", "baths"):
        if candidate.get(key):
            bits.append(f"{candidate[key]} {key}")
    if candidate.get("match_reason"):
        bits.append(str(candidate["match_reason"]))
    return " · ".join(bits) or None
