"""
Voice Intelligence — field walkthrough audio → transcript → structured dossier data.

Flow:
  mobile POST /api/voice/log-walkthrough/{lead_id}      → 202 immediately
    → audio staged to local disk (uuid filename, extension whitelist, size cap)
    → job on an in-process asyncio.Queue (no external broker; ws_hub has the
      same single-process note — Redis is the seam if we ever scale out)
    → worker: faster-whisper transcription (lazy import, runs in a thread)
        → Bedrock extraction via the existing ml_forge client
        → interaction_logs INSERT through tenant_tx (RLS live, append-only —
          this is the dossier stream from migration 0008, NOT mutated lead rows)
        → ws_hub.broadcast VOICE_NOTE_LOGGED to the tenant's dashboards

Degradation: if Bedrock is down or returns garbage, the raw transcript is still
logged to interaction_logs and broadcast — the field note is never lost just
because extraction failed.
"""

import asyncio
import importlib.util
import json
import logging
import os
import re
import threading
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent_profile import load_agent_identity
from audit_ledger import AuditCategory, ledger
from db.connection import tenant_tx
from intelligence_engine import IntelligenceInputError, negotiation_guidance
from ml_forge.bedrock_client import invoke_bedrock_model, PRIMARY_MODEL, SECONDARY_MODEL
from tenancy import TenantContext, require_context
import ws_hub

logger = logging.getLogger("oracle.voice_intel")

router = APIRouter(prefix="/api/voice", tags=["Voice Intelligence"])
comms_router = APIRouter(prefix="/api/comms", tags=["Communications"])

# ── Tunables ──────────────────────────────────────────────────────────────────
STAGING_DIR = Path(os.getenv("ORACLE_AUDIO_STAGING", "/tmp/oracle_audio"))
MAX_AUDIO_BYTES = int(os.getenv("ORACLE_AUDIO_MAX_BYTES", str(25 * 1024 * 1024)))
ALLOWED_SUFFIXES = {".m4a", ".wav", ".mp3", ".webm", ".ogg"}
QUEUE_MAX = int(os.getenv("ORACLE_VOICE_QUEUE_MAX", "100"))
# Tunable alongside ORACLE_JOB_WORKERS and RECON_WORKER_COUNT. Transcription is
# the heaviest per-task work in the process, so this is the knob that decides
# how much CPU a replica gives to voice versus serving requests.
WORKER_COUNT = max(1, int(os.getenv("ORACLE_VOICE_WORKERS", "2")))
WHISPER_MODEL_SIZE = os.getenv("ORACLE_WHISPER_MODEL", "base")

EXTRACTION_PROMPT = """Analyze the following real-estate agent voice walkthrough note:

\"\"\"{transcript}\"\"\"

Extract these data points and respond with STRICT JSON only — no prose, no markdown:
{{
  "price_adjustment": <integer dollars, or null if not mentioned>,
  "repair_notes": "<string summarizing any damage or repairs discussed, empty string if none>",
  "seller_sentiment": "<one of: 'High Intent', 'Hesitant', 'Cold', 'Unknown'>",
  "action_summary": "<one sentence: what happened on this walkthrough>"
}}"""


class VoiceSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    client_id: Optional[uuid.UUID] = None
    property_id: Optional[uuid.UUID] = None
    consent_recorded: bool
    consent_basis: str = Field(min_length=8, max_length=500)

    @model_validator(mode="after")
    def require_anchor(self) -> "VoiceSessionRequest":
        if not self.client_id and not self.property_id:
            raise ValueError("client_id or property_id is required")
        if self.consent_recorded and not self.consent_basis.strip():
            raise ValueError("consent_basis is required")
        return self


class VoiceTelemetryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    session_id: uuid.UUID
    transcript_chunk: str = Field(min_length=1, max_length=4_000)
    speaker: str = Field(default="CLIENT", pattern=r"^(CLIENT|AGENT|AI)$")
    is_final: bool = False


class ScriptChannel(str, Enum):
    VOICE = "VOICE"
    SMS = "SMS"
    EMAIL = "EMAIL"


class CommsScriptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    client_id: uuid.UUID
    property_id: uuid.UUID
    channel: ScriptChannel
    objective: str = Field(min_length=3, max_length=1_000)


