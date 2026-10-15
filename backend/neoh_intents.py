"""neoh_intents — what the ⌘K box understood, decided in code.

The universal input has to render an interface, not answer with a paragraph.
That only works if the question is classified by something deterministic:
these are ordered regular expressions, each with a resolver that calls
services this codebase already has and hands the result to `neoh_render`,
which is pure.

**The model is not in this path at all.** It is not asked to classify, and it
is certainly not asked to emit UI — a model that can emit markup can emit a
fabricated number inside a heading, and once rendered that is
indistinguishable from a real one. What the model still does is answer
everything these patterns do NOT cover: a miss returns `fallthrough`, and the
frontend then sends the same text down the existing chat channel. So the
fixed vocabulary never becomes a cage; it is a fast, honest path in front of
a general one.

Adding an intent means adding a pattern and a resolver here, and a primitive
the registry already draws. If a question needs a primitive that does not
exist, the answer is to add it to the vocabulary on purpose — not to let the
model improvise one.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Awaitable, Callable, Optional

import neoh_render as render
import semantic_language as lang_
import source_status as ss
from tenancy import TenantContext

logger = logging.getLogger(__name__)

MAX_TEXT = 400

#: How many people a name may match before Neoh asks instead of guessing.
NAME_MATCH_LIMIT = 5


class Intent:
    """One pattern and the resolver it hands its captures to.

    `lang` is the language the pattern is written in (en | es); the resolver
    answers in it, so a Spanish command gets a Spanish failure or miss."""

    __slots__ = ("name", "pattern", "resolve", "lang")

    def __init__(self, name: str, pattern: str, resolve: Callable[..., Awaitable[dict]],
                 lang: str = "en"):
        self.name = name
        self.pattern = re.compile(pattern, re.IGNORECASE)
        self.resolve = resolve
        self.lang = lang


_ACCENTED = {"á": "[aá]", "é": "[eé]", "í": "[ií]", "ó": "[oó]", "ú": "[uúü]", "ñ": "[nñ]"}


def _es(pattern: str) -> str:
    """A Spanish pattern that also matches its words typed without accents
    ("muestrame", "quien"). Only accented letters written in the pattern are
    widened; everything else is matched as written."""
    return "".join(_ACCENTED.get(ch, ch) for ch in pattern)


# ---------------------------------------------------------------------------
# Search legs: a failed leg is reported, never read as "nothing matches"
# ---------------------------------------------------------------------------

def _leg_status(found: dict[str, Any], kind: str) -> dict[str, Any]:
    """The C6 status of one search leg (search_api.search `source_status`).

    A leg named in `degraded`, or one with no recognisable status at all, is
    a failure: an answer that cannot show the leg was `checked` must not say
    the leg found nothing (T05.11, AC05.07).
    """
    entry = (found.get("source_status") or {}).get(kind)
    if kind in (found.get("degraded") or []) and not ss.is_failed(entry):
        return ss.unavailable(ss.UPSTREAM_ERROR)
    if not isinstance(entry, dict) or entry.get("status") not in ss.STATUSES:
        return ss.unavailable(ss.STATUS_MISSING)
    return dict(entry)


def _with_status(out: dict[str, Any], statuses: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Attach the per-source status the answer rests on."""
    return {**out, "degraded": ss.degraded(statuses), "source_status": statuses}


async def _search(ctx: TenantContext, query: str, kind: str) -> tuple[list[dict], dict[str, Any]]:
    """One leg's hits and its status. Runs in the caller's own RLS context."""
    import search_api

    found = await search_api.search(ctx, query, [kind], NAME_MATCH_LIMIT)
    hits = [hit for hit in found.get("results", []) if hit.get("kind") == kind]
    return hits, _leg_status(found, kind)


# ---------------------------------------------------------------------------
# Resolvers
# ---------------------------------------------------------------------------

