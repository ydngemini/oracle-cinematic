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
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status

from db.connection import tenant_tx
from ml_forge.bedrock_client import invoke_bedrock_model, PRIMARY_MODEL, SECONDARY_MODEL
from tenancy import TenantContext, require_context
import ws_hub

logger = logging.getLogger("oracle.voice_intel")

router = APIRouter(prefix="/api/voice", tags=["Voice Intelligence"])

# ── Tunables ──────────────────────────────────────────────────────────────────
STAGING_DIR = Path(os.getenv("ORACLE_AUDIO_STAGING", "/tmp/oracle_audio"))
MAX_AUDIO_BYTES = int(os.getenv("ORACLE_AUDIO_MAX_BYTES", str(25 * 1024 * 1024)))
ALLOWED_SUFFIXES = {".m4a", ".wav", ".mp3", ".webm", ".ogg"}
QUEUE_MAX = 100
WORKER_COUNT = 2
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
