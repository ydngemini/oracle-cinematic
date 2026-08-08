"""Canonical contact, exact intake, and consent-gated nurture API."""

from __future__ import annotations

import base64
import json
import re
import uuid
from datetime import date, datetime, timezone
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from contact_truth import (
    INTAKE_QUESTION_SET_VERSION,
    INTAKE_TOOL_ACCESS,
    ContactTruthConfigError,
    evaluate_nurture,
    lookup_hash,
    name_query_tokens,
    name_search_tokens,
    normalize_email,
    normalize_full_name,
    normalize_intake_answers,
    normalize_phone,
    nurture_idempotency_key,
    open_json,
    questions_for,
    seal_json,
)
from db.connection import tenant_tx
from tenancy import TenantContext, require_context


router = APIRouter(prefix="/api/crm", tags=["CRM contacts"])

_CONTACT_CURSOR_VERSION = 1


def _uuid(value: str, field_name: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"{field_name} must be a UUID") from exc


def _json_object(value: Any, default: dict[str, Any]) -> dict[str, Any]:
    if value is None:
        return dict(default)
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return dict(default)
    return dict(value) if isinstance(value, dict) else dict(default)


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    try:
        return row[key]
    except (KeyError, TypeError):
        return default


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _encode_contact_cursor(updated_at: datetime, contact_id: Any) -> str:
    payload = {
        "v": _CONTACT_CURSOR_VERSION,
        "updated_at": updated_at.isoformat(),
        "id": str(contact_id),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode_contact_cursor(value: str | None) -> tuple[datetime | None, str | None]:
    if not value:
        return None, None
    try:
        padded = value + "=" * (-len(value) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        if payload.get("v") != _CONTACT_CURSOR_VERSION:
            raise ValueError
        updated_at = datetime.fromisoformat(str(payload["updated_at"]))
        if updated_at.tzinfo is None:
            raise ValueError
        return updated_at, _uuid(payload["id"], "cursor id")
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "cursor is invalid or expired",
        ) from exc


def _search_material(tenant_id: str, query: str | None) -> tuple[str | None, str | None, list[str]]:
    if not query:
        return None, None, []
    cleaned = " ".join(query.split())
    email_token: str | None = None
    phone_token: str | None = None
    try:
        email_token = lookup_hash(tenant_id, "email", normalize_email(cleaned))
    except ValueError:
        pass
    try:
        phone_token = lookup_hash(tenant_id, "phone", normalize_phone(cleaned))
    except ValueError:
        pass
    return email_token, phone_token, name_query_tokens(tenant_id, cleaned)


async def search_contact_rows(
    conn: Any,
    ctx: TenantContext,
    *,
    query: str | None,
    limit: int,
    cursor: str | None = None,
) -> tuple[list[Any], str | None]:
    """Search the complete contact table using tenant-keyed blind indexes."""
    cursor_at, cursor_id = _decode_contact_cursor(cursor)
    email_token, phone_token, name_tokens = _search_material(ctx.tenant_id, query)
    rows = await conn.fetch(
        _CONTACT_SELECT
        + """
         WHERE contact.deleted_at IS NULL
           AND (
                $1::text IS NULL
                OR contact.email_lookup_hash=$2::char(64)
                OR contact.phone_lookup_hash=$3::char(64)
                OR (cardinality($4::text[]) > 0 AND contact.name_search_tokens @> $4::text[])
           )
           AND (
                $5::timestamptz IS NULL
                OR (contact.updated_at,contact.id) < ($5::timestamptz,$6::uuid)
           )
         ORDER BY contact.updated_at DESC, contact.id DESC
         LIMIT $7
        """,
        query,
        email_token,
        phone_token,
        name_tokens,
        cursor_at,
        cursor_id,
        limit + 1,
    )
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        tail = rows[-1]
        next_cursor = _encode_contact_cursor(tail["updated_at"], tail["id"])
    return list(rows), next_cursor


async def _canonical_assignee(conn: Any, ctx: TenantContext, agent_id: str) -> str:
    row = await conn.fetchrow(
        """
        SELECT agent_id FROM users
         WHERE tenant_id=$1::uuid AND lower(agent_id)=lower($2) AND is_active=true
        """,
        ctx.tenant_id,
        agent_id,
    )
    if row is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "assigned_agent_id must identify an active user in this brokerage",
        )
    return str(row["agent_id"])


def _contact_service_unavailable() -> HTTPException:
    return HTTPException(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        "Encrypted contact storage is not configured.",
    )


