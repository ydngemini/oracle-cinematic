"""Tenant-isolated persistence and safe internal mutations for AI chat."""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from fastapi import HTTPException, status

from ai_chat_models import SafeClientUpdate, SafeListingUpdate
from ai_tool_policy import READ_ONLY_TOOLS
# Aliased to the historical private names: ~50 call sites in this module
# use them, and renaming those would bury the real change.
from ai_tools_gated import TOOLS_HANDLED as _GATED_TOOL_NAMES, execute as _execute_gated_tool
from ai_tools_read import TOOLS_HANDLED as _READ_TOOL_NAMES, execute as _execute_read_tool
from record_json import clean as _clean, json_value as _json
from crypto import derive_tenant_key
from db.connection import tenant_tx
from rate_limiter import distributed_rate_limiter
from tenancy import TenantContext

logger = logging.getLogger("oracle.ai_chat_store")


def tenant_key(ctx: TenantContext) -> str:
    master = os.getenv("ORACLE_ENCRYPTION_MASTER_KEY", "")
    if not master:
        raise RuntimeError("AI chat encryption is unavailable")
    return derive_tenant_key(ctx.tenant_id, master)


async def _encrypt_text(conn, value: str, key: str) -> bytes:
    return await conn.fetchval("SELECT pgp_sym_encrypt($1::text, $2::text)", value, key)


async def _decrypt_text(conn, value: bytes, key: str) -> str:
    return await conn.fetchval("SELECT pgp_sym_decrypt($1::bytea, $2::text)", value, key)


async def _encrypt_bytes(conn, value: bytes, key: str) -> bytes:
    return await conn.fetchval("SELECT pgp_sym_encrypt_bytea($1::bytea, $2::text)", value, key)


async def _decrypt_bytes(conn, value: bytes, key: str) -> bytes:
    return await conn.fetchval("SELECT pgp_sym_decrypt_bytea($1::bytea, $2::text)", value, key)


async def resolve_record(conn, ctx: TenantContext, record_type: str, record_id: str) -> dict:
    """Resolve an owned record; public/shared rows are never valid write anchors."""
    queries = {
        "client": """
            SELECT id, full_name AS label, full_name, email, phone, client_type,
                   stage, lead_score, assignee_id, company, preferences, source, notes,
                   updated_at
              FROM clients
             WHERE id=$1::uuid AND tenant_id=$2::uuid AND archived_at IS NULL
        """,
        "lead": """
            SELECT id, COALESCE(payload->>'address', parcel_id) AS label, parcel_id,
                   state, motivation_score, underwriting, payload, dossier_status,
                   updated_at
              FROM leads
             WHERE id=$1::uuid AND tenant_id=$2::uuid
        """,
        "listing": """
            SELECT id, address AS label, address, price, status, lead_id,
                   seller_client_id, updated_at
              FROM listings
             WHERE id=$1::uuid AND tenant_id=$2::uuid
        """,
        "contract": """
            SELECT id, (document_type || ' · ' || template_key) AS label,
                   document_type, template_key, template_version, status,
                   attorney_review_required, lead_id, transaction_id, metadata,
                   updated_at
              FROM contract_documents
             WHERE id=$1::uuid AND tenant_id=$2::uuid
        """,
    }
    query = queries.get(record_type)
    if not query:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Unsupported record type")
    row = await conn.fetchrow(query, record_id, ctx.tenant_id)
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Record not found")
    return _clean(dict(row))


async def list_records(ctx: TenantContext, search: str = "", limit: int = 40) -> list[dict]:
    term = f"%{search.strip()}%"
    per_type = max(4, min(20, limit // 3 or 4))
    async with tenant_tx(ctx) as conn:
        clients = await conn.fetch(
            """SELECT id, full_name AS label, 'client' AS type, stage AS detail
                 FROM clients WHERE tenant_id=$1::uuid AND archived_at IS NULL
                   AND ($2='' OR full_name ILIKE $3 OR COALESCE(email,'') ILIKE $3)
                 ORDER BY updated_at DESC LIMIT $4""",
            ctx.tenant_id, search.strip(), term, per_type,
        )
        listings = await conn.fetch(
            """SELECT id, address AS label, 'listing' AS type, status AS detail
                 FROM listings WHERE tenant_id=$1::uuid
                   AND ($2='' OR address ILIKE $3)
                 ORDER BY updated_at DESC LIMIT $4""",
            ctx.tenant_id, search.strip(), term, per_type,
        )
        leads = await conn.fetch(
            """SELECT id, COALESCE(payload->>'address', parcel_id) AS label,
                       'lead' AS type, state AS detail
                 FROM leads WHERE tenant_id=$1::uuid
                   AND ($2='' OR parcel_id ILIKE $3 OR COALESCE(payload->>'address','') ILIKE $3)
                 ORDER BY updated_at DESC LIMIT $4""",
            ctx.tenant_id, search.strip(), term, per_type,
        )
        contracts = await conn.fetch(
            """SELECT id, (document_type || ' · ' || template_key) AS label,
                       'contract' AS type, status AS detail
                 FROM contract_documents WHERE tenant_id=$1::uuid
                   AND ($2='' OR document_type ILIKE $3 OR template_key ILIKE $3)
                 ORDER BY updated_at DESC LIMIT $4""",
            ctx.tenant_id, search.strip(), term, per_type,
        )
    rows = [*clients, *listings, *leads, *contracts]
    return [
        {"id": str(row["id"]), "type": row["type"], "label": row["label"], "detail": row["detail"]}
        for row in rows[:limit]
    ]


async def create_chat_request(
    ctx: TenantContext,
    *,
    request_id: str,
    content: str,
    context: Optional[dict],
    attachment_ids: list[str],
) -> dict:
    key = tenant_key(ctx)
    slot_reserved = False
    redis_request_claimed = False
    try:
        async with tenant_tx(ctx) as conn:
            # Redis provides the cross-replica fast path. Duplicate detection
            # precedes the concurrency reservation so retries never consume a
            # second active-response slot.
            async with distributed_rate_limiter() as limiter:
                if limiter is not None:
                    allowed, _ = await limiter.check_rate_limit(
                        ctx, max_requests=20, window_seconds=60
                    )
                    if not allowed:
                        raise HTTPException(
                            status.HTTP_429_TOO_MANY_REQUESTS,
                            "Please wait before sending another message",
                        )
                    is_duplicate, existing_id = await limiter.check_duplicate_request(
                        ctx, request_id
                    )
                    if is_duplicate:
                        existing = await conn.fetchrow(
                            """SELECT id, status FROM ai_chat_messages
                                WHERE tenant_id=$1::uuid AND user_id=$2
                                  AND request_id=$3::uuid AND role='assistant'""",
                            ctx.tenant_id, ctx.agent_id, request_id,
                        )
                        if existing:
                            return {
                                "assistant_id": str(existing["id"]),
                                "duplicate": True,
                                "status": existing["status"],
                            }
                        if existing_id and existing_id != "processing":
                            return {
                                "assistant_id": existing_id,
                                "duplicate": True,
                                "status": "completed",
                            }
                        raise HTTPException(
                            status.HTTP_409_CONFLICT,
                            "This request is already being processed",
                        )
                    redis_request_claimed = True
                    allowed, _ = await limiter.check_concurrency_limit(
                        ctx, max_active=2
                    )
                    if not allowed:
                        await limiter.mark_request_failed(ctx, request_id)
                        redis_request_claimed = False
                        raise HTTPException(
                            status.HTTP_429_TOO_MANY_REQUESTS,
                            "Two responses are already in progress",
                        )
                    slot_reserved = True

            # Serialize admission for this agent across backend replicas. The
            # database remains authoritative when Redis is unavailable.
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1::text, 0))",
                f"ai-chat:{ctx.tenant_id}:{ctx.agent_id}",
            )
            existing = await conn.fetchrow(
                """SELECT id, status FROM ai_chat_messages
                    WHERE tenant_id=$1::uuid AND user_id=$2 AND request_id=$3::uuid
                      AND role='assistant'""",
                ctx.tenant_id, ctx.agent_id, request_id,
            )
            if existing:
                if slot_reserved:
                    await release_concurrency(ctx)
                    slot_reserved = False
                if redis_request_claimed:
                    async with distributed_rate_limiter() as limiter:
                        if limiter is not None:
                            try:
                                await limiter.mark_request_completed(
                                    ctx, request_id, str(existing["id"])
                                )
                            except Exception:  # noqa: BLE001 - DB row proves the duplicate
                                logger.warning(
                                    "Redis duplicate marker failed for request_id=%s",
                                    request_id,
                                    exc_info=True,
                                )
                return {
                    "assistant_id": str(existing["id"]),
                    "duplicate": True,
                    "status": existing["status"],
                }
            active_count = await conn.fetchval(
                """SELECT count(*) FROM ai_chat_messages
                    WHERE tenant_id=$1::uuid AND user_id=$2 AND role='assistant'
                      AND status IN ('pending','streaming')""",
                ctx.tenant_id, ctx.agent_id,
            )
            if int(active_count) >= 2:
                raise HTTPException(
                    status.HTTP_429_TOO_MANY_REQUESTS,
                    "Two responses are already in progress",
                )
            recent_count = await conn.fetchval(
                """SELECT count(*) FROM ai_chat_messages
                    WHERE tenant_id=$1::uuid AND user_id=$2 AND role='user'
                      AND created_at > now() - interval '1 minute'""",
                ctx.tenant_id, ctx.agent_id,
            )
            if int(recent_count) >= 20:
                raise HTTPException(
                    status.HTTP_429_TOO_MANY_REQUESTS,
                    "Please wait before sending another message",
                )

            context_type = context.get("type") if context else None
            context_id = str(context.get("id")) if context else None
            if context:
                await resolve_record(conn, ctx, context_type, context_id)

            if attachment_ids:
                attachment_uuid_ids = [uuid.UUID(value) for value in attachment_ids]
                rows = await conn.fetch(
                    """SELECT id, record_type, record_id FROM ai_record_attachments
                        WHERE id=ANY($1::uuid[]) AND tenant_id=$2::uuid
                          AND owner_agent_id=$3 AND deleted_at IS NULL""",
                    attachment_uuid_ids, ctx.tenant_id, ctx.agent_id,
                )
                if len(rows) != len(attachment_ids):
                    raise HTTPException(
                        status.HTTP_422_UNPROCESSABLE_ENTITY,
                        "One or more attachments are unavailable",
                    )
                if any(
                    row["record_type"] != context_type
                    or str(row["record_id"]) != context_id
                    for row in rows
                ):
                    raise HTTPException(
                        status.HTTP_422_UNPROCESSABLE_ENTITY,
                        "Attachments must belong to the selected record",
                    )

            user_ct = await _encrypt_text(conn, content, key)
            assistant_ct = await _encrypt_text(conn, "", key)
            user_row = await conn.fetchrow(
                """INSERT INTO ai_chat_messages
                       (tenant_id,user_id,role,content_ciphertext,status,request_id,context_type,context_id)
                     VALUES ($1::uuid,$2,'user',$3,'completed',$4::uuid,$5,$6::uuid)
                     RETURNING id,created_at""",
                ctx.tenant_id, ctx.agent_id, user_ct, request_id,
                context_type, context_id,
            )
            assistant_row = await conn.fetchrow(
                """INSERT INTO ai_chat_messages
                       (tenant_id,user_id,role,content_ciphertext,status,request_id,context_type,context_id)
                     VALUES ($1::uuid,$2,'assistant',$3,'pending',$4::uuid,$5,$6::uuid)
                     RETURNING id,created_at""",
                ctx.tenant_id, ctx.agent_id, assistant_ct, request_id,
                context_type, context_id,
            )
            for attachment_id in attachment_ids:
                await conn.execute(
                    """INSERT INTO ai_chat_message_attachments
                           (tenant_id,message_id,attachment_id)
                         VALUES ($1::uuid,$2::uuid,$3::uuid)""",
                    ctx.tenant_id, user_row["id"], attachment_id,
                )
    except BaseException:
        if slot_reserved:
            await release_concurrency(ctx)
        if redis_request_claimed:
            async with distributed_rate_limiter() as limiter:
                if limiter is not None:
                    await limiter.mark_request_failed(ctx, request_id)
        raise

    async with distributed_rate_limiter() as limiter:
        if limiter is not None:
            try:
                await limiter.mark_request_completed(
                    ctx, request_id, str(assistant_row["id"])
                )
            except Exception:  # noqa: BLE001 - PostgreSQL remains authoritative
                logger.warning(
                    "Redis request completion marker failed for request_id=%s",
                    request_id,
                    exc_info=True,
                )
    return {
        "duplicate": False,
        "user_id": str(user_row["id"]),
        "assistant_id": str(assistant_row["id"]),
        "created_at": assistant_row["created_at"].isoformat(),
    }


