"""
Neoh Enterprise CRM — the investor/wholesaler client-management surface
(migration 0019). This router shares the `/api/crm` prefix with `crm.py` and
extends the two-sided client book into a full pipeline:

  * /clients/{id}                — ClientDetail (card + notes + tasks + timeline
                                   + showings), the drill-down for one person.
  * /clients/{id}/tags           — first-class tags (client_tags).
  * /clients/{id}/notes          — authored, pinnable notes (client_notes).
  * /clients/{id}/tasks + /tasks — follow-ups / reminders (client_tasks), both
                                   per-client and standalone.
  * /clients/{id}/timeline       — the unified client_activities feed.
  * /clients/bulk                — multi-select tag/stage/assign/archive.
  * /clients/segments            — saved views / smart filters (client_segments).
  * /comms/threads/{thread_id}   — the message transcript for one thread.
  * /comms/draft                 — an AI-assisted email draft that degrades to a
                                   clean templated draft when no LLM is wired.

Every meaningful mutation appends a client_activities row via crm._log_activity
so the timeline stays the single source of truth. All queries run inside
tenant_tx(ctx) — RLS does the tenant scoping (0019 FORCE'd).
"""

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator

from db.connection import tenant_tx
from tenancy import TenantContext, require_context

# Reuse the crm.py spine — helpers, constants, the activity logger, and the
# ClientCard serializer all live there and must stay consistent.
from crm import (
    CLIENT_STAGES,
    TASK_PRIORITIES,
    TASK_STATUSES,
    _CLIENT_COLS,
    _client_json,
    _iso,
    _loads,
    _log_activity,
    _public_message_channel,
    _require_uuid,
)

logger = logging.getLogger("oracle.crm.enterprise")

router = APIRouter(prefix="/api/crm", tags=["Agent CRM"])

DUE_FILTERS = {"today", "overdue", "week"}
BULK_ACTIONS = {"tag", "untag", "stage", "assign", "archive"}

# The house-portfolio rollup (listings sold + house-leads + houses shown) is
# defined once in crm.GET /clients; for a single ClientDetail we run the same
# UNION against a bound :client_id.
_HOUSES_SQL = """
    SELECT json_agg(json_build_object('id', x.id, 'address', x.address, 'kind', x.kind)) AS houses
      FROM (
            SELECT ls.id::text AS id, ls.address, 'listing' AS kind
              FROM listings ls
             WHERE ls.seller_client_id = $1
             UNION
            SELECT ld.id::text, ld.address, 'lead'
              FROM leads ld
             WHERE ld.seller_client_id = $1
               AND ld.address IS NOT NULL
               AND NOT EXISTS (SELECT 1 FROM listings l2 WHERE l2.lead_id = ld.id)
             UNION
            SELECT COALESCE(sl.id::text, sld.id::text),
                   COALESCE(sl.address, sld.address, sld.payload->>'address'),
                   CASE WHEN sl.id IS NOT NULL THEN 'listing' ELSE 'lead' END
              FROM showings s
              LEFT JOIN listings sl  ON sl.id  = s.listing_id
              LEFT JOIN leads    sld ON sld.id = s.lead_id
             WHERE s.client_id = $1
           ) x
"""


# ---------------------------------------------------------------------------
# Serializers
# ---------------------------------------------------------------------------

def _note_json(r) -> dict:
    return {
        "id": str(r["id"]),
        "body": r["body"],
        "author_id": r["author_id"],
        "pinned": r["pinned"],
        "created_at": _iso(r["created_at"]),
        "updated_at": _iso(r["updated_at"]),
    }


def _task_json(r, client_name: Optional[str] = None) -> dict:
    return {
        "id": str(r["id"]),
        "client_id": str(r["client_id"]) if r["client_id"] else None,
        "client_name": client_name,
        "title": r["title"],
        "details": r["details"],
        "due_at": _iso(r["due_at"]),
        "status": r["status"],
        "priority": r["priority"],
        "assignee_id": r["assignee_id"],
        "created_by": r["created_by"],
        "completed_at": _iso(r["completed_at"]),
        "created_at": _iso(r["created_at"]),
    }


def _activity_json(r) -> dict:
    return {
        "id": str(r["id"]),
        "kind": r["kind"],
        "summary": r["summary"],
        "meta": _loads(r["meta"], {}),
        "actor": r["actor"],
        "created_at": _iso(r["created_at"]),
    }