async def _who_to_call(ctx: TenantContext, _match: re.Match, lang: str = "en") -> dict[str, Any]:
    """Reuses the Command Center's briefing verbatim.

    Ranking it again here would give the same card two different positions in
    one product depending on which surface asked — the exact double-count the
    single-source rule exists to prevent.
    """
    import command_center

    briefing = await command_center.briefing(ctx)
    return render.who_to_call(briefing, lang)


async def _show_person(ctx: TenantContext, match: re.Match, lang: str = "en") -> dict[str, Any]:
    import search_api

    query = (match.group("name") or "").strip()
    if len(query) < 2:
        return render.fallthrough("name too short to resolve")

    matches, people = await _search(ctx, query, "people")
    statuses = {"people": people}
    if ss.is_failed(people):
        return render.sources_failed("show_person", query, statuses, lang)
    if not matches:
        # "Show 412 Delaware" is the same grammar pointing at a house. People
        # come first because a person named like a street is still a person;
        # this only runs when nobody matched AND the text reads as an address.
        if search_api.looks_like_address(query):
            properties, prop_status = await _search(ctx, query, "properties")
            statuses["properties"] = prop_status
            if ss.is_failed(prop_status):
                return render.sources_failed("show_person", query, statuses, lang)
            if properties:
                return _with_status(render.properties_found(properties, query, lang), statuses)
        # Every leg asked was `checked`: this is a verified miss.
        return _with_status(render.no_match("show_person", query, lang), statuses)
    if len(matches) > 1:
        return _with_status(render.person_choices(matches, query, lang), statuses)

    client_id = matches[0].get("id")
    record, intent, timeline = await _person_detail(ctx, client_id, lang)
    if record is None:
        # A search hit that will not load is a contact, not a client record —
        # say so rather than rendering an empty person card.
        return render.answer(
            "show_person",
            lang_.t("person.contact_only", lang, label=matches[0].get("label")),
            [],
        )
    return _with_status(render.person(record, intent, timeline, lang), statuses)


async def _properties_for(ctx: TenantContext, match: re.Match, lang: str = "en") -> dict[str, Any]:
    query = (match.group("name") or "").strip()
    if len(query) < 2:
        return render.fallthrough("name too short to resolve")

    matches, people = await _search(ctx, query, "people")
    statuses = {"people": people}
    if ss.is_failed(people):
        return render.sources_failed("properties_for", query, statuses, lang)
    if not matches:
        return _with_status(render.no_match("properties_for", query, lang), statuses)
    if len(matches) > 1:
        return _with_status(render.person_choices(matches, query, lang), statuses)

    name = matches[0].get("label") or query
    candidates, lookup = await _candidates_for(ctx, matches[0].get("id"))
    return _with_status(render.properties_for(name, candidates, lookup, lang), statuses)


async def _deal_blocker(ctx: TenantContext, match: re.Match, lang: str = "en") -> dict[str, Any]:
    query = (match.group("deal") or "").strip()
    if len(query) < 2:
        return render.fallthrough("no deal named")

    matches, deals = await _search(ctx, query, "deals")
    statuses = {"deals": deals}
    if ss.is_failed(deals):
        return render.sources_failed("deal_blocker", query, statuses, lang)
    if not matches:
        return _with_status(render.no_match("deal_blocker", query, lang), statuses)
    if len(matches) > 1:
        return _with_status(render.answer(
            "deal_blocker",
            lang_.t("choices.deals", lang, n=len(matches), query=query),
            [render.block("comparison", title=lang_.t("choices.which_deal", lang), options=[
                {
                    "id": str(m.get("id")),
                    "label": m.get("label"),
                    "detail": m.get("sublabel"),
                    "href": f"/deal/{m.get('id')}",
                }
                for m in matches
            ])],
        ), statuses)

    transaction, milestones = await _deal_detail(ctx, matches[0].get("id"))
    if transaction is None:
        return render.answer("deal_blocker", lang_.t("deal.not_loaded", lang), [])
    return _with_status(render.deal_blocker(transaction, milestones, lang), statuses)