async def release_concurrency(ctx: TenantContext) -> int:
    """Release a concurrency slot (decrement active response counter).

    Used when a response fails to start or completes.
    """
    async with distributed_rate_limiter() as limiter:
        if limiter is not None:
            return await limiter.release_concurrency(ctx)
    return 0


async def active_message_count(ctx: TenantContext) -> int:
    async with tenant_tx(ctx) as conn:
        return int(await conn.fetchval(
            """SELECT count(*) FROM ai_chat_messages
                WHERE tenant_id=$1::uuid AND user_id=$2 AND role='assistant'
                  AND status IN ('pending','streaming')""",
            ctx.tenant_id, ctx.agent_id,
        ))


async def recent_request_count(ctx: TenantContext) -> int:
    async with tenant_tx(ctx) as conn:
        return int(await conn.fetchval(
            """SELECT count(*) FROM ai_chat_messages
                WHERE tenant_id=$1::uuid AND user_id=$2 AND role='user'
                  AND created_at > now() - interval '1 minute'""",
            ctx.tenant_id, ctx.agent_id,
        ))


async def list_messages(
    ctx: TenantContext, *, before: Optional[datetime] = None, limit: int = 50
) -> list[dict]:
    key = tenant_key(ctx)
    async with tenant_tx(ctx) as conn:
        rows = await conn.fetch(
            """SELECT m.*, COALESCE(jsonb_agg(jsonb_build_object(
                         'id',a.id,'filename',a.filename,'media_type',a.media_type,
                         'byte_size',a.byte_size,'record_type',a.record_type,'record_id',a.record_id
                       )) FILTER (WHERE a.id IS NOT NULL), '[]'::jsonb) AS attachments
                   FROM ai_chat_messages m
              LEFT JOIN ai_chat_message_attachments ma ON ma.message_id=m.id
              LEFT JOIN ai_record_attachments a
                ON a.id=ma.attachment_id
               AND a.tenant_id=$1::uuid
               AND a.owner_agent_id=$2
               AND a.deleted_at IS NULL
                  WHERE m.tenant_id=$1::uuid AND m.user_id=$2
                    AND ($3::timestamptz IS NULL OR m.created_at < $3)
               GROUP BY m.id
               ORDER BY m.created_at DESC, m.id DESC LIMIT $4""",
            ctx.tenant_id, ctx.agent_id, before, max(1, min(100, limit)),
        )
        action_rows = await conn.fetch(
            """SELECT id,message_id,record_type,record_id,after_ciphertext,status,undo_expires_at
                 FROM ai_chat_actions
                WHERE tenant_id=$1::uuid AND user_id=$2 AND message_id=ANY($3::uuid[])""",
            ctx.tenant_id, ctx.agent_id, [row["id"] for row in rows],
        ) if rows else []
        actions_by_message: dict[str, list[dict]] = {}
        for action in action_rows:
            fields = json.loads(await _decrypt_text(conn, action["after_ciphertext"], key))
            actions_by_message.setdefault(str(action["message_id"]), []).append({
                "action_id": str(action["id"]), "record_type": action["record_type"],
                "record_id": str(action["record_id"]), "fields": fields,
                "status": action["status"], "undo_expires_at": action["undo_expires_at"].isoformat(),
            })
        result = []
        for row in reversed(rows):
            result.append({
                "id": str(row["id"]),
                "request_id": str(row["request_id"]),
                "role": row["role"],
                "content": await _decrypt_text(conn, row["content_ciphertext"], key),
                "status": row["status"],
                "context": ({"type": row["context_type"], "id": str(row["context_id"])}
                            if row["context_type"] else None),
                "attachments": _clean(_json(row["attachments"], [])),
                "model_id": row["model_id"],
                "error_code": row["error_code"],
                "actions": actions_by_message.get(str(row["id"]), []),
                "created_at": row["created_at"].isoformat(),
                "updated_at": row["updated_at"].isoformat(),
            })
    return result


async def save_attachment(
    ctx: TenantContext, *, record_type: str, record_id: str, filename: str,
    media_type: str, data: bytes, digest: str, extracted_text: Optional[str], scan_status: str,
) -> dict:
    key = tenant_key(ctx)
    async with tenant_tx(ctx) as conn:
        record = await resolve_record(conn, ctx, record_type, record_id)
        body_ct = await _encrypt_bytes(conn, data, key)
        text_ct = await _encrypt_text(conn, extracted_text, key) if extracted_text else None
        row = await conn.fetchrow(
            """INSERT INTO ai_record_attachments
                   (tenant_id,owner_agent_id,record_type,record_id,filename,media_type,byte_size,sha256,
                    bytes_ciphertext,extracted_text_ciphertext,scan_status,created_by)
                 VALUES ($1::uuid,$2,$3,$4::uuid,$5,$6,$7,$8,$9,$10,$11,$12)
                 RETURNING id,created_at""",
            ctx.tenant_id, ctx.agent_id, record_type, record_id, filename, media_type,
            len(data), digest, body_ct, text_ct, scan_status, ctx.agent_id,
        )
    return {
        "id": str(row["id"]), "record_type": record_type, "record_id": record_id,
        "record_label": record["label"], "filename": filename, "media_type": media_type,
        "byte_size": len(data), "scan_status": scan_status, "created_at": row["created_at"].isoformat(),
    }