def _message_json(r) -> dict:
    payload = _loads(r["payload"], {}) or {}
    return {
        "id": str(r["id"]),
        "direction": r["direction"],
        "channel": _public_message_channel(r["interaction_type"]),
        "subject": r["subject"],
        "body": payload.get("body"),
        "actor_role": r["actor_role"],
        "created_at": _iso(r["created_at"]),
        # interaction_logs has no provider-confirmed delivery state; honest null
        # rather than a fabricated status. Log-only metadata travels separately.
        "status": None,
        "delivery_status": payload.get("delivery_status"),
    }


def _showing_json(r) -> dict:
    return {
        "id": str(r["id"]),
        "address": r["address"],
        "listing_id": str(r["listing_id"]) if r["listing_id"] else None,
        "lead_id": str(r["lead_id"]) if r["lead_id"] else None,
        "shown_at": _iso(r["shown_at"]),
        "feedback": r["feedback"],
        "outcome": r["outcome"],
    }


def _norm_dt(dt: Optional[datetime]) -> Optional[datetime]:
    """Naive datetimes are assumed UTC (matches ShowingCreate house style)."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class TagCreate(BaseModel):
    tag: str = Field(..., min_length=1, max_length=60)


class NoteCreate(BaseModel):
    body: str = Field(..., min_length=1, max_length=20000)
    pinned: bool = False


class NotePatch(BaseModel):
    body: Optional[str] = Field(None, min_length=1, max_length=20000)
    pinned: Optional[bool] = None


class TaskCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=300)
    details: Optional[str] = Field(None, max_length=8000)
    due_at: Optional[datetime] = None
    priority: str = "normal"
    assignee_id: Optional[str] = Field(None, max_length=160)
    client_id: Optional[str] = None  # honoured only by the standalone POST /tasks

    @field_validator("priority")
    @classmethod
    def _priority(cls, v: str) -> str:
        if v not in TASK_PRIORITIES:
            raise ValueError(f"must be one of {sorted(TASK_PRIORITIES)}")
        return v

    @field_validator("client_id")
    @classmethod
    def _client(cls, v):
        if v is None:
            return v
        uuid.UUID(v)
        return v


class TaskPatch(BaseModel):
    status: Optional[str] = None
    title: Optional[str] = Field(None, min_length=1, max_length=300)
    details: Optional[str] = Field(None, max_length=8000)
    due_at: Optional[datetime] = None
    priority: Optional[str] = None

    @field_validator("status")
    @classmethod
    def _status(cls, v):
        if v is not None and v not in TASK_STATUSES:
            raise ValueError(f"must be one of {sorted(TASK_STATUSES)}")
        return v

    @field_validator("priority")
    @classmethod
    def _priority(cls, v):
        if v is not None and v not in TASK_PRIORITIES:
            raise ValueError(f"must be one of {sorted(TASK_PRIORITIES)}")
        return v


class SegmentCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=160)
    definition: dict = Field(default_factory=dict)


class BulkAction(BaseModel):
    ids: list[str] = Field(..., min_length=1, max_length=500)
    action: str
    value: Any = None

    @field_validator("action")
    @classmethod
    def _action(cls, v: str) -> str:
        if v not in BULK_ACTIONS:
            raise ValueError(f"must be one of {sorted(BULK_ACTIONS)}")
        return v

    @field_validator("ids")
    @classmethod
    def _ids(cls, v):
        for cid in v:
            uuid.UUID(cid)
        return v


class DraftRequest(BaseModel):
    client_id: str
    intent: str = Field("", max_length=2000)

    @field_validator("client_id")
    @classmethod
    def _client(cls, v: str) -> str:
        uuid.UUID(v)
        return v


# ---------------------------------------------------------------------------
# Segments — saved views / smart filters. Defined BEFORE the /clients/{id}
# detail route so the literal `/clients/segments` path wins route matching.
# ---------------------------------------------------------------------------

@router.get("/clients/segments")
async def list_segments(ctx: TenantContext = Depends(require_context)):
    try:
        async with tenant_tx(ctx) as conn:
            rows = await conn.fetch(
                "SELECT id, name, definition FROM client_segments ORDER BY lower(name)"
            )
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"Memory Core offline ({exc})")

    return {
        "segments": [
            {"id": str(r["id"]), "name": r["name"], "definition": _loads(r["definition"], {})}
            for r in rows
        ]
    }


@router.post("/clients/segments", status_code=status.HTTP_201_CREATED)
async def create_segment(
    body: SegmentCreate,
    ctx: TenantContext = Depends(require_context),
):
    try:
        async with tenant_tx(ctx) as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO client_segments (tenant_id, owner_id, name, definition)
                VALUES ($1, $2, $3, $4::jsonb)
             RETURNING id, name, definition
                """,
                ctx.tenant_id,
                ctx.agent_id,
                body.name.strip(),
                json.dumps(body.definition or {}),
            )
    except RuntimeError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"Memory Core offline — segment not persisted ({exc})",
        )

    return {"id": str(row["id"]), "name": row["name"], "definition": _loads(row["definition"], {})}