_COUNTER_CONTEXT = re.compile(
    r"(?:won['’]?t\s+take\s+less\s+than|wouldn['’]?t\s+take\s+less\s+than|"
    r"need(?:\s+at\s+least)?|counter(?:ing|\s+offer)?(?:\s+is|\s+at)?|"
    r"asking(?:\s+for|\s+price\s+is)?|my\s+number\s+is|I(?:'|’)?ll\s+take)"
    r"[^$0-9]{0,35}\$?\s*(\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)"
    r"\s*(k|thousand|m|million)?\b",
    re.IGNORECASE,
)
def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
            return decoded if isinstance(decoded, dict) else {}
        except (TypeError, ValueError):
            return {}
    return {}


def extract_counter_offer(transcript: str) -> Optional[Decimal]:
    """Extract only money tied to an explicit negotiation phrase."""
    match = _COUNTER_CONTEXT.search(transcript)
    if not match:
        return None
    try:
        amount = Decimal(match.group(1).replace(",", ""))
    except (InvalidOperation, AttributeError):
        return None
    suffix = str(match.group(2) or "").lower()
    if suffix in {"k", "thousand"}:
        amount *= Decimal("1000")
    elif suffix in {"m", "million"}:
        amount *= Decimal("1000000")
    if amount < 0 or amount > Decimal("1000000000"):
        return None
    return amount.quantize(Decimal("0.01"))


def _money_fact(value: Any) -> Optional[Decimal]:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return amount if amount.is_finite() and amount >= 0 else None


def _property_financials(underwriting: dict[str, Any], payload: dict[str, Any]) -> tuple[Any, Any]:
    arv = (
        underwriting.get("arv")
        or underwriting.get("after_repair_value")
        or payload.get("arv")
        or payload.get("after_repair_value")
    )
    rehab = (
        underwriting.get("rehab")
        or underwriting.get("rehab_estimate")
        or underwriting.get("estimated_rehab")
        or payload.get("rehab")
        or payload.get("rehab_estimate")
    )
    return arv, rehab


def _objective_objection_response(
    guidance: dict[str, Any],
    underwriting: dict[str, Any],
    payload: dict[str, Any],
) -> str:
    facts: list[tuple[str, Decimal]] = []
    repair_items = (
        underwriting.get("repair_items")
        or underwriting.get("rehab_breakdown")
        or payload.get("repair_items")
        or {}
    )
    if isinstance(repair_items, dict):
        for label, raw_amount in repair_items.items():
            amount = _money_fact(
                raw_amount.get("cost") if isinstance(raw_amount, dict) else raw_amount
            )
            if amount is not None:
                facts.append((str(label).replace("_", " "), amount))
    elif isinstance(repair_items, list):
        for item in repair_items:
            if not isinstance(item, dict):
                continue
            amount = _money_fact(item.get("cost") or item.get("estimate"))
            label = item.get("name") or item.get("item")
            if amount is not None and label:
                facts.append((str(label), amount))

    prefix = str(guidance["objection_draft"])
    if not facts:
        return prefix
    label, amount = max(facts, key=lambda item: item[1])
    return (
        f"{prefix} The current property record includes {label} at an estimated "
        f"${amount:,.0f}; verify that estimate and the underlying inspection before responding."
    )


def voice_session_group(tenant_id: str, session_id: str) -> str:
    return f"voice:{tenant_id}:{session_id}"


async def _broadcast_voice(ctx: TenantContext, session_id: str, frame: dict[str, Any]) -> None:
    await ws_hub.broadcast(ctx.tenant_id, frame)
    await ws_hub.broadcast(voice_session_group(ctx.tenant_id, session_id), frame)


@dataclass(frozen=True)
class VoiceJob:
    """One staged walkthrough. Carries the live TenantContext — the queue is
    in-process, so no serialization round-trip and no identity reconstruction."""
    ctx: TenantContext
    lead_id: str
    staged_path: Path
    original_filename: str


_queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_MAX)
_workers: list[asyncio.Task] = []

# faster-whisper model is loaded once and shared; its transcribe() is not
# guaranteed thread-safe, so a threading.Lock serializes the to_thread calls.
_whisper_model = None
_whisper_lock = threading.Lock()


def _transcription_available() -> bool:
    return importlib.util.find_spec("faster_whisper") is not None


# ── Ingestion endpoint ────────────────────────────────────────────────────────

def _stage_upload(src, dest: Path) -> int:
    """Copy the spooled upload to staging in 1 MiB chunks, enforcing the size
    cap as we go (runs in a thread — sync I/O is fine here)."""
    written = 0
    with open(dest, "wb") as out:
        while chunk := src.read(1024 * 1024):
            written += len(chunk)
            if written > MAX_AUDIO_BYTES:
                raise ValueError("audio exceeds size cap")
            out.write(chunk)
    return written


