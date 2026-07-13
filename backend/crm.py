"""
Agent CRM — the 5-tab mobile redesign's data spine (migration 0012).

One router, five surfaces:

  * /listings        — the marketplace card feed + the HouseProfile wizard's
                       write path. A house profile is the JOIN POINT between a
                       property and a person; beds/baths/sqft live on the
                       companion lead row (0012: "so the mobile listing card
                       needs no jsonb spelunking"), the listing carries
                       address/price/status.
  * /clients         — the two-sided client book (seller/buyer/both) with each
                       person's houses[] and last touch.
  * /clients/.../messages + /comms/threads — the unified comms spine over
                       interaction_logs, with durable email queueing through
                       email_outbox for the AI emailer's send worker.
  * /showings        — buyer ↔ property exposure edges.
  * /profile         — the agent's public-facing identity (My Profile tab),
                       upserted into the Memory Core's user_profiles row.

RLS does the tenant scoping — every query runs inside tenant_tx(ctx), so reads
never need a manual tenant WHERE; ctx.tenant_id is only injected on INSERTs
(the columns are NOT NULL with no default). AuditMiddleware logs mutations.
"""

import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator

from db.connection import tenant_tx
from tenancy import TenantContext, require_context
from outreach_compliance import Channel, enforce_outreach

logger = logging.getLogger("oracle.crm")

router = APIRouter(prefix="/api/crm", tags=["Agent CRM"])

# Mirrors of the 0012 CHECK constraints — validated here for a clean 422
# instead of a constraint-violation 500 (lead_dossier house style).
CLIENT_TYPES = {"seller", "buyer", "both"}
CLIENT_STAGES = {"lead", "active", "nurture", "under_contract", "closed", "lost"}
LISTING_STATUSES = {"draft", "active", "pending", "sold", "withdrawn"}
SHOWING_OUTCOMES = {"pending", "interested", "offer_made", "passed", "no_show"}
MESSAGE_CHANNELS = {"email", "message"}
MESSAGE_DIRECTIONS = {"inbound", "outbound"}
TASK_STATUSES = {"open", "done", "snoozed", "cancelled"}
TASK_PRIORITIES = {"low", "normal", "high", "urgent"}