async def list_attachments(ctx: TenantContext, record_type: str, record_id: str) -> list[dict]:
    async with tenant_tx(ctx) as conn:
        await resolve_record(conn, ctx, record_type, record_id)
        rows = await conn.fetch(
            """SELECT id,filename,media_type,byte_size,sha256,scan_status,created_at
                 FROM ai_record_attachments
                WHERE tenant_id=$1::uuid AND owner_agent_id=$2
                  AND record_type=$3 AND record_id=$4::uuid
                  AND deleted_at IS NULL ORDER BY created_at DESC""",
            ctx.tenant_id, ctx.agent_id, record_type, record_id,
        )
    return [{**_clean(dict(row)), "id": str(row["id"])} for row in rows]


async def get_attachment(ctx: TenantContext, attachment_id: str) -> tuple[dict, bytes]:
    key = tenant_key(ctx)
    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            """SELECT * FROM ai_record_attachments
                WHERE id=$1::uuid AND tenant_id=$2::uuid
                  AND owner_agent_id=$3 AND deleted_at IS NULL""",
            attachment_id, ctx.tenant_id, ctx.agent_id,
        )
        if not row:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Attachment not found")
        data = await _decrypt_bytes(conn, row["bytes_ciphertext"], key)
    return _clean(dict(row)), data


async def delete_attachment(ctx: TenantContext, attachment_id: str) -> None:
    async with tenant_tx(ctx) as conn:
        linked = await conn.fetchval(
            """SELECT EXISTS(SELECT 1 FROM ai_chat_message_attachments ma
                 JOIN ai_chat_messages m ON m.id=ma.message_id
                WHERE ma.attachment_id=$1::uuid AND m.status IN ('pending','streaming'))""",
            attachment_id,
        )
        if linked:
            raise HTTPException(status.HTTP_409_CONFLICT, "Attachment is being analyzed")
        result = await conn.execute(
            """UPDATE ai_record_attachments SET deleted_at=now()
                WHERE id=$1::uuid AND tenant_id=$2::uuid
                  AND owner_agent_id=$3 AND deleted_at IS NULL""",
            attachment_id, ctx.tenant_id, ctx.agent_id,
        )
        if result.endswith("0"):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Attachment not found")


async def load_response_bundle(ctx: TenantContext, assistant_id: str) -> dict:
    key = tenant_key(ctx)
    async with tenant_tx(ctx) as conn:
        assistant = await conn.fetchrow(
            """SELECT * FROM ai_chat_messages
                WHERE id=$1::uuid AND tenant_id=$2::uuid AND user_id=$3 AND role='assistant'""",
            assistant_id, ctx.tenant_id, ctx.agent_id,
        )
        if not assistant:
            raise RuntimeError("Assistant message is unavailable")
        rows = await conn.fetch(
            """SELECT * FROM ai_chat_messages
                WHERE tenant_id=$1::uuid AND user_id=$2 AND created_at <= $3
                ORDER BY created_at DESC LIMIT 24""",
            ctx.tenant_id, ctx.agent_id, assistant["created_at"],
        )
        messages = []
        for row in reversed(rows):
            if row["id"] == assistant["id"]:
                continue
            messages.append({
                "id": str(row["id"]), "role": row["role"],
                "content": await _decrypt_text(conn, row["content_ciphertext"], key),
            })
        record = None
        if assistant["context_type"]:
            record = await resolve_record(
                conn, ctx, assistant["context_type"], str(assistant["context_id"])
            )
        attachment_rows = await conn.fetch(
            """SELECT a.* FROM ai_record_attachments a
                 JOIN ai_chat_message_attachments ma ON ma.attachment_id=a.id
                 JOIN ai_chat_messages m ON m.id=ma.message_id
                WHERE m.request_id=$1::uuid AND m.user_id=$2
                  AND a.tenant_id=$3::uuid AND a.owner_agent_id=$2
                  AND a.deleted_at IS NULL""",
            assistant["request_id"], ctx.agent_id, ctx.tenant_id,
        )
        attachments = []
        for row in attachment_rows:
            attachments.append({
                "id": str(row["id"]), "filename": row["filename"],
                "media_type": row["media_type"],
                "data": await _decrypt_bytes(conn, row["bytes_ciphertext"], key),
                "extracted_text": (await _decrypt_text(conn, row["extracted_text_ciphertext"], key)
                                  if row["extracted_text_ciphertext"] else None),
            })
    return {"assistant": _clean(dict(assistant)), "messages": messages, "record": record,
            "attachments": attachments}


async def update_assistant(
    ctx: TenantContext, assistant_id: str, *, content: str, status_value: str,
    model_id: Optional[str] = None, error_code: Optional[str] = None,
) -> None:
    key = tenant_key(ctx)
    async with tenant_tx(ctx) as conn:
        ciphertext = await _encrypt_text(conn, content, key)
        await conn.execute(
            """UPDATE ai_chat_messages SET content_ciphertext=$1,status=$2,model_id=$3,error_code=$4
                WHERE id=$5::uuid AND tenant_id=$6::uuid AND user_id=$7""",
            ciphertext, status_value, model_id, error_code, assistant_id, ctx.tenant_id, ctx.agent_id,
        )


# Both this module and ``ai_chat_agent`` used to carry their own literal list
# of read-only tool names, and they had drifted apart. Membership decides
# whether a tool needs a selected record (below) and whether a result is
# broadcast as an applied record change (``ai_chat_agent._is_record_change``),
# so two answers to "is this a mutation?" was a bug waiting for a caller.
# ``call_contact`` was in this list while its handler creates a LIVE_CALL
# approval. It is classified LIVE_CALL now and, unlike before, requires a
# selected client: it used to take a model-supplied phone number.
_READ_ONLY_TOOLS = READ_ONLY_TOOLS


# The catalog in ``ai_chat_agent`` deliberately describes the product roadmap,
# but an LLM must only be offered tools that can complete against a verified
# local data source.  External MLS, public-record, legal, and billing tools
# stay out of the model surface until their licensed provider/workflow exists.
# This is distinct from the broader read-only catalog above, which is used to
# decide whether a direct tool request needs a selected write anchor.
#
# ``_READ_TOOL_NAMES`` is unioned in rather than listed by hand: a name is
# offered to the model exactly when ``ai_tools_read`` has a handler for it, so
# the allowlist cannot drift ahead of the implementation and advertise a tool
# that would answer "not implemented in this execution path".
_AGENT_AVAILABLE_TOOLS = frozenset({
    "codebase_summary",
    "web_search",
    "search_clients",
    "get_client_detail",
    "list_client_tasks",
    "list_client_activity",
    "get_client_contact_history",
    "list_deals",
    "get_deal_detail",
    "track_deadlines",
    "get_team_pipeline",
    "list_providers",
    "update_client",
    "add_client_note",
    "set_client_stage",
    "add_client_tag",
    "update_listing",
    "move_deal_stage",
    # P11 internal writes. Each records an ai_chat_actions row, so each is
    # reversible from its receipt; create_client is recorded but declares
    # itself not undoable, because deleting a client cascades to ten tables.
    "score_client_lead",
    "assign_client",
    "archive_client",
    "create_client",
    "create_deal_note",
    # P11 gated outreach. Each stages a command_executions row for approval and
    # touches no provider; the send happens in commands_api after a human
    # decides, behind guard_outreach.
    "draft_email",
    "draft_sms",
    "call_contact",
    # P12 financial / legal / calendar. Same shape: a request with an approval
    # id, and execution only through the human decision path.
    "draft_contract",
    "schedule_event",
    "publish_to_marketplace",
} | _READ_TOOL_NAMES)


def is_agent_tool_available(tool_name: str) -> bool:
    """Whether a tool is safe to advertise to a chat model in this release."""
    return tool_name in _AGENT_AVAILABLE_TOOLS


def _selected_uuid(
    *,
    tool_input: dict,
    input_name: str,
    context_type: Optional[str],
    context_id: Optional[str],
    expected_context: str,
) -> tuple[Optional[str], Optional[dict]]:
    raw_value = str(tool_input.get(input_name) or "").strip()
    try:
        target_id = str(uuid.UUID(raw_value))
    except (ValueError, AttributeError):
        return None, {"ok": False, "error": f"{input_name} must be a UUID."}
    try:
        selected_id = str(uuid.UUID(str(context_id or "")))
    except (ValueError, AttributeError):
        selected_id = ""
    if context_type != expected_context or target_id != selected_id:
        return None, {
            "ok": False,
            "error": f"I can only update the selected {expected_context}.",
        }
    return target_id, None