@router.delete("/clients/segments/{segment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_segment(
    segment_id: str,
    ctx: TenantContext = Depends(require_context),
):
    _require_uuid(segment_id, "segment_id")
    try:
        async with tenant_tx(ctx) as conn:
            deleted = await conn.fetchval(
                "DELETE FROM client_segments WHERE id = $1 RETURNING id", segment_id
            )
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"Memory Core offline ({exc})")

    if deleted is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "segment not found")
    return None


# ---------------------------------------------------------------------------
# Bulk — multi-select pipeline operations.
# ---------------------------------------------------------------------------

@router.post("/clients/bulk")
async def bulk_clients(
    body: BulkAction,
    ctx: TenantContext = Depends(require_context),
):
    action = body.action
    value = body.value

    # Per-action value validation up front — a clean 422 beats a half-applied loop.
    tag_val = stage_val = assign_val = None
    archived = True
    if action in ("tag", "untag"):
        tag_val = str(value).strip() if value is not None else ""
        if not tag_val:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "value (tag) is required")
    elif action == "stage":
        if value not in CLIENT_STAGES:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"value (stage) must be one of {sorted(CLIENT_STAGES)}",
            )
        stage_val = value
    elif action == "assign":
        assign_val = str(value).strip() if value not in (None, "") else None
    elif action == "archive":
        if isinstance(value, str):
            archived = value.strip().lower() in {"1", "true", "yes", "on"}
        elif value is None:
            archived = True
        else:
            archived = bool(value)

    updated = 0
    try:
        async with tenant_tx(ctx) as conn:
            for cid in body.ids:
                # RLS makes a missing / cross-tenant id invisible — skip it.
                if await conn.fetchval("SELECT 1 FROM clients WHERE id = $1", cid) is None:
                    continue

                if action == "tag":
                    await conn.execute(
                        "INSERT INTO client_tags (tenant_id, client_id, tag) "
                        "VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
                        ctx.tenant_id, cid, tag_val,
                    )
                    await _log_activity(conn, ctx, cid, "tag", f"Tagged: {tag_val}", {"tag": tag_val})
                elif action == "untag":
                    await conn.execute(
                        "DELETE FROM client_tags WHERE client_id = $1 AND lower(tag) = lower($2)",
                        cid, tag_val,
                    )
                    await _log_activity(conn, ctx, cid, "tag", f"Removed tag: {tag_val}",
                                        {"tag": tag_val, "removed": True})
                elif action == "stage":
                    cur = await conn.fetchrow("SELECT stage FROM clients WHERE id = $1", cid)
                    await conn.execute("UPDATE clients SET stage = $1 WHERE id = $2", stage_val, cid)
                    if cur and cur["stage"] != stage_val:
                        await _log_activity(conn, ctx, cid, "stage_change",
                                            f"Stage: {cur['stage']} → {stage_val}",
                                            {"from": cur["stage"], "to": stage_val})
                elif action == "assign":
                    await conn.execute("UPDATE clients SET assignee_id = $1 WHERE id = $2", assign_val, cid)
                    await _log_activity(conn, ctx, cid, "system",
                                        f"Assigned to {assign_val or 'unassigned'}",
                                        {"assignee_id": assign_val})
                elif action == "archive":
                    if archived:
                        await conn.execute("UPDATE clients SET archived_at = now() WHERE id = $1", cid)
                    else:
                        await conn.execute("UPDATE clients SET archived_at = NULL WHERE id = $1", cid)
                    await _log_activity(conn, ctx, cid, "system",
                                        "Client archived" if archived else "Client restored",
                                        {"archived": archived})
                updated += 1
    except RuntimeError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"Memory Core offline — bulk not applied ({exc})",
        )

    logger.info("Bulk %s applied to %d/%d clients (tenant=%s, agent=%s)",
                action, updated, len(body.ids), ctx.tenant_id, ctx.agent_id)
    return {"updated": updated}