# Shared column projection for clients — every client serializer reads these.
_CLIENT_COLS = (
    "id, full_name, email, phone, client_type, stage, lead_score, "
    "assignee_id, company, preferences, source, created_at, last_contacted_at"
)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# ", DE 19801" → "DE". Manual CRM houses still need leads.state (NOT NULL).
_ADDR_STATE_RE = re.compile(r",\s*([A-Za-z]{2})\s*\d{5}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _loads(value, default=None):
    """asyncpg can hand back json/jsonb as a raw string OR decoded — normalize."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return default if default is not None else {}
    if value is None:
        return default if default is not None else {}
    return value


def _iso(dt) -> Optional[str]:
    return dt.isoformat() if dt else None


def _num(value) -> Optional[float]:
    """numeric/Decimal → float for JSON; None passes through."""
    return float(value) if value is not None else None


def _require_uuid(value: str, name: str) -> str:
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"{name} must be a UUID")
    return value


def _check_optional_uuid(v: Optional[str]) -> Optional[str]:
    if v is None:
        return v
    try:
        uuid.UUID(v)
    except (ValueError, AttributeError, TypeError):
        raise ValueError("must be a UUID")
    return v


def _check_optional_email(v: Optional[str]) -> Optional[str]:
    if v is None or v == "":
        return v or None
    v = v.strip()
    if not _EMAIL_RE.match(v):
        raise ValueError("not a valid email address")
    return v


async def _log_activity(conn, ctx, client_id, kind: str, summary: str, meta: Optional[dict] = None):
    """Append a row to the client_activities timeline feed. Called on every
    meaningful client mutation. `actor` is the JWT agent_id; meta is jsonb.
    Runs inside the caller's tenant_tx so it shares the RLS context + tx."""
    await conn.execute(
        """
        INSERT INTO client_activities (tenant_id, client_id, kind, summary, meta, actor)
        VALUES ($1, $2, $3, $4, $5::jsonb, $6)
        """,
        ctx.tenant_id,
        client_id,
        kind,
        summary,
        json.dumps(meta or {}),
        ctx.agent_id,
    )


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class InlineSeller(BaseModel):
    full_name: str = Field(..., min_length=1, max_length=160)
    email: str = Field(..., max_length=254)
    phone: Optional[str] = Field(None, max_length=40)

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        v = v.strip()
        if not _EMAIL_RE.match(v):
            raise ValueError("not a valid email address")
        return v


class ListingCreate(BaseModel):
    address: str = Field(..., min_length=3, max_length=300)
    price: Optional[float] = Field(None, ge=0)
    beds: Optional[int] = Field(None, ge=0, le=200)
    baths: Optional[float] = Field(None, ge=0, le=99)   # numeric(3,1) ceiling
    sqft: Optional[int] = Field(None, ge=0)
    status: str = "active"
    seller_client_id: Optional[str] = None
    seller: Optional[InlineSeller] = None

    @field_validator("status")
    @classmethod
    def _status(cls, v: str) -> str:
        if v not in LISTING_STATUSES:
            raise ValueError(f"must be one of {sorted(LISTING_STATUSES)}")
        return v

    @field_validator("seller_client_id")
    @classmethod
    def _seller_id(cls, v):
        return _check_optional_uuid(v)


class ClientCreate(BaseModel):
    full_name: str = Field(..., min_length=1, max_length=160)
    email: Optional[str] = Field(None, max_length=254)
    phone: Optional[str] = Field(None, max_length=40)
    client_type: str
    stage: Optional[str] = None
    company: Optional[str] = Field(None, max_length=200)
    lead_score: Optional[int] = Field(None, ge=0, le=100)
    tags: list[str] = Field(default_factory=list)
    preferences: dict = Field(default_factory=dict)

    @field_validator("client_type")
    @classmethod
    def _type(cls, v: str) -> str:
        if v not in CLIENT_TYPES:
            raise ValueError(f"must be one of {sorted(CLIENT_TYPES)}")
        return v

    @field_validator("stage")
    @classmethod
    def _stage(cls, v):
        if v is not None and v not in CLIENT_STAGES:
            raise ValueError(f"must be one of {sorted(CLIENT_STAGES)}")
        return v

    @field_validator("email")
    @classmethod
    def _email(cls, v):
        return _check_optional_email(v)


class ClientPatch(BaseModel):
    full_name: Optional[str] = Field(None, min_length=1, max_length=160)
    email: Optional[str] = Field(None, max_length=254)
    phone: Optional[str] = Field(None, max_length=40)
    client_type: Optional[str] = None
    stage: Optional[str] = None
    lead_score: Optional[int] = Field(None, ge=0, le=100)
    assignee_id: Optional[str] = Field(None, max_length=160)
    company: Optional[str] = Field(None, max_length=200)
    preferences: Optional[dict] = None
    source: Optional[str] = Field(None, max_length=80)
    archived: Optional[bool] = None

    @field_validator("client_type")
    @classmethod
    def _type(cls, v):
        if v is not None and v not in CLIENT_TYPES:
            raise ValueError(f"must be one of {sorted(CLIENT_TYPES)}")
        return v

    @field_validator("stage")
    @classmethod
    def _stage(cls, v):
        if v is not None and v not in CLIENT_STAGES:
            raise ValueError(f"must be one of {sorted(CLIENT_STAGES)}")
        return v

    @field_validator("email")
    @classmethod
    def _email(cls, v):
        return _check_optional_email(v)


class MessageCreate(BaseModel):
    channel: str
    subject: Optional[str] = Field(None, max_length=300)
    body: str = Field(..., min_length=1, max_length=20000)
    direction: str = "outbound"

    @field_validator("channel")
    @classmethod
    def _channel(cls, v: str) -> str:
        if v not in MESSAGE_CHANNELS:
            raise ValueError(f"must be one of {sorted(MESSAGE_CHANNELS)}")
        return v

    @field_validator("direction")
    @classmethod
    def _direction(cls, v: str) -> str:
        if v not in MESSAGE_DIRECTIONS:
            raise ValueError(f"must be one of {sorted(MESSAGE_DIRECTIONS)}")
        return v


class ShowingCreate(BaseModel):
    client_id: str
    listing_id: Optional[str] = None
    lead_id: Optional[str] = None
    shown_at: Optional[datetime] = None
    feedback: Optional[str] = Field(None, max_length=4000)
    outcome: str = "pending"

    @field_validator("client_id")
    @classmethod
    def _client(cls, v: str) -> str:
        uuid.UUID(v)  # raises ValueError → 422
        return v

    @field_validator("listing_id", "lead_id")
    @classmethod
    def _props(cls, v):
        return _check_optional_uuid(v)

    @field_validator("outcome")
    @classmethod
    def _outcome(cls, v: str) -> str:
        if v not in SHOWING_OUTCOMES:
            raise ValueError(f"must be one of {sorted(SHOWING_OUTCOMES)}")
        return v


class ProfileUpdate(BaseModel):
    """Partial update of the 0012 public-identity fields. The Memory-Core
    columns (target_mao_pct, target_markets, profile_summary, …) are owned by
    other write paths and never touched here."""
    display_name: Optional[str] = Field(None, max_length=160)
    public_email: Optional[str] = Field(None, max_length=254)
    phone: Optional[str] = Field(None, max_length=40)
    headshot_url: Optional[str] = Field(None, max_length=1000)
    license_number: Optional[str] = Field(None, max_length=80)
    brokerage: Optional[str] = Field(None, max_length=160)
    bio: Optional[str] = Field(None, max_length=4000)
    email_signature: Optional[str] = Field(None, max_length=4000)

    @field_validator("public_email")
    @classmethod
    def _email(cls, v):
        return _check_optional_email(v)


# ---------------------------------------------------------------------------
# Serializers
# ---------------------------------------------------------------------------

def _listing_json(row) -> dict:
    seller = None
    if row["seller_id"] is not None:
        seller = {
            "id": str(row["seller_id"]),
            "full_name": row["seller_full_name"],
            "email": row["seller_email"],
        }
    return {
        "id": str(row["id"]),
        "address": row["address"],
        "price": _num(row["price"]),
        "status": row["status"],
        "beds": int(row["beds"]) if row["beds"] is not None else None,
        "baths": _num(row["baths"]),
        "sqft": row["sqft"],
        "cover_url": row["cover_url"],
        "seller": seller,
        "lead_id": str(row["lead_id"]) if row["lead_id"] else None,
        "created_at": _iso(row["created_at"]),
    }


def _client_json(row, houses=None, last_touch=None, *, tags=None,
                 open_tasks=0, last_activity=None) -> dict:
    """ClientCard. `houses` + `last_touch` are kept for back-compat with the
    existing 5-tab frontend; the enterprise fields (stage/score/assignee/company/
    tags/open_tasks/last_activity) are additive."""
    return {
        "id": str(row["id"]),
        "full_name": row["full_name"],
        "email": row["email"],
        "phone": row["phone"],
        "client_type": row["client_type"],
        "stage": row["stage"],
        "lead_score": int(row["lead_score"]) if row["lead_score"] is not None else 0,
        "assignee_id": row["assignee_id"],
        "company": row["company"],
        "preferences": _loads(row["preferences"], {}),
        "source": row["source"],
        "created_at": _iso(row["created_at"]),
        "last_contacted_at": _iso(row["last_contacted_at"]),
        "tags": tags if tags is not None else [],
        "open_tasks": int(open_tasks) if open_tasks is not None else 0,
        "last_activity": last_activity,
        "houses": houses if houses is not None else [],
        "last_touch": last_touch,
    }


def _interaction_json(row) -> dict:
    return {
        "id": str(row["id"]),
        "lead_id": str(row["lead_id"]) if row["lead_id"] else None,
        "client_id": str(row["client_id"]) if row["client_id"] else None,
        "actor_role": row["actor_role"],
        "interaction_type": row["interaction_type"],
        "direction": row["direction"],
        "subject": row["subject"],
        "payload": _loads(row["payload"], {}),
        "thread_id": str(row["thread_id"]) if row["thread_id"] else None,
        "created_at": _iso(row["created_at"]),
    }


def _profile_json(row) -> dict:
    """Null-safe: row may be None (agent has no user_profiles row yet)."""
    get = (lambda k: row[k]) if row is not None else (lambda k: None)
    return {
        "display_name": get("display_name"),
        "public_email": get("public_email"),
        "phone": get("phone"),
        "headshot_url": get("headshot_url"),
        "license_number": get("license_number"),
        "brokerage": get("brokerage"),
        "bio": get("bio"),
        "email_signature": get("email_signature"),
        "experience_level": get("experience_level"),
        "target_markets": _loads(get("target_markets"), []),
        "monthly_deal_target": get("monthly_deal_target"),
    }


# ---------------------------------------------------------------------------
# Listings — the marketplace feed + HouseProfile wizard write path.
# ---------------------------------------------------------------------------

@router.get("/listings")
async def list_listings(ctx: TenantContext = Depends(require_context)):
    """Marketplace card feed. beds/baths/sqft ride the companion lead (0012);
    cover_url is the first photo by sort_order via LATERAL."""
    try:
        async with tenant_tx(ctx) as conn:
            rows = await conn.fetch(
                """
                SELECT ls.id, ls.address, ls.price, ls.status, ls.lead_id, ls.created_at,
                       ld.beds, ld.baths, ld.sqft,
                       c.id        AS seller_id,
                       c.full_name AS seller_full_name,
                       c.email     AS seller_email,
                       pm.url      AS cover_url
                  FROM listings ls
                  LEFT JOIN leads   ld ON ld.id = ls.lead_id
                  LEFT JOIN clients c  ON c.id = ls.seller_client_id
                  LEFT JOIN LATERAL (
                        SELECT url
                          FROM property_media
                         WHERE kind = 'photo'
                           AND (listing_id = ls.id OR lead_id = ls.lead_id)
                         ORDER BY sort_order ASC, created_at ASC
                         LIMIT 1
                  ) pm ON true
                 ORDER BY ls.created_at DESC
                 LIMIT 500
                """
            )
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"Memory Core offline ({exc})")

    return {"listings": [_listing_json(r) for r in rows]}