@router.post("/log-walkthrough/{lead_id}", status_code=status.HTTP_202_ACCEPTED)
async def log_walkthrough(
    lead_id: str,
    audio_file: UploadFile = File(...),
    ctx: TenantContext = Depends(require_context),
):
    """Catch a mobile walkthrough recording, stage it, queue it, return 202.
    Heavy work (Whisper, Bedrock, DB) happens in the worker pool — never here."""
    try:
        uuid.UUID(lead_id)
    except ValueError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "lead_id must be a UUID")

    suffix = Path(audio_file.filename or "").suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            f"unsupported audio format {suffix or '(none)'} — expected one of {sorted(ALLOWED_SUFFIXES)}",
        )

    if not _transcription_available():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "transcription engine not installed on this node (pip install faster-whisper)",
        )

    # uuid-only filename — the client's filename never touches the path.
    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    staged = STAGING_DIR / f"walkthrough_{uuid.uuid4().hex}{suffix}"
    try:
        size = await asyncio.to_thread(_stage_upload, audio_file.file, staged)
    except ValueError:
        staged.unlink(missing_ok=True)
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"audio exceeds {MAX_AUDIO_BYTES // (1024 * 1024)} MiB cap",
        )

    job = VoiceJob(
        ctx=ctx,
        lead_id=lead_id,
        staged_path=staged,
        original_filename=audio_file.filename or staged.name,
    )
    try:
        _queue.put_nowait(job)
    except asyncio.QueueFull:
        staged.unlink(missing_ok=True)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "voice processing queue is full — retry shortly",
        )

    logger.info(
        "Walkthrough staged: lead=%s tenant=%s %d bytes (%s)",
        lead_id, ctx.tenant_id, size, suffix,
    )
    return {"status": "queued", "bytes": size}


@router.post("/session", status_code=status.HTTP_201_CREATED)
async def create_voice_session(
    body: VoiceSessionRequest,
    ctx: TenantContext = Depends(require_context),
):
    """Open a tenant-scoped transcription session with an explicit consent record."""
    async with tenant_tx(ctx) as conn:
        client_id = body.client_id
        if client_id:
            client_exists = await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM clients WHERE id=$1)",
                client_id,
            )
            if not client_exists:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Client not found.")

        lead_id = None
        if body.property_id:
            lead_id = await conn.fetchval(
                """
                SELECT id FROM leads WHERE id=$1
                UNION ALL
                SELECT lead_id FROM listings WHERE id=$1 AND lead_id IS NOT NULL
                LIMIT 1
                """,
                body.property_id,
            )
            if lead_id is None:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    "Voice telemetry requires a lead-backed property.",
                )

        session = await conn.fetchrow(
            """
            INSERT INTO live_call_sessions (
                tenant_id,client_id,lead_id,consent_recorded,consent_basis,
                started_at,transcript_status,created_by
            ) VALUES ($1::uuid,$2,$3,$4,$5,now(),$6,$7)
            RETURNING id,started_at,transcript_status
            """,
            ctx.tenant_id,
            client_id,
            lead_id,
            body.consent_recorded,
            body.consent_basis,
            "active" if body.consent_recorded else "pending",
            ctx.agent_id,
        )
        consent_event = await conn.fetchrow(
            """
            INSERT INTO negotiation_events (
                tenant_id,call_session_id,event_type,payload,model_version,created_by
            ) VALUES ($1::uuid,$2,'consent',$3::jsonb,
                      'explicit-transcription-consent-2026.07',$4)
            RETURNING id
            """,
            ctx.tenant_id,
            session["id"],
            json.dumps(
                {
                    "consent_recorded": body.consent_recorded,
                    "basis": body.consent_basis,
                }
            ),
            ctx.agent_id,
        )

    frame = {
        "type": "VOICE_SESSION",
        "version": 1,
        "session_id": str(session["id"]),
        "consent_recorded": body.consent_recorded,
        "transcript_status": session["transcript_status"],
        "started_at": session["started_at"].isoformat(),
        "event_id": consent_event["id"],
    }
    await _broadcast_voice(ctx, str(session["id"]), frame)
    await ledger.record(
        category=AuditCategory.USER_STATE_CHANGE,
        action="voice_session_created",
        tenant_id=ctx.tenant_id,
        user_id=ctx.agent_id,
        target_id=str(session["id"]),
        metadata={
            "client_id": str(client_id) if client_id else None,
            "lead_id": str(lead_id) if lead_id else None,
            "consent_recorded": body.consent_recorded,
        },
    )
    return frame


