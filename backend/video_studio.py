"""video_studio.py — Sora 2 video marketing-studio pipeline.

Turns property metadata + up to four photos into a finished marketing reel:

  text kind   : one script → one Sora 2 text-to-video generation (native audio)
  image kind  : one script → N per-photo prompts → N Sora 2 image-to-video
                generations → PyAV concatenation into one MP4 (no ffmpeg
                subprocess; the `av` wheel bundles its own libav)

Provider: Azure OpenAI Sora 2, addressed as plain REST multipart against
``ORACLE_AZURE_OPENAI_ENDPOINT`` with the deployment name from
``ORACLE_SORA_DEPLOYMENT`` (default ``sora-2-estate``). Auth follows the same
chain the Foundry chat client uses: managed identity (DefaultAzureCredential,
``AZURE_CLIENT_ID``) with the ``cognitiveservices.azure.com/.default`` scope,
or an API key when ``ORACLE_AZURE_OPENAI_API_KEY`` is set.

The worker is durable and DB-driven (pattern: automation_jobs.py): a poll loop
claims ``queued`` rows with ``FOR UPDATE SKIP LOCKED``, so a restart recovers
orphaned jobs instead of stranding them. Sora allows max 2 pending generations
per resource, so a 2-slot semaphore serialises clip submissions.

Job lifecycle: queued → scripting → generating → stitching → ready | failed | cancelled.

Final MP4 bytes land in media_blobs + a tenant-scoped property_media row
(kind='video') so delivery reuses the authenticated GET /api/media/{id} path;
quota (seconds consumed per tenant per day) is enforced here so a burst of
clips cannot blow past ORACLE_VIDEO_DAILY_QUOTA.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional
from uuid import UUID, uuid4

import media_storage
from db.connection import tenant_tx
from tenancy import Role, TenantContext

logger = logging.getLogger("oracle.video_studio")

# ---------------------------------------------------------------------------
# Configuration (catalogued in config.ENV_VARS["video_studio"])
# ---------------------------------------------------------------------------
AZURE_OPENAI_ENDPOINT: str = os.getenv("ORACLE_AZURE_OPENAI_ENDPOINT", "").rstrip("/")
SORA_DEPLOYMENT: str = os.getenv("ORACLE_SORA_DEPLOYMENT", "sora-2-estate")
SORA_API_KEY: str = os.getenv("ORACLE_AZURE_OPENAI_API_KEY", "")

MAX_IMAGES: int = int(os.getenv("ORACLE_VIDEO_MAX_IMAGES", "4"))
CLIP_SECONDS: int = int(os.getenv("ORACLE_VIDEO_CLIP_SECONDS", "8"))
DAILY_QUOTA_SECONDS: int = int(os.getenv("ORACLE_VIDEO_DAILY_QUOTA", "120"))
POLL_SECONDS: float = float(os.getenv("ORACLE_VIDEO_POLL_SECONDS", "5"))
JOB_LEASE_SECONDS: int = int(os.getenv("ORACLE_VIDEO_JOB_LEASE_SECONDS", "900"))

ALLOWED_SIZES: frozenset[str] = frozenset({"720x1280", "1280x720"})
ALLOWED_KINDS: frozenset[str] = frozenset({"text", "image"})
# Status chain (mirrors migration 0063 CHECK constraint).
ACTIVE_STATUSES: frozenset[str] = frozenset(
    {"queued", "scripting", "generating", "stitching"}
)

_PLATFORM_TENANT_ID = os.getenv("ORACLE_DEMO_TENANT_ID", "00000000-0000-0000-0000-000000000001")

_SORA_TIMEOUTS = {
    "submit": int(os.getenv("ORACLE_VIDEO_SUBMIT_TIMEOUT_SECONDS", "60")),
    "poll": int(os.getenv("ORACLE_VIDEO_POLL_TIMEOUT_SECONDS", "30")),
    "download": int(os.getenv("ORACLE_VIDEO_DOWNLOAD_TIMEOUT_SECONDS", "120")),
}


class VideoStudioError(Exception):
    """Raised for provider, quota, and stitching failures (message ≤500 chars)."""


class QuotaExceeded(VideoStudioError):
    pass


# ---------------------------------------------------------------------------
# Sora 2 client — sync (worker runs it in a thread), requests-based.
# ---------------------------------------------------------------------------
@lru_cache(maxsize=1)
def _sora_credential():
    """The long-lived credential object — safe to cache, refreshes internally."""
    from azure.identity import DefaultAzureCredential

    return DefaultAzureCredential(
        exclude_interactive_browser_credential=True,
        managed_identity_client_id=os.getenv("AZURE_CLIENT_ID") or None,
    )


def _sora_token() -> str:
    """Entra access token for the cognitive-services scope (managed identity).

    Deliberately NOT cached. Entra access tokens expire in ~60-90 minutes; an
    @lru_cache here pins the first one forever and every job after expiry 401s
    until the container restarts. Cache the credential (which refreshes on its
    own) and mint per use — the same shape db/connection.py:_azure_credential()
    uses. DefaultAzureCredential keeps its own token cache, so this is cheap.
    """
    if SORA_API_KEY:
        return ""
    if not AZURE_OPENAI_ENDPOINT:
        raise VideoStudioError("ORACLE_AZURE_OPENAI_ENDPOINT is not configured")
    token = _sora_credential().get_token("https://cognitiveservices.azure.com/.default")
    return token.token


def _sora_headers() -> dict[str, str]:
    token = _sora_token()
    if token:
        return {"Authorization": f"Bearer {token}"}
    return {"api-key": SORA_API_KEY}


def _sora_submit(*, prompt: str, size: str, seconds: int, image_bytes: Optional[bytes] = None) -> str:
    """Submit one generation; returns the provider job id."""
    import requests

    if not AZURE_OPENAI_ENDPOINT:
        raise VideoStudioError("ORACLE_AZURE_OPENAI_ENDPOINT is not configured")
    url = f"{AZURE_OPENAI_ENDPOINT}/openai/v1/videos"
    data = {
        "model": SORA_DEPLOYMENT,
        "prompt": prompt,
        "size": size,
        "seconds": str(seconds),
    }
    files = None
    if image_bytes is not None:
        files = {"input_reference": ("input.jpg", image_bytes, "image/jpeg")}
    try:
        response = requests.post(
            url, data=data, files=files, headers=_sora_headers(),
            timeout=_SORA_TIMEOUTS["submit"],
        )
    except requests.RequestException as exc:
        raise VideoStudioError(f"Sora submit failed: {exc}") from exc
    if response.status_code not in (200, 201, 202):
        raise VideoStudioError(
            f"Sora submit returned {response.status_code}: {response.text[:300]}"
        )
    body = response.json()
    job_id = body.get("id") or body.get("job_id")
    if not job_id:
        raise VideoStudioError(f"Sora submit missing job id: {body!r}")
    return str(job_id)


def _sora_status(job_id: str) -> tuple[str, str]:
    """Return (status, error). status: succeeded | failed | cancelled | pending/running."""
    import requests

    url = f"{AZURE_OPENAI_ENDPOINT}/openai/v1/videos/{job_id}"
    try:
        response = requests.get(url, headers=_sora_headers(), timeout=_SORA_TIMEOUTS["poll"])
    except requests.RequestException as exc:
        raise VideoStudioError(f"Sora status poll failed: {exc}") from exc
    if response.status_code == 404:
        return "failed", f"provider job {job_id} not found"
    if response.status_code != 200:
        raise VideoStudioError(
            f"Sora status poll returned {response.status_code}: {response.text[:300]}"
        )
    body = response.json()
    status = str(body.get("status", "")).lower()
    error = ""
    err = body.get("error") or {}
    if isinstance(err, dict):
        error = str(err.get("message") or err.get("code") or "")[:400]
    elif err:
        error = str(err)[:400]
    return status, error


def _sora_download(job_id: str) -> bytes:
    import requests

    url = f"{AZURE_OPENAI_ENDPOINT}/openai/v1/videos/{job_id}/content"
    try:
        response = requests.get(url, headers=_sora_headers(), timeout=_SORA_TIMEOUTS["download"])
    except requests.RequestException as exc:
        raise VideoStudioError(f"Sora download failed: {exc}") from exc
    if response.status_code != 200:
        raise VideoStudioError(
            f"Sora download returned {response.status_code}: {response.text[:300]}"
        )
    return response.content


async def generate_clip(
    prompt: str, size: str, seconds: int, image_bytes: Optional[bytes] = None
) -> bytes:
    """Generate one clip through the configured provider.

    This function is the whole provider seam: everything above it (scripting,
    prompt segmentation, captions, stitching, quota, storage, the durable
    worker) is provider-agnostic, and everything below it now lives in
    video_providers.py. Sora's deprecation is therefore a config change rather
    than a rewrite.

    Concurrency moved onto the provider — the old module-level 2-slot semaphore
    encoded *Sora's* per-resource limit and would have silently applied it to a
    backend with a different ceiling.
    """
    from video_providers import VideoProviderError, get_provider

    provider = get_provider()
    try:
        return await provider.generate(
            prompt=prompt, size=size, seconds=seconds, image_bytes=image_bytes
        )
    except VideoProviderError as exc:
        # Keep the worker's existing failure contract: it catches VideoStudioError
        # and writes the message to the job row.
        raise VideoStudioError(str(exc)) from exc


# ---------------------------------------------------------------------------
# Script drafting — Foundry Responses client (pattern: client_ai_automation).
# ---------------------------------------------------------------------------
_SCRIPT_MODEL = os.getenv("ORACLE_FOUNDRY_MODEL", "Kimi-K2.6")

_SCRIPT_INSTRUCTIONS = """You are the creative director of a real-estate video marketing studio.