@router.post("/listings", status_code=status.HTTP_201_CREATED)
async def create_listing(
    body: ListingCreate,
    ctx: TenantContext = Depends(require_context),
):
    """Create a house profile: optional inline seller (client row), a companion
    lead carrying the house specs (beds/baths/sqft have no listings columns —
    the lead IS the house record per 0012), then the listing itself."""
    if body.seller_client_id and body.seller:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "provide seller_client_id OR an inline seller, not both",
        )

    address = body.address.strip()
    try:
        async with tenant_tx(ctx) as conn:
            seller = None
            if body.seller_client_id:
                seller = await conn.fetchrow(
                    "SELECT id, full_name, email FROM clients WHERE id = $1",
                    body.seller_client_id,
                )
                if seller is None:
                    # Unknown id OR another tenant's client — RLS makes them
                    # indistinguishable, which is exactly the point.
                    raise HTTPException(status.HTTP_404_NOT_FOUND, "seller client not found")
            elif body.seller:
                seller = await conn.fetchrow(
                    """
                    INSERT INTO clients (tenant_id, full_name, email, phone, client_type, source)
                    VALUES ($1, $2, $3, $4, 'seller', 'house_profile')
                 RETURNING id, full_name, email
                    """,
                    ctx.tenant_id,
                    body.seller.full_name.strip(),
                    body.seller.email,
                    body.seller.phone,
                )

            seller_id = seller["id"] if seller else None

            # Companion lead — only when there are house specs to persist.
            lead_id = None
            if body.beds is not None or body.baths is not None or body.sqft is not None:
                m = _ADDR_STATE_RE.search(address)
                lead_state = m.group(1).upper() if m else "NA"
                lead_row = await conn.fetchrow(
                    """
                    INSERT INTO leads
                        (tenant_id, parcel_id, state, motivation_score, payload,
                         seller_client_id, address, asking_price, beds, baths, sqft)
                    VALUES ($1, $2, $3, 0, $4::jsonb, $5, $6, $7, $8, $9, $10)
                 RETURNING id
                    """,
                    ctx.tenant_id,
                    f"crm:{address[:96]}",
                    lead_state,
                    json.dumps({"address": address, "source": "crm_manual"}),
                    seller_id,
                    address,
                    body.price,
                    body.beds,
                    body.baths,
                    body.sqft,
                )
                lead_id = lead_row["id"]

            listing = await conn.fetchrow(
                """
                INSERT INTO listings (tenant_id, address, price, status, lead_id, seller_client_id)
                VALUES ($1, $2, $3, $4, $5, $6)
             RETURNING id, address, price, status, lead_id, created_at
                """,
                ctx.tenant_id,
                address,
                body.price,
                body.status,
                lead_id,
                seller_id,
            )
    except RuntimeError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"Memory Core offline — listing not persisted ({exc})",
        )

    logger.info(
        "Listing created: id=%s address=%r seller=%s lead=%s (tenant=%s, agent=%s)",
        listing["id"], address, seller_id, lead_id, ctx.tenant_id, ctx.agent_id,
    )
    return {
        "listing": {
            "id": str(listing["id"]),
            "address": listing["address"],
            "price": _num(listing["price"]),
            "status": listing["status"],
            "beds": body.beds,
            "baths": body.baths,
            "sqft": body.sqft,
            "cover_url": None,
            "seller": {
                "id": str(seller["id"]),
                "full_name": seller["full_name"],
                "email": seller["email"],
            } if seller else None,
            "lead_id": str(listing["lead_id"]) if listing["lead_id"] else None,
            "created_at": _iso(listing["created_at"]),
        }
    }