class ConsentGrant(BaseModel):
    model_config = ConfigDict(extra="forbid")

    granted: bool = False
    captured_at: datetime | None = None
    source: str | None = Field(None, max_length=80)

    @model_validator(mode="after")
    def _recorded_consent(self):
        if self.granted and self.captured_at is None:
            raise ValueError("captured_at is required when consent is granted")
        if self.captured_at is not None and self.captured_at.tzinfo is None:
            raise ValueError("captured_at must include a timezone")
        return self


class ContactConsent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: ConsentGrant = Field(default_factory=ConsentGrant)
    sms: ConsentGrant = Field(default_factory=ConsentGrant)
    voice: ConsentGrant = Field(default_factory=ConsentGrant)


class ContactSuppression(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    global_suppressed: bool = Field(False, alias="global")
    email: bool = False
    sms: bool = False
    voice: bool = False
    dnc: bool = False


def _validate_timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("timezone is not recognized") from exc
    return value


def _validate_birthday(month: int | None, day: int | None) -> None:
    if month is None and day is None:
        return
    if month is None or day is None:
        raise ValueError("birthday_month and birthday_day must be provided together")
    try:
        date(2000, month, day)
    except ValueError as exc:
        raise ValueError("birthday month/day is invalid") from exc


class ContactCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    full_name: str = Field(..., min_length=1, max_length=160)
    email: str | None = Field(None, max_length=254)
    phone: str | None = Field(None, max_length=40)
    assigned_agent_id: str | None = Field(None, max_length=160)
    birthday_month: int | None = Field(None, ge=1, le=12)
    birthday_day: int | None = Field(None, ge=1, le=31)
    timezone: str = Field("UTC", max_length=100)
    state_code: str | None = Field(None, min_length=2, max_length=2)
    preferred_channel: Literal["none", "email", "sms", "voice"] = "none"
    consent: ContactConsent = Field(default_factory=ContactConsent)
    suppression: ContactSuppression = Field(default_factory=ContactSuppression)
    nurture_enabled: bool = True
    source: str | None = Field(None, max_length=80)
    client_id: str | None = None

    @field_validator("full_name")
    @classmethod
    def _name(cls, value: str) -> str:
        return normalize_full_name(value)

    @field_validator("email")
    @classmethod
    def _email(cls, value: str | None) -> str | None:
        return normalize_email(value)

    @field_validator("phone")
    @classmethod
    def _phone(cls, value: str | None) -> str | None:
        return normalize_phone(value)

    @field_validator("timezone")
    @classmethod
    def _timezone(cls, value: str) -> str:
        return _validate_timezone(value)

    @field_validator("state_code")
    @classmethod
    def _state_code(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip().upper()
        if not re.fullmatch(r"[A-Z]{2}", value):
            raise ValueError("state_code must be two letters")
        return value

    @field_validator("client_id")
    @classmethod
    def _client_id(cls, value: str | None) -> str | None:
        return _uuid(value, "client_id") if value else None

    @model_validator(mode="after")
    def _coherent_contact(self):
        _validate_birthday(self.birthday_month, self.birthday_day)
        if self.preferred_channel == "email" and not self.email:
            raise ValueError("email is required for preferred_channel=email")
        if self.preferred_channel in ("sms", "voice") and not self.phone:
            raise ValueError("phone is required for the selected preferred channel")
        if self.consent.email.granted and not self.email:
            raise ValueError("email is required when email consent is granted")
        if (self.consent.sms.granted or self.consent.voice.granted) and not self.phone:
            raise ValueError("phone is required when SMS or voice consent is granted")
        return self


class ContactPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    full_name: str | None = Field(None, min_length=1, max_length=160)
    email: str | None = Field(None, max_length=254)
    phone: str | None = Field(None, max_length=40)
    assigned_agent_id: str | None = Field(None, max_length=160)
    birthday_month: int | None = Field(None, ge=1, le=12)
    birthday_day: int | None = Field(None, ge=1, le=31)
    timezone: str | None = Field(None, max_length=100)
    state_code: str | None = Field(None, min_length=2, max_length=2)
    preferred_channel: Literal["none", "email", "sms", "voice"] | None = None
    consent: ContactConsent | None = None
    suppression: ContactSuppression | None = None
    nurture_enabled: bool | None = None
    source: str | None = Field(None, max_length=80)
    deleted: bool | None = None

    @field_validator("full_name")
    @classmethod
    def _name(cls, value: str | None) -> str | None:
        return normalize_full_name(value) if value is not None else None

    @field_validator("email")
    @classmethod
    def _email(cls, value: str | None) -> str | None:
        return normalize_email(value)

    @field_validator("phone")
    @classmethod
    def _phone(cls, value: str | None) -> str | None:
        return normalize_phone(value)

    @field_validator("timezone")
    @classmethod
    def _timezone(cls, value: str | None) -> str | None:
        return _validate_timezone(value) if value is not None else None

    @field_validator("state_code")
    @classmethod
    def _state_code(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip().upper()
        if not re.fullmatch(r"[A-Z]{2}", value):
            raise ValueError("state_code must be two letters")
        return value

    @model_validator(mode="after")
    def _birthday_pair(self):
        supplied = self.model_fields_set
        for field_name in (
            "timezone",
            "preferred_channel",
            "consent",
            "suppression",
            "nurture_enabled",
            "deleted",
        ):
            if field_name in supplied and getattr(self, field_name) is None:
                raise ValueError(f"{field_name} cannot be null")
        if "birthday_month" in supplied or "birthday_day" in supplied:
            if not {"birthday_month", "birthday_day"}.issubset(supplied):
                raise ValueError("birthday_month and birthday_day must be updated together")
            _validate_birthday(self.birthday_month, self.birthday_day)
        return self


class PropertyRelationshipCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_id: str | None = None
    property_ref_kind: Literal["lead", "listing", "public_record", "manual"]
    property_ref_id: str | None = None
    property_label: str | None = Field(None, max_length=400)
    relationship_type: Literal["owner", "seller", "buyer", "occupant", "other"]
    purchase_date: date | None = None
    closing_date: date | None = None
    anniversary_enabled: bool = True

    @field_validator("client_id", "property_ref_id")
    @classmethod
    def _ids(cls, value: str | None, info) -> str | None:
        return _uuid(value, info.field_name) if value else None

    @field_validator("property_label")
    @classmethod
    def _label(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("property_label must not be empty")
        return normalized

    @model_validator(mode="after")
    def _source_shape(self):
        if self.property_ref_kind == "manual":
            if self.property_ref_id is not None or not self.property_label:
                raise ValueError("manual properties require only property_label")
        elif self.property_ref_id is None:
            raise ValueError("source-backed properties require property_ref_id")
        return self


class IntakeSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    persona: Literal["buyer", "seller"]
    answers: list[str] = Field(..., min_length=3, max_length=3)
    transcript: str | None = Field(None, max_length=20_000)

    @field_validator("answers")
    @classmethod
    def _answers(cls, values: list[str]) -> list[str]:
        cleaned = [" ".join(value.split()) for value in values]
        if any(not value for value in cleaned):
            raise ValueError("all three answers are required")
        if any(len(value) > 2_000 for value in cleaned):
            raise ValueError("an answer exceeds the 2000 character limit")
        return cleaned


class NurtureJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_type: Literal["birthday", "home_anniversary"]
    channel: Literal["email", "sms"]
    relationship_id: str | None = None

    @field_validator("relationship_id")
    @classmethod
    def _relationship_id(cls, value: str | None) -> str | None:
        return _uuid(value, "relationship_id") if value else None

    @model_validator(mode="after")
    def _relationship_shape(self):
        if self.event_type == "birthday" and self.relationship_id is not None:
            raise ValueError("birthday jobs do not accept relationship_id")
        if self.event_type == "home_anniversary" and self.relationship_id is None:
            raise ValueError("home_anniversary jobs require relationship_id")
        return self


_CONTACT_SELECT = """
    SELECT contact.id, contact.assigned_agent_id, contact.pii_ciphertext,
           contact.email_lookup_hash, contact.phone_lookup_hash,
           contact.birthday_month, contact.birthday_day, contact.timezone,
           contact.state_code,
           contact.preferred_channel, contact.consent, contact.suppression,
           contact.nurture_enabled, contact.source, contact.legacy_client_id,
           contact.data_state, contact.deleted_at, contact.created_at,
           contact.updated_at,
           legacy.full_name AS legacy_full_name,
           legacy.email AS legacy_email,
           legacy.phone AS legacy_phone
      FROM agent_contacts AS contact
      LEFT JOIN LATERAL (
          SELECT client.full_name, client.email, client.phone
            FROM clients AS client
           WHERE client.contact_id = contact.id
              OR client.id = contact.legacy_client_id
           ORDER BY (client.id = contact.legacy_client_id) DESC, client.created_at
           LIMIT 1
      ) AS legacy ON true
"""


async def _contact_json(conn: Any, ctx: TenantContext, row: Any) -> dict[str, Any]:
    ciphertext = _row_get(row, "pii_ciphertext")
    if ciphertext:
        pii = await open_json(conn, ctx.tenant_id, ciphertext)
    else:
        legacy_email = _row_get(row, "legacy_email")
        legacy_phone = _row_get(row, "legacy_phone")
        try:
            legacy_email = normalize_email(legacy_email)
        except ValueError:
            pass
        try:
            legacy_phone = normalize_phone(legacy_phone)
        except ValueError:
            pass
        pii = {
            "full_name": _row_get(row, "legacy_full_name"),
            "email": legacy_email,
            "phone": legacy_phone,
        }
    return {
        "id": str(_row_get(row, "id")),
        "full_name": pii.get("full_name"),
        "email": pii.get("email"),
        "phone": pii.get("phone"),
        "assigned_agent_id": _row_get(row, "assigned_agent_id"),
        "birthday_month": _row_get(row, "birthday_month"),
        "birthday_day": _row_get(row, "birthday_day"),
        "timezone": _row_get(row, "timezone", "UTC"),
        "state_code": _row_get(row, "state_code"),
        "preferred_channel": _row_get(row, "preferred_channel", "none"),
        "consent": _json_object(_row_get(row, "consent"), {}),
        "suppression": _json_object(_row_get(row, "suppression"), {}),
        "nurture_enabled": bool(_row_get(row, "nurture_enabled", True)),
        "source": _row_get(row, "source"),
        "legacy_client_id": (
            str(_row_get(row, "legacy_client_id"))
            if _row_get(row, "legacy_client_id") else None
        ),
        "data_state": _row_get(row, "data_state", "sealed"),
        "deleted_at": _iso(_row_get(row, "deleted_at")),
        "created_at": _iso(_row_get(row, "created_at")),
        "updated_at": _iso(_row_get(row, "updated_at")),
    }


@router.get("/contacts")
async def list_contacts(
    q: str | None = Query(None, min_length=1, max_length=160),
    limit: int = Query(100, ge=1, le=200),
    cursor: str | None = Query(None, max_length=1024),
    ctx: TenantContext = Depends(require_context),
):
    try:
        async with tenant_tx(ctx) as conn:
            rows, next_cursor = await search_contact_rows(
                conn,
                ctx,
                query=q,
                limit=limit,
                cursor=cursor,
            )
            contacts = [await _contact_json(conn, ctx, row) for row in rows]
    except ContactTruthConfigError:
        raise _contact_service_unavailable()
    except RuntimeError:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Contact store is offline.")

    return {"contacts": contacts, "count": len(contacts), "next_cursor": next_cursor}


@router.post("/contacts", status_code=status.HTTP_201_CREATED)
async def create_contact(
    body: ContactCreate,
    ctx: TenantContext = Depends(require_context),
):
    pii = {"full_name": body.full_name, "email": body.email, "phone": body.phone}
    try:
        email_hash = lookup_hash(ctx.tenant_id, "email", body.email)
        phone_hash = lookup_hash(ctx.tenant_id, "phone", body.phone)
        search_tokens = name_search_tokens(ctx.tenant_id, body.full_name)
        async with tenant_tx(ctx) as conn:
            assigned_agent_id = (
                await _canonical_assignee(conn, ctx, body.assigned_agent_id)
                if body.assigned_agent_id
                else ctx.agent_id
            )
            if body.client_id:
                client = await conn.fetchrow(
                    "SELECT id,contact_id FROM clients WHERE id=$1::uuid",
                    body.client_id,
                )
                if client is None:
                    raise HTTPException(status.HTTP_404_NOT_FOUND, "client not found")
                if _row_get(client, "contact_id") is not None:
                    raise HTTPException(status.HTTP_409_CONFLICT, "client already has a contact")

            ciphertext = await seal_json(conn, ctx.tenant_id, pii)
            row = await conn.fetchrow(
                """
                INSERT INTO agent_contacts (
                    tenant_id,assigned_agent_id,pii_ciphertext,
                    email_lookup_hash,phone_lookup_hash,name_search_tokens,birthday_month,birthday_day,
                    timezone,state_code,preferred_channel,consent,suppression,nurture_enabled,
                    source,legacy_client_id,data_state
                ) VALUES (
                    $1::uuid,$2,$3::bytea,$4,$5,$6::text[],$7,$8,$9,$10,$11,$12::jsonb,$13::jsonb,
                    $14,$15,$16::uuid,'sealed'
                )
                RETURNING *
                """,
                ctx.tenant_id,
                assigned_agent_id,
                ciphertext,
                email_hash,
                phone_hash,
                search_tokens,
                body.birthday_month,
                body.birthday_day,
                body.timezone,
                body.state_code,
                body.preferred_channel,
                json.dumps(body.consent.model_dump(mode="json")),
                json.dumps(body.suppression.model_dump(mode="json", by_alias=True)),
                body.nurture_enabled,
                body.source,
                body.client_id,
            )
            if body.client_id:
                await conn.execute(
                    """
                    UPDATE clients
                       SET contact_id=$1::uuid, full_name=$2, email=$3, phone=$4
                     WHERE id=$5::uuid
                    """,
                    _row_get(row, "id"),
                    body.full_name,
                    body.email,
                    body.phone,
                    body.client_id,
                )
            contact = await _contact_json(conn, ctx, row)
    except ContactTruthConfigError:
        raise _contact_service_unavailable()
    except RuntimeError:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Contact store is offline.")
    return {"contact": contact}


@router.get("/contacts/{contact_id}")
async def get_contact(
    contact_id: str,
    ctx: TenantContext = Depends(require_context),
):
    try:
        contact_id = _uuid(contact_id, "contact_id")
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))
    try:
        async with tenant_tx(ctx) as conn:
            row = await conn.fetchrow(
                _CONTACT_SELECT + " WHERE contact.id=$1::uuid",
                contact_id,
            )
            if row is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "contact not found")
            contact = await _contact_json(conn, ctx, row)
    except ContactTruthConfigError:
        raise _contact_service_unavailable()
    except RuntimeError:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Contact store is offline.")
    return {"contact": contact}


@router.patch("/contacts/{contact_id}")
async def update_contact(
    contact_id: str,
    body: ContactPatch,
    ctx: TenantContext = Depends(require_context),
):
    try:
        contact_id = _uuid(contact_id, "contact_id")
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "no fields to update")

    pii_fields = {"full_name", "email", "phone"}
    try:
        async with tenant_tx(ctx) as conn:
            current = await conn.fetchrow(
                _CONTACT_SELECT + " WHERE contact.id=$1::uuid",
                contact_id,
            )
            if current is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "contact not found")
            current_json = await _contact_json(conn, ctx, current)

            merged_email = fields.get("email", current_json["email"])
            merged_phone = fields.get("phone", current_json["phone"])
            merged_preferred = fields.get(
                "preferred_channel", current_json["preferred_channel"]
            )
            merged_consent = (
                body.consent.model_dump(mode="json")
                if "consent" in fields and body.consent is not None
                else current_json["consent"]
            )

            def has_grant(channel: str) -> bool:
                grant = merged_consent.get(channel, {})
                return grant.get("granted") is True if isinstance(grant, dict) else grant is True

            if merged_preferred == "email" and not merged_email:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    "email is required for preferred_channel=email",
                )
            if merged_preferred in ("sms", "voice") and not merged_phone:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    "phone is required for the selected preferred channel",
                )
            if has_grant("email") and not merged_email:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    "email cannot be cleared while email consent is active",
                )
            if (has_grant("sms") or has_grant("voice")) and not merged_phone:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    "phone cannot be cleared while SMS or voice consent is active",
                )

            sets: list[str] = []
            args: list[Any] = []
            if pii_fields.intersection(fields):
                pii = {
                    "full_name": fields.get("full_name", current_json["full_name"]),
                    "email": fields.get("email", current_json["email"]),
                    "phone": fields.get("phone", current_json["phone"]),
                }
                if not pii["full_name"]:
                    raise HTTPException(
                        status.HTTP_422_UNPROCESSABLE_ENTITY,
                        "full_name cannot be cleared",
                    )
                ciphertext = await seal_json(conn, ctx.tenant_id, pii)
                for column, value, cast in (
                    ("pii_ciphertext", ciphertext, "::bytea"),
                    ("email_lookup_hash", lookup_hash(ctx.tenant_id, "email", pii["email"]), ""),
                    ("phone_lookup_hash", lookup_hash(ctx.tenant_id, "phone", pii["phone"]), ""),
                    ("name_search_tokens", name_search_tokens(ctx.tenant_id, pii["full_name"]), "::text[]"),
                    ("data_state", "sealed", ""),
                ):
                    args.append(value)
                    sets.append(f"{column}=${len(args)}{cast}")
            for column in (
                "assigned_agent_id", "birthday_month", "birthday_day", "timezone", "state_code",
                "preferred_channel", "nurture_enabled", "source",
            ):
                if column in fields:
                    value = fields[column]
                    if column == "assigned_agent_id" and value is not None:
                        value = await _canonical_assignee(conn, ctx, value)
                    args.append(value)
                    sets.append(f"{column}=${len(args)}")
            for column in ("consent", "suppression"):
                if column in fields:
                    value = getattr(body, column)
                    args.append(json.dumps(value.model_dump(mode="json", by_alias=True)))
                    sets.append(f"{column}=${len(args)}::jsonb")
            if "deleted" in fields:
                sets.append("deleted_at=now()" if fields["deleted"] else "deleted_at=NULL")

            args.append(contact_id)
            # Every interpolated column/cast above comes from this model's fixed
            # whitelist; all user values remain asyncpg bind parameters.
            await conn.execute(
                f"UPDATE agent_contacts SET {', '.join(sets)} "
                f"WHERE id=${len(args)}::uuid",
                *args,
            )
            if pii_fields.intersection(fields):
                await conn.execute(
                    """
                    UPDATE clients
                       SET full_name=$2, email=$3, phone=$4
                     WHERE contact_id=$1::uuid
                    """,
                    contact_id,
                    pii["full_name"],
                    pii["email"],
                    pii["phone"],
                )
            row = await conn.fetchrow(
                _CONTACT_SELECT + " WHERE contact.id=$1::uuid",
                contact_id,
            )
            contact = await _contact_json(conn, ctx, row)
    except ContactTruthConfigError:
        raise _contact_service_unavailable()
    except RuntimeError:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Contact store is offline.")
    return {"contact": contact}


