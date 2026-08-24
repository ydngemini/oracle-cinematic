"""reconstruction_worker.py — async worker pool that turns a capture into a
walkable Gaussian splat, then records it so the tour resolver flips the property
to tier 3.

Long jobs (20-60 min) must NOT live on a request/websocket (those get cancelled
on disconnect + hit the 300s idle watchdog). So this mirrors voice_intel.py:
an in-process asyncio.Queue + fixed worker pool started/stopped in the server
lifespan; callers POST → 202 → poll a reconstruction_jobs row.

Pipeline (provider-agnostic; see reconstruction_providers.py):
  gather source photos → provider.reconstruct() → .ply/.spz/.sog/.splat
  → convert to .sog (PlayCanvas splat-transform) → store
  → property_media row kind='splat' (+ AI-disclosure manifest) → broadcast SPLAT_READY

The delivery format is `.sog`, not `.splat`. See _convert_to_delivery: no
released splat-transform can write `.splat`, so the old target was unreachable
for every provider that emits PLY. The row's `kind` stays 'splat' — it names the
kind of media, not the container — and stored `.splat` assets still load.
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
    CONVERTIBLE_SUFFIXES,
    DELIVERY_SUFFIX,
    SPATIAL_AI_DISCLOSURE,
    SPLAT_OUTPUT_DIR,
    SPLAT_TRANSFORM_VERSION,
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


def _carry_camera_poses(src: Path, out: Path) -> None:
    """Keep a capture's camera poses attached to the file that gets delivered.

    The sidecar is written next to the provider's RAW output, and conversion
    produces a differently-named file. Without this the poses are left behind in
    a temp directory that is deleted at the end of the job — which is the same
    way COLMAP's poses were lost before, one step further along.
    """
    import capture_poses

    source = capture_poses.sidecar_for(src)
    if src == out or not source.is_file():
        return
    try:
        shutil.copyfile(source, capture_poses.sidecar_for(out))
    except OSError as exc:
        logger.info("Could not carry camera poses to %s (%s)", out.name, exc)


async def _convert_to_delivery(src: Path, work_dir: Path, media_id: str) -> Path:
    """Convert a provider's raw output into the format the viewer is served.

    **Delivery is `.sog`, and that is forced on us rather than preferred.**
    This function used to ask splat-transform to write `.splat`, which it has
    never been able to do: `.splat` (antimatter15) is an *input* format in every
    published version — v2.7.1, v3.0.0 and 3.3.0 all list it `input ✅ /
    output ❌`, and the pinned binary's own `--help` agrees:

        SUPPORTED OUTPUTS
          .ply .compressed.ply .sog .spz meta.json lod-meta.json .glb
          .csv .html .voxel.json .webp null

    So every provider that emits PLY from splatfacto — `local`, `cloud`,
    `aws_batch`, `runpod`, `oncompute`, i.e. all the real ones — failed here,
    and only StubProvider ever got through, because `write_demo_splat` writes
    `.splat` bytes itself and never invokes the converter. That is the whole
    reason the only splat ever to reach the database is the synthetic one.

    `.sog` is the right target rather than a workaround: splat-transform writes
    it and calls it recommended, the PlayCanvas engine already in `package.json`
    renders it (`GSplatSogData` / `GSplatSogResource` ship in 2.21.3), it is the
    format SuperSplat's own viewer loads, and it is roughly an order of
    magnitude smaller than PLY — which is what makes phone-scan upload and
    service-worker prefetch tractable at all.

    Anything already deliverable passes through untouched. That keeps `.splat`
    working: assets recorded before this change still load, and the stub path
    still runs on hosts with no Node installed.
    """
    name = src.name.lower()
    if name.endswith(DELIVERY_SUFFIX) or name.endswith(".splat"):
        # .sog is current; .splat is legacy-but-renderable, so neither needs a
        # conversion pass. Converting .splat here would make the stub path
        # depend on Node for no gain.
        return src

    if not any(name.endswith(ext) for ext in CONVERTIBLE_SUFFIXES):
        raise ProviderError(
            f"{src.name!r} is not a splat format this pipeline can deliver "
            f"(expected one of {', '.join(CONVERTIBLE_SUFFIXES)}, .sog or .splat)"
        )

    out = work_dir / f"{media_id}{DELIVERY_SUFFIX}"
    if shutil.which("splat-transform"):
        cmd = ["splat-transform", str(src), str(out)]
    elif shutil.which("npx"):
        # Pinned deliberately. An unpinned `npx -y` resolves to whatever is
        # latest at run time, which is exactly how the format support this
        # pipeline depended on changed underneath it without a code change.
        cmd = ["npx", "-y", f"@playcanvas/splat-transform@{SPLAT_TRANSFORM_VERSION}",
               str(src), str(out)]
    else:
        raise ProviderError(
            f"splat-transform not installed — cannot convert {src.suffix} to "
            f"{DELIVERY_SUFFIX} (npm i -g "
            f"@playcanvas/splat-transform@{SPLAT_TRANSFORM_VERSION}; needs Node >= 22)"
        )
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    o, _ = await asyncio.wait_for(proc.communicate(), timeout=600)
    if proc.returncode != 0 or not out.is_file():
        tail = (o or b"")[-400:].decode("utf-8", "replace")
        # Name the input format. An SPZ v4 file hitting a splat-transform too
        # old to read it fails right here, and "conversion failed" alone sends
        # the reader looking at the GPU job instead of the pin.
        raise ProviderError(
            f"splat-transform could not convert {src.name} to {DELIVERY_SUFFIX}: {tail}"
        )
    _carry_camera_poses(src, out)
    return out


async def _store_splat(
    src_splat: Path, media_id: str, *, provider: str, address: str, tenant_id: str,
    generated: bool = True, extra_manifest: Optional[dict] = None,
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
    # `generated` is False for a scan someone captured with a phone: those bytes
    # are a photographic record of a real room, not a reconstruction inferred
    # from photos, and the AI disclosure would be a false statement about how
    # they were made. `extra_manifest` is where the uploader's attestation is
    # recorded, so the claim keeps an author.
    manifest = {
        "mediaId": media_id,
        "address": address,
        "generated": generated,
        "generator": provider,
        **({"disclosure": SPATIAL_AI_DISCLOSURE} if generated else {}),
        **(extra_manifest or {}),
    }

    if not media_storage.storage_available():
        raise ProviderError(
            "Object storage is not configured, so a finished reconstruction has "
            "nowhere durable to live. Set ORACLE_STORAGE_BACKEND (and its "
            "backend's settings) before running captures."
        )

    import object_storage

    # Follow the artifact's real suffix rather than hardcoding one. The
    # converter passes a legacy .splat through untouched, so a fixed .sog key
    # here would label those bytes as a format they are not — and the extension
    # is what the frontend's assetLoader dispatches on.
    suffix = DELIVERY_SUFFIX if src_splat.name.lower().endswith(DELIVERY_SUFFIX) else ".splat"
    splat_key = f"splats/{tenant_id}/{media_id}{suffix}"
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

    # Camera poses ride along under the artifact's own key, so a later floor
    # plan pass can find them the same way it would on disk. Best-effort: the
    # splat is the deliverable and a missing sidecar only means the plan falls
    # back to inferring up from geometry, which is what it did before.
    import capture_poses

    poses = capture_poses.sidecar_for(src_splat)
    if poses.is_file():
        try:
            await asyncio.to_thread(
                object_storage.put_file,
                splat_key + capture_poses.CAMERA_SIDECAR_SUFFIX,
                poses, "application/json",
            )
        except Exception:  # noqa: BLE001
            logger.info("Could not store camera poses for media %s", media_id)

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
            raw = await provider.reconstruct(images, work)            # .ply/.spz/.sog/.splat
            splat = await _convert_to_delivery(raw, work, media_id)   # .sog (or legacy .splat)
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


#: How often to sweep for leaked GPU pods.
REAP_INTERVAL_SECONDS = int(os.environ.get("RECON_REAP_INTERVAL", "1800") or 1800)


def _reap_max_age_seconds() -> int:
    """How old one of our pods must be before a sweep will terminate it.

    **This must exceed the job timeout, and by a real margin.** The reaper
    cannot tell a leaked pod from one a *different backend replica* is actively
    using — it sees only a name and an age — so a max age below the job ceiling
    would have it terminating live reconstructions mid-training, which reads as
    a mysterious GPU failure rather than a configuration mistake.

    Derived from the pod timeout rather than set independently, so raising the
    one moves the other and the relationship cannot silently invert.
    """
    try:
        job_ceiling = int(os.environ.get("RECON_POD_TIMEOUT", "5400") or 5400)
    except ValueError:
        job_ceiling = 5400
    floor = job_ceiling * 2 + 1800
    try:
        configured = int(os.environ.get("RECON_REAP_MAX_AGE", "0") or 0)
    except ValueError:
        configured = 0
    if configured and configured < floor:
        logger.warning(
            "RECON_REAP_MAX_AGE=%ds is below the safe floor of %ds for a "
            "%ds job ceiling; using the floor so the sweep cannot terminate a "
            "reconstruction that is still running.",
            configured, floor, job_ceiling,
        )
    return max(configured, floor)


async def _reaper_loop() -> None:
    """Terminate our own GPU pods that outlived any plausible job.

    A pod bills by the hour whether or not it is computing, and a leak is
    completely silent — nothing in the product surfaces one. `reconstruct` has a
    `finally` that covers a failing job, but it cannot cover this process being
    killed between creating a pod and reaching that block. That window is
    exactly what this closes, which is why the **first sweep runs immediately at
    startup**: the most likely leak is one left by the crash that caused this
    restart.

    A no-op for every provider without a reaper (stub, local, the S3-staged
    ones), so it costs nothing on a deployment that never rents anything.
    """
    while True:
        try:
            reap = getattr(get_provider(), "reap_stale_pods", None)
            if reap is not None:
                reaped = await asyncio.to_thread(reap, _reap_max_age_seconds())
                if reaped:
                    logger.warning(
                        "Reaped %d leaked GPU pod(s): %s", len(reaped), ", ".join(reaped)
                    )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            # Never let a sweep failure take down the worker pool or the server.
            # An unfunded or misconfigured account raises here every cycle, and
            # that is a reason to log, not to stop accepting captures.
            logger.exception("Leaked-pod sweep failed; will retry")
        await asyncio.sleep(REAP_INTERVAL_SECONDS)


async def start_reconstruction_workers() -> None:
    """Called from server lifespan startup."""
    for i in range(WORKER_COUNT):
        _workers.append(asyncio.create_task(_worker_loop(i)))
    # Tracked in the same list so shutdown cancels it with everything else —
    # a reaper outliving the pool would keep a dead event loop alive.
    _workers.append(asyncio.create_task(_reaper_loop()))


async def stop_reconstruction_workers() -> None:
    """Called from server lifespan shutdown — cancel and drain."""
    for task in _workers:
        task.cancel()
    await asyncio.gather(*_workers, return_exceptions=True)
    _workers.clear()