# ---------------------------------------------------------------------------
# Clients — the two-sided book.
# ---------------------------------------------------------------------------

_SORT_SQL = {
    "recent": "c.created_at DESC",
    "score": "c.lead_score DESC, c.created_at DESC",
    "name": "c.full_name ASC",
    "last_contacted": "c.last_contacted_at DESC NULLS LAST, c.created_at DESC",
}


@router.get("/clients")
async def list_clients(
    type: str = Query("all"),
    stage: Optional[str] = Query(None),
    tag: Optional[str] = Query(None),
    q: Optional[str] = Query(None),
    score_min: Optional[int] = Query(None, ge=0, le=100),
    assignee: Optional[str] = Query(None),
    sort: str = Query("recent"),
    ctx: TenantContext = Depends(require_context),
):
    """Enterprise client book (ClientCard feed). Back-compat: no params → all
    non-archived clients ordered most-recent-first. 'both' rows appear in both
    seller/buyer segments. houses[] = listings they're selling + house-leads
    (skipping leads already promoted to a listing) + houses a buyer was shown.

    Filters: type, stage, tag (case-insensitive), q (name/email/company ILIKE),
    score_min, assignee. sort ∈ recent|score|name|last_contacted."""
    if type not in {"seller", "buyer", "all"}:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "type must be seller, buyer or all")
    if stage is not None and stage not in CLIENT_STAGES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"stage must be one of {sorted(CLIENT_STAGES)}")
    order_sql = _SORT_SQL.get(sort)
    if order_sql is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"sort must be one of {sorted(_SORT_SQL)}")

    # Dynamic WHERE — every fragment references a model/whitelist-derived column
    # with a positional placeholder, so this assembly is injection-safe.
    where = ["c.archived_at IS NULL"]
    args: list = []
    if type != "all":
        args.append(type)
        where.append(f"(c.client_type = ${len(args)} OR c.client_type = 'both')")
    if stage is not None:
        args.append(stage)
        where.append(f"c.stage = ${len(args)}")
    if score_min is not None:
        args.append(score_min)
        where.append(f"c.lead_score >= ${len(args)}")
    if assignee:
        args.append(assignee)
        where.append(f"c.assignee_id = ${len(args)}")
    if q:
        args.append(f"%{q.strip()}%")
        where.append(
            f"(c.full_name ILIKE ${len(args)} OR c.email ILIKE ${len(args)} "
            f"OR c.company ILIKE ${len(args)})"
        )
    if tag:
        args.append(tag.strip())
        where.append(
            f"EXISTS (SELECT 1 FROM client_tags ct2 WHERE ct2.client_id = c.id "
            f"AND lower(ct2.tag) = lower(${len(args)}))"
        )

    try:
        async with tenant_tx(ctx) as conn:
            rows = await conn.fetch(
                f"""
                SELECT c.id, c.full_name, c.email, c.phone, c.client_type,
                       c.stage, c.lead_score, c.assignee_id, c.company,
                       c.preferences, c.source, c.created_at, c.last_contacted_at,
                       h.houses,
                       COALESCE(tg.tags, '[]'::json) AS tags,
                       COALESCE(ot.open_tasks, 0)    AS open_tasks,
                       lt.interaction_type AS last_touch_type,
                       lt.created_at       AS last_touch_at,
                       la.kind AS la_kind, la.summary AS la_summary,
                       la.created_at AS la_created_at
                  FROM clients c
                  LEFT JOIN LATERAL (
                        SELECT json_agg(json_build_object(
                                   'id', x.id, 'address', x.address, 'kind', x.kind,
                                   'lead_id', x.lead_id)) AS houses
                          FROM (
                                SELECT ls.id::text AS id, ls.address, 'listing' AS kind,
                                       ls.lead_id::text AS lead_id
                                  FROM listings ls
                                 WHERE ls.seller_client_id = c.id
                                 UNION
                                SELECT ld.id::text, ld.address, 'lead', ld.id::text
                                  FROM leads ld
                                 WHERE ld.seller_client_id = c.id
                                   AND ld.address IS NOT NULL
                                   AND NOT EXISTS (SELECT 1 FROM listings l2 WHERE l2.lead_id = ld.id)
                                 UNION
                                SELECT COALESCE(sl.id::text, sld.id::text),
                                       COALESCE(sl.address, sld.address, sld.payload->>'address'),
                                       CASE WHEN sl.id IS NOT NULL THEN 'listing' ELSE 'lead' END,
                                       COALESCE(sl.lead_id,sld.id)::text
                                  FROM showings s
                                  LEFT JOIN listings sl  ON sl.id  = s.listing_id
                                  LEFT JOIN leads    sld ON sld.id = s.lead_id
                                 WHERE s.client_id = c.id
                               ) x
                  ) h ON true
                  LEFT JOIN LATERAL (
                        SELECT json_agg(t.tag ORDER BY lower(t.tag)) AS tags
                          FROM client_tags t WHERE t.client_id = c.id
                  ) tg ON true
                  LEFT JOIN LATERAL (
                        SELECT count(*) AS open_tasks
                          FROM client_tasks ct
                         WHERE ct.client_id = c.id AND ct.status = 'open'
                  ) ot ON true
                  LEFT JOIN LATERAL (
                        SELECT il.interaction_type, il.created_at
                          FROM interaction_logs il
                         WHERE il.client_id = c.id
                         ORDER BY il.created_at DESC
                         LIMIT 1
                  ) lt ON true
                  LEFT JOIN LATERAL (
                        SELECT a.kind, a.summary, a.created_at
                          FROM client_activities a
                         WHERE a.client_id = c.id
                         ORDER BY a.created_at DESC
                         LIMIT 1
                  ) la ON true
                 WHERE {" AND ".join(where)}
                 ORDER BY {order_sql}
                 LIMIT 500
                """,
                *args,
            )
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"Memory Core offline ({exc})")

    clients = []
    for r in rows:
        last_touch = None
        if r["last_touch_at"] is not None:
            last_touch = {
                "interaction_type": r["last_touch_type"],
                "created_at": _iso(r["last_touch_at"]),
            }
        last_activity = None
        if r["la_created_at"] is not None:
            last_activity = {
                "kind": r["la_kind"],
                "summary": r["la_summary"],
                "created_at": _iso(r["la_created_at"]),
            }
        clients.append(_client_json(
            r,
            _loads(r["houses"], []) or [],
            last_touch,
            tags=_loads(r["tags"], []) or [],
            open_tasks=r["open_tasks"],
            last_activity=last_activity,
        ))
    return {"clients": clients}