# ---------------------------------------------------------------------------
# Tasks — standalone queue (per-client task creation lives further down).
# ---------------------------------------------------------------------------

@router.get("/tasks")
async def list_tasks(
    status_f: Optional[str] = Query(None, alias="status"),
    client_id: Optional[str] = Query(None),
    due: Optional[str] = Query(None),
    ctx: TenantContext = Depends(require_context),
):
    """Task queue with due filters. due=today|overdue|week; overdue excludes
    already-closed tasks (done/cancelled)."""
    if status_f is not None and status_f not in TASK_STATUSES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            f"status must be one of {sorted(TASK_STATUSES)}")
    if due is not None and due not in DUE_FILTERS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            f"due must be one of {sorted(DUE_FILTERS)}")
    if client_id is not None:
        _require_uuid(client_id, "client_id")

    where, args = [], []
    if status_f is not None:
        args.append(status_f)
        where.append(f"ct.status = ${len(args)}")
    if client_id is not None:
        args.append(client_id)
        where.append(f"ct.client_id = ${len(args)}")
    if due == "today":
        where.append("ct.due_at >= date_trunc('day', now()) "
                     "AND ct.due_at < date_trunc('day', now()) + interval '1 day'")
    elif due == "overdue":
        where.append("ct.due_at < now() AND ct.status NOT IN ('done', 'cancelled')")
    elif due == "week":
        where.append("ct.due_at >= now() AND ct.due_at < now() + interval '7 days'")
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    try:
        async with tenant_tx(ctx) as conn:
            rows = await conn.fetch(
                f"""
                SELECT ct.id, ct.client_id, ct.title, ct.details, ct.due_at,
                       ct.status, ct.priority, ct.assignee_id, ct.created_by,
                       ct.completed_at, ct.created_at,
                       c.full_name AS client_name
                  FROM client_tasks ct
                  LEFT JOIN clients c ON c.id = ct.client_id
                {where_sql}
                 ORDER BY ct.due_at ASC NULLS LAST, ct.created_at DESC
                 LIMIT 500
                """,
                *args,
            )
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"Memory Core offline ({exc})")

    return {"tasks": [_task_json(r, r["client_name"]) for r in rows]}


@router.post("/tasks", status_code=status.HTTP_201_CREATED)
async def create_task(
    body: TaskCreate,
    ctx: TenantContext = Depends(require_context),
):
    """Standalone task. Pass client_id in the body to anchor it to a client
    (which also writes a timeline activity); omit it for a free-floating to-do."""
    due_at = _norm_dt(body.due_at)
    try:
        async with tenant_tx(ctx) as conn:
            client_name = None
            if body.client_id is not None:
                crow = await conn.fetchrow(
                    "SELECT full_name FROM clients WHERE id = $1", body.client_id
                )
                if crow is None:
                    raise HTTPException(status.HTTP_404_NOT_FOUND, "client not found")
                client_name = crow["full_name"]

            row = await conn.fetchrow(
                """
                INSERT INTO client_tasks
                    (tenant_id, client_id, title, details, due_at,
                     priority, assignee_id, created_by)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
             RETURNING id, client_id, title, details, due_at, status, priority,
                       assignee_id, created_by, completed_at, created_at
                """,
                ctx.tenant_id,
                body.client_id,
                body.title.strip(),
                body.details,
                due_at,
                body.priority,
                body.assignee_id,
                ctx.agent_id,
            )
            if body.client_id is not None:
                await _log_activity(conn, ctx, body.client_id, "task",
                                    f"Task created: {row['title']}",
                                    {"task_id": str(row["id"]), "priority": body.priority})
    except RuntimeError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"Memory Core offline — task not persisted ({exc})",
        )

    return _task_json(row, client_name)


