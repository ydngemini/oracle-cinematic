"""Authenticated REST and WebSocket entry points for private AI chat."""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import os
import socket
import struct
import uuid
from datetime import datetime
from pathlib import Path
from typing import Annotated, Optional
from urllib.parse import quote

from fastapi import (
    APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, WebSocket, status,
)
from fastapi.responses import Response
from pydantic import ValidationError

import ws_hub
from ai_chat_models import ChatSendFrame
from ai_chat_store import (
    active_message_count,
    create_chat_request,
    delete_attachment,
    get_attachment,
    list_attachments,
    list_messages,
    list_records,
    recent_request_count,
    release_concurrency,
    save_attachment,
    undo_action,
    update_assistant,
)
from automation_jobs import enqueue_job
from platform_policy import ActionRisk, Feature, feature_enabled
from tenancy import TenantContext, require_context

# Import registers the durable response handler before workers start.
import ai_chat_agent  # noqa: F401,E402

logger = logging.getLogger("oracle.ai_chat_api")
router = APIRouter(prefix="/api/ai/chat", tags=["Private AI Chat"])

MAX_FILE_BYTES = 12 * 1024 * 1024
MAX_FILES = 5
ALLOWED_TYPES = {
    "application/pdf": {".pdf"},
    "image/jpeg": {".jpg", ".jpeg"},
    "image/png": {".png"},
    "image/webp": {".webp"},
}


def _enabled() -> bool:
    return feature_enabled(Feature.AI_CHAT)


def _require_enabled() -> None:
    if not _enabled():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Feature is not enabled")


def _safe_filename(filename: str) -> str:
    name = Path(filename or "attachment").name
    name = "".join(char for char in name if char.isprintable() and char not in "\r\n\x00")
    return name[:180] or "attachment"


def _sniff_type(data: bytes) -> Optional[str]:
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _clamd_scan_sync(data: bytes) -> str:
    from config import IS_DEV

    if b"EICAR-STANDARD-ANTIVIRUS-TEST-FILE" in data:
        raise ValueError("Attachment failed malware screening")
    socket_path = os.getenv("ORACLE_CLAMD_SOCKET", "")
    socket_host = os.getenv("ORACLE_CLAMD_HOST", "")
    socket_port = int(os.getenv("ORACLE_CLAMD_PORT", "3310"))
    if not socket_path and not socket_host:
        if IS_DEV:
            return "unavailable_dev"
        raise RuntimeError("Attachment scanning is temporarily unavailable")
    try:
        family = socket.AF_UNIX if socket_path else socket.AF_INET
        address = socket_path if socket_path else (socket_host, socket_port)
        with socket.socket(family, socket.SOCK_STREAM) as client:
            client.settimeout(20)
            client.connect(address)
            client.sendall(b"zINSTREAM\0")
            for offset in range(0, len(data), 64 * 1024):
                chunk = data[offset:offset + 64 * 1024]
                client.sendall(struct.pack(">I", len(chunk)) + chunk)
            client.sendall(struct.pack(">I", 0))
            response = client.recv(4096).decode("utf-8", "replace")
    except OSError as exc:
        if IS_DEV:
            logger.warning("ClamAV unavailable in development: %s", exc)
            return "unavailable_dev"
        raise RuntimeError("Attachment scanning is temporarily unavailable") from exc
    if " FOUND" in response:
        raise ValueError("Attachment failed malware screening")
    if " OK" not in response:
        raise RuntimeError("Attachment scanning did not complete")
    return "clean"


def _extract_pdf_text_sync(data: bytes) -> Optional[str]:
    try:
        from pypdf import PdfReader
    except ImportError:
        return None
    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        text = "\n\n".join((page.extract_text() or "") for page in reader.pages[:120]).strip()
        return text[:300_000] or None
    except Exception as exc:  # noqa: BLE001 - malformed but structurally PDF uploads remain inspectable
        logger.info("PDF text extraction skipped: %s", exc)
        return None


@router.get("/status")
async def chat_status(ctx: TenantContext = Depends(require_context)):
    return {"enabled": _enabled(), "agent_id": ctx.agent_id}


@router.get("/messages")
async def messages(
    before: Optional[datetime] = None,
    limit: int = Query(50, ge=1, le=100),
    ctx: TenantContext = Depends(require_context),
):
    _require_enabled()
    return {"messages": await list_messages(ctx, before=before, limit=limit)}


@router.get("/records")
async def records(
    q: str = Query("", max_length=160),
    limit: int = Query(40, ge=1, le=80),
    ctx: TenantContext = Depends(require_context),
):
    _require_enabled()
    return {"records": await list_records(ctx, q, limit)}


@router.get("/attachments")
async def attachments(
    record_type: str = Query(..., pattern="^(client|lead|listing|contract)$"),
    record_id: uuid.UUID = Query(...),
    ctx: TenantContext = Depends(require_context),
):
    _require_enabled()
    return {"attachments": await list_attachments(ctx, record_type, str(record_id))}