def _tool_uuid(tool_input: dict, input_name: str) -> tuple[Optional[str], Optional[dict]]:
    """Validate an untrusted tool UUID before it reaches a tenant query."""
    try:
        return str(uuid.UUID(str(tool_input.get(input_name) or "").strip())), None
    except (ValueError, AttributeError):
        return None, {"ok": False, "error": f"{input_name} must be a UUID."}


def _safe_search_term(value: object) -> tuple[Optional[str], Optional[dict]]:
    term = str(value or "").strip()
    if not 1 <= len(term) <= 160:
        return None, {"ok": False, "error": "query must be 1-160 characters."}
    # Avoid silently turning user-entered '%' and '_' into unbounded wildcard
    # searches. The string remains a query parameter, never SQL text.
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%", None


async def _read_clients(conn, ctx: TenantContext, tool_name: str, tool_input: dict) -> dict:
    if tool_name == "search_clients":
        term, error = _safe_search_term(tool_input.get("query"))
        if error:
            return error
        rows = await conn.fetch(
            """
            SELECT c.id,c.full_name,c.email,c.phone,c.client_type,c.stage,
                   c.lead_score,c.assignee_id,c.company,c.last_contacted_at,c.updated_at,
                   COALESCE(array_agg(DISTINCT ct.tag)
                       FILTER (WHERE ct.tag IS NOT NULL), ARRAY[]::text[]) AS tags
              FROM clients c
              LEFT JOIN client_tags ct
                ON ct.client_id=c.id AND ct.tenant_id=c.tenant_id
             WHERE c.tenant_id=$1::uuid AND c.archived_at IS NULL
               AND (
                   c.full_name ILIKE $2 ESCAPE '\\'
                   OR COALESCE(c.email,'') ILIKE $2 ESCAPE '\\'
                   OR COALESCE(c.phone,'') ILIKE $2 ESCAPE '\\'
                   OR COALESCE(c.company,'') ILIKE $2 ESCAPE '\\'
                   OR EXISTS (
                       SELECT 1 FROM client_tags search_tag
                        WHERE search_tag.tenant_id=c.tenant_id
                          AND search_tag.client_id=c.id
                          AND search_tag.tag ILIKE $2 ESCAPE '\\'
                   )
               )
             GROUP BY c.id
             ORDER BY c.lead_score DESC,c.updated_at DESC
             LIMIT 20
            """,
            ctx.tenant_id, term,
        )
        return {
            "ok": True,
            "action_type": tool_name,
            "count": len(rows),
            "clients": [_clean(dict(row)) for row in rows],
        }

    client_id, error = _tool_uuid(tool_input, "client_id")
    if error:
        return error
    client = await conn.fetchrow(
        """
        SELECT id,full_name,email,phone,client_type,stage,lead_score,assignee_id,
               company,preferences,source,last_contacted_at,created_at,updated_at
          FROM clients
         WHERE id=$1::uuid AND tenant_id=$2::uuid AND archived_at IS NULL
        """,
        client_id, ctx.tenant_id,
    )
    if not client:
        return {"ok": False, "error": "Client not found."}

    if tool_name == "get_client_detail":
        # tenant_tx yields one asyncpg connection. Execute these independently;
        # concurrent operations on the same connection are rejected by asyncpg.
        tags = await conn.fetch(
            """SELECT tag FROM client_tags
                 WHERE client_id=$1::uuid AND tenant_id=$2::uuid
                 ORDER BY lower(tag) LIMIT 50""",
            client_id, ctx.tenant_id,
        )
        notes = await conn.fetch(
            """SELECT id,body,author_id,pinned,created_at,updated_at
                 FROM client_notes
                WHERE client_id=$1::uuid AND tenant_id=$2::uuid
                ORDER BY pinned DESC,created_at DESC LIMIT 20""",
            client_id, ctx.tenant_id,
        )
        open_tasks = await conn.fetchval(
            """SELECT count(*) FROM client_tasks
                 WHERE client_id=$1::uuid AND tenant_id=$2::uuid AND status='open'""",
            client_id, ctx.tenant_id,
        )
        result = _clean(dict(client))
        result.update({
            "tags": [row["tag"] for row in tags],
            "notes": [_clean(dict(row)) for row in notes],
            "open_task_count": int(open_tasks or 0),
        })
        return {"ok": True, "action_type": tool_name, "client": result}

    if tool_name == "list_client_activity":
        rows = await conn.fetch(
            """SELECT id,kind,summary,meta,actor,created_at
                 FROM client_activities
                WHERE client_id=$1::uuid AND tenant_id=$2::uuid
                ORDER BY created_at DESC LIMIT 50""",
            client_id, ctx.tenant_id,
        )
        return {
            "ok": True, "action_type": tool_name, "client_id": client_id,
            "activity": [_clean(dict(row)) for row in rows],
        }

    rows = await conn.fetch(
        """SELECT id,kind,summary,meta,actor,created_at
             FROM client_activities
            WHERE client_id=$1::uuid AND tenant_id=$2::uuid
              AND kind IN ('message','showing')
            ORDER BY created_at DESC LIMIT 50""",
        client_id, ctx.tenant_id,
    )
    return {
        "ok": True, "action_type": tool_name, "client_id": client_id,
        "contact_history": [_clean(dict(row)) for row in rows],
    }


async def _read_client_tasks(conn, ctx: TenantContext, tool_input: dict) -> dict:
    raw_client_id = tool_input.get("client_id")
    client_id = None
    if raw_client_id not in (None, ""):
        client_id, error = _tool_uuid(tool_input, "client_id")
        if error:
            return error
    rows = await conn.fetch(
        """
        SELECT ct.id,ct.client_id,ct.title,ct.details,ct.due_at,ct.status,
               ct.priority,ct.assignee_id,ct.completed_at,ct.created_at,
               c.full_name AS client_name
          FROM client_tasks ct
          LEFT JOIN clients c ON c.id=ct.client_id AND c.tenant_id=ct.tenant_id
         WHERE ct.tenant_id=$1::uuid
           AND ($2::uuid IS NULL OR ct.client_id=$2::uuid)
         ORDER BY (ct.status='open') DESC,ct.due_at ASC NULLS LAST,ct.created_at DESC
         LIMIT 50
        """,
        ctx.tenant_id, client_id,
    )
    return {
        "ok": True, "action_type": "list_client_tasks", "count": len(rows),
        "tasks": [_clean(dict(row)) for row in rows],
    }