Write a 20-25 second reel script for the property described. Return PLAIN TEXT ONLY:
- A 60-90 word spoken narration (short punchy sentences, present tense, property focus).
- No headings, no labels, no scene directions, no timestamps, no emoji.

Style: premium, emotional, market-savvy. Never invent facts beyond the property data
given. If a fact is absent (e.g. no price or beds), do not invent one — describe the
space generically instead. No protected-class language; no claims about schools,
neighborhood demographics, or investment guarantees."""


def _foundry_responses_client():
    endpoint = os.getenv("ORACLE_FOUNDRY_PROJECT_ENDPOINT", "").rstrip("/")
    if not endpoint:
        raise RuntimeError("Foundry project endpoint is not configured")
    from azure.ai.projects import AIProjectClient
    from azure.identity import DefaultAzureCredential

    credential = DefaultAzureCredential(
        exclude_interactive_browser_credential=True,
        managed_identity_client_id=os.getenv("AZURE_CLIENT_ID") or None,
    )
    return AIProjectClient(endpoint=endpoint, credential=credential).get_openai_client()


def draft_script(property_info: dict[str, Any], *, max_chars: int = 800) -> str:
    """Draft a reel narration from property metadata. Falls back to a
    deterministic template when the Foundry endpoint is not configured."""
    summary = _property_summary(property_info)
    try:
        response = _foundry_responses_client().responses.create(
            model=_SCRIPT_MODEL,
            instructions=_SCRIPT_INSTRUCTIONS,
            input=[{"role": "user", "content": f"PROPERTY DATA:\n{summary}"}],
            max_output_tokens=512,
            reasoning={"effort": "low"},
            store=False,
        )
        text = str(getattr(response, "output_text", "") or "").strip()
        if text:
            return text[:max_chars]
    except Exception as exc:  # noqa: BLE001 — template fallback on any provider hiccup
        logger.warning("Script drafting fell back to template: %s", exc)
    return _template_script(property_info)


def _property_summary(property_info: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in (
        "address", "city", "state", "zip_code", "bedrooms", "bathrooms", "sqft",
        "year_built", "price", "estimated_value", "land_use", "zoning_district",
        "features", "description",
    ):
        value = property_info.get(key)
        if value in (None, "", [], {}):
            continue
        if isinstance(value, (list, tuple)):
            value = ", ".join(str(v) for v in value)
        parts.append(f"{key}: {value}")
    return "\n".join(parts) or "unknown property"


def _template_script(property_info: dict[str, Any]) -> str:
    address = property_info.get("address") or "this listing"
    beds = property_info.get("bedrooms")
    baths = property_info.get("bathrooms")
    sqft = property_info.get("sqft")
    bits = [f"Welcome to {address}."]
    if beds or baths or sqft:
        bits.append(f"Featuring {beds or '?'} bedrooms, {baths or '?'} bathrooms, and {sqft or '?'} square feet of potential.")
    bits.append("Move-in ready for its next chapter — schedule your private tour today.")
    return " ".join(bits)


def _script_segments(script: str, count: int) -> list[str]:
    """Split one script into the N narration chunks the clips are built from.

    Shared by the Sora prompts and the burned-in captions so the words on screen
    are the words that shot was generated from — they cannot drift apart.
    """
    sentences = [s.strip() for s in script.replace("—", ". ").split(".") if s.strip()]
    if count <= 1 or not sentences:
        return [script]
    segments: list[str] = []
    for i in range(count):
        start = i * len(sentences) // count
        end = (i + 1) * len(sentences) // count
        segments.append(" ".join(sentences[start:end]) or sentences[min(i, len(sentences) - 1)])
    return segments


def _per_clip_prompts(script: str, count: int) -> list[str]:
    """Split one script across N clips (one short sentence each when possible)."""
    segments = _script_segments(script, count)
    if count <= 1 or not script.strip():
        return segments
    return [
        segment + " — cinematic establishing shot of the property exterior, premium real-estate reel style."
        for segment in segments
    ]


# ---------------------------------------------------------------------------
# Burned-in captions.
#
# Listing reels are watched sound-off by default, so narration that exists only
# in the audio track reaches nobody. The text is drawn into the pixels rather
# than shipped as a subtitle track because every social destination strips or
# ignores sidecar tracks on upload.
#
# Drawn with Pillow, not libavfilter: the bundled PyAV build carries neither
# `drawtext` nor `subtitles` (both need a freetype-enabled libav), while Pillow
# is already a dependency here for `_resize_to_size`.
# ---------------------------------------------------------------------------
CAPTION_MAX_CHARS = 72       # ~3 wrapped lines at 720w; beyond that nobody reads it
CAPTION_MAX_LINES = 3
_CAPTION_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
)


def caption_text(segment: str, *, max_chars: int = CAPTION_MAX_CHARS) -> str:
    """One clip's narration reduced to something readable in a few seconds."""
    clean = " ".join((segment or "").split())
    # Drop the prompt's cinematic direction if a prompt was passed in by mistake.
    clean = clean.split(" — cinematic establishing shot")[0].strip()
    if len(clean) <= max_chars:
        return clean
    cut = clean[:max_chars].rsplit(" ", 1)[0].rstrip(",;:")
    return (cut or clean[:max_chars]).rstrip() + "…"