@router.patch("/tasks/{task_id}")
async def update_task(
    task_id: str,
    body: TaskPatch,
    ctx: TenantContext = Depends(require_context),
):
    """status=done stamps completed_at + writes a timeline activity; reopening
    (status away from done) clears completed_at."""
    _require_uuid(task_id, "task_id")
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "no fields to update")

    sets, args = [], []
    for col in ("status", "title", "details", "priority"):
        if col in fields:
            sets.append(f"{col} = ${len(args) + 1}")
            args.append(fields[col])
    if "due_at" in fields:
        sets.append(f"due_at = ${len(args) + 1}")
        args.append(_norm_dt(fields["due_at"]))
    if "status" in fields:
        # done → stamp; any other status → clear (reopened).
        sets.append("completed_at = now()" if fields["status"] == "done" else "completed_at = NULL")
    args.append(task_id)

    try:
        async with tenant_tx(ctx) as conn:
            row = await conn.fetchrow(
                f"""
                WITH upd AS (
                    UPDATE client_tasks
                       SET {", ".join(sets)}
                     WHERE id = ${len(args)}
                 RETURNING id, client_id, title, details, due_at, status, priority,
                           assignee_id, created_by, completed_at, created_at
                )
                SELECT upd.*, c.full_name AS client_name
                  FROM upd LEFT JOIN clients c ON c.id = upd.client_id
                """,
                *args,
            )
            if row is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "task not found")
            if fields.get("status") == "done" and row["client_id"] is not None:
                await _log_activity(conn, ctx, row["client_id"], "task",
                                    f"Task completed: {row['title']}",
                                    {"task_id": str(row["id"])})
    except RuntimeError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"Memory Core offline — task not persisted ({exc})",
        )

    return _task_json(row, row["client_name"])


# ---------------------------------------------------------------------------
# Comms — thread transcript + AI draft.
# ---------------------------------------------------------------------------

@router.get("/comms/threads/{thread_id}")
async def comms_thread(
    thread_id: str,
    ctx: TenantContext = Depends(require_context),
):
    """Full transcript for one thread, oldest-first."""
    _require_uuid(thread_id, "thread_id")
    try:
        async with tenant_tx(ctx) as conn:
            rows = await conn.fetch(
                """
                SELECT id, interaction_type, direction, subject, payload,
                       actor_role, created_at
                  FROM interaction_logs
                 WHERE thread_id = $1
                 ORDER BY created_at ASC
                 LIMIT 500
                """,
                thread_id,
            )
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"Memory Core offline ({exc})")

    return {"messages": [_message_json(r) for r in rows]}


def _signature(profile) -> str:
    if profile is not None and profile["email_signature"]:
        return profile["email_signature"]
    parts = []
    if profile is not None:
        if profile["display_name"]:
            parts.append(profile["display_name"])
        if profile["brokerage"]:
            parts.append(profile["brokerage"])
    return "\n".join(parts) if parts else "Your Acquisitions Team"


def _template_draft(intent: str, client, profile) -> dict:
    """A clean, never-crashes templated email built from the agent's signature
    and the client's context. This is the graceful-degradation path used when
    no LLM is wired (CRM_DRAFT_LLM unset)."""
    full = client["full_name"] or ""
    first = full.strip().split()[0] if full.strip() else "there"
    intent_clean = (intent or "").strip()

    opener = {
        "seller": "I wanted to reach out about your property and the options we can put in front of you.",
        "buyer": "I wanted to follow up on the kind of properties you're looking for.",
        "both": "I wanted to touch base on where things stand on your buying and selling goals.",
    }.get(client["client_type"], "I wanted to follow up with you.")

    if intent_clean:
        subject = intent_clean.split("\n")[0].split(".")[0].strip()[:140] or f"Following up, {first}"
        opener = intent_clean
    else:
        subject = f"Following up, {first}"

    company_line = f" at {client['company']}" if client["company"] else ""
    body = (
        f"Hi {first},\n\n"
        f"{opener}\n\n"
        f"Whenever works for you{company_line}, I'd love to find a few minutes to connect — "
        f"happy to work around your schedule.\n\n"
        f"Best,\n{_signature(profile)}"
    )
    return {"subject": subject, "body": body}