@router.post("/clients", status_code=status.HTTP_201_CREATED)
async def create_client(
    body: ClientCreate,
    ctx: TenantContext = Depends(require_context),
):
    stage = body.stage or "lead"
    score = body.lead_score or 0
    try:
        async with tenant_tx(ctx) as conn:
            row = await conn.fetchrow(
                f"""
                INSERT INTO clients
                    (tenant_id, full_name, email, phone, client_type,
                     stage, lead_score, company, preferences)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb)
             RETURNING {_CLIENT_COLS}
                """,
                ctx.tenant_id,
                body.full_name.strip(),
                body.email,
                body.phone,
                body.client_type,
                stage,
                score,
                (body.company or None),
                json.dumps(body.preferences or {}),
            )

            tags: list[str] = []
            for raw in body.tags or []:
                t = (raw or "").strip()
                if not t:
                    continue
                await conn.execute(
                    "INSERT INTO client_tags (tenant_id, client_id, tag) "
                    "VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
                    ctx.tenant_id, row["id"], t,
                )
            tag_rows = await conn.fetch(
                "SELECT tag FROM client_tags WHERE client_id = $1 ORDER BY lower(tag)",
                row["id"],
            )
            tags = [tr["tag"] for tr in tag_rows]

            await _log_activity(
                conn, ctx, row["id"], "created",
                f"Client created: {row['full_name']}",
                {"client_type": body.client_type, "stage": stage},
            )
    except RuntimeError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"Memory Core offline — client not persisted ({exc})",
        )

    logger.info(
        "Client created: id=%s type=%s stage=%s (tenant=%s, agent=%s)",
        row["id"], body.client_type, stage, ctx.tenant_id, ctx.agent_id,
    )
    return {"client": _client_json(row, [], None, tags=tags)}