# ---------------------------------------------------------------------------
# The table. Order matters: the first pattern that matches wins, so the more
# specific questions are listed before the ones that would also swallow them.
# ---------------------------------------------------------------------------

#: What a Spanish deal question names. "el trato de Delaware" and "la venta
#: de 155 Main" name Delaware and 155 Main; the noun phrase is not the deal.
_DEAL_ES = (
    r"(?:(?:a\s+)?(?:el|la|al)\s+(?:trato|operación|negocio|cierre|venta|compra|deal)\s+"
    r"(?:de(?:l)?\s+(?:la\s+|el\s+)?|en\s+)|al\s+|a\s+)?(?P<deal>.+?)\s*\??$"
)

INTENTS: tuple[Intent, ...] = (
    # "find properties for Sarah" must beat "show Sarah", which would match too.
    Intent(
        "properties_for",
        r"^\s*(?:find|show|get|pull|what)\s+(?:me\s+)?(?:some\s+)?"
        r"(?:propert(?:y|ies)|houses?|homes?|listings?)\s+(?:for|to)\s+(?P<name>.+?)\s*\??$",
        _properties_for,
    ),
    # "busca casas para Sarah", "muéstrame propiedades para Sarah".
    Intent(
        "properties_for",
        _es(r"^\s*¿?\s*(?:búsca(?:me|le)?|encuéntra(?:me|le)?|muéstra(?:me|le)?|enséña(?:me|le)?|"
            r"dame|tráe(?:me|le)?|qué|cuáles)\s+(?:(?:las|los|unas|unos|algunas|algunos)\s+)?"
            r"(?:propiedad(?:es)?|casas?|viviendas?|inmuebles?|listados?|anuncios?)\s+"
            r"(?:hay\s+)?para\s+(?P<name>.+?)\s*\??$"),
        _properties_for, "es",
    ),
    Intent(
        "deal_blocker",
        r"^\s*(?:what(?:'s| is| are)?\s+)?(?:holding|blocking|stuck|stopping|"
        r"waiting\s+on|hold(?:ing)?\s+up)\s*(?:on\s+|up\s+)?(?P<deal>.+?)\s*(?:up)?\s*\??$",
        _deal_blocker,
    ),
    # "¿qué está frenando 155 Main?", "¿por qué está atascado el trato de Delaware?"
    Intent(
        "deal_blocker",
        _es(r"^\s*¿?\s*(?:(?:qué|que)\s+)?(?:(?:está|están)\s+)?"
            r"(?:frenando|deteniendo|bloqueando|atrasando|retrasando|parando|atascando|"
            r"frena|detiene|bloquea|atrasa|retrasa)\s+" + _DEAL_ES),
        _deal_blocker, "es",
    ),
    Intent(
        "deal_blocker",
        _es(r"^\s*¿?\s*(?:(?:en|por)\s+qué\s+)?(?:está|están|sigue|siguen)\s+"
            r"(?:atascad[oa]s?|estancad[oa]s?|parad[oa]s?|frenad[oa]s?|detenid[oa]s?)\s+" + _DEAL_ES),
        _deal_blocker, "es",
    ),
    Intent(
        "who_to_call",
        r"^\s*(?:who|whom)\s+(?:should|do|can|must)\s+i\s+"
        r"(?:call|ring|phone|contact|reach|talk\s+to|follow\s+up\s+with)"
        r"(?:\s+.*)?\??\s*$",
        _who_to_call,
    ),
    # "¿A quién debo llamar?", "¿con quién tengo que hacer seguimiento?"
    Intent(
        "who_to_call",
        _es(r"^\s*¿?\s*(?:a|con)\s+quién(?:es)?\s+(?:le\s+|les\s+)?"
            r"(?:(?:debo|debería|debemos|deberíamos|tengo\s+que|tenemos\s+que|puedo|hay\s+que|me\s+toca)\s+)?"
            r"(?:llamar|llamo|contactar|contacto|hablar|hablo|telefonear|"
            r"(?:dar|doy|hacer|hago)(?:le|les)?\s+seguimiento|seguir)"
            r"(?:\s+.*)?\??\s*$"),
        _who_to_call, "es",
    ),
    # The bare imperative form of the same question.
    Intent(
        "who_to_call",
        r"^\s*(?:my\s+)?(?:call\s+list|call\s+queue|who\s+to\s+call|"
        r"next\s+best\s+(?:action|call)s?)\s*\??\s*$",
        _who_to_call,
    ),
    Intent(
        "who_to_call",
        _es(r"^\s*¿?\s*(?:mi\s+)?(?:lista\s+de\s+llamadas|cola\s+de\s+llamadas|a\s+quién\s+llamar|"
            r"(?:la\s+)?(?:siguiente|próxima)\s+mejor\s+(?:acción|llamada)|"
            r"(?:las\s+)?(?:mejores\s+)?próximas\s+(?:acciones|llamadas))\s*\??\s*$"),
        _who_to_call, "es",
    ),
    Intent(
        "show_person",
        r"^\s*(?:show|open|pull\s+up|bring\s+up|tell\s+me\s+about|who\s+is)\s+"
        r"(?:me\s+)?(?P<name>.+?)\s*\??$",
        _show_person,
    ),
    # "muéstrame a Sarah Chen", "háblame de Marcus", "¿quién es Dana?"
    Intent(
        "show_person",
        _es(r"^\s*¿?\s*(?:muéstra(?:me|le)?|ábre(?:me|le)?|enséña(?:me|le)?|tráe(?:me|le)?|"
            r"háblame\s+(?:de|sobre)|cuéntame\s+(?:de|sobre)|quién\s+es)\s+"
            r"(?:(?:a|al)\s+)?(?P<name>.+?)\s*\??$"),
        _show_person, "es",
    ),
)