async def _llm_draft(intent: str, client, profile) -> Optional[dict]:
    """Optional LLM-backed draft via the existing Bedrock path. Opt-in
    (CRM_DRAFT_LLM=1) and fully guarded — any failure/timeout returns None so
    the caller falls back to the template. Never raises."""
    if os.environ.get("CRM_DRAFT_LLM", "").strip().lower() not in {"1", "true", "yes", "on"}:
        return None
    try:
        from ml_forge.bedrock_client import invoke_bedrock_model, PRIMARY_MODEL

        ctx_lines = [
            f"Recipient: {client['full_name']} ({client['client_type']}, stage {client['stage']})",
        ]
        if client["company"]:
            ctx_lines.append(f"Company: {client['company']}")
        prompt = (
            "You are a real-estate investor's assistant writing a concise, warm, "
            "professional outreach email. Return EXACTLY:\n"
            "Subject: <one line>\n\n<body>\n\n"
            "Do not invent facts. Keep it under 160 words.\n\n"
            + "\n".join(ctx_lines)
            + f"\n\nIntent: {intent or 'a friendly check-in'}\n"
            + f"Sign off as:\n{_signature(profile)}\n"
        )
        text = await asyncio.wait_for(
            asyncio.to_thread(invoke_bedrock_model, PRIMARY_MODEL, prompt, 600),
            timeout=20,
        )
        if not text or not text.strip():
            return None
        text = text.strip()
        subject = ""
        body = text
        if text.lower().startswith("subject:"):
            head, _, rest = text.partition("\n")
            subject = head.split(":", 1)[1].strip()
            body = rest.strip()
        if not subject:
            subject = (intent or "Following up").split("\n")[0][:140]
        return {"subject": subject, "body": body}
    except Exception as exc:  # noqa: BLE001 — degrade, never crash the request
        logger.warning("CRM draft LLM path failed, using template fallback: %s", exc)
        return None


@router.post("/comms/draft")
async def comms_draft(
    body: DraftRequest,
    ctx: TenantContext = Depends(require_context),
):
    """AI-assisted email draft. Uses the agent's email_signature + client
    context. Degrades gracefully to a clean templated draft when no LLM is
    available — always returns {subject, body}."""
    try:
        async with tenant_tx(ctx) as conn:
            client = await conn.fetchrow(
                "SELECT id, full_name, email, client_type, stage, company "
                "FROM clients WHERE id = $1",
                body.client_id,
            )
            if client is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "client not found")
            profile = await conn.fetchrow(
                "SELECT display_name, brokerage, email_signature "
                "FROM user_profiles WHERE user_id = $1",
                ctx.agent_id,
            )
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"Memory Core offline ({exc})")

    draft = await _llm_draft(body.intent, client, profile)
    if draft is None:
        draft = _template_draft(body.intent, client, profile)
    return draft


# ---------------------------------------------------------------------------
# Client detail + sub-resources. The /clients/{client_id} catch-all GET is
# registered AFTER the literal /clients/segments + /clients/bulk routes above.
# ---------------------------------------------------------------------------

@router.get("/clients/{client_id}")
async def get_client_detail(
    client_id: str,
    ctx: TenantContext = Depends(require_context),
):
    """ClientDetail = ClientCard + notes + tasks + timeline + showings."""
    _require_uuid(client_id, "client_id")
    try:
        async with tenant_tx(ctx) as conn:
            c = await conn.fetchrow(
                f"SELECT {_CLIENT_COLS} FROM clients WHERE id = $1", client_id
            )
            if c is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "client not found")

            houses = await conn.fetchval(_HOUSES_SQL, client_id)
            tag_rows = await conn.fetch(
                "SELECT tag FROM client_tags WHERE client_id = $1 ORDER BY lower(tag)", client_id
            )
            open_tasks = await conn.fetchval(
                "SELECT count(*) FROM client_tasks WHERE client_id = $1 AND status = 'open'", client_id
            )
            note_rows = await conn.fetch(
                """SELECT id, body, author_id, pinned, created_at, updated_at
                     FROM client_notes WHERE client_id = $1
                    ORDER BY pinned DESC, created_at DESC""",
                client_id,
            )
            task_rows = await conn.fetch(
                """SELECT id, client_id, title, details, due_at, status, priority,
                          assignee_id, created_by, completed_at, created_at
                     FROM client_tasks WHERE client_id = $1
                    ORDER BY (status = 'open') DESC, due_at ASC NULLS LAST, created_at DESC""",
                client_id,
            )
            timeline_rows = await conn.fetch(
                """SELECT id, kind, summary, meta, actor, created_at
                     FROM client_activities WHERE client_id = $1
                    ORDER BY created_at DESC LIMIT 100""",
                client_id,
            )
            showing_rows = await conn.fetch(
                """SELECT s.id, s.shown_at, s.outcome, s.feedback, s.listing_id, s.lead_id,
                          COALESCE(ls.address, ld.address, ld.payload->>'address') AS address
                     FROM showings s
                     LEFT JOIN listings ls ON ls.id = s.listing_id
                     LEFT JOIN leads    ld ON ld.id = s.lead_id
                    WHERE s.client_id = $1
                    ORDER BY s.shown_at DESC""",
                client_id,
            )
            last_touch_row = await conn.fetchrow(
                """SELECT interaction_type, created_at FROM interaction_logs
                    WHERE client_id = $1 ORDER BY created_at DESC LIMIT 1""",
                client_id,
            )
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"Memory Core offline ({exc})")

    last_touch = None
    if last_touch_row is not None:
        last_touch = {
            "interaction_type": last_touch_row["interaction_type"],
            "created_at": _iso(last_touch_row["created_at"]),
        }
    last_activity = None
    if timeline_rows:
        last_activity = {
            "kind": timeline_rows[0]["kind"],
            "summary": timeline_rows[0]["summary"],
            "created_at": _iso(timeline_rows[0]["created_at"]),
        }

    detail = _client_json(
        c, _loads(houses, []) or [], last_touch,
        tags=[r["tag"] for r in tag_rows],
        open_tasks=open_tasks,
        last_activity=last_activity,
    )
    detail["notes"] = [_note_json(n) for n in note_rows]
    detail["tasks"] = [_task_json(t, c["full_name"]) for t in task_rows]
    detail["timeline"] = [_activity_json(a) for a in timeline_rows]
    detail["showings"] = [_showing_json(s) for s in showing_rows]
    return detail