@router.post("/attachments", status_code=status.HTTP_201_CREATED)
async def upload_attachments(
    record_type: Annotated[str, Form(pattern="^(client|lead|listing|contract)$")],
    record_id: Annotated[uuid.UUID, Form()],
    files: Annotated[list[UploadFile], File()],
    ctx: TenantContext = Depends(require_context),
):
    _require_enabled()
    if not files or len(files) > MAX_FILES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Upload 1 to {MAX_FILES} files")
    prepared = []
    for upload in files:
        filename = _safe_filename(upload.filename or "attachment")
        declared = (upload.content_type or "").lower()
        data = await upload.read(MAX_FILE_BYTES + 1)
        await upload.close()
        if not data or len(data) > MAX_FILE_BYTES:
            raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "Each file must be 12 MB or smaller")
        actual = _sniff_type(data)
        suffix = Path(filename).suffix.lower()
        if actual not in ALLOWED_TYPES or declared != actual or suffix not in ALLOWED_TYPES[actual]:
            raise HTTPException(
                status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                "File extension, declared type, and file signature must match PDF, JPEG, PNG, or WebP",
            )
        try:
            scan_status = await asyncio.to_thread(_clamd_scan_sync, data)
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
        extracted = await asyncio.to_thread(_extract_pdf_text_sync, data) if actual == "application/pdf" else None
        prepared.append((filename, actual, data, extracted, scan_status))
    saved = []
    for filename, actual, data, extracted, scan_status in prepared:
        saved.append(await save_attachment(
            ctx, record_type=record_type, record_id=str(record_id), filename=filename,
            media_type=actual, data=data, digest=hashlib.sha256(data).hexdigest(),
            extracted_text=extracted, scan_status=scan_status,
        ))
    return {"attachments": saved}


@router.get("/attachments/{attachment_id}/download")
async def download_attachment(
    attachment_id: uuid.UUID,
    ctx: TenantContext = Depends(require_context),
):
    _require_enabled()
    metadata, data = await get_attachment(ctx, str(attachment_id))
    filename = _safe_filename(metadata["filename"])
    disposition = f"attachment; filename*=UTF-8''{quote(filename)}"
    return Response(
        content=data, media_type=metadata["media_type"],
        headers={"Content-Disposition": disposition, "Cache-Control": "private, no-store",
                 "X-Content-Type-Options": "nosniff"},
    )


@router.delete("/attachments/{attachment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_attachment(
    attachment_id: uuid.UUID,
    ctx: TenantContext = Depends(require_context),
):
    _require_enabled()
    await delete_attachment(ctx, str(attachment_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/actions/{action_id}/undo")
async def undo(
    action_id: uuid.UUID,
    ctx: TenantContext = Depends(require_context),
):
    _require_enabled()
    return await undo_action(ctx, str(action_id))


async def _reject(websocket: WebSocket, request_id: Optional[str], code: str, message: str) -> None:
    await websocket.send_json({
        "type": "AI_CHAT_REJECTED", "version": 1, "request_id": request_id,
        "code": code, "message": message,
    })


async def handle_chat_websocket(
    ctx: TenantContext, websocket: WebSocket, raw_message: dict,
) -> None:
    """Validate, persist, enqueue, and ACK one versioned chat frame.

    Every stage logs at INFO. This path logged nothing for its whole life,
    and the first time a frame went in and nothing came out there was no way
    to tell admission from persistence from enqueue — the socket's receive
    loop is sequential, so one silent stall here also stops every later
    frame on that connection.
    """
    request_id = raw_message.get("request_id")
    logger.info("ai_chat frame received tenant=%s request_id=%s", ctx.tenant_id, request_id)
    if not _enabled():
        logger.info("ai_chat rejected request_id=%s code=FEATURE_DISABLED", request_id)
        await _reject(websocket, request_id, "FEATURE_DISABLED", "Assistant is unavailable")
        return
    try:
        frame = ChatSendFrame.model_validate(raw_message)
    except ValidationError:
        logger.info("ai_chat rejected request_id=%s code=INVALID_MESSAGE", request_id)
        await _reject(websocket, request_id, "INVALID_MESSAGE", "Check the message and selected files")
        return

    context = frame.context.model_dump(mode="json") if frame.context else None
    logger.info("ai_chat persisting request_id=%s context=%s", frame.request_id, context and context.get("type"))
    try:
        created = await create_chat_request(
            ctx, request_id=str(frame.request_id), content=frame.content,
            context=context, attachment_ids=[str(value) for value in frame.attachment_ids],
        )
    except HTTPException as exc:
        code = "RATE_LIMITED" if exc.status_code == status.HTTP_429_TOO_MANY_REQUESTS else "INVALID_CONTEXT"
        await _reject(websocket, str(frame.request_id), code, str(exc.detail))
        return
    accepted = {
        "type": "AI_CHAT_ACCEPTED", "version": 1, "request_id": str(frame.request_id),
        "message_id": created["assistant_id"], "user_message_id": created.get("user_id"),
        "duplicate": created["duplicate"], "status": created.get("status", "pending"),
    }
    logger.info(
        "ai_chat accepted request_id=%s assistant_id=%s duplicate=%s",
        frame.request_id, created.get("assistant_id"), created["duplicate"],
    )
    await ws_hub.broadcast_user(ctx.tenant_id, ctx.agent_id, accepted)
    if created["duplicate"]:
        return
    try:
        await enqueue_job(
            ctx, job_type="ai_chat:response",
            payload={
                "tenant_id": ctx.tenant_id, "user_id": ctx.agent_id,
                "request_id": str(frame.request_id), "assistant_id": created["assistant_id"],
            },
            idempotency_key=f"ai-chat:{ctx.agent_id}:{frame.request_id}",
            created_by=ctx.agent_id, priority=20, max_attempts=3,
            risk=ActionRisk.INTERNAL_EDIT,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("AI chat enqueue failed: %s", exc)
        # No worker will run its finally block when enqueueing fails.
        await release_concurrency(ctx)
        await update_assistant(
            ctx, created["assistant_id"], content="The assistant could not start. Please try again.",
            status_value="failed", error_code="AI_QUEUE_UNAVAILABLE",
        )
        await _reject(websocket, str(frame.request_id), "AI_QUEUE_UNAVAILABLE", "The assistant could not start")