#: Intents whose answer ignores whatever is on screen. "Who should I call?"
#: from Home is the tenant-wide call queue; the same words asked with a
#: property open mean "who should I call about THIS house", and answering with
#: the generic queue was a confident answer to a different question.
RECORD_AGNOSTIC = frozenset({"who_to_call"})


async def ask(ctx: TenantContext, text: str, *, has_record: bool = False,
              lang_hint: Optional[str] = None) -> dict[str, Any]:
    """Classify, resolve, render. Never raises; a failure falls through.

    A resolver that breaks must not cost the person their question: the text
    still reaches the model, which is what would have happened without this
    module at all.

    `has_record`: a record (property, client, ...) is open. Intents that would
    answer without it fall through to the model, which is given the record.

    `lang_hint`: the language a spoken question was recognised in (L05-05).
    It never overrides the answer language here: a pattern only matches words
    of its own language, so the pattern that matched is already conclusive
    text evidence, and the text wins when it is confident. The hint matters on
    the chat path, for turns too short to tell (semantic_turn.prepare_turn).
    """
    cleaned = (text or "").strip()[:MAX_TEXT]
    if not cleaned:
        return render.fallthrough("empty")

    for intent in INTENTS:
        match = intent.pattern.match(cleaned)
        if not match:
            continue
        if has_record and intent.name in RECORD_AGNOSTIC:
            return render.fallthrough(f"{intent.name} asked about the open record")
        try:
            return await intent.resolve(ctx, match, intent.lang)
        except Exception:  # noqa: BLE001 — a broken resolver degrades to chat
            # The request can name a customer; log its size, not its words (C5.4).
            logger.exception("neoh intent %s failed (request of %d chars)", intent.name, len(cleaned))
            return render.fallthrough(f"resolver {intent.name} failed")
    return render.fallthrough("no pattern matched")


# ---------------------------------------------------------------------------
# Data access. Kept here rather than in the renderer so the renderer stays
# pure and testable without a database.
# ---------------------------------------------------------------------------