@router.patch("/clients/{client_id}")
async def update_client(
    client_id: str,
    body: ClientPatch,
    ctx: TenantContext = Depends(require_context),
):
    _require_uuid(client_id, "client_id")

    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "no fields to update")
    for col in ("full_name", "client_type"):
        if col in fields and fields[col] is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"{col} cannot be null")

    # Column names come from the ClientPatch model's declared fields — never
    # from raw client input — so this f-string assembly is injection-safe.
    sets, args = [], []
    for col in ("full_name", "email", "phone", "client_type",
                "stage", "lead_score", "assignee_id", "company", "source"):
        if col not in fields:
            continue
        sets.append(f"{col} = ${len(args) + 1}")
        args.append(fields[col])
    if "preferences" in fields:
        sets.append(f"preferences = ${len(args) + 1}::jsonb")
        args.append(json.dumps(fields["preferences"] or {}))
    if "archived" in fields:
        # archived flips the soft-archive timestamp; no bound param needed.
        sets.append("archived_at = now()" if fields["archived"] else "archived_at = NULL")
    if not sets:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "no fields to update")
    args.append(client_id)

    try:
        async with tenant_tx(ctx) as conn:
            current = await conn.fetchrow(
                "SELECT id, full_name, stage, lead_score FROM clients WHERE id = $1",
                client_id,
            )
            if current is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "client not found")

            row = await conn.fetchrow(
                f"""
                UPDATE clients
                   SET {", ".join(sets)}
                 WHERE id = ${len(args)}
             RETURNING {_CLIENT_COLS}
                """,
                *args,
            )

            # Lifecycle activities — only when the value actually moved.
            if "stage" in fields and fields["stage"] != current["stage"]:
                await _log_activity(
                    conn, ctx, client_id, "stage_change",
                    f"Stage: {current['stage']} → {fields['stage']}",
                    {"from": current["stage"], "to": fields["stage"]},
                )
            if "lead_score" in fields and fields["lead_score"] != current["lead_score"]:
                await _log_activity(
                    conn, ctx, client_id, "score_change",
                    f"Lead score: {current['lead_score']} → {fields['lead_score']}",
                    {"from": current["lead_score"], "to": fields["lead_score"]},
                )
            if "archived" in fields:
                await _log_activity(
                    conn, ctx, client_id, "system",
                    "Client archived" if fields["archived"] else "Client restored",
                    {"archived": bool(fields["archived"])},
                )

            tag_rows = await conn.fetch(
                "SELECT tag FROM client_tags WHERE client_id = $1 ORDER BY lower(tag)",
                client_id,
            )
            tags = [tr["tag"] for tr in tag_rows]
    except RuntimeError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"Memory Core offline — update not persisted ({exc})",
        )

    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "client not found")
    return {"client": _client_json(row, [], None, tags=tags)}


@router.get("/clients/{client_id}/interactions")
async def client_interactions(
    client_id: str,
    limit: int = Query(50, ge=1, le=200),
    ctx: TenantContext = Depends(require_context),
):
    """Newest-first touch history — direct client anchors plus anything logged
    against the houses (leads) this client is selling."""
    _require_uuid(client_id, "client_id")

    try:
        async with tenant_tx(ctx) as conn:
            rows = await conn.fetch(
                """
                SELECT id, lead_id, client_id, actor_role, interaction_type,
                       direction, subject, payload, thread_id, created_at
                  FROM interaction_logs
                 WHERE client_id = $1
                    OR lead_id IN (SELECT id FROM leads WHERE seller_client_id = $1)
                 ORDER BY created_at DESC
                 LIMIT $2
                """,
                client_id,
                limit,
            )
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"Memory Core offline ({exc})")

    return {"interactions": [_interaction_json(r) for r in rows]}