@router.post(
    "/contacts/{contact_id}/properties",
    status_code=status.HTTP_201_CREATED,
)
async def create_property_relationship(
    contact_id: str,
    body: PropertyRelationshipCreate,
    ctx: TenantContext = Depends(require_context),
):
    try:
        contact_id = _uuid(contact_id, "contact_id")
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))
    try:
        async with tenant_tx(ctx) as conn:
            contact = await conn.fetchrow(
                "SELECT id FROM agent_contacts WHERE id=$1::uuid AND deleted_at IS NULL",
                contact_id,
            )
            if contact is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "contact not found")
            if body.property_ref_kind == "lead":
                property_source = await conn.fetchrow(
                    "SELECT id FROM leads WHERE id=$1::uuid",
                    body.property_ref_id,
                )
            elif body.property_ref_kind == "listing":
                property_source = await conn.fetchrow(
                    "SELECT id FROM listings WHERE id=$1::uuid",
                    body.property_ref_id,
                )
            elif body.property_ref_kind == "public_record":
                property_source = await conn.fetchrow(
                    "SELECT id FROM public_property_records WHERE id=$1::uuid",
                    body.property_ref_id,
                )
            else:
                property_source = {"id": None}
            if body.property_ref_kind != "manual" and property_source is None:
                # Tenant RLS intentionally makes a foreign-tenant private record
                # indistinguishable from a missing UUID.
                raise HTTPException(
                    status.HTTP_404_NOT_FOUND,
                    "property reference not found",
                )
            label_ciphertext = None
            if body.property_label:
                label_ciphertext = await seal_json(
                    conn, ctx.tenant_id, {"label": body.property_label},
                )
            row = await conn.fetchrow(
                """
                INSERT INTO contact_property_relationships (
                    tenant_id,contact_id,client_id,property_ref_kind,property_ref_id,
                    property_label_ciphertext,relationship_type,purchase_date,
                    closing_date,anniversary_enabled
                ) VALUES (
                    $1::uuid,$2::uuid,$3::uuid,$4,$5::uuid,$6::bytea,$7,$8,$9,$10
                ) RETURNING *
                """,
                ctx.tenant_id,
                contact_id,
                body.client_id,
                body.property_ref_kind,
                body.property_ref_id,
                label_ciphertext,
                body.relationship_type,
                body.purchase_date,
                body.closing_date,
                body.anniversary_enabled,
            )
    except ContactTruthConfigError:
        raise _contact_service_unavailable()
    except RuntimeError:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Contact store is offline.")
    return {
        "relationship": {
            "id": str(_row_get(row, "id")),
            "contact_id": contact_id,
            "client_id": str(_row_get(row, "client_id")) if _row_get(row, "client_id") else None,
            "property_ref_kind": _row_get(row, "property_ref_kind"),
            "property_ref_id": (
                str(_row_get(row, "property_ref_id"))
                if _row_get(row, "property_ref_id") else None
            ),
            "property_label": body.property_label,
            "relationship_type": _row_get(row, "relationship_type"),
            "purchase_date": _iso(_row_get(row, "purchase_date")),
            "closing_date": _iso(_row_get(row, "closing_date")),
            "anniversary_enabled": bool(_row_get(row, "anniversary_enabled")),
        }
    }