async def _read_deals(conn, ctx: TenantContext, tool_name: str, tool_input: dict) -> dict:
    if tool_name == "list_deals":
        state = str(tool_input.get("state") or "").strip().upper() or None
        stage = str(tool_input.get("stage") or "").strip() or None
        if state and (len(state) != 2 or not state.isalpha()):
            return {"ok": False, "error": "state must be a two-letter code."}
        allowed_stages = {"draft", "under_contract", "marketing", "assigned", "closed", "expired", "dead"}
        if stage and stage not in allowed_stages:
            return {"ok": False, "error": "stage is not a valid pipeline state."}
        rows = await conn.fetch(
            """
            SELECT l.id,l.parcel_id,l.address,l.state,l.motivation_score,l.dossier_status,
                   l.contract_execution_date,l.contract_expires_at,l.updated_at,
                   c.id AS seller_client_id,c.full_name AS seller_name
              FROM leads l
              LEFT JOIN clients c ON c.id=l.seller_client_id AND c.tenant_id=l.tenant_id
             WHERE l.tenant_id=$1::uuid
               AND ($2::text IS NULL OR l.state=$2)
               AND ($3::text IS NULL OR l.dossier_status=$3)
             ORDER BY l.contract_expires_at ASC NULLS LAST,l.updated_at DESC
             LIMIT 50
            """,
            ctx.tenant_id, state, stage,
        )
        return {"ok": True, "action_type": tool_name, "count": len(rows), "deals": [_clean(dict(row)) for row in rows]}

    if tool_name == "get_deal_detail":
        deal_id, error = _tool_uuid(tool_input, "deal_id")
        if error:
            return error
        deal = await conn.fetchrow(
            """
            SELECT l.id,l.parcel_id,l.address,l.state,l.motivation_score,l.underwriting,
                   l.payload,l.dossier_status,l.contract_execution_date,l.contract_expires_at,
                   l.updated_at,c.id AS seller_client_id,c.full_name AS seller_name,
                   c.email AS seller_email,c.phone AS seller_phone
              FROM leads l
              LEFT JOIN clients c ON c.id=l.seller_client_id AND c.tenant_id=l.tenant_id
             WHERE l.id=$1::uuid AND l.tenant_id=$2::uuid
            """,
            deal_id, ctx.tenant_id,
        )
        if not deal:
            return {"ok": False, "error": "Deal not found."}
        transactions = await conn.fetch(
            """SELECT id,status,property_address,purchase_price,earnest_money,
                      financing_amount,offer_deadline,inspection_deadline,
                      financing_deadline,closing_deadline,updated_at
                 FROM transactions
                WHERE lead_id=$1::uuid AND tenant_id=$2::uuid
                ORDER BY updated_at DESC LIMIT 10""",
            deal_id, ctx.tenant_id,
        )
        return {
            "ok": True, "action_type": tool_name, "deal": _clean(dict(deal)),
            "transactions": [_clean(dict(row)) for row in transactions],
        }

    raw_deal_id = tool_input.get("deal_id")
    deal_id = None
    if raw_deal_id not in (None, ""):
        deal_id, error = _tool_uuid(tool_input, "deal_id")
        if error:
            return error
    rows = await conn.fetch(
        """
        WITH deadline_rows AS (
            SELECT l.id AS deal_id,'contract_expiration'::text AS deadline_type,
                   l.contract_expires_at::timestamptz AS due_at,l.dossier_status AS status
              FROM leads l
             WHERE l.tenant_id=$1::uuid AND l.contract_expires_at IS NOT NULL
            UNION ALL
            SELECT t.lead_id,'offer'::text,t.offer_deadline::timestamptz,t.status
              FROM transactions t
             WHERE t.tenant_id=$1::uuid AND t.lead_id IS NOT NULL AND t.offer_deadline IS NOT NULL
            UNION ALL
            SELECT t.lead_id,'inspection'::text,t.inspection_deadline::timestamptz,t.status
              FROM transactions t
             WHERE t.tenant_id=$1::uuid AND t.lead_id IS NOT NULL AND t.inspection_deadline IS NOT NULL
            UNION ALL
            SELECT t.lead_id,'financing'::text,t.financing_deadline::timestamptz,t.status
              FROM transactions t
             WHERE t.tenant_id=$1::uuid AND t.lead_id IS NOT NULL AND t.financing_deadline IS NOT NULL
            UNION ALL
            SELECT t.lead_id,'closing'::text,t.closing_deadline::timestamptz,t.status
              FROM transactions t
             WHERE t.tenant_id=$1::uuid AND t.lead_id IS NOT NULL AND t.closing_deadline IS NOT NULL
        )
        SELECT d.deal_id,d.deadline_type,d.due_at,d.status,l.address,l.state,
               (d.due_at < now()) AS overdue
          FROM deadline_rows d
          JOIN leads l ON l.id=d.deal_id AND l.tenant_id=$1::uuid
         WHERE ($2::uuid IS NULL OR d.deal_id=$2::uuid)
           -- The tool is advertised as "upcoming" and the ASC + LIMIT 50 window
           -- would otherwise be filled entirely by long-expired history on any
           -- tenant with closed deals, hiding what is actually due this week.
           -- A single deal is asked about explicitly, so it keeps its full set.
           AND ($2::uuid IS NOT NULL OR d.due_at >= now() - interval '14 days')
         ORDER BY d.due_at ASC
         LIMIT 50
        """,
        ctx.tenant_id, deal_id,
    )
    return {"ok": True, "action_type": tool_name, "count": len(rows), "deadlines": [_clean(dict(row)) for row in rows]}


async def _read_team_or_providers(conn, ctx: TenantContext, tool_name: str) -> dict:
    if tool_name == "get_team_pipeline":
        rows = await conn.fetch(
            """SELECT dossier_status AS stage,count(*)::int AS deal_count,
                      count(*) FILTER (
                          WHERE contract_expires_at IS NOT NULL
                            AND contract_expires_at <= now() + interval '14 days'
                            AND contract_expires_at >= now()
                      )::int AS expiring_within_14_days
                 FROM leads
                WHERE tenant_id=$1::uuid
                GROUP BY dossier_status
                ORDER BY dossier_status""",
            ctx.tenant_id,
        )
        return {"ok": True, "action_type": tool_name, "stages": [_clean(dict(row)) for row in rows]}
    rows = await conn.fetch(
        """SELECT provider,account_label,expires_at,last_validated_at,disabled_at,
                  validation_status,validation_error,validated_capabilities,updated_at
             FROM provider_credentials
            WHERE tenant_id=$1::uuid
            ORDER BY provider,updated_at DESC""",
        ctx.tenant_id,
    )
    # Ciphertext and refresh credentials never leave the database through AI.
    return {"ok": True, "action_type": tool_name, "providers": [_clean(dict(row)) for row in rows]}


# Columns an undo is allowed to write back, per record type. The `before` state
# is decrypted out of the ledger and its keys are interpolated into SQL as
# column names — so this is simultaneously the list of what is reversible and
# the thing that keeps that interpolation from being an injection point.
_UNDO_COLUMNS: dict[str, tuple[str, frozenset[str]]] = {
    "client": ("clients", frozenset({
        "full_name", "email", "phone", "client_type", "stage", "lead_score",
        "company", "assignee_id", "archived_at",
    })),
    "listing": ("listings", frozenset({"address", "status", "price"})),
    "lead": ("leads", frozenset({"dossier_status", "contract_execution_date"})),
    "deal": ("transactions", frozenset({"notes"})),
}

# Tables a row_delete undo may remove from. Nothing in the schema references
# any of them, so deleting a row the assistant inserted deletes exactly that
# row and nothing downstream of it. `clients` is deliberately absent: a client
# delete cascades to ten tables, which is not an undo.
_UNDO_DELETABLE_TABLES = frozenset({"client_notes", "client_tags", "client_activities"})


async def _record_action(
    conn, ctx: TenantContext, *, key: str, user_id: str, message_id: str,
    tool_name: str, record_type: str, record_id: str, before: dict, after: dict,
    undo_kind: str, expected_updated_at=None, undo_payload: Optional[dict] = None,
):
    """Write the ledger row that makes a mutation undoable.

    Every tool that changes a record goes through here. It used to be inline in
    the shared field-update tail, which meant the six tools that returned early
    wrote no ledger row at all while still being broadcast to the UI as applied,
    undoable receipts — an Undo button pointing at no action id.

    ``undo_kind`` is NOT NULL in the schema on purpose: a new tool that forgets
    to say how it is reversed fails here rather than shipping another dead button.
    """
    before_ct = await _encrypt_text(conn, json.dumps(before, default=str), key)
    after_ct = await _encrypt_text(conn, json.dumps(after, default=str), key)
    return await conn.fetchrow(
        """INSERT INTO ai_chat_actions
               (tenant_id,user_id,message_id,action_type,record_type,record_id,
                before_ciphertext,after_ciphertext,expected_updated_at,
                undo_kind,undo_payload)
             VALUES ($1::uuid,$2,$3::uuid,$4,$5,$6::uuid,$7,$8,$9,$10,$11::jsonb)
             RETURNING id,undo_expires_at""",
        ctx.tenant_id, user_id, message_id, tool_name, record_type, record_id,
        before_ct, after_ct, expected_updated_at, undo_kind,
        json.dumps(undo_payload) if undo_payload is not None else None,
    )


def _applied(
    tool_name: str, action, *, record_type: str, record_id: str, undo_kind: str,
    undo_unavailable_reason: Optional[str] = None, **extra,
) -> dict:
    """The receipt shape for an applied mutation.

    ``undoable`` is what the UI reads: an action recorded for audit but not
    reversible must not render a button that cannot work.
    """
    undoable = undo_kind != "none"
    return {
        "ok": True,
        "action_type": tool_name,
        "action_id": str(action["id"]),
        "record_type": record_type,
        "record_id": str(record_id),
        "undoable": undoable,
        "undo_expires_at": action["undo_expires_at"].isoformat() if undoable else None,
        "undo_unavailable_reason": None if undoable else undo_unavailable_reason,
        **extra,
    }