@router.get("/clients/{client_id}/timeline")
async def client_timeline(
    client_id: str,
    limit: int = Query(50, ge=1, le=200),
    ctx: TenantContext = Depends(require_context),
):
    _require_uuid(client_id, "client_id")
    try:
        async with tenant_tx(ctx) as conn:
            if await conn.fetchval("SELECT 1 FROM clients WHERE id = $1", client_id) is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "client not found")
            rows = await conn.fetch(
                """SELECT id, kind, summary, meta, actor, created_at
                     FROM client_activities WHERE client_id = $1
                    ORDER BY created_at DESC LIMIT $2""",
                client_id, limit,
            )
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"Memory Core offline ({exc})")

    return {"timeline": [_activity_json(r) for r in rows]}


# ---- Tags ----------------------------------------------------------------

@router.post("/clients/{client_id}/tags", status_code=status.HTTP_201_CREATED)
async def add_tag(
    client_id: str,
    body: TagCreate,
    ctx: TenantContext = Depends(require_context),
):
    _require_uuid(client_id, "client_id")
    tag = body.tag.strip()
    if not tag:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "tag cannot be blank")
    try:
        async with tenant_tx(ctx) as conn:
            if await conn.fetchval("SELECT 1 FROM clients WHERE id = $1", client_id) is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "client not found")
            inserted = await conn.fetchval(
                "INSERT INTO client_tags (tenant_id, client_id, tag) "
                "VALUES ($1, $2, $3) ON CONFLICT DO NOTHING RETURNING id",
                ctx.tenant_id, client_id, tag,
            )
            if inserted is not None:
                await _log_activity(conn, ctx, client_id, "tag", f"Tagged: {tag}", {"tag": tag})
            tags = [r["tag"] for r in await conn.fetch(
                "SELECT tag FROM client_tags WHERE client_id = $1 ORDER BY lower(tag)", client_id
            )]
    except RuntimeError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"Memory Core offline — tag not persisted ({exc})",
        )
    return {"tags": tags}


@router.delete("/clients/{client_id}/tags/{tag}")
async def remove_tag(
    client_id: str,
    tag: str,
    ctx: TenantContext = Depends(require_context),
):
    _require_uuid(client_id, "client_id")
    try:
        async with tenant_tx(ctx) as conn:
            if await conn.fetchval("SELECT 1 FROM clients WHERE id = $1", client_id) is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "client not found")
            removed = await conn.fetchval(
                "DELETE FROM client_tags WHERE client_id = $1 AND lower(tag) = lower($2) RETURNING id",
                client_id, tag,
            )
            if removed is not None:
                await _log_activity(conn, ctx, client_id, "tag", f"Removed tag: {tag}",
                                    {"tag": tag, "removed": True})
            tags = [r["tag"] for r in await conn.fetch(
                "SELECT tag FROM client_tags WHERE client_id = $1 ORDER BY lower(tag)", client_id
            )]
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"Memory Core offline ({exc})")
    return {"tags": tags}


# ---- Notes ---------------------------------------------------------------

