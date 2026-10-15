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

import semantic_language as lang_

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


#: What a person reads for each search leg that could not be asked.
_SOURCE_NOUNS = {
    "people": "people",
    "properties": "properties",
    "deals": "deals",
    "conversations": "conversations",
    "records": "public records",
}


def _source_noun(kind: str, lang: Optional[str]) -> str:
    key = f"source.{kind}"
    return lang_.t(key, lang) if key in lang_.MESSAGES else _SOURCE_NOUNS.get(kind, kind)


def sources_failed(
    intent: str, query: str, statuses: dict[str, dict[str, Any]],
    lang: Optional[str] = None,
) -> dict[str, Any]:
    """A search the answer depended on did not answer (CONTRACT C6, T05.11).

    A failed leg is not "Nothing matches": that sentence is a verified miss,
    and nothing was verified. The answer names each source that failed and
    how (timed out vs unavailable), carries the per-source status as data,
    and has an evidence block so the surface renders it instead of passing
    the question on as if the lookup had been clean.
    """
    failed = {
        k: v for k, v in statuses.items()
        if (v or {}).get("status") in ("timed_out", "unavailable")
    }
    def how(entry: dict[str, Any]) -> str:
        status = "timed_out" if entry.get("status") == "timed_out" else "unavailable"
        return lang_.t(f"failed.how.{status}", lang)

    names = [_source_noun(k, lang) for k in failed]
    if len(failed) == 1:
        (kind, entry), = failed.items()
        lead = lang_.t("failed.one", lang, source=names[0], query=query, how=how(entry))
    else:
        lead = lang_.t("failed.many", lang, sources=", ".join(names), query=query)
    return {
        "intent": intent,
        "spoken": lead + lang_.t("failed.tail", lang),
        "blocks": [block("evidence", items=[
            {
                "label": lang_.t("failed.label", lang, Source=_source_noun(k, lang).capitalize(),
                                 source=_source_noun(k, lang), how=how(v)),
                "detail": lang_.t("failed.detail", lang),
            }
            for k, v in failed.items()
        ])],
        "fallthrough": False,
        "degraded": True,
        "source_status": statuses,
    }


def no_match(intent: str, query: str, lang: Optional[str] = None) -> dict[str, Any]:
    """A VERIFIED miss: every leg the answer rests on was `checked`. The
    caller attaches the per-source status that proves it."""
    return answer(intent, lang_.t(f"miss.{intent}", lang, query=query), [])


# ---------------------------------------------------------------------------
# Intent renderers
# ---------------------------------------------------------------------------

def who_to_call(briefing: dict[str, Any], lang: Optional[str] = None) -> dict[str, Any]:
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
            _nobody_sentence(briefing, lang),
            [block("metric", **_portfolio_metric(attention, lang))] if attention.get("portfolio") else [],
        )

    spoken = (
        lang_.t("queue.first_one", lang, subject=queue[0]["subject"])
        if len(queue) == 1
        else lang_.t("queue.first_many", lang, subject=queue[0]["subject"], more=len(queue) - 1)
    )
    return answer(
        "who_to_call",
        spoken,
        [
            block("call_queue", items=[_queue_item(o, i) for i, o in enumerate(queue)]),
            block("metric", **_portfolio_metric(attention, lang)),
        ],
    )


def person(record: dict[str, Any], intent: Optional[dict[str, Any]],
           timeline: list[dict[str, Any]], lang: Optional[str] = None) -> dict[str, Any]:
    """One person: who they are, what Neoh reads, what actually happened.

    In Spanish the reading and the dispute questions are re-rendered from
    their structured parts (verdict and scores; the two beliefs), and the
    client type and stage use their Spanish names. The person's name, the
    recorded values and the timeline's own lines are record content and are
    shown as stored."""
    name = record.get("full_name") or lang_.t("person.unnamed", lang)
    latent = (intent or {}).get("latent") or {}
    summary = _intent_summary(intent, lang)
    disputes = (intent or {}).get("disputes") or []

    blocks = [block(
        "person",
        id=str(record.get("id")),
        name=name,
        subtitle=" · ".join(
            _enum_word(prefix, record.get(field), lang)
            for prefix, field in (("client_type", "client_type"), ("stage", "stage"))
            if record.get(field)
        ),
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
            {"label": _dispute_label(d, lang), "detail": d.get("predicate")}
            for d in disputes[:3]
        ]))
    return answer("show_person", summary or f"{name}.", blocks)