async def _execute_safe_tool(
    ctx: TenantContext, user_id: str, message_id: str, tool_name: str,
    tool_input: dict, context_type: Optional[str], context_id: Optional[str],
) -> dict:
    needs_context = (
        tool_name not in _READ_ONLY_TOOLS
        and tool_name not in (
            "codebase_summary",
            "web_search",
            "create_client",
            "generate_contract",
            "generate_assignment_agreement",
            "publish_to_marketplace",
        )
    )
    if needs_context and (not context_type or not context_id):
        return {"ok": False, "error": "Select the record you want me to update first."}
    key = tenant_key(ctx)
    async with tenant_tx(ctx) as conn:
        if tool_name in {
            "search_clients", "get_client_detail", "list_client_activity",
            "get_client_contact_history",
        }:
            return await _read_clients(conn, ctx, tool_name, tool_input)
        if tool_name == "list_client_tasks":
            return await _read_client_tasks(conn, ctx, tool_input)
        if tool_name in {"list_deals", "get_deal_detail", "track_deadlines"}:
            return await _read_deals(conn, ctx, tool_name, tool_input)
        if tool_name in {"get_team_pipeline", "list_providers"}:
            return await _read_team_or_providers(conn, ctx, tool_name)
        if tool_name in _READ_TOOL_NAMES:
            return await _execute_read_tool(conn, ctx, tool_name, tool_input)
        if tool_name == "update_client":
            model = SafeClientUpdate.model_validate(tool_input)
            if context_type != "client" or str(model.client_id) != context_id:
                return {"ok": False, "error": "I can only edit the selected client."}
            fields = model.model_dump(exclude={"client_id"}, exclude_none=True)
            table, id_field = "clients", str(model.client_id)
            # Assignment remains a human decision and source is immutable unless
            # a user edits it through the CRM's explicit source field.
            permitted = ("full_name","email","phone","client_type","stage","lead_score","company")
        elif tool_name == "update_listing":
            model = SafeListingUpdate.model_validate(tool_input)
            if context_type != "listing" or str(model.listing_id) != context_id:
                return {"ok": False, "error": "I can only edit the selected listing."}
            fields = model.model_dump(exclude={"listing_id"}, exclude_none=True)
            table, id_field = "listings", str(model.listing_id)
            permitted = ("address", "status")
        elif tool_name == "codebase_summary":
            from ai_chat_agent import _codebase_summary
            return {"ok": True, "summary": _codebase_summary(), "action_type": "codebase_summary"}
        elif tool_name == "web_search":
            from ai_chat_agent import _web_search
            query = str(tool_input.get("query", "")).strip()
            if len(query) < 3:
                return {"ok": False, "error": "Search query must be at least 3 characters."}
            try:
                result = await _web_search(query)
                return {"ok": True, "results": result, "action_type": "web_search"}
            except RuntimeError as exc:
                return {"ok": False, "error": str(exc)[:256]}
        elif tool_name == "add_client_note":
            target_id, target_error = _selected_uuid(
                tool_input=tool_input,
                input_name="client_id",
                context_type=context_type,
                context_id=context_id,
                expected_context="client",
            )
            if target_error:
                return target_error
            note = str(tool_input.get("note", "")).strip()
            if not note or len(note) > 8000:
                return {"ok": False, "error": "Note must be 1-8000 characters."}
            if not await conn.fetchval(
                """SELECT 1 FROM clients
                    WHERE id=$1::uuid AND tenant_id=$2::uuid
                      AND archived_at IS NULL""",
                target_id, ctx.tenant_id,
            ):
                return {"ok": False, "error": "The selected client no longer exists."}
            note_row = await conn.fetchrow(
                """INSERT INTO client_notes
                       (tenant_id,client_id,body,author_id,pinned)
                     VALUES ($1::uuid,$2::uuid,$3,$4,false)
                     RETURNING id,created_at""",
                ctx.tenant_id, target_id, note, user_id,
            )
            activity_row = await conn.fetchrow(
                """INSERT INTO client_activities
                       (tenant_id,client_id,kind,summary,meta,actor)
                     VALUES ($1::uuid,$2::uuid,'note','Note added',
                             jsonb_build_object('note_id',$3::text),$4)
                     RETURNING id""",
                ctx.tenant_id, target_id, str(note_row["id"]), user_id,
            )
            action = await _record_action(
                conn, ctx, key=key, user_id=user_id, message_id=message_id,
                tool_name=tool_name, record_type="client", record_id=target_id,
                before={}, after={"note": note}, undo_kind="row_delete",
                undo_payload={"deletes": [
                    {"table": "client_notes", "ids": [str(note_row["id"])]},
                    {"table": "client_activities", "ids": [str(activity_row["id"])]},
                ]},
            )
            return _applied(
                tool_name, action, record_type="client", record_id=target_id,
                undo_kind="row_delete",
                note_id=str(note_row["id"]),
                created_at=note_row["created_at"].isoformat(),
                detail="Note recorded in the client activity feed.",
            )
        elif tool_name == "create_deal_note":
            raw_deal_id = str(tool_input.get("deal_id") or "").strip()
            try:
                deal_id = str(uuid.UUID(raw_deal_id))
            except ValueError:
                return {"ok": False, "error": "deal_id must be a UUID."}
            anchor_columns = {
                "client": "client_id",
                "lead": "lead_id",
                "listing": "listing_id",
            }
            anchor_column = anchor_columns.get(str(context_type))
            try:
                anchor_id = str(uuid.UUID(str(context_id or "")))
            except ValueError:
                anchor_id = ""
            if not anchor_column or not anchor_id:
                return {"ok": False, "error": "Select a record linked to the deal first."}
            note = str(tool_input.get("note", "")).strip()
            if not note or len(note) > 5000:
                return {"ok": False, "error": "Note must be 1-5000 characters."}
            # Read the prior text under FOR UPDATE rather than through a
            # sub-SELECT in RETURNING: that would work, because a subquery sees
            # the statement's own snapshot, but the correctness of the undo
            # should not rest on knowing that rule.
            previous_notes = await conn.fetchrow(
                f"""SELECT notes FROM transactions
                     WHERE id=$1::uuid AND tenant_id=$2::uuid
                       AND {anchor_column}=$3::uuid FOR UPDATE""",
                deal_id, ctx.tenant_id, anchor_id,
            )
            if not previous_notes:
                return {"ok": False, "error": "Deal not found."}
            deal_row = await conn.fetchrow(
                f"""UPDATE transactions
                      SET notes=concat_ws(E'\n\n',NULLIF(notes,''),$3),
                          updated_by=$4,version=version+1,updated_at=now()
                    WHERE id=$1::uuid AND tenant_id=$2::uuid
                      AND {anchor_column}=$5::uuid
                    RETURNING id,version,updated_at,notes""",
                deal_id, ctx.tenant_id, note, user_id, anchor_id,
            )
            if not deal_row:
                return {"ok": False, "error": "Deal not found."}
            # The note is appended, so the reversal is the text as it stood —
            # not an empty string, which would delete whatever was there first.
            action = await _record_action(
                conn, ctx, key=key, user_id=user_id, message_id=message_id,
                tool_name=tool_name, record_type="deal", record_id=deal_id,
                before={"notes": previous_notes["notes"]},
                after={"notes": deal_row["notes"]},
                undo_kind="field_restore",
                expected_updated_at=deal_row["updated_at"],
            )
            return _applied(
                tool_name, action, record_type="deal", record_id=deal_id,
                undo_kind="field_restore", version=deal_row["version"],
                detail="Deal note persisted.",
            )
        elif tool_name == "set_client_stage":
            id_field, target_error = _selected_uuid(
                tool_input=tool_input,
                input_name="client_id",
                context_type=context_type,
                context_id=context_id,
                expected_context="client",
            )
            if target_error:
                return target_error
            stage = str(tool_input.get("stage", ""))
            if stage not in ("lead","active","nurture","under_contract","closed","lost"):
                return {"ok": False, "error": "Invalid stage value."}
            fields = {"stage": stage}
            table, permitted = "clients", ("stage",)
        elif tool_name == "add_client_tag":
            target_id, target_error = _selected_uuid(
                tool_input=tool_input,
                input_name="client_id",
                context_type=context_type,
                context_id=context_id,
                expected_context="client",
            )
            if target_error:
                return target_error
            tags = list(dict.fromkeys(
                tag.strip() for tag in str(tool_input.get("tags", "")).split(",")
                if tag.strip()
            ))[:10]
            if not tags:
                return {"ok": False, "error": "Provide at least one tag."}
            if any(len(tag) > 60 for tag in tags):
                return {"ok": False, "error": "Each tag must be 60 characters or fewer."}
            if not await conn.fetchval(
                """SELECT 1 FROM clients
                    WHERE id=$1::uuid AND tenant_id=$2::uuid
                      AND archived_at IS NULL""",
                target_id, ctx.tenant_id,
            ):
                return {"ok": False, "error": "The selected client no longer exists."}
            # RETURNING yields nothing for a tag that already existed, which is
            # exactly right: an undo must remove the tags this action added and
            # leave a pre-existing one alone.
            inserted_tag_ids: list[str] = []
            added_tags: list[str] = []
            for tag in tags:
                row = await conn.fetchrow(
                    """INSERT INTO client_tags (tenant_id,client_id,tag)
                         VALUES ($1::uuid,$2::uuid,$3)
                         ON CONFLICT DO NOTHING
                         RETURNING id""",
                    ctx.tenant_id, target_id, tag,
                )
                if row:
                    inserted_tag_ids.append(str(row["id"]))
                    added_tags.append(tag)
            activity_row = await conn.fetchrow(
                """INSERT INTO client_activities
                       (tenant_id,client_id,kind,summary,meta,actor)
                     VALUES ($1::uuid,$2::uuid,'tag',$3,
                             jsonb_build_object('tags',$4::text[]),$5)
                     RETURNING id""",
                ctx.tenant_id, target_id, f"Tagged: {', '.join(tags)}", tags, user_id,
            )
            action = await _record_action(
                conn, ctx, key=key, user_id=user_id, message_id=message_id,
                tool_name=tool_name, record_type="client", record_id=target_id,
                before={}, after={"tags_added": added_tags},
                undo_kind="row_delete",
                undo_payload={"deletes": [
                    {"table": "client_tags", "ids": inserted_tag_ids},
                    {"table": "client_activities", "ids": [str(activity_row["id"])]},
                ]},
            )
            return _applied(
                tool_name, action, record_type="client", record_id=target_id,
                undo_kind="row_delete", tags=tags, tags_added=added_tags,
                tags_already_present=[t for t in tags if t not in added_tags],
                detail="Client tags persisted.",
            )
        elif tool_name == "assign_client":
            id_field, target_error = _selected_uuid(
                tool_input=tool_input,
                input_name="client_id",
                context_type=context_type,
                context_id=context_id,
                expected_context="client",
            )
            if target_error:
                return target_error
            assignee = str(tool_input.get("agent_id") or "").strip()
            if not assignee or len(assignee) > 160:
                return {"ok": False, "error": "agent_id must be 1-160 characters."}
            # The reason this was a flat refusal is the reason it needs a check
            # rather than a ban: assignee_id is a free-text column, so without
            # this an assistant could route a client to a string that is nobody.
            # It is not a permission boundary — the RLS policy on clients is
            # tenant-only — so reassignment stays an INTERNAL_EDIT.
            if not await conn.fetchval(
                """SELECT 1 FROM team_memberships
                    WHERE tenant_id=$1::uuid AND user_id=$2 AND status='active'""",
                ctx.tenant_id, assignee,
            ):
                return {
                    "ok": False,
                    "error": (
                        f"{assignee!r} is not an active member of this workspace, "
                        f"so the client would be assigned to nobody."
                    ),
                }
            fields = {"assignee_id": assignee}
            table, permitted = "clients", ("assignee_id",)
        elif tool_name == "score_client_lead":
            id_field, target_error = _selected_uuid(
                tool_input=tool_input,
                input_name="client_id",
                context_type=context_type,
                context_id=context_id,
                expected_context="client",
            )
            if target_error:
                return target_error
            try:
                score = int(tool_input["score"])
            except (KeyError, ValueError, TypeError):
                return {"ok": False, "error": "Score must be an integer 0-100."}
            if not 0 <= score <= 100:
                return {"ok": False, "error": "Score must be an integer 0-100."}
            fields = {"lead_score": score}
            table, permitted = "clients", ("lead_score",)
        elif tool_name == "archive_client":
            target_id, target_error = _selected_uuid(
                tool_input=tool_input,
                input_name="client_id",
                context_type=context_type,
                context_id=context_id,
                expected_context="client",
            )
            if target_error:
                return target_error
            archived = await conn.fetchrow(
                """UPDATE clients
                      SET archived_at=now(),updated_at=now()
                    WHERE id=$1::uuid AND tenant_id=$2::uuid
                      AND archived_at IS NULL
                    RETURNING id,archived_at,updated_at""",
                target_id, ctx.tenant_id,
            )
            if not archived:
                return {"ok": False, "error": "The selected client is unavailable or already archived."}
            await conn.execute(
                """INSERT INTO client_activities
                       (tenant_id,client_id,kind,summary,meta,actor)
                     VALUES ($1::uuid,$2::uuid,'system','Client archived',
                             '{"archived":true}'::jsonb,$3)""",
                ctx.tenant_id, target_id, user_id,
            )
            # The archive activity row is deliberately left behind by an undo:
            # it happened, and the timeline should say so.
            action = await _record_action(
                conn, ctx, key=key, user_id=user_id, message_id=message_id,
                tool_name=tool_name, record_type="client", record_id=target_id,
                before={"archived_at": None},
                after={"archived_at": archived["archived_at"]},
                undo_kind="field_restore",
                expected_updated_at=archived["updated_at"],
            )
            return _applied(
                tool_name, action, record_type="client", record_id=target_id,
                undo_kind="field_restore",
                archived_at=archived["archived_at"].isoformat(),
                detail="Client archived.",
            )
        elif tool_name == "create_client":
            name = str(tool_input.get("full_name", "")).strip()
            email = str(tool_input.get("email") or "").strip() or None
            phone = str(tool_input.get("phone") or "").strip() or None
            client_type = str(tool_input.get("client_type") or "seller").strip()
            if not name or len(name) > 160:
                return {"ok": False, "error": "full_name must be 1-160 characters."}
            if email and (len(email) > 254 or "@" not in email):
                return {"ok": False, "error": "Provide a valid email address."}
            if phone and len(phone) > 40:
                return {"ok": False, "error": "Phone must be 40 characters or fewer."}
            if client_type not in ("seller", "buyer", "both"):
                return {"ok": False, "error": "client_type must be seller, buyer, or both."}
            client = await conn.fetchrow(
                """INSERT INTO clients
                       (tenant_id,full_name,email,phone,client_type,stage,
                        lead_score,preferences,source)
                     VALUES ($1::uuid,$2,$3,$4,$5,'lead',0,'{}'::jsonb,'personal_ai')
                     RETURNING id,full_name,created_at""",
                ctx.tenant_id, name, email, phone, client_type,
            )
            await conn.execute(
                """INSERT INTO client_activities
                       (tenant_id,client_id,kind,summary,meta,actor)
                     VALUES ($1::uuid,$2::uuid,'created',$3,
                             jsonb_build_object('client_type',$4),$5)""",
                ctx.tenant_id, client["id"], f"Client created: {name}",
                client_type, user_id,
            )
            # Recorded for audit, but not reversible: deleting a client cascades
            # to buyer profiles, notes, tasks, tags, showings, interaction logs
            # and the email outbox, any of which may have appeared in the undo
            # window. Archiving is the reversal this domain actually has, and
            # saying so beats a button that would quietly destroy ten tables.
            action = await _record_action(
                conn, ctx, key=key, user_id=user_id, message_id=message_id,
                tool_name=tool_name, record_type="client",
                record_id=str(client["id"]), before={},
                after={"full_name": name, "client_type": client_type},
                undo_kind="none",
            )
            return _applied(
                tool_name, action, record_type="client",
                record_id=str(client["id"]), undo_kind="none",
                undo_unavailable_reason=(
                    "Deleting a client would cascade to everything attached to "
                    "it since. Use archive_client instead, which is reversible."
                ),
                created_at=client["created_at"].isoformat(),
                detail=f"Client '{name}' created.",
            )
        elif tool_name == "move_deal_stage":
            stage = str(tool_input.get("stage", ""))
            if context_type != "lead":
                return {"ok": False, "error": "Select the pipeline lead you want to move."}
            raw_deal_id = str(tool_input.get("deal_id") or "").strip()
            try:
                deal_id = str(uuid.UUID(raw_deal_id))
                selected_id = str(uuid.UUID(str(context_id or "")))
            except ValueError:
                return {"ok": False, "error": "deal_id must be a UUID."}
            if deal_id != selected_id:
                return {"ok": False, "error": "I can only move the selected pipeline lead."}
            if stage not in ("draft", "under_contract", "marketing", "assigned", "closed", "expired", "dead"):
                return {
                    "ok": False,
                    "error": "That stage is not a durable pipeline state. Use draft, under_contract, marketing, assigned, closed, expired, or dead.",
                }
            previous = await conn.fetchrow(
                """SELECT dossier_status,contract_execution_date FROM leads
                    WHERE id=$1::uuid AND tenant_id=$2::uuid FOR UPDATE""",
                deal_id, ctx.tenant_id,
            )
            if not previous:
                return {"ok": False, "error": "The selected pipeline lead no longer exists."}
            moved = await conn.fetchrow(
                """UPDATE leads
                      SET dossier_status=$1,
                          contract_execution_date=CASE
                              WHEN $1='under_contract' AND contract_execution_date IS NULL
                              THEN now() ELSE contract_execution_date END,
                          updated_at=now()
                    WHERE id=$2::uuid AND tenant_id=$3::uuid
                    RETURNING id,dossier_status,contract_execution_date,
                              contract_expires_at,updated_at""",
                stage, deal_id, ctx.tenant_id,
            )
            if not moved:
                return {"ok": False, "error": "The selected pipeline lead no longer exists."}
            # contract_execution_date is stamped as a side effect of moving to
            # under_contract, so an undo that restored only the stage would
            # leave a lead that was never under contract carrying an execution
            # date.
            action = await _record_action(
                conn, ctx, key=key, user_id=user_id, message_id=message_id,
                tool_name=tool_name, record_type="lead", record_id=deal_id,
                before={"dossier_status": previous["dossier_status"],
                        "contract_execution_date": previous["contract_execution_date"]},
                after={"dossier_status": moved["dossier_status"],
                       "contract_execution_date": moved["contract_execution_date"]},
                undo_kind="field_restore",
                expected_updated_at=moved["updated_at"],
            )
            return _applied(
                tool_name, action, record_type="lead", record_id=deal_id,
                undo_kind="field_restore", stage=moved["dossier_status"],
                detail=f"Pipeline lead moved to {moved['dossier_status']}.",
            )
        elif tool_name in ("generate_contract", "generate_assignment_agreement"):
            return {
                "ok": False,
                "error": (
                    "Use draft_contract instead. It reads every term from the "
                    "transaction, names any that are not recorded rather than "
                    "inventing them, and files the result for attorney review. "
                    "These two names remain refusals because they imply the "
                    "assistant generates the terms."
                ),
            }
        elif tool_name in _GATED_TOOL_NAMES:
            return await _execute_gated_tool(
                conn, ctx, tool_name, tool_input,
                user_id=user_id, message_id=message_id,
                context_type=context_type, context_id=context_id,
            )
        else:
            return {
                "ok": False,
                "error": f"The '{tool_name}' tool is not implemented in this execution path.",
            }
        if not fields:
            return {"ok": False, "error": "No editable fields were supplied."}
        if any(name not in permitted for name in fields):
            return {"ok": False, "error": "The requested field is approval-gated."}

        live_record_clause = " AND archived_at IS NULL" if table == "clients" else ""
        current = await conn.fetchrow(
            f"SELECT {','.join(permitted)},updated_at FROM {table} "
            f"WHERE id=$1::uuid AND tenant_id=$2::uuid{live_record_clause} FOR UPDATE",
            id_field, ctx.tenant_id,
        )
        if not current:
            return {"ok": False, "error": "The selected record no longer exists."}
        before = {name: _clean(current[name]) for name in fields}
        assignments = ",".join(f"{name}=${index}" for index, name in enumerate(fields, start=1))
        values = list(fields.values())
        updated = await conn.fetchrow(
            f"UPDATE {table} SET {assignments} WHERE id=${len(values)+1}::uuid "
            f"AND tenant_id=${len(values)+2}::uuid{live_record_clause} RETURNING updated_at",
            *values, id_field, ctx.tenant_id,
        )
        action = await _record_action(
            conn, ctx, key=key, user_id=user_id, message_id=message_id,
            tool_name=tool_name, record_type=context_type, record_id=context_id,
            before=before, after=fields, undo_kind="field_restore",
            expected_updated_at=updated["updated_at"],
        )
        if table == "clients":
            # Only a real value change is a human override — a write that
            # re-submits the existing stage/score must not take the client off
            # AI stewardship.
            stage_changed = "stage" in fields and fields["stage"] != current["stage"]
            score_changed = (
                "lead_score" in fields and fields["lead_score"] != current["lead_score"]
            )
            if stage_changed or score_changed:
                await conn.execute(
                    """
                    INSERT INTO client_ai_state
                        (client_id,tenant_id,score_mode,stage_mode,status)
                    VALUES (
                        $1::uuid,$2::uuid,
                        CASE WHEN $3 THEN 'manual' ELSE 'auto' END,
                        CASE WHEN $4 THEN 'manual' ELSE 'auto' END,
                        'queued'
                    )
                    ON CONFLICT (client_id) DO UPDATE SET
                        score_mode=CASE WHEN $3 THEN 'manual' ELSE client_ai_state.score_mode END,
                        stage_mode=CASE WHEN $4 THEN 'manual' ELSE client_ai_state.stage_mode END,
                        status=CASE WHEN client_ai_state.enabled THEN 'queued' ELSE 'disabled' END
                    """,
                    id_field,
                    ctx.tenant_id,
                    score_changed,
                    stage_changed,
                )
            await conn.execute(
                """INSERT INTO client_activities
                   (tenant_id,client_id,kind,summary,meta,actor)
                   VALUES ($1::uuid,$2::uuid,'system',$3,$4::jsonb,$5)""",
                ctx.tenant_id, id_field, "Personal AI updated the client profile",
                json.dumps({"fields": sorted(fields)}), user_id,
            )
    return _applied(
        tool_name, action, record_type=context_type, record_id=context_id,
        undo_kind="field_restore", fields=fields,
    )