def _captions_enabled(source: dict[str, Any]) -> bool:
    """Per-job opt-out over an env default. On by default: a silent reel with no
    text is the failure mode this exists to prevent."""
    requested = source.get("captions")
    if requested is not None:
        return bool(requested)
    return os.getenv("ORACLE_VIDEO_CAPTIONS", "1").strip().lower() in {"1", "true", "yes", "on"}


def _caption_font(height: int):
    """Largest readable font we can actually load, degrading to Pillow's default."""
    from PIL import ImageFont

    size = max(14, height // 28)
    for path in _CAPTION_FONT_CANDIDATES:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except Exception:  # noqa: BLE001 — try the next candidate
                continue
    try:
        return ImageFont.load_default(size=size)   # Pillow >= 10.1
    except TypeError:
        return ImageFont.load_default()


def _wrap_caption(draw, text: str, font, max_width: int) -> list[str]:
    """Word-wrap to at most ``CAPTION_MAX_LINES``.

    Overflow is ellipsised rather than dropped: the character cap and the pixel
    wrap disagree at some font sizes, and a caption that quietly loses its last
    clause reads as a complete sentence that says the wrong thing.
    """
    words = text.split()
    lines: list[str] = []
    current = ""
    overflowed = False
    for i, word in enumerate(words):
        candidate = f"{current} {word}".strip()
        if current and draw.textlength(candidate, font=font) > max_width:
            if len(lines) + 1 >= CAPTION_MAX_LINES and i < len(words):
                lines.append(current)
                overflowed = True
                break
            lines.append(current)
            current = word
        else:
            current = candidate
    if not overflowed and current:
        lines.append(current)
    if overflowed and lines:
        last = lines[-1].rstrip(",;:")
        while last and draw.textlength(last + "…", font=font) > max_width:
            last = last.rsplit(" ", 1)[0] if " " in last else last[:-1]
        lines[-1] = (last or lines[-1]) + "…"
    return lines


def draw_caption(image, text: str):
    """Draw a lower-third caption onto a PIL image, in place. Returns the image.

    The dark band is what makes this legible over an unknown frame — white text
    alone disappears against a bright sky, which is most of a property exterior.
    """
    from PIL import Image, ImageDraw

    text = caption_text(text)
    if not text:
        return image
    if image.mode != "RGB":
        image = image.convert("RGB")
    width, height = image.size
    font = _caption_font(height)
    margin = max(8, width // 24)

    measure = ImageDraw.Draw(image)
    lines = _wrap_caption(measure, text, font, width - 2 * margin)
    if not lines:
        return image

    ascent = max(1, int(getattr(font, "size", height // 22)))
    line_h = int(ascent * 1.35)
    block_h = line_h * len(lines)
    pad = max(6, ascent // 2)
    top = height - block_h - 2 * pad - max(12, height // 12)
    top = max(0, top)

    # Composite the band through an alpha layer so it darkens rather than hides.
    band = Image.new("RGBA", image.size, (0, 0, 0, 0))
    ImageDraw.Draw(band).rectangle(
        [(0, top), (width, min(height, top + block_h + 2 * pad))], fill=(0, 0, 0, 140)
    )
    image = Image.alpha_composite(image.convert("RGBA"), band).convert("RGB")

    draw = ImageDraw.Draw(image)
    y = top + pad
    for line in lines:
        line_w = draw.textlength(line, font=font)
        x = max(margin, (width - int(line_w)) // 2)
        # Cheap outline: legible even where the band is thin over a bright frame.
        for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            draw.text((x + dx, y + dy), line, font=font, fill=(0, 0, 0))
        draw.text((x, y), line, font=font, fill=(255, 255, 255))
        y += line_h
    return image


# ---------------------------------------------------------------------------
# Stitching — PyAV (no ffmpeg subprocess; `av` bundles its own libav).
# ---------------------------------------------------------------------------
def _clip_probe(path: Path) -> tuple[Optional[str], Optional[int], Optional[int]]:
    import av

    with av.open(str(path)) as container:
        stream = next((s for s in container.streams.video if s), None)
        if stream is None:
            raise VideoStudioError(f"clip has no video stream: {path.name}")
        return stream.codec_context.name, stream.codec_context.width, stream.codec_context.height


def stitch_clips(
    clip_paths: list[Path],
    out_path: Path,
    *,
    size: str = "720x1280",
    captions: Optional[list[str]] = None,
) -> None:
    """Concatenate clips into one MP4.

    When every clip shares codec/width/height, packets are stream-copied
    (no re-encode — fast and lossless). If any parameter differs, all clips are
    re-encoded to h264/yuv420p at the job's target size as a fallback.

    ``captions`` is one caption per clip (shorter lists pad with no caption).
    Burning text into the pixels means touching every frame, so passing captions
    always takes the re-encode path — that cost is the feature, not a fallback.
    """
    if not clip_paths:
        raise VideoStudioError("no clips to stitch")
    wants_captions = bool(captions) and any((c or "").strip() for c in captions)
    if len(clip_paths) == 1 and not wants_captions:
        import shutil

        shutil.copyfile(clip_paths[0], out_path)
        return

    if wants_captions:
        _transcode_concat(clip_paths, out_path, size=size, captions=captions)
        return

    probes = [_clip_probe(path) for path in clip_paths]
    homogeneous = len({p for p in probes if p[0]}) == 1 and len({p for p in probes if p[1]}) == 1 and len({p for p in probes if p[2]}) == 1
    if homogeneous:
        _stream_copy_concat(clip_paths, out_path)
    else:
        _transcode_concat(clip_paths, out_path, size=size)


def _stream_copy_concat(clip_paths: list[Path], out_path: Path) -> None:
    import av

    width, height = 0, 0
    with av.open(str(clip_paths[0])) as first:
        stream = next(s for s in first.streams.video if s)
        width, height = stream.codec_context.width, stream.codec_context.height
    with av.open(str(out_path), "w") as output:
        out_stream = None
        # Every clip's timestamps restart at its own origin. Copied through
        # unchanged, the second clip's first packet carries a DTS behind the last
        # one written and the muxer rejects the file outright.
        #
        # Each clip is therefore shifted to begin where the previous one ended.
        # The shift is anchored on the clip's *first DTS*, not on zero: with
        # B-frames the first DTS is negative (decode order precedes display
        # order), so anchoring on zero leaves the boundary still going backwards.
        # Safe here precisely because this path only runs for clips sharing a
        # codec, size, and therefore time_base.
        offset = 0
        for path in clip_paths:
            with av.open(str(path)) as container:
                src = next(s for s in container.streams.video if s)
                if out_stream is None:
                    out_stream = output.add_stream(template=src)
                shift: Optional[int] = None
                clip_end = offset
                for packet in container.demux(src):
                    if packet.dts is None:
                        continue        # demux's trailing flush packet
                    if shift is None:
                        shift = offset - packet.dts
                    packet.stream = out_stream
                    packet.dts += shift
                    packet.pts = (packet.pts if packet.pts is not None else packet.dts) + shift
                    clip_end = max(clip_end, packet.pts + (packet.duration or 0))
                    output.mux(packet)
                offset = clip_end
    logger.info("Stream-copied %d clips -> %s (%dx%d)", len(clip_paths), out_path.name, width, height)


def _transcode_concat(
    clip_paths: list[Path],
    out_path: Path,
    *,
    size: str,
    captions: Optional[list[str]] = None,
) -> None:
    import av
    from fractions import Fraction

    width_s, height_s = size.lower().split("x")
    width, height = int(width_s), int(height_s)
    time_base = Fraction(1, 30)
    with av.open(str(out_path), "w") as output:
        out_stream = output.add_stream("h264", rate=30)
        out_stream.width, out_stream.height, out_stream.pix_fmt = width, height, "yuv420p"
        out_stream.time_base = time_base
        index = 0            # continuous CFR clock across every clip
        for clip_index, path in enumerate(clip_paths):
            caption = (captions[clip_index] if captions and clip_index < len(captions) else "") or ""
            with av.open(str(path)) as container:
                for frame in container.decode(video=0):
                    # Each source clip's timestamps restart at zero. Re-stamping
                    # onto one monotonic clock is what makes the concatenation
                    # muxable at all — source pts would go backwards at every
                    # clip boundary.
                    if caption.strip():
                        image = draw_caption(frame.to_image(), caption)
                        frame = av.VideoFrame.from_image(image)
                    frame = frame.reformat(width=width, height=height, format="yuv420p")
                    frame.pts = index
                    frame.time_base = time_base
                    index += 1
                    for packet in out_stream.encode(frame):
                        output.mux(packet)
        for packet in out_stream.encode():
            output.mux(packet)
    logger.info(
        "Transcoded %d clips -> %s (%dx%d, %d frames, captions=%s)",
        len(clip_paths), out_path.name, width, height, index,
        bool(captions and any((c or "").strip() for c in captions)),
    )


# ---------------------------------------------------------------------------
# Worker — durable DB-driven poller (pattern: automation_jobs.py).
# ---------------------------------------------------------------------------
def _platform_context(worker_id: str) -> TenantContext:
    return TenantContext(
        agent_id=worker_id,
        tenant_id=_PLATFORM_TENANT_ID,
        role=Role.PLATFORM_ADMIN,
    )


def _job_ctx(row: dict[str, Any]) -> TenantContext:
    """A tenant-scoped context for a claimed job row (RLS applies to all its
    subsequent reads/writes — image fetch, quota, media storage)."""
    return TenantContext(
        agent_id="video-studio-worker",
        tenant_id=row["tenant_id"],
        role=Role.AGENT,
    )


async def _claim_next_job(worker_id: str) -> Optional[dict[str, Any]]:
    if os.getenv("ORACLE_VIDEO_STUDIO_DISABLED"):
        return None
    from db.connection import get_pool

    pool = get_pool()
    if pool is None:
        return None
    ctx = _platform_context(worker_id)
    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            """
            -- An in-flight job is reclaimable ONLY once its lease has expired.
            -- FOR UPDATE SKIP LOCKED protects the row for the length of this
            -- transaction and no longer, so without the updated_at test a second
            -- worker picks up a job seconds after the first started generating —
            -- Sora bills the same reel twice, both workers reserve quota, and
            -- both call _store_video, orphaning one uploaded MP4. The pipeline
            -- heartbeats updated_at through _set_status at each stage, so a live
            -- job keeps renewing while a crashed one goes stale and is recovered.
            WITH candidate AS (
                SELECT id
                  FROM video_studio_jobs
                 WHERE status = 'queued'
                    OR (status IN ('scripting', 'generating', 'stitching')
                        AND updated_at < now() - ($1::int * interval '1 second'))
                 ORDER BY created_at ASC
                 FOR UPDATE SKIP LOCKED
                 LIMIT 1
            )
            UPDATE video_studio_jobs AS j
               SET updated_at = now()
              FROM candidate
             WHERE j.id = candidate.id
            RETURNING j.*
            """,
            JOB_LEASE_SECONDS,
        )
        if row is None:
            return None
        result = dict(row)
        result["source"] = json.loads(result.get("source") or "{}")
        result["provider_job_ids"] = list(result.get("provider_job_ids") or [])
        return result


async def _set_status(ctx: TenantContext, job_id: str, status: str, **fields) -> None:
    sets = ["status = $2"]
    vals: list[Any] = [UUID(job_id), status]
    for key, value in fields.items():
        vals.append(value)
        sets.append(f"{key} = ${len(vals)}")
    async with tenant_tx(ctx) as conn:
        await conn.execute(
            f"UPDATE video_studio_jobs SET {', '.join(sets)} WHERE id = $1", *vals
        )


async def _quota_consumed_today(ctx: TenantContext, conn) -> float:
    row = await conn.fetchrow(
        """
        SELECT COALESCE(SUM(quota_consumed_units), 0)::float AS consumed
          FROM video_studio_jobs
         WHERE tenant_id = $1::uuid
           AND quota_daily_slot = CURRENT_DATE
           AND status NOT IN ('failed', 'cancelled')
        """,
        ctx.tenant_id,
    )
    return float(row["consumed"] or 0)


async def _load_input_images(ctx: TenantContext, image_media_ids: list[str]) -> list[bytes]:
    """Fetch photo bytes via the RLS-scoped property_media → media_blobs join,
    so a job can never read another tenant's images even with valid ids."""
    if not image_media_ids:
        return []
    results: list[bytes] = []
    async with tenant_tx(ctx) as conn:
        # LEFT JOIN: a storage-backed photo has no media_blobs row, and an
        # inner join here dropped it — the studio then rendered a reel from
        # fewer images than the agent selected, without saying so.
        rows = await conn.fetch(
            """
            SELECT pm.id, pm.s3_key, mb.bytes
              FROM property_media AS pm
              LEFT JOIN media_blobs AS mb ON mb.media_id = pm.id
             WHERE pm.id = ANY($1::uuid[])
               AND pm.kind = 'photo'
               AND (pm.s3_key IS NOT NULL OR mb.media_id IS NOT NULL)
             ORDER BY array_position($1::uuid[], pm.id)
            """,
            [UUID(value) for value in image_media_ids],
        )
    for row in rows:
        data = await media_storage.load_media_bytes(row)
        if data is not None:
            results.append(data)
    return results


def _resize_to_size(data: bytes, size: str) -> bytes:
    """Center-crop + resize to Sora's exact target resolution (JPEG, ≤1.9MB)."""
    from PIL import Image, ImageOps

    width_s, height_s = size.lower().split("x")
    target = (int(width_s), int(height_s))
    image = Image.open(__import__("io").BytesIO(data))
    image = ImageOps.fit(image, target, method=Image.LANCZOS)
    out = __import__("io").BytesIO()
    image.save(out, format="JPEG", quality=90)
    return out.getvalue()


async def _store_video(
    ctx: TenantContext, mp4_bytes: bytes, *, kind: str, source: dict[str, Any]
) -> str:
    """Persist the finished MP4 to object storage + a tenant-scoped
    property_media row (kind='video'). Returns the media id.

    Video never goes in media_blobs. Migration 0066 enforces
    ``CHECK (kind <> 'video' OR s3_key IS NOT NULL)`` precisely because bytea is
    right for photos and wrong for video — a handful of clips would bloat the
    table, every WAL segment and every base backup — so inserting a blob-backed
    video row does not merely waste space, it violates the constraint and fails
    the job at the last step.
    """
    import object_storage

    if not object_storage.is_configured():
        raise VideoStudioError(
            "Durable object storage is not configured (ORACLE_STORAGE_BACKEND); "
            "the finished video has nowhere to live."
        )
    media_id = uuid4()
    lead_id = source.get("lead_id")
    listing_id = source.get("listing_id")
    storage_key = f"video-studio/{ctx.tenant_id}/{media_id}.mp4"
    # Upload before the row exists: a failed upload leaves no dangling record,
    # and an orphaned object is cheap next to a media row that 404s on playback.
    await asyncio.to_thread(
        object_storage.put_bytes, storage_key, mp4_bytes, "video/mp4"
    )
    async with tenant_tx(ctx) as conn:
        base = await conn.fetchval(
            """
            SELECT COALESCE(MAX(sort_order), -1)
              FROM property_media
             WHERE ($1::uuid IS NOT NULL AND lead_id = $1)
                OR ($2::uuid IS NOT NULL AND listing_id = $2)
            """,
            UUID(lead_id) if lead_id else None,
            UUID(listing_id) if listing_id else None,
        )
        # provenance/generator are NOT optional here. Migration 0071 defaults
        # provenance to 'captured' and states that only 'captured' may support a
        # claim that the media shows the actual home. Omitting them — as this
        # INSERT did — made the database assert that a fully model-generated
        # marketing reel was a real capture of the property. The reconstruction
        # path has always written both (reconstruction_worker.py:424).
        from video_providers import get_provider

        provider = get_provider()
        await conn.execute(
            """
            INSERT INTO property_media
                (id, tenant_id, lead_id, listing_id, kind, url, sort_order,
                 s3_key, content_type, provenance, generator)
            VALUES ($1, $2, $3, $4, 'video', $5, $6, $7, 'video/mp4', $8, $9)
            """,
            media_id,
            ctx.tenant_id,
            UUID(lead_id) if lead_id else None,
            UUID(listing_id) if listing_id else None,
            f"/api/media/{media_id}",
            int(base) + 1,
            storage_key,
            provider.produces,
            provider.name,
        )
    return str(media_id)


async def _process_job(row: dict[str, Any], worker_id: str) -> None:
    job_id = row["id"]
    kind = row["kind"]
    size = row["size"]
    source = row["source"]
    script = row["script"]
    ctx = _job_ctx(row)

    # Script phase (text kind only — image jobs accept the script as-is).
    if kind == "text" and not script.strip():
        await _set_status(ctx, job_id, "scripting")
        script = await asyncio.to_thread(draft_script, source.get("property") or {})
        await _set_status(ctx, job_id, "scripting", script=script)

    # Quota gate: verify the tenant still has headroom before spending.
    clip_seconds = int(os.getenv("ORACLE_VIDEO_CLIP_SECONDS", str(CLIP_SECONDS)))
    image_ids = list(source.get("images") or []) if kind == "image" else []
    clip_count = max(1, len(image_ids))
    estimated = clip_count * clip_seconds
    async with tenant_tx(ctx) as conn:
        # Check-and-RESERVE, not check-then-act. quota_consumed_units stays 0
        # until a job reaches 'ready', so a bare read lets every worker that
        # starts before the first one finishes see consumed=0 and pass — the
        # tenant then blows past the cap on real, paid generation. The advisory
        # lock serialises the gate per tenant-day, and writing the estimate
        # immediately makes this job visible to the next worker's SUM.
        # 'failed'/'cancelled' are already excluded from that SUM, so an aborted
        # job releases its own reservation, and the 'ready' write below replaces
        # the estimate with the units actually spent.
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtext($1::text || CURRENT_DATE::text))",
            ctx.tenant_id,
        )
        consumed = await _quota_consumed_today(ctx, conn)
        if consumed + estimated > DAILY_QUOTA_SECONDS:
            remaining = max(0.0, DAILY_QUOTA_SECONDS - consumed)
            raise QuotaExceeded(
                f"Daily video quota exceeded — {estimated}s requested, {remaining:.0f}s remaining"
            )
        # Stamp the slot to the day the quota is actually SPENT, not the day the
        # job was queued. _quota_consumed_today filters quota_daily_slot =
        # CURRENT_DATE, so a job queued at 23:55 and generated after midnight
        # would otherwise reserve against yesterday's slot — invisible to today's
        # SUM, letting the whole backlog through the cap. Migration 0068 adds the
        # GRANT UPDATE that makes this column writable by the app role.
        await conn.execute(
            """
            UPDATE video_studio_jobs
               SET quota_consumed_units = $2,
                   quota_daily_slot = CURRENT_DATE
             WHERE id = $1
            """,
            UUID(job_id),
            float(estimated),
        )

    await _set_status(ctx, job_id, "generating")
    images: list[bytes] = []
    if image_ids:
        images = await _load_input_images(ctx, image_ids)
        if not images:
            raise VideoStudioError("None of the referenced photos could be read (tenant-scoped)")
        images = [await asyncio.to_thread(_resize_to_size, data, size) for data in images]

    narration = script or "A premium real-estate property tour."
    prompts = _per_clip_prompts(narration, clip_count)
    # Captions come from the same segmentation as the prompts, so the words on
    # screen are the words the shot was generated from.
    captions = (
        [caption_text(segment) for segment in _script_segments(narration, clip_count)]
        if _captions_enabled(source)
        else None
    )
    if source.get("voiceover"):
        prompts = [
            prompt + " The narration is spoken aloud over the footage, warm confident female voice."
            for prompt in prompts
        ]
    clips_dir = Path(tempfile.mkdtemp(prefix="vs_clips_"))
    provider_ids: list[str] = []
    clip_paths: list[Path] = []
    try:
        for index, prompt in enumerate(prompts[:clip_count]):
            image = images[index] if index < len(images) else None
            # Renew the claim before each clip. Sora takes minutes per clip, so a
            # multi-clip job can easily outlive JOB_LEASE_SECONDS between stage
            # transitions — and a lease that expires mid-generation is exactly
            # the double-billing _claim_next_job's lease test exists to prevent.
            # The updated_at trigger fires on this UPDATE, which is the heartbeat.
            await _set_status(ctx, job_id, "generating")
            clip_bytes = await generate_clip(
                prompt, size, clip_seconds, image_bytes=image
            )
            provider_ids.append("")
            clip_path = clips_dir / f"clip_{index}.mp4"
            clip_path.write_bytes(clip_bytes)
            clip_paths.append(clip_path)
            await _set_status(ctx, job_id, "generating", provider_job_ids=provider_ids)

        await _set_status(ctx, job_id, "stitching")
        final = clips_dir / "final.mp4"
        await asyncio.to_thread(
            stitch_clips, clip_paths, final, size=size, captions=captions
        )
        media_id = await _store_video(ctx, final.read_bytes(), kind=kind, source=source)
        units = clip_count * clip_seconds
        await _set_status(
            ctx, job_id, "ready",
            media_id=UUID(media_id),
            quota_consumed_units=units,
            provider_job_ids=provider_ids,
        )
    finally:
        import shutil

        shutil.rmtree(clips_dir, ignore_errors=True)

    try:
        from ws_hub import broadcast

        await broadcast(ctx.tenant_id, {
            "type": "VIDEO_READY",
            "jobId": job_id,
            "mediaId": media_id,
            "kind": kind,
        })
    except Exception:  # noqa: BLE001 — broadcast is best-effort; the row is the source of truth
        logger.debug("VIDEO_READY broadcast failed for job %s", job_id)
    logger.info("Video studio job completed: job=%s media=%s kind=%s clips=%d",
                job_id, media_id, kind, len(clip_paths))


async def _worker_loop(worker_id: str) -> None:
    logger.info("Video studio worker %s online.", worker_id)
    while True:
        try:
            row = await _claim_next_job(worker_id)
            if row is None:
                await asyncio.sleep(POLL_SECONDS)
                continue
            try:
                await _process_job(row, worker_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — one bad job must not kill the worker
                logger.exception("Video studio job failed: job=%s tenant=%s", row["id"], row["tenant_id"])
                try:
                    await _set_status(
                        _job_ctx(row), row["id"], "failed", error=str(exc)[:500]
                    )
                except Exception:  # noqa: BLE001
                    logger.debug("could not mark job %s failed", row["id"])
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — claim loop must survive transient DB errors
            logger.warning("Video studio worker %s claim error: %s", worker_id, exc)
            await asyncio.sleep(min(30.0, POLL_SECONDS * 2))


_workers: list[asyncio.Task] = []


async def start_video_studio_workers() -> None:
    """Called from server lifespan startup."""
    count = int(os.getenv("ORACLE_VIDEO_WORKER_COUNT", "1"))
    for i in range(count):
        _workers.append(asyncio.create_task(_worker_loop(f"vs-{i}")))


async def stop_video_studio_workers() -> None:
    """Called from server lifespan shutdown — cancel and drain."""
    for task in _workers:
        task.cancel()
    await asyncio.gather(*_workers, return_exceptions=True)
    _workers.clear()