def _enum_word(prefix: str, value: Any, lang: Optional[str]) -> str:
    """A client_type / stage value: as stored in English, named in Spanish."""
    key = f"{prefix}.{value}"
    if lang_.lang_or_default(lang) == "en" or key not in lang_.MESSAGES:
        return str(value)
    return lang_.t(key, lang)


def _intent_summary(intent: Optional[dict[str, Any]], lang: Optional[str]) -> Optional[str]:
    """The reading as stored (English), or re-rendered in Spanish from its
    verdict and scores. An unknown verdict keeps the stored sentence."""
    latent = (intent or {}).get("latent") or {}
    summary = latent.get("summary")
    if lang_.lang_or_default(lang) == "en" or not summary:
        return summary
    import intent_states

    declared = ((intent or {}).get("declared") or {}).get("score")
    observed = ((intent or {}).get("observed") or {}).get("score")
    return intent_states.reconcile_summary(
        str(latent.get("verdict")), declared, observed, lang) or summary


def _dispute_label(dispute: dict[str, Any], lang: Optional[str]) -> str:
    if lang_.lang_or_default(lang) == "en":
        return dispute.get("question") or lang_.t("dispute.fallback", lang)
    import belief_store

    return belief_store.dispute_question(dispute, lang) or lang_.t("dispute.fallback", lang)


def person_choices(matches: list[dict[str, Any]], query: str,
                   lang: Optional[str] = None) -> dict[str, Any]:
    """More than one match is a question, not a guess."""
    # The count is of what MATCHED; the list is capped. Saying "6 match" above
    # five cards without a word about the sixth is a small lie of arrangement.
    shown = len(matches[:MAX_QUEUE])
    more = "" if shown == len(matches) else lang_.t("choices.more", lang, shown=shown)
    return answer(
        "show_person",
        lang_.t("choices.people", lang, n=len(matches), query=query, more=more),
        [block("comparison", title=lang_.t("choices.which", lang), options=[
            {
                "id": str(m.get("id")),
                "label": m.get("label") or m.get("full_name") or lang_.t("choices.unnamed", lang),
                "detail": m.get("sublabel"),
                "href": m.get("href") or f"/p/{m.get('id')}",
            }
            for m in matches[:MAX_QUEUE]
        ])],
    )


def properties_found(matches: list[dict[str, Any]], query: str,
                     lang: Optional[str] = None) -> dict[str, Any]:
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
        lang_.t("choices.properties", lang, n=len(matches), query=query),
        [block("comparison", title=lang_.t("choices.which", lang), options=[
            {
                "id": str(m.get("id")),
                "label": m.get("label"),
                "detail": m.get("sublabel"),
                "href": m.get("href") or f"/property/{m.get('id')}",
            }
            for m in matches[:MAX_QUEUE]
        ])],
    )


def properties_for(
    name: str, candidates: list[dict[str, Any]], lookup: Optional[dict[str, Any]] = None,
    lang: Optional[str] = None,
) -> dict[str, Any]:
    """Candidates the automation already maintains — not a new matcher.

    Inventing a second ranking here would give the same client two different
    "best" houses depending on which surface asked.

    `lookup` is the public-record source status the automation recorded
    (client_ai_state.source_status, migration 0135). An empty shortlist after
    a lookup that timed out, failed or never ran is not "nothing matched", and
    is never said as if it were (audit A02).
    """
    # No lookup passed: a caller with no source status (the legacy shape).
    status = lookup.get("status") if lookup is not None else "checked"
    if not candidates and status in ("timed_out", "unavailable"):
        last = lookup.get("last_checked_at")
        how = lang_.t("shortlist.how." + ("timed_out" if status == "timed_out" else "failed"), lang)
        return answer(
            "properties_for",
            lang_.t("shortlist.failed", lang, name=name, how=how)
            + (lang_.t("shortlist.last", lang, date=str(last)[:10]) if last else ""),
            [],
        )
    if not candidates and status not in ("checked", "not_applicable"):
        return answer("properties_for", lang_.t("shortlist.unchecked", lang, name=name), [])
    if not candidates:
        return answer("properties_for", lang_.t("shortlist.empty", lang, name=name), [])
    shown = candidates[:MAX_COMPARISON]
    return answer(
        "properties_for",
        lang_.t("shortlist.count", lang, n=len(shown), name=name),
        [block("comparison", title=lang_.t("shortlist.title", lang, name=name), options=[
            {
                "id": str(c.get("id") or c.get("property_id") or ""),
                "label": c.get("address") or lang_.t("shortlist.no_address", lang),
                "detail": _property_detail(c, lang),
                "href": f"/property/{c.get('id') or c.get('property_id')}" if (c.get("id") or c.get("property_id")) else None,
            }
            for c in shown
        ])],
    )