async def execute_safe_tool(
    ctx: TenantContext, user_id: str, message_id: str, tool_name: str,
    tool_input: dict, context_type: Optional[str], context_id: Optional[str],
) -> dict:
    """Execute a tool and never report success unless its durable work completed."""
    try:
        return await _execute_safe_tool(
            ctx, user_id, message_id, tool_name, tool_input,
            context_type, context_id,
        )
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, str) else "The request was rejected."
        return {"ok": False, "error": str(detail)[:500]}
    except (TypeError, ValueError) as exc:
        return {"ok": False, "error": str(exc)[:500] or "The tool input is invalid."}
    except Exception:  # noqa: BLE001 - provider receives a safe, structured failure
        logger.exception("Personal AI tool mutation failed: tool=%s", tool_name)
        return {
            "ok": False,
            "error": "The requested change could not be persisted. No success was recorded.",
        }


async def _undo_field_restore(conn, ctx: TenantContext, action, *, key: str,
                              action_id: str) -> None:
    """Put the recorded column values back, refusing on a concurrent edit."""
    mapping = _UNDO_COLUMNS.get(action["record_type"])
    if not mapping:
        raise HTTPException(status.HTTP_409_CONFLICT, "Action cannot be undone")
    table, allowed = mapping
    current = await conn.fetchrow(
        f"SELECT updated_at FROM {table} WHERE id=$1::uuid AND tenant_id=$2::uuid FOR UPDATE",
        action["record_id"], ctx.tenant_id,
    )
    if not current or current["updated_at"] != action["expected_updated_at"]:
        await conn.execute("UPDATE ai_chat_actions SET status='conflict' WHERE id=$1::uuid", action_id)
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Record changed after this action; undo was not applied",
        )
    before = json.loads(await _decrypt_text(conn, action["before_ciphertext"], key))
    if not isinstance(before, dict) or not before:
        raise HTTPException(status.HTTP_409_CONFLICT, "Action cannot be undone")
    # These keys become column names in the statement below. They come out of
    # our own ciphertext, but an allowlist is what makes that interpolation
    # safe by construction rather than by trust.
    unexpected = sorted(set(before) - allowed)
    if unexpected:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Action refers to fields that are not restorable: {', '.join(unexpected)}",
        )
    assignments = ",".join(f"{name}=${index}" for index, name in enumerate(before, start=1))
    values = list(before.values())
    await conn.execute(
        f"UPDATE {table} SET {assignments},updated_at=now() "
        f"WHERE id=${len(values)+1}::uuid AND tenant_id=${len(values)+2}::uuid",
        *values, action["record_id"], ctx.tenant_id,
    )