def _interaction_word(kind: Optional[str], lang: str) -> str:
    """A timeline row's type, shown when it has no subject line: the stored
    machine name in English (as always), its Spanish name where one exists."""
    raw = (kind or "").replace("_", " ")
    key = f"interaction.{kind}"
    if lang_.lang_or_default(lang) == "en" or key not in lang_.MESSAGES:
        return raw
    return lang_.t(key, lang)


async def _person_detail(
    ctx: TenantContext, client_id: Optional[str], lang: str = "en",
) -> tuple[Optional[dict], Optional[dict], list[dict]]:
    import intent_states
    from db.connection import tenant_tx

    if not client_id:
        return None, None, []

    async with tenant_tx(ctx) as conn:
        # No tenant predicate: RLS scopes this, and repeating half the policy
        # would hide the row from a platform admin.
        record = await conn.fetchrow(
            """SELECT id, full_name, client_type, stage, email, phone, company
                 FROM clients WHERE id=$1::uuid""",
            client_id,
        )
        if record is None:
            return None, None, []
        import sealed_text  # lazy: crypto pulls in tenancy

        # 0154 (A01): the subject is sealed at rest; open both shapes.
        rows = await sealed_text.open_rows(conn, await conn.fetch(
            """SELECT interaction_type, direction, created_at, subject,
                      subject_ciphertext, tenant_id AS subject_ciphertext_tenant_id
                 FROM interaction_logs
                WHERE client_id=$1::uuid
                ORDER BY created_at DESC
                LIMIT 5""",
            client_id,
        ), sealed_text.LOG_SUBJECT)

    timeline = [
        {
            # `subject` is the human line where one exists (an email subject,
            # a call note); the type is the honest fallback. There is no
            # `summary` column and no `occurred_at` — this table records when
            # it was written, in `created_at`.
            "label": (r["subject"] or _interaction_word(r["interaction_type"], lang)),
            "at": r["created_at"].isoformat() if r["created_at"] else None,
            "direction": r["direction"],
            "done": True,
        }
        for r in rows
    ]

    intent: Optional[dict[str, Any]] = None
    try:
        intent = await intent_states.read_intent(ctx, str(client_id))
    except Exception:  # noqa: BLE001 — the card is still worth showing
        logger.exception("read_intent failed for %s", client_id)

    return dict(record), intent, timeline


async def _candidates_for(
    ctx: TenantContext, client_id: Optional[str],
) -> tuple[list[dict], dict]:
    """The shortlist the automation already maintains, not a new matcher —
    with the public-record source status that says what an empty one means.

    `{}` for the status means nothing was recorded (no reconciliation yet, or
    a row from before migration 0135): never read as a checked zero.
    """
    import client_ai_automation
    from db.connection import tenant_tx

    if not client_id:
        return [], {}
    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            "SELECT * FROM client_ai_state WHERE client_id=$1::uuid",
            client_id,
        )
    if row is None:
        return [], {}
    state = client_ai_automation.automation_state_json(row)
    candidates = state.get("property_candidates") or []
    lookup = (state.get("source_status") or {}).get(client_ai_automation.PROPERTY_SOURCE)
    return (
        [c for c in candidates if isinstance(c, dict)],
        lookup if isinstance(lookup, dict) else {},
    )


async def _deal_detail(
    ctx: TenantContext, transaction_id: Optional[str],
) -> tuple[Optional[dict], list[dict]]:
    from db.connection import tenant_tx

    if not transaction_id:
        return None, []
    async with tenant_tx(ctx) as conn:
        transaction = await conn.fetchrow(
            "SELECT * FROM transactions WHERE id=$1::uuid", transaction_id,
        )
        if transaction is None:
            return None, []
        milestones = await conn.fetch(
            """SELECT milestone_type, title, status, due_at, completed_at
                 FROM transaction_milestones
                WHERE transaction_id=$1::uuid
                ORDER BY due_at NULLS LAST, created_at""",
            transaction_id,
        )
    return dict(transaction), [dict(m) for m in milestones]