def deal_blocker(transaction: dict[str, Any], milestones: list[dict[str, Any]],
                 lang: Optional[str] = None) -> dict[str, Any]:
    """What is holding a deal up is a fact on a row, not an opinion.

    The milestone titles and types are the brokerage's own words (free text
    on the row) and are never translated; only the sentence around them is."""
    stored = transaction.get("property_address")
    address = stored or lang_.t("deal.this", lang)
    # Inside a Spanish sentence the fallback is lower case ("en este trato").
    inline = stored or lang_.t("deal.this_inline", lang)
    open_ones = [
        m for m in milestones
        if not m.get("completed_at") and m.get("status") not in ("completed", "skipped")
    ]
    open_ones.sort(key=lambda m: (m.get("due_at") or "9999"))

    if not milestones:
        spoken = lang_.t("deal.no_milestones", lang, address=inline, Address=address)
    elif not open_ones:
        spoken = lang_.t("deal.all_done", lang, address=inline)
    else:
        nxt = open_ones[0]
        title = (nxt.get("title") or (nxt.get("milestone_type") or "").replace("_", " ")
                 or lang_.t("deal.next_milestone", lang))
        spoken = lang_.t("deal.next_open", lang, title=title.strip().capitalize(), address=inline)

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


def _portfolio_metric(attention: dict[str, Any], lang: Optional[str] = None) -> dict[str, Any]:
    portfolio = attention.get("portfolio") or {}
    return {
        "label": lang_.t("queue.portfolio_label", lang),
        "value": portfolio.get("total"),
        "unit": "currency",
        # The caveat travels as a prop so it cannot be dropped in rendering.
        # A number this uncertain shown bare is the false precision the
        # expected-value module exists to refuse.
        "caveat": _portfolio_caveat(portfolio, lang),
        "calibrated": portfolio.get("calibrated", False),
    }


def _portfolio_caveat(portfolio: dict[str, Any], lang: Optional[str]) -> Optional[str]:
    """expected_value.portfolio's caveat: as given in English, re-rendered in
    Spanish from the same counts. Without the counts the given text stands —
    a caveat is never dropped for want of a translation."""
    caveat = portfolio.get("caveat")
    if lang_.lang_or_default(lang) == "en" or not caveat:
        return caveat
    import expected_value

    outcomes = portfolio.get("outcomes_observed")
    if not isinstance(outcomes, int):
        return caveat
    return expected_value.portfolio_caveat(bool(portfolio.get("calibrated")), outcomes, lang)


def _nobody_sentence(briefing: dict[str, Any], lang: Optional[str] = None) -> str:
    """An empty queue has three different causes and they must not read alike."""
    suppressed = briefing.get("suppressed_low_confidence") or 0
    failed = briefing.get("detectors_failed") or []
    if failed:
        key = "nobody.failed_one" if len(failed) == 1 else "nobody.failed_many"
        return lang_.t(key, lang, n=len(failed))
    if suppressed:
        key = "nobody.suppressed_one" if suppressed == 1 else "nobody.suppressed_many"
        return lang_.t(key, lang, n=suppressed)
    return lang_.t("nobody.quiet", lang)


def _property_detail(candidate: dict[str, Any], lang: Optional[str] = None) -> Optional[str]:
    bits = []
    price = candidate.get("price") or candidate.get("list_price")
    if price:
        try:
            bits.append(f"${float(price):,.0f}")
        except (TypeError, ValueError):
            pass
    for key in ("beds", "baths"):
        if candidate.get(key):
            bits.append(lang_.t(f"detail.{key}", lang, n=candidate[key]))
    if candidate.get("match_reason"):
        bits.append(str(candidate["match_reason"]))
    return " · ".join(bits) or None