@router.get("/clients/{client_id}/notes")
async def list_notes(
    client_id: str,
    ctx: TenantContext = Depends(require_context),
):
    _require_uuid(client_id, "client_id")
    try:
        async with tenant_tx(ctx) as conn:
            if await conn.fetchval("SELECT 1 FROM clients WHERE id = $1", client_id) is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "client not found")
            rows = await conn.fetch(
                """SELECT id, body, author_id, pinned, created_at, updated_at
                     FROM client_notes WHERE client_id = $1
                    ORDER BY pinned DESC, created_at DESC""",
                client_id,
            )
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"Memory Core offline ({exc})")
    return {"notes": [_note_json(r) for r in rows]}


@router.post("/clients/{client_id}/notes", status_code=status.HTTP_201_CREATED)
async def create_note(
    client_id: str,
    body: NoteCreate,
    ctx: TenantContext = Depends(require_context),
):
    _require_uuid(client_id, "client_id")
    try:
        async with tenant_tx(ctx) as conn:
            if await conn.fetchval("SELECT 1 FROM clients WHERE id = $1", client_id) is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "client not found")
            row = await conn.fetchrow(
                """
                INSERT INTO client_notes (tenant_id, client_id, body, author_id, pinned)
                VALUES ($1, $2, $3, $4, $5)
             RETURNING id, body, author_id, pinned, created_at, updated_at
                """,
                ctx.tenant_id, client_id, body.body, ctx.agent_id, body.pinned,
            )
            await _log_activity(conn, ctx, client_id, "note", "Note added",
                                {"note_id": str(row["id"]), "pinned": body.pinned})
    except RuntimeError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"Memory Core offline — note not persisted ({exc})",
        )
    return _note_json(row)


@router.patch("/clients/{client_id}/notes/{note_id}")
async def update_note(
    client_id: str,
    note_id: str,
    body: NotePatch,
    ctx: TenantContext = Depends(require_context),
):
    _require_uuid(client_id, "client_id")
    _require_uuid(note_id, "note_id")
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "no fields to update")

    sets, args = [], []
    for col in ("body", "pinned"):
        if col in fields:
            sets.append(f"{col} = ${len(args) + 1}")
            args.append(fields[col])
    args.extend([note_id, client_id])

    try:
        async with tenant_tx(ctx) as conn:
            row = await conn.fetchrow(
                f"""
                UPDATE client_notes
                   SET {", ".join(sets)}
                 WHERE id = ${len(args) - 1} AND client_id = ${len(args)}
             RETURNING id, body, author_id, pinned, created_at, updated_at
                """,
                *args,
            )
    except RuntimeError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"Memory Core offline — note not persisted ({exc})",
        )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "note not found")
    return _note_json(row)


@router.delete("/clients/{client_id}/notes/{note_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_note(
    client_id: str,
    note_id: str,
    ctx: TenantContext = Depends(require_context),
):
    _require_uuid(client_id, "client_id")
    _require_uuid(note_id, "note_id")
    try:
        async with tenant_tx(ctx) as conn:
            deleted = await conn.fetchval(
                "DELETE FROM client_notes WHERE id = $1 AND client_id = $2 RETURNING id",
                note_id, client_id,
            )
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"Memory Core offline ({exc})")
    if deleted is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "note not found")
    return None


# ---- Per-client tasks ----------------------------------------------------

@router.post("/clients/{client_id}/tasks", status_code=status.HTTP_201_CREATED)
async def create_client_task(
    client_id: str,
    body: TaskCreate,
    ctx: TenantContext = Depends(require_context),
):
    _require_uuid(client_id, "client_id")
    due_at = _norm_dt(body.due_at)
    try:
        async with tenant_tx(ctx) as conn:
            crow = await conn.fetchrow("SELECT full_name FROM clients WHERE id = $1", client_id)
            if crow is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "client not found")
            row = await conn.fetchrow(
                """
                INSERT INTO client_tasks
                    (tenant_id, client_id, title, details, due_at,
                     priority, assignee_id, created_by)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
             RETURNING id, client_id, title, details, due_at, status, priority,
                       assignee_id, created_by, completed_at, created_at
                """,
                ctx.tenant_id, client_id, body.title.strip(), body.details, due_at,
                body.priority, body.assignee_id, ctx.agent_id,
            )
            await _log_activity(conn, ctx, client_id, "task",
                                f"Task created: {row['title']}",
                                {"task_id": str(row["id"]), "priority": body.priority})
    except RuntimeError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"Memory Core offline — task not persisted ({exc})",
        )
    return _task_json(row, crow["full_name"])