@router.get("/intake/questions/{persona}")
async def intake_questions(
    persona: Literal["buyer", "seller"],
    _ctx: TenantContext = Depends(require_context),
):
    return {
        "persona": persona,
        "version": INTAKE_QUESTION_SET_VERSION,
        "questions": list(questions_for(persona)),
        "tool_access": list(INTAKE_TOOL_ACCESS),
    }


@router.post(
    "/contacts/{contact_id}/intakes",
    status_code=status.HTTP_201_CREATED,
)
async def create_intake(
    contact_id: str,
    body: IntakeSubmission,
    ctx: TenantContext = Depends(require_context),
):
    try:
        contact_id = _uuid(contact_id, "contact_id")
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))
    questions = questions_for(body.persona)
    normalized = normalize_intake_answers(body.persona, body.answers)
    transcript = body.transcript or "\n".join(
        f"Q: {question}\nA: {answer}"
        for question, answer in zip(questions, body.answers, strict=True)
    )
    raw = {
        "version": INTAKE_QUESTION_SET_VERSION,
        "persona": body.persona,
        "questions": list(questions),
        "answers": body.answers,
    }
    try:
        async with tenant_tx(ctx) as conn:
            contact = await conn.fetchrow(
                """
                SELECT contact.id,contact.assigned_agent_id,
                       linked_client.id AS client_id
                  FROM agent_contacts AS contact
                  LEFT JOIN LATERAL (
                      SELECT id FROM clients
                       WHERE contact_id=contact.id
                       ORDER BY created_at LIMIT 1
                  ) AS linked_client ON true
                 WHERE contact.id=$1::uuid AND contact.deleted_at IS NULL
                """,
                contact_id,
            )
            if contact is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "contact not found")
            raw_ciphertext = await seal_json(conn, ctx.tenant_id, raw)
            normalized_ciphertext = await seal_json(conn, ctx.tenant_id, normalized)
            transcript_ciphertext = await seal_json(
                conn, ctx.tenant_id, {"transcript": transcript},
            )
            session = await conn.fetchrow(
                """
                INSERT INTO contact_intake_sessions (
                    tenant_id,contact_id,client_id,persona,question_set_version,
                    question_count,raw_answers_ciphertext,normalized_fields_ciphertext,
                    transcript_ciphertext,tool_access,status,created_by
                ) VALUES (
                    $1::uuid,$2::uuid,$3::uuid,$4,$5,3,$6::bytea,$7::bytea,
                    $8::bytea,ARRAY[]::text[],'handoff_pending',$9
                ) RETURNING id,created_at
                """,
                ctx.tenant_id,
                contact_id,
                _row_get(contact, "client_id"),
                body.persona,
                INTAKE_QUESTION_SET_VERSION,
                raw_ciphertext,
                normalized_ciphertext,
                transcript_ciphertext,
                ctx.agent_id,
            )
            task = await conn.fetchrow(
                """
                INSERT INTO intake_handoff_tasks (
                    tenant_id,intake_session_id,contact_id,client_id,title,
                    assigned_agent_id,due_at
                ) VALUES ($1::uuid,$2::uuid,$3::uuid,$4::uuid,$5,$6,now())
                RETURNING id,status,due_at
                """,
                ctx.tenant_id,
                _row_get(session, "id"),
                contact_id,
                _row_get(contact, "client_id"),
                f"Review {body.persona} intake",
                _row_get(contact, "assigned_agent_id") or ctx.agent_id,
            )
    except ContactTruthConfigError:
        raise _contact_service_unavailable()
    except RuntimeError:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Contact store is offline.")
    return {
        "intake": {
            "id": str(_row_get(session, "id")),
            "persona": body.persona,
            "version": INTAKE_QUESTION_SET_VERSION,
            "questions": list(questions),
            "normalized_fields": normalized,
            "tool_access": list(INTAKE_TOOL_ACCESS),
            "status": "handoff_pending",
            "created_at": _iso(_row_get(session, "created_at")),
        },
        "handoff_task": {
            "id": str(_row_get(task, "id")),
            "status": _row_get(task, "status"),
            "due_at": _iso(_row_get(task, "due_at")),
        },
    }


