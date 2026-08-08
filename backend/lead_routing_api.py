"""Signed lead intake, deterministic routing, and brokerage routing controls."""

from __future__ import annotations

import hashlib
import hmac
import json
import base64
import re
import secrets
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from contact_truth import (
    lookup_hash,
    name_search_tokens,
    normalize_email,
    normalize_full_name,
    normalize_phone,
    open_json,
    seal_json,
)
from db.connection import tenant_tx
from platform_policy import Feature, feature_enabled
from speed_to_lead import enqueue_speed_to_lead
from tenancy import Role, TenantContext, apply_rls_context, require_context, require_role


router = APIRouter(prefix="/api/crm/routing", tags=["CRM lead routing"])
public_router = APIRouter(prefix="/api/public/lead-intake", tags=["Public lead intake"])

_SOURCE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")
_ZIP_RE = re.compile(r"^\d{5}$")
_STATE_RE = re.compile(r"^[A-Z]{2}$")
_SIGNATURE_WINDOW_SECONDS = 5 * 60
_MAX_WEBHOOK_BYTES = 128 * 1024


def _row_json(row: Any) -> dict[str, Any]:
    result = dict(row)
    for key, value in list(result.items()):
        if isinstance(value, (uuid.UUID, datetime)):
            result[key] = value.isoformat() if isinstance(value, datetime) else str(value)
    result.pop("webhook_secret_ciphertext", None)
    return result


def _webhook_signature(secret: str, timestamp: str, body: bytes) -> str:
    message = timestamp.encode("ascii") + b"." + body
    return "sha256=" + hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def _verify_webhook_signature(secret: str, timestamp: str, body: bytes, supplied: str) -> None:
    try:
        received_at = int(timestamp)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=401, detail="Webhook timestamp is invalid.") from exc
    if abs(int(time.time()) - received_at) > _SIGNATURE_WINDOW_SECONDS:
        raise HTTPException(status_code=401, detail="Webhook timestamp is outside the replay window.")
    expected = _webhook_signature(secret, timestamp, body)
    if not hmac.compare_digest(expected, supplied):
        raise HTTPException(status_code=401, detail="Webhook signature is invalid.")


class ConnectorCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    source_key: str = Field(min_length=2, max_length=64)
    name: str = Field(min_length=2, max_length=120)

    @field_validator("source_key")
    @classmethod
    def validate_source(cls, value: str) -> str:
        value = value.casefold()
        if not _SOURCE_RE.fullmatch(value):
            raise ValueError("source_key must use lowercase letters, digits, underscores, or hyphens")
        return value


class ConnectorUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str | None = Field(default=None, min_length=2, max_length=120)
    active: bool | None = None

    @model_validator(mode="after")
    def require_change(self) -> "ConnectorUpdate":
        if self.name is None and self.active is None:
            raise ValueError("name or active is required")
        return self


class RoutingRuleInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=2, max_length=120)
    priority: int = Field(default=100, ge=0, le=10_000)
    enabled: bool = True
    source_key: str | None = Field(default=None, max_length=64)
    zip_codes: list[str] = Field(default_factory=list, max_length=2_000)
    state_codes: list[str] = Field(default_factory=list, max_length=56)
    intent: Literal["any", "buyer", "seller"] = "any"
    assignment_mode: Literal["round_robin", "fixed_agent"] = "round_robin"
    agent_ids: list[str] = Field(default_factory=list, max_length=500)

    @field_validator("source_key")
    @classmethod
    def validate_optional_source(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.casefold()
        if not _SOURCE_RE.fullmatch(value):
            raise ValueError("source_key is invalid")
        return value

    @field_validator("zip_codes")
    @classmethod
    def validate_zips(cls, values: list[str]) -> list[str]:
        cleaned = list(dict.fromkeys(value.strip() for value in values))
        if any(not _ZIP_RE.fullmatch(value) for value in cleaned):
            raise ValueError("zip_codes must contain five-digit ZIP codes")
        return cleaned

    @field_validator("state_codes")
    @classmethod
    def validate_states(cls, values: list[str]) -> list[str]:
        cleaned = list(dict.fromkeys(value.strip().upper() for value in values))
        if any(not _STATE_RE.fullmatch(value) for value in cleaned):
            raise ValueError("state_codes must contain two-letter codes")
        return cleaned

    @field_validator("agent_ids")
    @classmethod
    def validate_agents(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip() for value in values if value.strip()))

    @model_validator(mode="after")
    def validate_fixed_agent(self) -> "RoutingRuleInput":
        if self.assignment_mode == "fixed_agent" and not self.agent_ids:
            raise ValueError("fixed_agent rules require at least one agent_id")
        return self


class RoutingAgentUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    accepting_leads: bool = True
    capacity: int = Field(default=100, ge=0, le=100_000)


class LeadIntakePayload(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    external_event_id: str = Field(min_length=1, max_length=240)
    full_name: str = Field(min_length=1, max_length=160)
    email: str | None = Field(default=None, max_length=254)
    phone: str | None = Field(default=None, max_length=40)
    intent: Literal["buyer", "seller"]
    zip_code: str | None = Field(default=None, pattern=r"^\d{5}$")
    state_code: str | None = Field(default=None, min_length=2, max_length=2)
    message: str | None = Field(default=None, max_length=8_000)
    source_url: str | None = Field(default=None, max_length=2_000)
    campaign: str | None = Field(default=None, max_length=160)

    @field_validator("full_name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return normalize_full_name(value)

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str | None) -> str | None:
        return normalize_email(value)

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, value: str | None) -> str | None:
        return normalize_phone(value)

    @field_validator("state_code")
    @classmethod
    def validate_state(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.upper()
        if not _STATE_RE.fullmatch(value):
            raise ValueError("state_code must be two letters")
        return value

    @field_validator("source_url")
    @classmethod
    def validate_source_url(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith("https://"):
            raise ValueError("source_url must use HTTPS")
        return value

    @model_validator(mode="after")
    def validate_destination(self) -> "LeadIntakePayload":
        if not (self.email or self.phone):
            raise ValueError("email or phone is required")
        return self


async def _canonical_rule_agents(conn: Any, tenant_id: str, agent_ids: list[str]) -> list[str]:
    if not agent_ids:
        return []
    rows = await conn.fetch(
        """
        SELECT agent_id FROM users
         WHERE tenant_id=$1::uuid AND is_active=true AND lower(agent_id)=ANY($2::text[])
        """,
        tenant_id,
        [value.casefold() for value in agent_ids],
    )
    by_key = {str(row["agent_id"]).casefold(): str(row["agent_id"]) for row in rows}
    missing = [value for value in agent_ids if value.casefold() not in by_key]
    if missing:
        raise HTTPException(status_code=422, detail="Every routing agent must be an active brokerage user.")
    return [by_key[value.casefold()] for value in agent_ids]


@router.get("/connectors")
async def list_connectors(ctx: TenantContext = Depends(require_context)) -> dict[str, Any]:
    require_role(ctx, Role.BROKER_OWNER)
    async with tenant_tx(ctx) as conn:
        rows = await conn.fetch(
            """
            SELECT id,public_id,source_key,name,active,created_at,updated_at
              FROM lead_source_connectors ORDER BY created_at DESC
            """
        )
    return {"connectors": [_row_json(row) for row in rows], "secrets_exposed": False}


@router.post("/connectors", status_code=status.HTTP_201_CREATED)
async def create_connector(body: ConnectorCreate, ctx: TenantContext = Depends(require_context)) -> dict[str, Any]:
    require_role(ctx, Role.BROKER_OWNER)
    secret = secrets.token_urlsafe(32)
    async with tenant_tx(ctx) as conn:
        encrypted = await seal_json(conn, ctx.tenant_id, {"webhook_secret": secret})
        row = await conn.fetchrow(
            """
            INSERT INTO lead_source_connectors (
                tenant_id,source_key,name,webhook_secret_ciphertext,created_by
            ) VALUES ($1::uuid,$2,$3,$4::bytea,$5) RETURNING *
            """,
            ctx.tenant_id,
            body.source_key,
            body.name,
            encrypted,
            ctx.agent_id,
        )
    connector = _row_json(row)
    return {
        "connector": connector,
        "webhook_path": f"/api/public/lead-intake/{connector['public_id']}",
        "webhook_secret_once": secret,
        "signature": "HMAC-SHA256 over <unix_timestamp>.<raw_body>",
    }


@router.patch("/connectors/{connector_id}")
async def update_connector(
    connector_id: uuid.UUID,
    body: ConnectorUpdate,
    ctx: TenantContext = Depends(require_context),
) -> dict[str, Any]:
    require_role(ctx, Role.BROKER_OWNER)
    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            """
            UPDATE lead_source_connectors
               SET name=COALESCE($2,name),active=COALESCE($3,active),updated_at=now()
             WHERE id=$1::uuid
             RETURNING id,public_id,source_key,name,active,created_at,updated_at
            """,
            connector_id,
            body.name,
            body.active,
        )
    if row is None:
        raise HTTPException(status_code=404, detail="Connector not found.")
    return {"connector": _row_json(row), "secrets_exposed": False}


@router.post("/connectors/{connector_id}/rotate-secret")
async def rotate_connector_secret(connector_id: uuid.UUID, ctx: TenantContext = Depends(require_context)) -> dict[str, Any]:
    require_role(ctx, Role.BROKER_OWNER)
    secret = secrets.token_urlsafe(32)
    async with tenant_tx(ctx) as conn:
        encrypted = await seal_json(conn, ctx.tenant_id, {"webhook_secret": secret})
        row = await conn.fetchrow(
            """
            UPDATE lead_source_connectors
               SET webhook_secret_ciphertext=$2::bytea,updated_at=now()
             WHERE id=$1::uuid AND active=true RETURNING public_id
            """,
            connector_id,
            encrypted,
        )
    if row is None:
        raise HTTPException(status_code=404, detail="Connector not found.")
    return {"public_id": str(row["public_id"]), "webhook_secret_once": secret}


@router.get("/rules")
async def list_rules(ctx: TenantContext = Depends(require_context)) -> dict[str, Any]:
    async with tenant_tx(ctx) as conn:
        rows = await conn.fetch("SELECT * FROM lead_routing_rules ORDER BY priority,id")
    return {"rules": [_row_json(row) for row in rows]}


@router.post("/rules", status_code=status.HTTP_201_CREATED)
async def create_rule(body: RoutingRuleInput, ctx: TenantContext = Depends(require_context)) -> dict[str, Any]:
    require_role(ctx, Role.BROKER_OWNER)
    async with tenant_tx(ctx) as conn:
        agent_ids = await _canonical_rule_agents(conn, ctx.tenant_id, body.agent_ids)
        row = await conn.fetchrow(
            """
            INSERT INTO lead_routing_rules (
                tenant_id,name,priority,enabled,source_key,zip_codes,state_codes,
                intent,assignment_mode,agent_ids,created_by
            ) VALUES ($1::uuid,$2,$3,$4,$5,$6::text[],$7::text[],$8,$9,$10::text[],$11)
            RETURNING *
            """,
            ctx.tenant_id,
            body.name,
            body.priority,
            body.enabled,
            body.source_key,
            body.zip_codes,
            body.state_codes,
            body.intent,
            body.assignment_mode,
            agent_ids,
            ctx.agent_id,
        )
    return {"rule": _row_json(row)}


@router.put("/rules/{rule_id}")
async def update_rule(
    rule_id: uuid.UUID,
    body: RoutingRuleInput,
    ctx: TenantContext = Depends(require_context),
) -> dict[str, Any]:
    require_role(ctx, Role.BROKER_OWNER)
    async with tenant_tx(ctx) as conn:
        agent_ids = await _canonical_rule_agents(conn, ctx.tenant_id, body.agent_ids)
        row = await conn.fetchrow(
            """
            UPDATE lead_routing_rules
               SET name=$2,priority=$3,enabled=$4,source_key=$5,
                   zip_codes=$6::text[],state_codes=$7::text[],intent=$8,
                   assignment_mode=$9,agent_ids=$10::text[],updated_at=now()
             WHERE id=$1::uuid
             RETURNING *
            """,
            rule_id,
            body.name,
            body.priority,
            body.enabled,
            body.source_key,
            body.zip_codes,
            body.state_codes,
            body.intent,
            body.assignment_mode,
            agent_ids,
        )
    if row is None:
        raise HTTPException(status_code=404, detail="Routing rule not found.")
    return {"rule": _row_json(row)}


@router.put("/agents/{agent_id}")
async def configure_routing_agent(agent_id: str, body: RoutingAgentUpdate, ctx: TenantContext = Depends(require_context)) -> dict[str, Any]:
    require_role(ctx, Role.BROKER_OWNER)
    async with tenant_tx(ctx) as conn:
        canonical = await _canonical_rule_agents(conn, ctx.tenant_id, [agent_id])
        row = await conn.fetchrow(
            """
            INSERT INTO agent_routing_state (tenant_id,agent_id,accepting_leads,capacity)
            VALUES ($1::uuid,$2,$3,$4)
            ON CONFLICT (tenant_id,agent_id) DO UPDATE
                SET accepting_leads=EXCLUDED.accepting_leads,capacity=EXCLUDED.capacity,
                    updated_at=now()
            RETURNING *
            """,
            ctx.tenant_id,
            canonical[0],
            body.accepting_leads,
            body.capacity,
        )
    return {"agent": _row_json(row)}


@router.get("/agents")
async def list_routing_agents(ctx: TenantContext = Depends(require_context)) -> dict[str, Any]:
    async with tenant_tx(ctx) as conn:
        rows = await conn.fetch(
            """
            SELECT u.agent_id,u.role,COALESCE(s.accepting_leads,true) AS accepting_leads,
                   COALESCE(s.capacity,100) AS capacity,s.last_assigned_at,
                   count(c.id)::int AS assigned_open_contacts
              FROM users u
              LEFT JOIN agent_routing_state s
                ON s.tenant_id=u.tenant_id AND s.agent_id=u.agent_id
              LEFT JOIN agent_contacts c
                ON c.tenant_id=u.tenant_id AND c.assigned_agent_id=u.agent_id
               AND c.deleted_at IS NULL
             WHERE u.is_active=true
             GROUP BY u.agent_id,u.role,s.accepting_leads,s.capacity,s.last_assigned_at
             ORDER BY u.agent_id
            """
        )
    return {"agents": [_row_json(row) for row in rows]}


def _encode_event_cursor(received_at: datetime, event_id: Any) -> str:
    raw = json.dumps(
        {"v": 1, "received_at": received_at.isoformat(), "id": str(event_id)},
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_event_cursor(value: str | None) -> tuple[datetime | None, str | None]:
    if not value:
        return None, None
    try:
        padded = value + "=" * (-len(value) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        if payload.get("v") != 1:
            raise ValueError
        received_at = datetime.fromisoformat(str(payload["received_at"]))
        if received_at.tzinfo is None:
            raise ValueError
        event_id = str(uuid.UUID(str(payload["id"])))
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=422, detail="Event cursor is invalid.") from exc
    return received_at, event_id


@router.get("/events")
async def list_routing_events(
    event_status: Literal["received", "routed", "unassigned", "failed"] | None = Query(
        default=None,
        alias="status",
    ),
    source_key: str | None = Query(default=None, max_length=64),
    cursor: str | None = Query(default=None, max_length=500),
    limit: int = Query(default=50, ge=1, le=200),
    ctx: TenantContext = Depends(require_context),
) -> dict[str, Any]:
    require_role(ctx, Role.BROKER_OWNER)
    cursor_at, cursor_id = _decode_event_cursor(cursor)
    async with tenant_tx(ctx) as conn:
        rows = await conn.fetch(
            """
            SELECT id,external_event_id,source_key,intent,zip_code,state_code,
                   status,contact_id,assigned_agent_id,route_reason,
                   received_at,routed_at,updated_at
              FROM lead_intake_events
             WHERE ($1::text IS NULL OR status=$1)
               AND ($2::text IS NULL OR source_key=$2)
               AND (
                    $3::timestamptz IS NULL
                    OR (received_at,id) < ($3::timestamptz,$4::uuid)
               )
             ORDER BY received_at DESC,id DESC
             LIMIT $5
            """,
            event_status,
            source_key.casefold() if source_key else None,
            cursor_at,
            cursor_id,
            limit + 1,
        )
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        next_cursor = _encode_event_cursor(rows[-1]["received_at"], rows[-1]["id"])
    return {
        "events": [_row_json(row) for row in rows],
        "next_cursor": next_cursor,
        "payloads_exposed": False,
    }


@router.get("/metrics")
async def routing_metrics(
    days: int = Query(default=30, ge=1, le=365),
    ctx: TenantContext = Depends(require_context),
) -> dict[str, Any]:
    require_role(ctx, Role.BROKER_OWNER)
    async with tenant_tx(ctx) as conn:
        totals = await conn.fetchrow(
            """
            SELECT count(*)::int AS received,
                   count(*) FILTER (WHERE status='routed')::int AS routed,
                   count(*) FILTER (WHERE status='unassigned')::int AS unassigned,
                   count(DISTINCT contact_id) FILTER (WHERE contact_id IS NOT NULL)::int
                       AS unique_contacts
              FROM lead_intake_events
             WHERE received_at >= now() - ($1::int * interval '1 day')
            """,
            days,
        )
        by_source = await conn.fetch(
            """
            SELECT source_key,count(*)::int AS received,
                   count(*) FILTER (WHERE status='routed')::int AS routed
              FROM lead_intake_events
             WHERE received_at >= now() - ($1::int * interval '1 day')
             GROUP BY source_key ORDER BY received DESC,source_key
            """,
            days,
        )
        by_agent = await conn.fetch(
            """
            SELECT assigned_agent_id,count(*)::int AS assigned
              FROM lead_intake_events
             WHERE received_at >= now() - ($1::int * interval '1 day')
               AND status='routed' AND assigned_agent_id IS NOT NULL
             GROUP BY assigned_agent_id ORDER BY assigned DESC,assigned_agent_id
            """,
            days,
        )
        # First-response latency. Percentiles rather than a mean: first-response
        # distributions are long-tailed (one lead that sat overnight drags an
        # average past the point of meaning), and the industry claim this feature
        # is chasing is stated as a threshold, so p50/p90 and the under-90s share
        # are what actually answer "are we hitting it?".
        response = await conn.fetchrow(
            """
            SELECT count(*)::int AS attempts,
                   count(*) FILTER (WHERE disposition IN ('staged','sent'))::int AS responded,
                   count(*) FILTER (WHERE disposition='blocked')::int AS blocked,
                   count(*) FILTER (WHERE disposition='skipped')::int AS skipped,
                   count(*) FILTER (WHERE disposition='failed')::int AS failed,
                   percentile_disc(0.5) WITHIN GROUP (
                       ORDER BY extract(epoch FROM (responded_at - lead_created_at))
                   ) FILTER (WHERE disposition IN ('staged','sent')) AS p50_seconds,
                   percentile_disc(0.9) WITHIN GROUP (
                       ORDER BY extract(epoch FROM (responded_at - lead_created_at))
                   ) FILTER (WHERE disposition IN ('staged','sent')) AS p90_seconds,
                   count(*) FILTER (
                       WHERE disposition IN ('staged','sent')
                         AND responded_at - lead_created_at <= interval '90 seconds'
                   )::int AS under_90s
              FROM lead_response_events
             WHERE responded_at >= now() - ($1::int * interval '1 day')
            """,
            days,
        )
    total_json = _row_json(totals)
    received = int(total_json.get("received") or 0)
    routed = int(total_json.get("routed") or 0)
    total_json["routing_rate"] = round(routed / received, 4) if received else 0.0

    resp_json = _row_json(response)
    responded = int(resp_json.get("responded") or 0)
    for key in ("p50_seconds", "p90_seconds"):
        raw = resp_json.get(key)
        resp_json[key] = round(float(raw), 1) if raw is not None else None
    resp_json["under_90s_rate"] = (
        round(int(resp_json.get("under_90s") or 0) / responded, 4) if responded else 0.0
    )
    # Leads that arrived but produced no response row at all. Without this the
    # latency numbers describe only the leads the automation reached — the same
    # survivorship bias the ledger's 'blocked' rows exist to avoid.
    resp_json["no_attempt"] = max(0, received - int(resp_json.get("attempts") or 0))
    resp_json["enabled"] = feature_enabled(Feature.SPEED_TO_LEAD, default=False)

    return {
        "window_days": days,
        "totals": total_json,
        "by_source": [_row_json(row) for row in by_source],
        "by_agent": [_row_json(row) for row in by_agent],
        "first_response": resp_json,
        "evidence_status": "observed",
    }


async def _choose_agent(conn: Any, tenant_id: str, source_key: str, payload: LeadIntakePayload) -> tuple[str | None, str]:
    rule = await conn.fetchrow(
        """
        SELECT * FROM lead_routing_rules
         WHERE tenant_id=$5::uuid
           AND enabled=true
           AND (source_key IS NULL OR source_key=$1)
           AND (cardinality(zip_codes)=0 OR $2=ANY(zip_codes))
           AND (cardinality(state_codes)=0 OR $3=ANY(state_codes))
           AND (intent='any' OR intent=$4)
         ORDER BY priority,id LIMIT 1
        """,
        source_key,
        payload.zip_code,
        payload.state_code,
        payload.intent,
        tenant_id,
    )
    rule_agents = list(rule["agent_ids"]) if rule else []
    rows = await conn.fetch(
        """
        SELECT u.agent_id,COALESCE(s.accepting_leads,true) AS accepting_leads,
               COALESCE(s.capacity,100) AS capacity,s.last_assigned_at,
               count(c.id)::int AS open_contacts
          FROM users u
          LEFT JOIN agent_routing_state s
            ON s.tenant_id=u.tenant_id AND s.agent_id=u.agent_id
          LEFT JOIN agent_contacts c
            ON c.tenant_id=u.tenant_id AND c.assigned_agent_id=u.agent_id
           AND c.deleted_at IS NULL
         WHERE u.tenant_id=$1::uuid AND u.is_active=true
           AND (cardinality($2::text[])=0 OR u.agent_id=ANY($2::text[]))
         GROUP BY u.agent_id,s.accepting_leads,s.capacity,s.last_assigned_at
        """,
        tenant_id,
        rule_agents,
    )
    eligible = [row for row in rows if row["accepting_leads"] and row["open_contacts"] < row["capacity"]]
    if not eligible:
        return None, "no_active_agent_with_capacity"
    if rule and rule["assignment_mode"] == "fixed_agent":
        order = {agent_id: index for index, agent_id in enumerate(rule_agents)}
        selected = min(eligible, key=lambda row: order.get(row["agent_id"], len(order)))
    else:
        selected = min(
            eligible,
            key=lambda row: (
                row["last_assigned_at"] is not None,
                row["last_assigned_at"] or datetime.min.replace(tzinfo=timezone.utc),
                row["open_contacts"],
                row["agent_id"],
            ),
        )
    reason = f"rule:{rule['id']}" if rule else "default_round_robin"
    return str(selected["agent_id"]), reason


@public_router.post("/{public_id}", status_code=status.HTTP_202_ACCEPTED)
async def receive_lead(
    public_id: uuid.UUID,
    request: Request,
    x_oracle_timestamp: str = Header(..., alias="X-Oracle-Timestamp"),
    x_oracle_signature: str = Header(..., alias="X-Oracle-Signature"),
) -> dict[str, Any]:
    body_bytes = await request.body()
    if len(body_bytes) > _MAX_WEBHOOK_BYTES:
        raise HTTPException(status_code=413, detail="Webhook body is too large.")
    bootstrap = TenantContext(
        agent_id="lead-intake",
        tenant_id="00000000-0000-0000-0000-000000000000",
        role=Role.PLATFORM_ADMIN,
    )
    async with tenant_tx(bootstrap) as conn:
        connector = await conn.fetchrow(
            "SELECT * FROM lead_source_connectors WHERE public_id=$1::uuid AND active=true",
            public_id,
        )
        if connector is None:
            raise HTTPException(status_code=404, detail="Lead connector not found.")
        tenant_id = str(connector["tenant_id"])
        secret_payload = await open_json(conn, tenant_id, connector["webhook_secret_ciphertext"])
        secret = secret_payload.get("webhook_secret")
        if not isinstance(secret, str):
            raise HTTPException(status_code=503, detail="Lead connector secret is unavailable.")
        _verify_webhook_signature(secret, x_oracle_timestamp, body_bytes, x_oracle_signature)
        try:
            payload = LeadIntakePayload.model_validate_json(body_bytes)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Lead payload is invalid.") from exc

        # The cross-tenant bootstrap context is needed only to resolve the
        # untrusted public connector UUID.  Once its signature is valid, switch
        # this transaction to the resolved tenant so FORCE RLS protects every
        # CRM query below even if a future query omits an explicit tenant clause.
        await apply_rls_context(
            conn,
            TenantContext(
                agent_id=f"lead-intake:{connector['id']}",
                tenant_id=tenant_id,
                role=Role.BROKER_OWNER,
            ),
        )
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtext($1))",
            tenant_id,
        )
        payload_digest = hashlib.sha256(body_bytes).hexdigest()

        existing_event = await conn.fetchrow(
            """
            SELECT id,status,contact_id,assigned_agent_id,payload_digest
              FROM lead_intake_events
             WHERE connector_id=$1::uuid AND external_event_id=$2
            """,
            connector["id"],
            payload.external_event_id,
        )
        if existing_event:
            if str(existing_event["payload_digest"]) != payload_digest:
                raise HTTPException(
                    status_code=409,
                    detail="external_event_id was already used for a different payload.",
                )
            return {
                "event_id": str(existing_event["id"]),
                "status": existing_event["status"],
                "accepted": True,
                "idempotent_replay": True,
            }

        encrypted_payload = await seal_json(conn, tenant_id, payload.model_dump(mode="json"))
        event = await conn.fetchrow(
            """
            INSERT INTO lead_intake_events (
                tenant_id,connector_id,external_event_id,payload_ciphertext,payload_digest,
                source_key,intent,zip_code,state_code,status
            ) VALUES ($1::uuid,$2::uuid,$3,$4::bytea,$5,$6,$7,$8,$9,'received')
            ON CONFLICT (tenant_id,connector_id,external_event_id) DO NOTHING
            RETURNING *
            """,
            tenant_id,
            connector["id"],
            payload.external_event_id,
            encrypted_payload,
            payload_digest,
            connector["source_key"],
            payload.intent,
            payload.zip_code,
            payload.state_code,
        )
        if event is None:
            raced_event = await conn.fetchrow(
                """
                SELECT id,status,payload_digest
                  FROM lead_intake_events
                 WHERE connector_id=$1::uuid AND external_event_id=$2
                """,
                connector["id"],
                payload.external_event_id,
            )
            if raced_event is None:
                raise HTTPException(status_code=503, detail="Lead intake could not be reconciled.")
            if str(raced_event["payload_digest"]) != payload_digest:
                raise HTTPException(
                    status_code=409,
                    detail="external_event_id was already used for a different payload.",
                )
            return {
                "event_id": str(raced_event["id"]),
                "status": raced_event["status"],
                "accepted": True,
                "idempotent_replay": True,
            }
        email_hash = lookup_hash(tenant_id, "email", payload.email)
        phone_hash = lookup_hash(tenant_id, "phone", payload.phone)
        contact = await conn.fetchrow(
            """
            SELECT id,assigned_agent_id,legacy_client_id FROM agent_contacts
             WHERE deleted_at IS NULL AND (
                ($1::char(64) IS NOT NULL AND email_lookup_hash=$1::char(64)) OR
                ($2::char(64) IS NOT NULL AND phone_lookup_hash=$2::char(64))
             ) ORDER BY updated_at DESC LIMIT 1
            """,
            email_hash,
            phone_hash,
        )
        duplicate = contact is not None
        if contact is not None and contact["assigned_agent_id"]:
            assigned_agent_id = str(contact["assigned_agent_id"])
            route_reason = "existing_contact_owner"
        else:
            assigned_agent_id, route_reason = await _choose_agent(
                conn, tenant_id, str(connector["source_key"]), payload
            )

        if contact is None:
            client = await conn.fetchrow(
                """
                INSERT INTO clients (
                    tenant_id,full_name,email,phone,client_type,stage,lead_score,
                    assignee_id,preferences,source
                ) VALUES ($1::uuid,$2,$3,$4,$5,'lead',0,$6,$7::jsonb,$8)
                RETURNING id
                """,
                tenant_id,
                payload.full_name,
                payload.email,
                payload.phone,
                payload.intent,
                assigned_agent_id,
                json.dumps({"zip_code": payload.zip_code, "state_code": payload.state_code}),
                connector["source_key"],
            )
            contact_ciphertext = await seal_json(
                conn,
                tenant_id,
                {"full_name": payload.full_name, "email": payload.email, "phone": payload.phone},
            )
            contact = await conn.fetchrow(
                """
                INSERT INTO agent_contacts (
                    tenant_id,assigned_agent_id,pii_ciphertext,email_lookup_hash,
                    phone_lookup_hash,name_search_tokens,timezone,state_code,source,
                    legacy_client_id,data_state
                ) VALUES ($1::uuid,$2,$3::bytea,$4,$5,$6::text[],'UTC',$7,$8,$9::uuid,'sealed')
                RETURNING id,assigned_agent_id,legacy_client_id
                """,
                tenant_id,
                assigned_agent_id,
                contact_ciphertext,
                email_hash,
                phone_hash,
                name_search_tokens(tenant_id, payload.full_name),
                payload.state_code,
                connector["source_key"],
                client["id"],
            )
            await conn.execute(
                "UPDATE clients SET contact_id=$2::uuid WHERE id=$1::uuid",
                client["id"],
                contact["id"],
            )
        elif assigned_agent_id and not contact["assigned_agent_id"]:
            await conn.execute(
                "UPDATE agent_contacts SET assigned_agent_id=$2 WHERE id=$1::uuid",
                contact["id"],
                assigned_agent_id,
            )

        if assigned_agent_id:
            await conn.execute(
                """
                INSERT INTO agent_routing_state (tenant_id,agent_id,last_assigned_at)
                VALUES ($1::uuid,$2,now())
                ON CONFLICT (tenant_id,agent_id) DO UPDATE
                    SET last_assigned_at=now(),updated_at=now()
                """,
                tenant_id,
                assigned_agent_id,
            )
        final_status = "routed" if assigned_agent_id else "unassigned"
        await conn.execute(
            """
            UPDATE lead_intake_events
               SET status=$2,contact_id=$3::uuid,assigned_agent_id=$4,
                   route_reason=$5,routed_at=now(),updated_at=now()
             WHERE id=$1::uuid
            """,
            event["id"],
            final_status,
            contact["id"],
            assigned_agent_id,
            route_reason,
        )

    # Speed-to-lead fires AFTER the intake transaction commits, deliberately.
    # Enqueuing inside it would make a queue hiccup roll back a lead we already
    # accepted and signed for, and the job's first act is to read the contact
    # row this transaction just wrote — which an uncommitted tx cannot serve.
    # Returns a state dict rather than raising; intake never fails on it.
    first_response = await enqueue_speed_to_lead(
        TenantContext(
            agent_id=f"lead-intake:{connector['id']}",
            tenant_id=tenant_id,
            role=Role.BROKER_OWNER,
        ),
        contact_id=str(contact["id"]),
        client_id=str(contact["legacy_client_id"]) if contact["legacy_client_id"] else None,
        intake_event_id=str(event["id"]),
        state_code=payload.state_code,
        lead_created_at=event["received_at"],
        reason=f"intake:{connector['source_key']}",
    )
    return {
        "event_id": str(event["id"]),
        "status": final_status,
        "accepted": True,
        "assigned": assigned_agent_id is not None,
        "existing_contact": duplicate,
        "idempotent_replay": False,
        # Surfaced so a connector operator can tell "we queued a first response"
        # from "the feature is off" without reading server logs.
        "first_response": first_response.get("state", "unknown"),
    }