@router.post("/telemetry")
async def ingest_voice_telemetry(
    body: VoiceTelemetryRequest,
    ctx: TenantContext = Depends(require_context),
):
    """Persist a consented transcript chunk and calculate live MAO when applicable."""
    async with tenant_tx(ctx) as conn:
        call = await conn.fetchrow(
            """
            SELECT session.id,session.client_id,session.lead_id,
                   session.consent_recorded,session.transcript_status,
                   lead.underwriting,lead.payload
              FROM live_call_sessions AS session
              LEFT JOIN leads AS lead ON lead.id=session.lead_id
             WHERE session.id=$1
             FOR SHARE OF session
            """,
            body.session_id,
        )
        if call is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Voice session not found.")
        if not call["consent_recorded"]:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Consented transcription is required before telemetry ingestion.",
            )
        if call["transcript_status"] in {"complete", "failed", "deleted"}:
            raise HTTPException(status.HTTP_409_CONFLICT, "Voice session is not active.")

        underwriting = _json_object(call["underwriting"])
        property_payload = _json_object(call["payload"])
        counter = extract_counter_offer(body.transcript_chunk) if body.speaker == "CLIENT" else None
        guidance = None
        unavailable_reason = None
        if counter is not None:
            arv, rehab = _property_financials(underwriting, property_payload)
            if arv in (None, "") or rehab in (None, ""):
                unavailable_reason = "ARV and rehab estimates are required for MAO evaluation."
            else:
                try:
                    guidance = negotiation_guidance(
                        counter_offer=counter,
                        arv=arv,
                        rehab=rehab,
                        acquisition_ratio="0.70",
                        amber_tolerance="0.05",
                    )
                    guidance["arv"] = float(Decimal(str(arv)))
                    guidance["rehab"] = float(Decimal(str(rehab)))
                    guidance["objection_draft"] = _objective_objection_response(
                        guidance,
                        underwriting,
                        property_payload,
                    )
                except IntelligenceInputError as exc:
                    unavailable_reason = str(exc)

        event_payload = {
            "speaker": body.speaker,
            "is_final": body.is_final,
            "guidance": guidance,
            "mao_unavailable_reason": unavailable_reason,
        }
        event = await conn.fetchrow(
            """
            INSERT INTO negotiation_events (
                tenant_id,call_session_id,event_type,transcript_excerpt,
                counter_offer,arv,rehab,mao,threshold,payload,
                model_version,created_by
            ) VALUES (
                $1::uuid,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb,
                'objective-voice-telemetry-2026.07',$11
            ) RETURNING id,created_at
            """,
            ctx.tenant_id,
            body.session_id,
            "counter_offer" if counter is not None else "transcript",
            body.transcript_chunk,
            counter,
            (guidance or {}).get("arv"),
            (guidance or {}).get("rehab"),
            (guidance or {}).get("mao"),
            (guidance or {}).get("threshold"),
            json.dumps(event_payload, default=str),
            ctx.agent_id,
        )
        await conn.execute(
            """
            UPDATE live_call_sessions
               SET transcript_status='active',started_at=COALESCE(started_at,now())
             WHERE id=$1
            """,
            body.session_id,
        )

    frame = {
        "type": "VOICE_TELEMETRY",
        "version": 1,
        "session_id": str(body.session_id),
        "event_id": event["id"],
        "created_at": event["created_at"].isoformat(),
        "transcript": {
            "speaker": body.speaker,
            "text": body.transcript_chunk,
            "is_final": body.is_final,
        },
        "counter_offer": float(counter) if counter is not None else None,
        "mao": (guidance or {}).get("mao"),
        "threshold": (guidance or {}).get("threshold"),
        "objection_draft": (guidance or {}).get("objection_draft"),
        "mao_unavailable_reason": unavailable_reason,
        "formula": "MAO = ARV * 0.70 - Rehab",
        "requires_agent_approval": True,
    }
    await _broadcast_voice(ctx, str(body.session_id), frame)
    return frame


