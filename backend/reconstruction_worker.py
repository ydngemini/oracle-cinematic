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


async def _gather_source_images(job: ReconstructionJob, dest: Path) -> list[Path]:
    """Pull the property's uploaded photos (RLS-scoped) into a work dir as the
    reconstruction input. Empty is fine for the stub provider."""
    dest.mkdir(parents=True, exist_ok=True)
    out: list[Path] = []
    async with tenant_tx(job.ctx) as conn:
        rows = await conn.fetch(
            """
            SELECT pm.id, mb.bytes, mb.content_type
              FROM property_media pm
              JOIN media_blobs mb ON mb.media_id = pm.id
             WHERE pm.kind = 'photo'
               AND (($1::uuid IS NOT NULL AND pm.lead_id = $1)
                 OR ($2::uuid IS NOT NULL AND pm.listing_id = $2))
             ORDER BY pm.sort_order ASC
            """,
            UUID(job.lead_id) if job.lead_id else None,
            UUID(job.listing_id) if job.listing_id else None,
        )
    for r in rows:
        ext = ".png" if (r["content_type"] or "").endswith("png") else ".jpg"
        p = dest / f"{r['id']}{ext}"
        p.write_bytes(bytes(r["bytes"]))
        out.append(p)
    return out


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


def _store_splat(src_splat: Path, media_id: str, *, provider: str, address: str) -> tuple[str, Optional[str]]:
    """Persist the .splat + an AI-provenance manifest. Returns (public_url, s3_key)
    where s3_key is None for the filesystem seam (we don't own a remote object).

    Two seams, selected by ORACLE_SPLAT_STORAGE:
      • fs  (default) — copy into SPLAT_OUTPUT_DIR, served by server.py at
        /public/splats/{id}.splat. In the dev stack ORACLE_SPLAT_DIR points at the
        bind-mounted /app/.splatdata so the file survives `--force-recreate`.
      • s3  — Fargate prod has no persistent disk, so push the splat + manifest to
        S3 and serve them via the bucket (or a CDN in front of it). The object key
        is recorded on the property_media row so we own the canonical artifact.
    """
    manifest = {
        "mediaId": media_id,
        "address": address,
        "generated": True,
        "generator": provider,
        "disclosure": SPATIAL_AI_DISCLOSURE,
    }
    storage = os.environ.get("ORACLE_SPLAT_STORAGE", "fs").lower()

    if storage == "s3":  # pragma: no cover — prod path (needs bucket + boto3 + creds)
        bucket = os.environ.get("ORACLE_SPLAT_S3_BUCKET")
        if not bucket:
            raise ProviderError(
                "ORACLE_SPLAT_STORAGE=s3 but ORACLE_SPLAT_S3_BUCKET is unset — "
                "set the bucket name (and optionally ORACLE_SPLAT_CDN_BASE)."
            )
        import boto3  # lazy import — only the prod S3 path needs the AWS SDK
        s3 = boto3.client("s3")
        splat_key = f"splats/{media_id}.splat"
        manifest_key = f"splats/{media_id}.json"
        s3.put_object(
            Bucket=bucket, Key=splat_key,
            Body=src_splat.read_bytes(), ContentType="application/octet-stream",
        )
        s3.put_object(
            Bucket=bucket, Key=manifest_key,
            Body=json.dumps(manifest, indent=2).encode("utf-8"),
            ContentType="application/json",
        )
        # Public URL: CDN base if fronted by CloudFront, else the raw S3 endpoint.
        cdn_base = os.environ.get("ORACLE_SPLAT_CDN_BASE", f"https://{bucket}.s3.amazonaws.com")
        return f"{cdn_base.rstrip('/')}/{splat_key}", splat_key

    # ── filesystem seam (default) ──────────────────────────────────────────────
    SPLAT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    dest = SPLAT_OUTPUT_DIR / f"{media_id}.splat"
    if src_splat.resolve() != dest.resolve():
        shutil.copyfile(src_splat, dest)
    (SPLAT_OUTPUT_DIR / f"{media_id}.json").write_text(json.dumps(manifest, indent=2))
    return f"/public/splats/{media_id}.splat", None


async def _record_media(
    job: ReconstructionJob, media_id: str, url: str, s3_key: Optional[str] = None
) -> None:
    """Insert the splat as a property_media row the resolver reads for tier 3.
    s3_key is the canonical object key when stored on S3 (None for the fs seam)."""
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
            INSERT INTO property_media (id, tenant_id, lead_id, listing_id, kind, url, s3_key, sort_order)
            VALUES ($1, $2, $3, $4, 'splat', $5, $6, $7)
            """,
            UUID(media_id),
            job.ctx.tenant_id,
            UUID(job.lead_id) if job.lead_id else None,
            UUID(job.listing_id) if job.listing_id else None,
            url,
            s3_key,
            int(base) + 1,
        )


async def _process(job: ReconstructionJob) -> None:
    provider = get_provider()
    await _set_status(job.ctx, job.job_id, "running", provider=provider.name, progress=10)
    media_id = str(uuid4())
    with tempfile.TemporaryDirectory(prefix="recon_") as tmp:
        work = Path(tmp)
        images = await _gather_source_images(job, work / "images")
        raw = await provider.reconstruct(images, work)            # .ply or .splat
        splat = await _convert_to_splat(raw, work, media_id)      # standard .splat
        url, s3_key = _store_splat(splat, media_id, provider=provider.name, address=job.listing_id or job.lead_id or "")
        await _record_media(job, media_id, url, s3_key)
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