@router.post("/clients/{client_id}/messages", status_code=status.HTTP_201_CREATED)
async def send_message(
    client_id: str,
    body: MessageCreate,
    ctx: TenantContext = Depends(require_context),
):
    """Log an outbound agent message into the comms spine; email also lands in
    email_outbox for the send worker. Threads continue the latest thread_id for
    this client+channel, else start a fresh one (gen_random_uuid)."""
    _require_uuid(client_id, "client_id")
    direction = body.direction
    outbound = direction == "outbound"

    try:
        async with tenant_tx(ctx) as conn:
            client = await conn.fetchrow(
                "SELECT id, full_name, email, client_type FROM clients WHERE id = $1",
                client_id,
            )
            if client is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "client not found")
            if body.channel == "email" and outbound and not client["email"]:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    "client has no email address on file",
                )

            # Compliance gate: never SEND to a contact who has opted out / is on
            # the do-not-contact list. Raises 451 when blocked. Only outbound
            # email leaves the building; inbound logging is just a record.
            if body.channel == "email" and outbound:
                await enforce_outreach(
                    ctx, channel=Channel.EMAIL, contact=client["email"], state_code=None,
                    conn=conn,
                )

            # actor_role must satisfy the interaction_logs CHECK. Outbound is the
            # agent; inbound is attributed to the client's side (both→buyer).
            if outbound:
                actor_role = "agent"
            else:
                actor_role = {"seller": "seller", "buyer": "buyer", "both": "buyer"}.get(
                    client["client_type"], "buyer"
                )

            interaction = await conn.fetchrow(
                """
                INSERT INTO interaction_logs
                    (tenant_id, client_id, actor_role, interaction_type,
                     direction, subject, payload, thread_id)
                VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb,
                        COALESCE(
                            (SELECT thread_id
                               FROM interaction_logs
                              WHERE client_id = $2
                                AND interaction_type = $4
                                AND thread_id IS NOT NULL
                              ORDER BY created_at DESC
                              LIMIT 1),
                            gen_random_uuid()))
             RETURNING id, lead_id, client_id, actor_role, interaction_type,
                       direction, subject, payload, thread_id, created_at
                """,
                ctx.tenant_id,
                client_id,
                actor_role,
                body.channel,
                direction,
                body.subject,
                json.dumps({"body": body.body}),
            )

            queued_email_id = None
            if body.channel == "email" and outbound:
                outbox = await conn.fetchrow(
                    """
                    INSERT INTO email_outbox
                        (tenant_id, client_id, thread_id, to_email, subject, body_text, created_by)
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                 RETURNING id
                    """,
                    ctx.tenant_id,
                    client_id,
                    interaction["thread_id"],
                    client["email"],
                    body.subject or "(no subject)",
                    body.body,
                    ctx.agent_id or "agent",
                )
                queued_email_id = str(outbox["id"])

            # Outbound touch advances last_contacted_at (drives sort + nurture).
            if outbound:
                await conn.execute(
                    "UPDATE clients SET last_contacted_at = now() WHERE id = $1",
                    client_id,
                )

            await _log_activity(
                conn, ctx, client_id, "message",
                f"{direction.capitalize()} {body.channel}: {body.subject or '(no subject)'}",
                {
                    "channel": body.channel,
                    "direction": direction,
                    "thread_id": str(interaction["thread_id"]) if interaction["thread_id"] else None,
                },
            )
    except RuntimeError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"Memory Core offline — message not persisted ({exc})",
        )

    logger.info(
        "Message logged: client=%s channel=%s direction=%s queued_email=%s (tenant=%s, agent=%s)",
        client_id, body.channel, direction, queued_email_id, ctx.tenant_id, ctx.agent_id,
    )
    return {"interaction": _interaction_json(interaction), "queued_email_id": queued_email_id}


# ---------------------------------------------------------------------------
# Showings — buyer ↔ property exposure edges.
# ---------------------------------------------------------------------------

@router.post("/showings", status_code=status.HTTP_201_CREATED)
async def create_showing(
    body: ShowingCreate,
    ctx: TenantContext = Depends(require_context),
):
    if not body.listing_id and not body.lead_id:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "listing_id or lead_id is required",
        )

    shown_at = body.shown_at
    if shown_at is not None and shown_at.tzinfo is None:
        shown_at = shown_at.replace(tzinfo=timezone.utc)

    try:
        async with tenant_tx(ctx) as conn:
            # Pre-flight existence checks under RLS — clean 404s instead of
            # FK-violation 500s (FK checks bypass RLS; these don't).
            if await conn.fetchval("SELECT 1 FROM clients WHERE id = $1", body.client_id) is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "client not found")
            if body.listing_id and await conn.fetchval(
                "SELECT 1 FROM listings WHERE id = $1", body.listing_id
            ) is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "listing not found")
            if body.lead_id and await conn.fetchval(
                "SELECT 1 FROM leads WHERE id = $1", body.lead_id
            ) is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "lead not found")

            row = await conn.fetchrow(
                """
                INSERT INTO showings
                    (tenant_id, client_id, listing_id, lead_id, shown_at, feedback, outcome)
                VALUES ($1, $2, $3, $4, COALESCE($5::timestamptz, now()), $6, $7)
             RETURNING id, client_id, listing_id, lead_id, shown_at, feedback, outcome, created_at
                """,
                ctx.tenant_id,
                body.client_id,
                body.listing_id,
                body.lead_id,
                shown_at,
                body.feedback,
                body.outcome,
            )
    except RuntimeError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"Memory Core offline — showing not persisted ({exc})",
        )

    logger.info(
        "Showing logged: client=%s listing=%s lead=%s outcome=%s (tenant=%s, agent=%s)",
        body.client_id, body.listing_id, body.lead_id, body.outcome, ctx.tenant_id, ctx.agent_id,
    )
    return {
        "showing": {
            "id": str(row["id"]),
            "client_id": str(row["client_id"]),
            "listing_id": str(row["listing_id"]) if row["listing_id"] else None,
            "lead_id": str(row["lead_id"]) if row["lead_id"] else None,
            "shown_at": _iso(row["shown_at"]),
            "feedback": row["feedback"],
            "outcome": row["outcome"],
            "created_at": _iso(row["created_at"]),
        }
    }