@router.get("/telemetry")
async def read_voice_telemetry(
    session_id: Optional[uuid.UUID] = None,
    after_event_id: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=250),
    ctx: TenantContext = Depends(require_context),
):
    """REST recovery feed for browsers whose voice socket is offline."""
    async with tenant_tx(ctx) as conn:
        if session_id is None:
            session_id = await conn.fetchval(
                """
                SELECT id FROM live_call_sessions
                 WHERE transcript_status IN ('pending','active')
                 ORDER BY created_at DESC
                 LIMIT 1
                """
            )
        if session_id is None:
            return {"session_id": None, "events": [], "next_event_id": after_event_id}
        session_exists = await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM live_call_sessions WHERE id=$1)",
            session_id,
        )
        if not session_exists:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Voice session not found.")
        rows = await conn.fetch(
            """
            SELECT id,event_type,transcript_excerpt,counter_offer,arv,rehab,mao,
                   threshold,payload,created_at
              FROM negotiation_events
             WHERE call_session_id=$1 AND id>$2
             ORDER BY id ASC
             LIMIT $3
            """,
            session_id,
            after_event_id,
            limit,
        )

    events = []
    for row in rows:
        payload = _json_object(row["payload"])
        guidance = _json_object(payload.get("guidance"))
        events.append(
            {
                "type": "VOICE_TELEMETRY",
                "version": 1,
                "session_id": str(session_id),
                "event_id": row["id"],
                "created_at": row["created_at"].isoformat(),
                "event_type": row["event_type"],
                "transcript": {
                    "speaker": payload.get("speaker") or "SYSTEM",
                    "text": row["transcript_excerpt"],
                    "is_final": bool(payload.get("is_final")),
                } if row["transcript_excerpt"] else None,
                "counter_offer": float(row["counter_offer"]) if row["counter_offer"] is not None else None,
                "arv": float(row["arv"]) if row["arv"] is not None else None,
                "rehab": float(row["rehab"]) if row["rehab"] is not None else None,
                "mao": float(row["mao"]) if row["mao"] is not None else None,
                "threshold": row["threshold"],
                "objection_draft": guidance.get("objection_draft"),
                "mao_unavailable_reason": payload.get("mao_unavailable_reason"),
            }
        )
    return {
        "session_id": str(session_id),
        "events": events,
        "next_event_id": events[-1]["event_id"] if events else after_event_id,
    }


@comms_router.post("/generate-script")
async def generate_comms_script(
    body: CommsScriptRequest,
    ctx: TenantContext = Depends(require_context),
):
    """Build an editable outreach draft exclusively from tenant CRM facts."""
    async with tenant_tx(ctx) as conn:
        client = await conn.fetchrow(
            "SELECT id,full_name,email,phone FROM clients WHERE id=$1 AND archived_at IS NULL",
            body.client_id,
        )
        if client is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Client not found.")
        property_row = await conn.fetchrow(
            """
            SELECT id,address,payload FROM leads WHERE id=$1
            UNION ALL
            SELECT id,address,jsonb_build_object('price',price)
              FROM listings WHERE id=$1
            LIMIT 1
            """,
            body.property_id,
        )
        if property_row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Property not found.")
        interaction_count = await conn.fetchval(
            """
            SELECT count(*) FROM interaction_logs
             WHERE client_id=$1 AND created_at >= now()-interval '30 days'
            """,
            body.client_id,
        )

    profile = await load_agent_identity(ctx)
    first_name = str(client["full_name"] or "there").split()[0]
    address = str(property_row["address"] or "the property")
    signature = str(profile.get("signature") or profile.get("name") or "").strip()
    objective = " ".join(body.objective.split())
    base = f"Hi {first_name}, {objective} Regarding {address}, I’m available to review the verified details with you."
    if body.channel is ScriptChannel.SMS:
        script = f"{base} — {profile.get('name') or signature}".strip()
        subject = None
    elif body.channel is ScriptChannel.EMAIL:
        script = f"{base}\n\n{signature}".strip()
        subject = f"Follow-up regarding {address}"
    else:
        from outreach_compliance import AI_VOICE_DISCLOSURE

        script = f"{AI_VOICE_DISCLOSURE} {base}"
        subject = None

    await ledger.record(
        category=AuditCategory.USER_STATE_CHANGE,
        action="comms_script_drafted",
        tenant_id=ctx.tenant_id,
        user_id=ctx.agent_id,
        target_id=str(body.client_id),
        metadata={
            "property_id": str(body.property_id),
            "channel": body.channel.value,
            "recent_interactions_reviewed": int(interaction_count or 0),
        },
    )
    return {
        "channel": body.channel.value,
        "client_id": str(body.client_id),
        "property_id": str(body.property_id),
        "subject": subject,
        "script": script,
        "requires_approval": True,
        "facts_used": {
            "client_name": client["full_name"],
            "property_address": address,
            "agent_name": profile.get("name"),
            "agent_tone": profile.get("communication_tone"),
            "recent_interactions_reviewed": int(interaction_count or 0),
        },
        "compliance_note": "Review state-specific outreach and recording requirements before dispatch.",
    }


