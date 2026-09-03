"""One search across people, properties, deals and conversations.

Work has one box. Behind it there were six independent client-side filters,
each over an already-loaded list, so "Sarah" found her in People and nowhere
else, and "155 Main" found nothing unless the right tab was already open.

This is a fan-out over the service functions that already exist, in one
round trip, with two properties the six filters never had:

**Every leg runs, and a failed leg is named.** `asyncio.gather` with
`return_exceptions=True`, and the response carries `degraded: [kind, ...]`
for any leg that raised. A search that silently returns three of four kinds
is a lie about the corpus; an agent who cannot find a deal needs to know
whether it is absent or whether the deals leg fell over.

**One hit shape.** `{kind, id, label, sublabel, href, score}` for every kind,
so the surface that renders results has no per-kind branch and a generative
primitive can show any hit without knowing what it is.

No new index and no migration. Contacts are blind-indexed (there is no
plaintext name to trigram), clients and transactions are small per tenant,
and the one large corpus — public_property_records, 8.5 million rows behind
a gin_trgm_ops index from 0050 — is an opt-in leg that a bare query does not
touch unless it looks like an address. Service functions are called
directly; nothing here makes an HTTP request to itself.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Awaitable, Callable, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from db.connection import tenant_tx
from tenancy import TenantContext, require_context

logger = logging.getLogger("oracle.search_api")

router = APIRouter(prefix="/api/search", tags=["search"])

KINDS: tuple[str, ...] = ("people", "properties", "deals", "conversations", "records")

#: Kinds a bare query runs. `records` — the 8.5-million-row public corpus —
#: is NOT among them: a person's name searched across every parcel in the
#: state is a question nobody asked, and the first live probe showed that
#: leg exhausting its budget on every term, nonsense included. It joins the
#: default set only when the query looks like an address (see
#: `looks_like_address`) or when the caller asks for it by name.
DEFAULT_KINDS: tuple[str, ...] = ("people", "properties", "deals", "conversations")


def looks_like_address(q: str) -> bool:
    """A digit followed by a word is how nearly every street address starts.
    Deliberately crude: a false positive costs one budgeted query, a false
    negative costs a public-records hit the tenant's own leads leg may still
    supply."""
    return bool(re.search(r"\d+\s+[A-Za-z]", q or ""))

#: Per-leg ceiling. The box shows a handful per kind; a leg that returns
#: five hundred rows has answered a question nobody asked.
MAX_PER_KIND = 25

#: Per-leg time budget, in milliseconds, applied as SET LOCAL statement_timeout
#: inside each leg's transaction. asyncpg's own command_timeout is thirty
#: seconds, and a search box that takes thirty seconds to say "nothing" has
#: failed; two seconds is long enough for every indexed leg here and short
#: enough that a pathological query reports itself as degraded while the
#: other three kinds still answer. The first live probe found exactly that
#: shape: a public-records ILIKE on a term every Delaware row contains.
LEG_TIMEOUT_MS = 2000


async def _budget(conn: Any) -> None:
    """Cap this transaction's statements. SET LOCAL resets at commit, so it
    cannot leak into the next borrower of the pooled connection."""
    await conn.execute(f"SET LOCAL statement_timeout = {int(LEG_TIMEOUT_MS)}")


def _hit(kind: str, id_: Any, label: str, sublabel: Optional[str], href: str, score: float) -> dict[str, Any]:
    return {
        "kind": kind,
        "id": str(id_),
        "label": label,
        "sublabel": sublabel,
        "href": href,
        "score": round(float(score), 3),
    }


def score_match(query: str, *fields: Optional[str]) -> float:
    """A deterministic relevance in [0, 1] from where the query sits in the text.

    Exact beats prefix beats word-start beats anywhere. It is a sort key, not
    a probability, and it is the same rule for every kind so a person and a
    property with the same match quality tie rather than one kind always
    winning by construction.
    """
    q = (query or "").strip().lower()
    if not q:
        return 0.0
    best = 0.0
    for field in fields:
        if not field:
            continue
        text = str(field).lower()
        if text == q:
            score = 1.0
        elif text.startswith(q):
            score = 0.85
        elif any(word.startswith(q) for word in text.split()):
            score = 0.7
        elif q in text:
            score = 0.5
        else:
            continue
        best = max(best, score)
    return best


# ---------------------------------------------------------------------------
# Legs. Each takes (ctx, q, limit) and returns a list of hits. Each is
# independent so one can be swapped or stubbed without touching the others.
# ---------------------------------------------------------------------------

async def _people(ctx: TenantContext, q: str, limit: int) -> list[dict[str, Any]]:
    """Clients by plaintext ILIKE, then contacts through the blind index.

    Contacts carry their PII encrypted; the lateral join to the legacy client
    row is the only plaintext name available to a list, so a contact with no
    legacy client is labelled by the channel it was found on rather than
    invented. search_contact_rows already does the tenant-keyed hashing.
    """
    from contacts_api import _contact_json, search_contact_rows

    pattern = f"%{q}%"
    async with tenant_tx(ctx) as conn:
        await _budget(conn)
        clients = await conn.fetch(
            """
            SELECT id, full_name, email, phone, client_type, stage
              FROM clients
             WHERE archived_at IS NULL
               AND (full_name ILIKE $1 OR email ILIKE $1 OR company ILIKE $1)
             ORDER BY updated_at DESC
             LIMIT $2
            """,
            pattern, limit,
        )
        contact_rows, _ = await search_contact_rows(conn, ctx, query=q, limit=limit)
        # _contact_json decrypts the PII envelope; it is the same serializer
        # the contact list uses, so a name shown here is the name shown there.
        contacts = [await _contact_json(conn, ctx, r) for r in contact_rows]

    hits: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in clients:
        seen.add(str(row["id"]))
        hits.append(_hit(
            "people", row["id"], row["full_name"] or "Unnamed",
            " · ".join(p for p in (row["client_type"], row["stage"], row["email"]) if p) or None,
            f"/p/{row['id']}",
            score_match(q, row["full_name"], row["email"]),
        ))
    for row in contacts:
        legacy = row.get("legacy_client_id")
        if legacy and str(legacy) in seen:
            continue
        name = row.get("full_name") or None
        email = row.get("email") or None
        label = name or email or "Contact"
        target = f"/p/{legacy}" if legacy else f"/work?type=people&q={q}"
        hits.append(_hit(
            "people", row["id"], label,
            email if name else None,
            target,
            score_match(q, name, email) or 0.4,
        ))
    return hits


async def _properties(ctx: TenantContext, q: str, limit: int) -> list[dict[str, Any]]:
    """The tenant's own properties: leads and listings, by address.

    This is what "155 Main" means to an agent — the house they are working,
    not every parcel in the state that shares the string. The public corpus
    is its own leg (`_records`), opt-in, because it is 8.5 million rows and
    a name-shaped query against it is both meaningless and expensive.
    """
    pattern = f"%{q}%"
    async with tenant_tx(ctx) as conn:
        await _budget(conn)
        # The explicit tenant_id here is NOT the RLS-duplication anti-pattern
        # fixed in belief_store — it is the same documented business-scope
        # exception opportunity_engine carries, and here it is also load-
        # bearing for the planner. `leads` is the 8.5-million-row harvested
        # corpus; with only RLS's app_current_tenant() call to go on, the
        # planner has no constant to match a tenant index against and
        # sequentially scans the lot, which is exactly what the first probe
        # saw: this leg exhausting its budget on a tenant that owns no leads.
        # A search is one tenant's search, so the constant is correct as well
        # as fast.
        leads = await conn.fetch(
            """
            SELECT id, address, state, dossier_status
              FROM leads
             WHERE tenant_id = $3::uuid
               AND address ILIKE $1
             ORDER BY updated_at DESC
             LIMIT $2
            """,
            pattern, limit, ctx.tenant_id,
        )
        listings = await conn.fetch(
            """
            SELECT id, address, status, price
              FROM listings
             WHERE tenant_id = $3::uuid
               AND address ILIKE $1
             ORDER BY updated_at DESC
             LIMIT $2
            """,
            pattern, limit, ctx.tenant_id,
        )
    hits = [
        _hit(
            "properties", r["id"], r["address"] or "Unaddressed lead",
            " · ".join(p for p in (r["state"], r["dossier_status"]) if p) or None,
            f"/property/{r['id']}",
            score_match(q, r["address"]),
        )
        for r in leads
    ]
    hits += [
        _hit(
            "properties", r["id"], r["address"] or "Unaddressed listing",
            " · ".join(p for p in (
                r["status"],
                f"${float(r['price']):,.0f}" if r["price"] is not None else None,
            ) if p) or None,
            f"/property/{r['id']}",
            score_match(q, r["address"]),
        )
        for r in listings
    ]
    return hits


async def _records(ctx: TenantContext, q: str, limit: int) -> list[dict[str, Any]]:
    """Public records via the trigram-indexed document. Opt-in.

    No ORDER BY, on purpose: LIMIT lets an index scan stop after twenty-five
    hits, but a sort has to see every match first, and a term like
    "delaware" matches every Delaware record there is. Relevance ordering
    happens in Python over the rows that come back, where it is cheap. The
    statement budget still applies; a term that exhausts it reports the leg
    as degraded rather than stalling the other four.
    """
    pattern = f"%{q}%"
    async with tenant_tx(ctx) as conn:
        await _budget(conn)
        records = await conn.fetch(
            """
            SELECT id, address, city, state, zip_code
              FROM public_property_records
             WHERE search_document ILIKE $1
             LIMIT $2
            """,
            pattern, limit,
        )
    return [
        _hit(
            "records", r["id"], r["address"] or "Unaddressed record",
            ", ".join(p for p in (r["city"], r["state"], r["zip_code"]) if p) or None,
            f"/property/{r['id']}",
            score_match(q, r["address"]),
        )
        for r in records
    ]


async def _deals(ctx: TenantContext, q: str, limit: int) -> list[dict[str, Any]]:
    pattern = f"%{q}%"
    async with tenant_tx(ctx) as conn:
        await _budget(conn)
        rows = await conn.fetch(
            """
            SELECT t.id, t.property_address, t.status, t.closing_deadline,
                   c.full_name AS client_name
              FROM transactions t
              LEFT JOIN clients c ON c.id = t.client_id
             WHERE t.property_address ILIKE $1 OR c.full_name ILIKE $1
             ORDER BY t.updated_at DESC
             LIMIT $2
            """,
            pattern, limit,
        )
    return [
        _hit(
            "deals", r["id"], r["property_address"] or r["client_name"] or "Untitled deal",
            " · ".join(p for p in (
                r["status"],
                r["client_name"],
                r["closing_deadline"].isoformat() if r["closing_deadline"] else None,
            ) if p) or None,
            f"/deal/{r['id']}",
            score_match(q, r["property_address"], r["client_name"]),
        )
        for r in rows
    ]


async def _conversations(ctx: TenantContext, q: str, limit: int) -> list[dict[str, Any]]:
    """The comms rollup, filtered in Python. It is already one query per
    tenant and already small; a second SQL shape for the same rows would be a
    second thing to keep correct."""
    from crm import comms_threads

    payload = await comms_threads(ctx)
    threads = payload.get("threads", []) if isinstance(payload, dict) else []
    ql = q.lower()
    hits = []
    for t in threads:
        haystack = " ".join(str(t.get(k) or "") for k in ("full_name", "subject", "snippet"))
        if ql not in haystack.lower():
            continue
        client_id = t.get("client_id")
        hits.append(_hit(
            "conversations", client_id or t.get("id") or haystack[:20],
            t.get("full_name") or "Conversation",
            t.get("snippet") or t.get("subject"),
            f"/p/{client_id}" if client_id else f"/work?type=conversations&q={q}",
            score_match(q, t.get("full_name"), t.get("subject"), t.get("snippet")),
        ))
        if len(hits) >= limit:
            break
    return hits


LEGS: dict[str, Callable[[TenantContext, str, int], Awaitable[list[dict[str, Any]]]]] = {
    "people": _people,
    "properties": _properties,
    "deals": _deals,
    "conversations": _conversations,
    "records": _records,
}


async def search(ctx: TenantContext, q: str, kinds: list[str], limit: int) -> dict[str, Any]:
    """Run every requested leg at once and say which ones failed."""
    q = (q or "").strip()
    limit = max(1, min(MAX_PER_KIND, int(limit)))
    kinds = [k for k in kinds if k in LEGS]
    if not kinds:
        kinds = list(DEFAULT_KINDS)
        if looks_like_address(q):
            kinds.append("records")

    if len(q) < 2:
        return {"query": q, "results": [], "counts": {k: 0 for k in kinds}, "degraded": []}

    outcomes = await asyncio.gather(
        *(LEGS[k](ctx, q, limit) for k in kinds), return_exceptions=True,
    )

    results: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    degraded: list[str] = []
    for kind, outcome in zip(kinds, outcomes):
        if isinstance(outcome, BaseException):
            logger.warning("search leg %s failed", kind, exc_info=outcome)
            degraded.append(kind)
            counts[kind] = 0
            continue
        counts[kind] = len(outcome)
        results.extend(outcome)

    # Score first, then kind order as a stable tiebreak, so the same query
    # always renders the same list.
    order = {k: i for i, k in enumerate(KINDS)}
    results.sort(key=lambda h: (-h["score"], order.get(h["kind"], 99), h["label"]))
    return {"query": q, "results": results, "counts": counts, "degraded": degraded}


@router.get("")
async def search_endpoint(
    q: str = Query("", max_length=200),
    types: str = Query("", description="comma-separated subset of people,properties,deals,conversations"),
    limit: int = Query(10, ge=1, le=MAX_PER_KIND),
    ctx: TenantContext = Depends(require_context),
):
    requested = [t.strip() for t in types.split(",") if t.strip()]
    unknown = [t for t in requested if t not in KINDS]
    if unknown:
        raise HTTPException(422, f"unknown search type(s): {', '.join(unknown)}")
    return await search(ctx, q, requested, limit)