async def _undo_row_delete(conn, ctx: TenantContext, action) -> None:
    """Remove exactly the rows the action inserted.

    No ``expected_updated_at`` check: these rows are append-only additions to a
    timeline, so a later note by someone else is not a conflict with removing
    this one. The tenant predicate and the recorded ids are the whole scope.
    """
    payload = _json(action["undo_payload"], {}) or {}
    for target in payload.get("deletes", []):
        table = str(target.get("table") or "")
        ids = [str(value) for value in target.get("ids") or []]
        if not ids:
            continue
        if table not in _UNDO_DELETABLE_TABLES:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"Undo cannot delete from {table!r}.",
            )
        await conn.execute(
            f"DELETE FROM {table} WHERE tenant_id=$1::uuid AND id = ANY($2::uuid[])",
            ctx.tenant_id, ids,
        )


async def undo_action(ctx: TenantContext, action_id: str) -> dict:
    key = tenant_key(ctx)
    async with tenant_tx(ctx) as conn:
        action = await conn.fetchrow(
            """SELECT * FROM ai_chat_actions WHERE id=$1::uuid AND tenant_id=$2::uuid
                  AND user_id=$3 FOR UPDATE""",
            action_id, ctx.tenant_id, ctx.agent_id,
        )
        if not action:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Action not found")
        if action["status"] != "applied":
            raise HTTPException(status.HTTP_409_CONFLICT, "Action is no longer undoable")
        if action["undo_expires_at"] <= datetime.now(timezone.utc):
            raise HTTPException(status.HTTP_409_CONFLICT, "Undo window has expired")
        undo_kind = action["undo_kind"]
        if undo_kind == "none":
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "This action was recorded for audit but cannot be reversed.",
            )
        if undo_kind == "row_delete":
            await _undo_row_delete(conn, ctx, action)
        else:
            await _undo_field_restore(conn, ctx, action, key=key, action_id=action_id)
        await conn.execute(
            "UPDATE ai_chat_actions SET status='undone',undone_at=now() WHERE id=$1::uuid",
            action_id,
        )
    return {"id": action_id, "status": "undone", "record_type": action["record_type"],
            "record_id": str(action["record_id"])}