# ── Worker pipeline ───────────────────────────────────────────────────────────

def _transcribe(path: Path) -> str:
    from faster_whisper import WhisperModel  # lazy — heavy import, optional dep

    global _whisper_model
    with _whisper_lock:
        if _whisper_model is None:
            logger.info("Loading faster-whisper model %r…", WHISPER_MODEL_SIZE)
            _whisper_model = WhisperModel(
                WHISPER_MODEL_SIZE, device="auto", compute_type="auto"
            )
        segments, _info = _whisper_model.transcribe(str(path), vad_filter=True)
        return " ".join(seg.text.strip() for seg in segments).strip()


def _extract_json(text: str) -> Optional[dict]:
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None


async def _extract(transcript: str) -> Optional[dict]:
    """Structured extraction with the same primary→secondary fallback as
    tour_generator. Returns None when both models fail — caller degrades."""
    prompt = EXTRACTION_PROMPT.format(transcript=transcript)
    raw = await asyncio.to_thread(invoke_bedrock_model, PRIMARY_MODEL, prompt, 1024)
    if not raw:
        raw = await asyncio.to_thread(invoke_bedrock_model, SECONDARY_MODEL, prompt, 1024)
    return _extract_json(raw) if raw else None


async def _process(job: VoiceJob) -> None:
    try:
        transcript = await asyncio.to_thread(_transcribe, job.staged_path)
        if not transcript:
            logger.warning("Empty transcript for lead %s — nothing to log.", job.lead_id)
            return

        extracted = await _extract(transcript)
        if extracted is None:
            logger.warning(
                "Extraction failed for lead %s — logging raw transcript only.",
                job.lead_id,
            )

        summary = (extracted or {}).get("action_summary") or (
            transcript[:140] + ("…" if len(transcript) > 140 else "")
        )
        sentiment = (extracted or {}).get("seller_sentiment") or "Unknown"

        payload = {
            "transcript": transcript,
            "extraction": extracted,
            "source_filename": job.original_filename,
            "agent_id": job.ctx.agent_id,
        }
        # Append-only dossier stream (migration 0008). RLS is live via tenant_tx;
        # tenant_id is still written explicitly because the column is NOT NULL.
        async with tenant_tx(job.ctx) as conn:
            await conn.execute(
                """
                INSERT INTO interaction_logs
                    (tenant_id, lead_id, actor_role, interaction_type, payload)
                VALUES ($1, $2, 'agent', 'voice_note', $3::jsonb)
                """,
                job.ctx.tenant_id,
                job.lead_id,
                json.dumps(payload),
            )

        reached = await ws_hub.broadcast(
            job.ctx.tenant_id,
            {
                "type": "VOICE_NOTE_LOGGED",
                "lead_id": job.lead_id,
                "summary": summary,
                "sentiment": sentiment,
                "price_adjustment": (extracted or {}).get("price_adjustment"),
            },
        )
        logger.info(
            "Voice note logged: lead=%s sentiment=%r broadcast→%d socket(s)",
            job.lead_id, sentiment, reached,
        )
    finally:
        job.staged_path.unlink(missing_ok=True)


async def _worker_loop(worker_id: int) -> None:
    logger.info("Voice worker %d online.", worker_id)
    while True:
        job = await _queue.get()
        try:
            await _process(job)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — one bad job must not kill the worker
            logger.exception(
                "Voice job failed: lead=%s tenant=%s", job.lead_id, job.ctx.tenant_id
            )
        finally:
            _queue.task_done()


async def start_voice_workers() -> None:
    """Called from server lifespan startup."""
    for i in range(WORKER_COUNT):
        _workers.append(asyncio.create_task(_worker_loop(i)))


async def stop_voice_workers() -> None:
    """Called from server lifespan shutdown — cancel and drain."""
    for task in _workers:
        task.cancel()
    await asyncio.gather(*_workers, return_exceptions=True)
    _workers.clear()
