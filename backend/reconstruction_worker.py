"""reconstruction_worker.py — async worker pool that turns a capture into a
walkable Gaussian splat, then records it so the tour resolver flips the property
to tier 3.

Long jobs (20-60 min) must NOT live on a request/websocket (those get cancelled
on disconnect + hit the 300s idle watchdog). So this mirrors voice_intel.py:
an in-process asyncio.Queue + fixed worker pool started/stopped in the server
lifespan; callers POST → 202 → poll a reconstruction_jobs row.

Pipeline (provider-agnostic; see reconstruction_providers.py):
  gather source photos → provider.reconstruct() → .ply/.splat
  → convert to standard .splat (PlayCanvas splat-transform) → store
  → property_media row kind='splat' (+ AI-disclosure manifest) → broadcast SPLAT_READY
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from uuid import UUID, uuid4

import ws_hub
import media_storage
from db.connection import tenant_tx
from tenancy import TenantContext
from reconstruction_providers import (
    SPATIAL_AI_DISCLOSURE,
    SPLAT_OUTPUT_DIR,
    ProviderError,
    get_provider,
)

logger = logging.getLogger("oracle.reconstruction.worker")

QUEUE_MAX = int(os.environ.get("RECON_QUEUE_MAX", "20"))
WORKER_COUNT = int(os.environ.get("RECON_WORKER_COUNT", "1"))


@dataclass(frozen=True)
class ReconstructionJob:
    """One enqueued reconstruction. Carries the live TenantContext (in-process
    queue → no serialization), the DB job id, and the target property."""
    ctx: TenantContext
    job_id: str
    lead_id: Optional[str]
    listing_id: Optional[str]


_queue: "asyncio.Queue[ReconstructionJob]" = asyncio.Queue(maxsize=QUEUE_MAX)
_workers: list[asyncio.Task] = []


def enqueue(job: ReconstructionJob) -> None:
    """Non-blocking; raises asyncio.QueueFull when saturated (caller → 503)."""
    _queue.put_nowait(job)


async def _set_status(ctx: TenantContext, job_id: str, status: str, **fields) -> None:
    sets = ["status = $2"]
    vals = [UUID(job_id), status]
    for k, v in fields.items():
        vals.append(v)
        sets.append(f"{k} = ${len(vals)}")
    async with tenant_tx(ctx) as conn:
        await conn.execute(
            f"UPDATE reconstruction_jobs SET {', '.join(sets)} WHERE id = $1", *vals
        )


# A walk-through video is the natural way to capture a house with a phone, and
# reconstruction wants overlapping stills. Two frames a second is roughly a step
# apart at walking pace, which lands inside the 70–80% overlap the solver needs
# without producing hundreds of near-identical frames.
_VIDEO_SAMPLE_FPS = float(os.environ.get("RECON_VIDEO_SAMPLE_FPS", "2") or 2)
# Hard ceiling per video so one long clip cannot blow past the provider's own
# image cap (MAX_CAPTURE_IMAGES) or fill the work dir.
_VIDEO_MAX_FRAMES = int(os.environ.get("RECON_VIDEO_MAX_FRAMES", "240") or 240)


async def _extract_video_frames(video: Path, dest: Path, prefix: str) -> list[Path]:
    """Sample stills out of a capture video. Returns [] when nothing usable came out.

    Raises ProviderError when ffmpeg is missing entirely: a capture made of
    video would otherwise reconstruct from zero images and report success, which
    is the failure mode this whole phase exists to remove.
    """
    if not shutil.which("ffmpeg"):
        raise ProviderError(
            "This capture is video, but ffmpeg is not installed on the backend "
            "so frames cannot be extracted. Install ffmpeg (it is in the "
            "backend image) or upload still photos instead."
        )

    pattern = str(dest / f"{prefix}_%04d.jpg")
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-nostdin", "-loglevel", "error",
        "-i", str(video),
        "-vf", f"fps={_VIDEO_SAMPLE_FPS}",
        "-frames:v", str(_VIDEO_MAX_FRAMES),
        "-q:v", "2",
        pattern,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        output, _ = await asyncio.wait_for(proc.communicate(), timeout=900)
    except asyncio.TimeoutError:
        proc.kill()
        raise ProviderError(f"Frame extraction timed out for {video.name}.")

    if proc.returncode != 0:
        detail = (output or b"")[-400:].decode("utf-8", "replace").strip()
        raise ProviderError(f"Frame extraction failed for {video.name}: {detail}")

    frames = sorted(dest.glob(f"{prefix}_*.jpg"))
    logger.info("Extracted %d frame(s) from %s at %sfps", len(frames), video.name, _VIDEO_SAMPLE_FPS)
    return frames


async def _gather_source_images(
    job: ReconstructionJob, dest: Path
) -> tuple[list[Path], dict]:
    """Pull the property's capture media (RLS-scoped) into a work dir as the
    reconstruction input.

    Photos are used as-is; videos are sampled into stills. Returns the image
    paths and a count of what went in, which the capture session records so
    "which media produced this splat" stays answerable afterwards.

    Empty is fine for the stub provider, which captures nothing anyway.
    """
    dest.mkdir(parents=True, exist_ok=True)
    out: list[Path] = []
    counts = {"photos": 0, "videos": 0, "frames": 0}
    async with tenant_tx(job.ctx) as conn:
        # LEFT JOIN, not JOIN: photos uploaded on a deployment with object
        # storage carry an s3_key and no blob row. An inner join silently
        # returned zero images for them, so the reconstruction ran on nothing
        # and produced a "successful" job with no capture behind it.
        rows = await conn.fetch(
            """
            SELECT pm.id, pm.kind, pm.s3_key, mb.bytes,
                   COALESCE(pm.content_type, mb.content_type) AS content_type
              FROM property_media pm
              LEFT JOIN media_blobs mb ON mb.media_id = pm.id
             WHERE pm.kind IN ('photo', 'video')
               AND (pm.s3_key IS NOT NULL OR mb.media_id IS NOT NULL)
               AND (($1::uuid IS NOT NULL AND pm.lead_id = $1)
                 OR ($2::uuid IS NOT NULL AND pm.listing_id = $2))
             ORDER BY pm.sort_order ASC
            """,
            UUID(job.lead_id) if job.lead_id else None,
            UUID(job.listing_id) if job.listing_id else None,
        )
    for r in rows:
        try:
            data = await media_storage.load_media_bytes(r)
        except Exception as exc:  # noqa: BLE001 — one unreadable item is not fatal
            logger.warning("Skipping unreadable source media %s: %s", r["id"], exc)
            continue
        if data is None:
            continue

        if r["kind"] == "video":
            # Staged to disk first: ffmpeg reads a file, and a capture video can
            # be hundreds of megabytes that we do not want to hold twice.
            container = ".mov" if "quicktime" in (r["content_type"] or "") else ".mp4"
            staged = dest / f"{r['id']}{container}"
            staged.write_bytes(data)
            try:
                frames = await _extract_video_frames(staged, dest, str(r["id"]))
                out.extend(frames)
                counts["videos"] += 1
                counts["frames"] += len(frames)
            finally:
                # The frames are the input; the video itself must not be handed
                # to a provider expecting images.
                staged.unlink(missing_ok=True)
            continue

        ext = ".png" if (r["content_type"] or "").endswith("png") else ".jpg"
        p = dest / f"{r['id']}{ext}"
        p.write_bytes(data)
        out.append(p)
        counts["photos"] += 1

    logger.info(
        "Capture input for job %s: %d photo(s), %d video(s) -> %d frame(s), %d image(s) total",
        job.job_id, counts["photos"], counts["videos"], counts["frames"], len(out),
    )
    return out, counts


async def _convert_to_splat(src: Path, work_dir: Path, media_id: str) -> Path:
    """A provider may return .splat (use as-is) or .ply (convert via the MIT
    PlayCanvas splat-transform — NOT spatial_agent's non-standard converter)."""
    if src.suffix == ".splat":
        return src
    out = work_dir / f"{media_id}.splat"
    if shutil.which("splat-transform"):
        cmd = ["splat-transform", str(src), str(out)]
    elif shutil.which("npx"):
        cmd = ["npx", "-y", "@playcanvas/splat-transform", str(src), str(out)]
    else:
        raise ProviderError(
            "splat-transform not installed — cannot convert .ply "
            "(npm i -g @playcanvas/splat-transform)"
        )
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    o, _ = await asyncio.wait_for(proc.communicate(), timeout=600)
    if proc.returncode != 0 or not out.is_file():
        raise ProviderError(f"splat-transform failed: {(o or b'')[-400:].decode('utf-8', 'replace')}")
    return out


async def _store_splat(
    src_splat: Path, media_id: str, *, provider: str, address: str, tenant_id: str
) -> tuple[str, Optional[str]]:
    """Persist the .splat + its AI-provenance manifest. Returns (url, storage_key).

    One seam: object storage, whichever backend is configured (a mounted
    filesystem, Azure Blob, or S3 — see object_storage). The returned URL is the
    authenticated `/api/media/{id}` route, the same one every photo and video
    goes through, so reading a reconstruction requires a token that can read the
    property_media row behind it.

    This replaced two seams selected by ORACLE_SPLAT_STORAGE. The default one
    copied the splat into a directory served by an unauthenticated StaticFiles
    mount at `/public/splats/{id}.splat`, and the other returned a raw bucket or
    CDN URL. Both meant a finished reconstruction of somebody's home was
    fetchable by anyone who could guess or observe the filename.
    """
    manifest = {
        "mediaId": media_id,
        "address": address,
        "generated": True,
        "generator": provider,
        "disclosure": SPATIAL_AI_DISCLOSURE,
    }

    if not media_storage.storage_available():
        raise ProviderError(
            "Object storage is not configured, so a finished reconstruction has "
            "nowhere durable to live. Set ORACLE_STORAGE_BACKEND (and its "
            "backend's settings) before running captures."
        )

    import object_storage

    splat_key = f"splats/{tenant_id}/{media_id}.splat"
    manifest_key = f"splats/{tenant_id}/{media_id}.json"
    await asyncio.to_thread(
        object_storage.put_file, splat_key, src_splat, "application/octet-stream"
    )
    await asyncio.to_thread(
        object_storage.put_bytes,
        manifest_key,
        json.dumps(manifest, indent=2).encode("utf-8"),
        "application/json",
    )
    return f"/api/media/{media_id}", splat_key


async def _record_media(
    job: ReconstructionJob,
    media_id: str,
    url: str,
    s3_key: Optional[str] = None,
    *,
    provenance: str = "captured",
    generator: Optional[str] = None,
) -> None:
    """Insert the splat as a property_media row the resolver reads for tier 3.

    s3_key is the canonical object key when stored on S3 (None for the fs seam).

    `provenance` is what the row actually depicts, taken from the provider that
    produced it. It is the only thing standing between the stub provider's demo
    room and the tour telling a user they are walking through the actual home,
    so it is passed explicitly rather than defaulted at the call site.
    """
    async with tenant_tx(job.ctx) as conn:
        base = await conn.fetchval(
            """
            SELECT COALESCE(MAX(sort_order), -1)
              FROM property_media
             WHERE ($1::uuid IS NOT NULL AND lead_id = $1)
                OR ($2::uuid IS NOT NULL AND listing_id = $2)
            """,
            UUID(job.lead_id) if job.lead_id else None,
            UUID(job.listing_id) if job.listing_id else None,
        )
        await conn.execute(
            """
            INSERT INTO property_media (
                id, tenant_id, lead_id, listing_id, kind, url, s3_key, sort_order,
                provenance, generator
            )
            VALUES ($1, $2, $3, $4, 'splat', $5, $6, $7, $8, $9)
            """,
            UUID(media_id),
            job.ctx.tenant_id,
            UUID(job.lead_id) if job.lead_id else None,
            UUID(job.listing_id) if job.listing_id else None,
            url,
            s3_key,
            int(base) + 1,
            provenance,
            generator,
        )


async def _open_capture_session(job: ReconstructionJob) -> Optional[str]:
    """Record this attempt. Returns the session id, or None if it cannot be written.

    Best-effort on purpose: the session is provenance, not a precondition, and a
    reconstruction the agent is waiting on should not fail because a bookkeeping
    row could not be inserted.
    """
    try:
        async with tenant_tx(job.ctx) as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO capture_sessions
                    (tenant_id, lead_id, listing_id, reconstruction_job_id, status)
                VALUES ($1, $2, $3, $4::uuid, 'running')
                RETURNING id
                """,
                job.ctx.tenant_id,
                UUID(job.lead_id) if job.lead_id else None,
                UUID(job.listing_id) if job.listing_id else None,
                job.job_id,
            )
            return str(row["id"]) if row else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not open a capture session for job %s: %s", job.job_id, exc)
        return None


async def _close_capture_session(
    job: ReconstructionJob,
    session_id: Optional[str],
    *,
    status: str,
    counts: Optional[dict] = None,
    result_media_id: Optional[str] = None,
    failure_reason: Optional[str] = None,
) -> None:
    """Finish the session record, and retire any earlier successful attempt.

    Superseding matters: without it a re-capture leaves two splats on the
    property and the resolver picks by sort order, so a corrected capture can
    lose to the bad one it was meant to replace.
    """
    if session_id is None:
        return
    try:
        async with tenant_tx(job.ctx) as conn:
            await conn.execute(
                """
                UPDATE capture_sessions
                   SET status = $2,
                       photo_count = COALESCE($3, photo_count),
                       video_count = COALESCE($4, video_count),
                       frame_count = COALESCE($5, frame_count),
                       result_media_id = $6::uuid,
                       failure_reason = $7,
                       completed_at = now()
                 WHERE id = $1::uuid
                """,
                session_id, status,
                (counts or {}).get("photos"),
                (counts or {}).get("videos"),
                (counts or {}).get("frames"),
                result_media_id,
                failure_reason,
            )
            if status == "succeeded":
                await conn.execute(
                    """
                    UPDATE capture_sessions
                       SET status = 'superseded'
                     WHERE id <> $1::uuid
                       AND status = 'succeeded'
                       AND (($2::uuid IS NOT NULL AND lead_id = $2)
                         OR ($3::uuid IS NOT NULL AND listing_id = $3))
                    """,
                    session_id,
                    UUID(job.lead_id) if job.lead_id else None,
                    UUID(job.listing_id) if job.listing_id else None,
                )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not close capture session %s: %s", session_id, exc)


async def _process(job: ReconstructionJob) -> None:
    provider = get_provider()
    await _set_status(job.ctx, job.job_id, "running", provider=provider.name, progress=10)
    media_id = str(uuid4())
    session_id = await _open_capture_session(job)
    try:
        with tempfile.TemporaryDirectory(prefix="recon_") as tmp:
            work = Path(tmp)
            images, counts = await _gather_source_images(job, work / "images")
            raw = await provider.reconstruct(images, work)            # .ply or .splat
            splat = await _convert_to_splat(raw, work, media_id)      # standard .splat
            url, s3_key = await _store_splat(
                splat, media_id,
                provider=provider.name,
                address=job.listing_id or job.lead_id or "",
                tenant_id=str(job.ctx.tenant_id),
            )
            await _record_media(
                job, media_id, url, s3_key,
                provenance=getattr(provider, "produces", "captured"),
                generator=provider.name,
            )
    except Exception as exc:
        await _close_capture_session(
            job, session_id, status="failed", failure_reason=str(exc)[:2000]
        )
        raise
    await _close_capture_session(
        job, session_id, status="succeeded", counts=counts, result_media_id=media_id
    )
    await _set_status(job.ctx, job.job_id, "succeeded", media_id=UUID(media_id), progress=100)
    try:
        await ws_hub.broadcast(job.ctx.tenant_id, {
            "type": "SPLAT_READY",
            "splatUrl": url,
            "leadId": job.lead_id,
            "listingId": job.listing_id,
            "jobId": job.job_id,
            "disclosure": SPATIAL_AI_DISCLOSURE,
        })
    except Exception:  # noqa: BLE001 — broadcast is best-effort; the row is the source of truth
        logger.debug("SPLAT_READY broadcast failed for job %s", job.job_id)
    logger.info("Reconstruction succeeded: job=%s media=%s provider=%s", job.job_id, media_id, provider.name)


async def _worker_loop(worker_id: int) -> None:
    logger.info("Reconstruction worker %d online.", worker_id)
    while True:
        job = await _queue.get()
        try:
            await _process(job)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — one bad job must not kill the worker
            logger.exception("Reconstruction job failed: job=%s tenant=%s", job.job_id, job.ctx.tenant_id)
            try:
                await _set_status(job.ctx, job.job_id, "failed", error=str(e)[:500])
            except Exception:  # noqa: BLE001
                logger.debug("could not mark job %s failed", job.job_id)
        finally:
            _queue.task_done()


async def start_reconstruction_workers() -> None:
    """Called from server lifespan startup."""
    for i in range(WORKER_COUNT):
        _workers.append(asyncio.create_task(_worker_loop(i)))


async def stop_reconstruction_workers() -> None:
    """Called from server lifespan shutdown — cancel and drain."""
    for task in _workers:
        task.cancel()
    await asyncio.gather(*_workers, return_exceptions=True)
    _workers.clear()