@router.post("/contacts/{contact_id}/nurture-jobs")
async def reserve_nurture_job(
    contact_id: str,
    body: NurtureJobRequest,
    ctx: TenantContext = Depends(require_context),
):
    try:
        contact_id = _uuid(contact_id, "contact_id")
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))
    now = datetime.now(timezone.utc)
    try:
        async with tenant_tx(ctx) as conn:
            contact = await conn.fetchrow(
                """
                SELECT id,birthday_month,birthday_day,timezone,consent,suppression,
                       nurture_enabled
                  FROM agent_contacts
                 WHERE id=$1::uuid AND deleted_at IS NULL
                """,
                contact_id,
            )
            if contact is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "contact not found")

            event_month = _row_get(contact, "birthday_month")
            event_day = _row_get(contact, "birthday_day")
            if body.event_type == "home_anniversary":
                relationship = await conn.fetchrow(
                    """
                    SELECT id,closing_date,purchase_date,anniversary_enabled
                      FROM contact_property_relationships
                     WHERE id=$1::uuid AND contact_id=$2::uuid
                    """,
                    body.relationship_id,
                    contact_id,
                )
                if relationship is None:
                    raise HTTPException(
                        status.HTTP_404_NOT_FOUND,
                        "contact property relationship not found",
                    )
                anniversary = _row_get(relationship, "closing_date") or _row_get(
                    relationship, "purchase_date"
                )
                event_month = anniversary.month if anniversary else None
                event_day = anniversary.day if anniversary else None
                nurture_enabled = bool(_row_get(contact, "nurture_enabled")) and bool(
                    _row_get(relationship, "anniversary_enabled")
                )
            else:
                nurture_enabled = bool(_row_get(contact, "nurture_enabled"))

            consent = _json_object(_row_get(contact, "consent"), {})
            suppression = _json_object(_row_get(contact, "suppression"), {})
            decision = evaluate_nurture(
                event_type=body.event_type,
                channel=body.channel,
                event_month=event_month,
                event_day=event_day,
                timezone_name=_row_get(contact, "timezone", "UTC"),
                consent=consent,
                suppression=suppression,
                nurture_enabled=nurture_enabled,
                now=now,
            )
            if not decision.eligible:
                return {
                    "created": False,
                    "job": None,
                    "decision": {
                        "eligible": False,
                        "reason": decision.reason,
                        "local_date": decision.local_date.isoformat(),
                        "timezone": decision.timezone,
                    },
                }

            key = nurture_idempotency_key(
                ctx.tenant_id,
                contact_id,
                body.event_type,
                body.channel,
                decision.calendar_year,
            )
            policy_snapshot = {
                "version": "nurture-policy-v1",
                "consent_confirmed": True,
                "suppression_checked": True,
                "quiet_hours": {"start": 20, "end": 8},
                "timezone": decision.timezone,
                "evaluated_at": now.isoformat(),
            }
            row = await conn.fetchrow(
                """
                INSERT INTO contact_nurture_jobs (
                    tenant_id,contact_id,relationship_id,event_type,channel,
                    calendar_year,idempotency_key,scheduled_for,state,policy_snapshot
                ) VALUES (
                    $1::uuid,$2::uuid,$3::uuid,$4,$5,$6,$7,$8,'scheduled',$9::jsonb
                )
                ON CONFLICT (tenant_id,contact_id,event_type,channel,calendar_year)
                DO NOTHING
                RETURNING id,state,scheduled_for,calendar_year,idempotency_key
                """,
                ctx.tenant_id,
                contact_id,
                body.relationship_id,
                body.event_type,
                body.channel,
                decision.calendar_year,
                key,
                now,
                json.dumps(policy_snapshot),
            )
            created = row is not None
            if row is None:
                row = await conn.fetchrow(
                    """
                    SELECT id,state,scheduled_for,calendar_year,idempotency_key
                      FROM contact_nurture_jobs
                     WHERE contact_id=$1::uuid AND event_type=$2
                       AND channel=$3 AND calendar_year=$4
                    """,
                    contact_id,
                    body.event_type,
                    body.channel,
                    decision.calendar_year,
                )
    except RuntimeError:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Contact store is offline.")
    return {
        "created": created,
        "job": {
            "id": str(_row_get(row, "id")),
            "state": _row_get(row, "state"),
            "scheduled_for": _iso(_row_get(row, "scheduled_for")),
            "calendar_year": _row_get(row, "calendar_year"),
            "idempotency_key": _row_get(row, "idempotency_key"),
        },
        "decision": {
            "eligible": True,
            "reason": decision.reason,
            "local_date": decision.local_date.isoformat(),
            "timezone": decision.timezone,
        },
    }