# ---------------------------------------------------------------------------
# Comms — one thread card per client with any touch history.
# ---------------------------------------------------------------------------

@router.get("/comms/threads")
async def comms_threads(ctx: TenantContext = Depends(require_context)):
    """Per-client comms rollup: every interaction_logs row reaches a client
    either directly (client_id) or through one of their house-leads
    (leads.seller_client_id). Latest row wins as `last`; newest threads first."""
    try:
        async with tenant_tx(ctx) as conn:
            rows = await conn.fetch(
                """
                WITH touches AS (
                    SELECT COALESCE(il.client_id, ld.seller_client_id) AS cid,
                           il.interaction_type, il.direction, il.subject,
                           left(il.payload->>'body', 80) AS snippet_body,
                           il.created_at
                      FROM interaction_logs il
                      LEFT JOIN leads ld ON ld.id = il.lead_id
                     WHERE COALESCE(il.client_id, ld.seller_client_id) IS NOT NULL
                )
                SELECT c.id, c.full_name, c.client_type,
                       t.interaction_type, t.direction, t.subject,
                       t.snippet_body, t.created_at,
                       n.cnt
                  FROM clients c
                  JOIN LATERAL (
                        SELECT interaction_type, direction, subject, snippet_body, created_at
                          FROM touches
                         WHERE cid = c.id
                         ORDER BY created_at DESC
                         LIMIT 1
                  ) t ON true
                  JOIN LATERAL (
                        SELECT count(*) AS cnt FROM touches WHERE cid = c.id
                  ) n ON true
                 ORDER BY t.created_at DESC
                 LIMIT 200
                """
            )
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"Memory Core offline ({exc})")

    return {
        "threads": [
            {
                "client": {
                    "id": str(r["id"]),
                    "full_name": r["full_name"],
                    "client_type": r["client_type"],
                },
                "last": {
                    "interaction_type": r["interaction_type"],
                    "direction": r["direction"],
                    "subject": r["subject"],
                    "snippet": r["snippet_body"] or r["subject"] or r["interaction_type"],
                    "created_at": _iso(r["created_at"]),
                },
                "count": int(r["cnt"]),
            }
            for r in rows
        ]
    }


# ---------------------------------------------------------------------------
# My Profile — the agent's public-facing identity (user_profiles, 0012 cols).
# ---------------------------------------------------------------------------

_PROFILE_RETURNING = """display_name, public_email, phone, headshot_url,
           license_number, brokerage, bio, email_signature,
           experience_level, target_markets, monthly_deal_target"""


@router.get("/profile")
async def get_profile(ctx: TenantContext = Depends(require_context)):
    """The authenticated agent's profile row — null-safe empty shape when the
    agent has no user_profiles row yet (pre-onboarding)."""
    try:
        async with tenant_tx(ctx) as conn:
            row = await conn.fetchrow(
                f"SELECT {_PROFILE_RETURNING} FROM user_profiles WHERE user_id = $1",
                ctx.agent_id,
            )
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"Memory Core offline ({exc})")

    return {"profile": _profile_json(row)}


@router.put("/profile")
async def put_profile(
    body: ProfileUpdate,
    ctx: TenantContext = Depends(require_context),
):
    """Partial upsert of the CRM identity fields, keyed on the JWT's agent_id.
    Memory-Core fields are preserved untouched; the ON CONFLICT tenant guard
    means a recycled user_id in another tenant is a no-op rather than a
    cross-tenant overwrite (agent_profile.py house idiom)."""
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "no fields to update")

    # Column names come from the ProfileUpdate model's declared fields — never
    # from raw client input — so this f-string assembly is injection-safe.
    cols = list(fields.keys())
    insert_cols = ["user_id", "tenant_id"] + cols
    placeholders = ", ".join(f"${i + 1}" for i in range(len(insert_cols)))
    set_sql = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols)
    args = [ctx.agent_id, ctx.tenant_id] + [fields[c] for c in cols]

    try:
        async with tenant_tx(ctx) as conn:
            row = await conn.fetchrow(
                f"""
                INSERT INTO user_profiles ({", ".join(insert_cols)})
                VALUES ({placeholders})
                ON CONFLICT (user_id) DO UPDATE
                   SET {set_sql}
                 WHERE user_profiles.tenant_id = EXCLUDED.tenant_id
             RETURNING {_PROFILE_RETURNING}
                """,
                *args,
            )
    except RuntimeError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"Memory Core offline — profile not persisted ({exc})",
        )

    if row is None:
        # ON CONFLICT fired but the WHERE tenant guard rejected the update.
        raise HTTPException(status.HTTP_409_CONFLICT, "profile id exists under a different tenant")

    logger.info(
        "Profile updated: agent=%s fields=%s (tenant=%s)",
        ctx.agent_id, sorted(fields.keys()), ctx.tenant_id,
    )
    return {"profile": _profile_json(row)}
