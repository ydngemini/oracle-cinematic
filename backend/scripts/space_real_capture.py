#!/usr/bin/env python3
"""Neoh Space real-capture run (§59/§60) — the production worker path, no database.

    python backend/scripts/space_real_capture.py --images DIR --env PATH/.env \
        --out OUTDIR [--check-only] [--max-images N]

Runs `reconstruction_worker._process` — the exact code a queued build runs —
against REAL photographs and the REAL configured GPU provider, with two
substitutions so nothing touches a live database or object store:

* `tenant_tx` is a recorder: every statement the worker would write (stage
  transitions, provider job id, diagnostics, the atomic publish) is captured
  and printed, nothing is executed;
* object storage writes to OUTDIR/objects instead of the configured backend.

Everything else is production: the capture quality gate and frame selection,
the provider (RunPod pod: create → SSH → COLMAP + training → .sog → terminate),
delivery conversion, scene.json v2 (orientation, tilt, scale, entry view),
floor-plan derivation (which refuses honestly without a scale anchor), cost
recording. Afterwards the RunPod API is asked directly whether the pod still
exists, so "terminated" is verified rather than assumed.

Only RUNPOD_*/RECON_*/RECONSTRUCTION_PROVIDER are read from --env, and no
value is ever printed. One run spends real money (bounded by
RECON_POD_MAX_COST_USD); --check-only spends nothing.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
os.environ.setdefault("ORACLE_SKIP_DOTENV", "1")

_ALLOWED_PREFIXES = ("RUNPOD_", "RECON_", "RECONSTRUCTION_PROVIDER")


def load_env(path: Path) -> list[str]:
    names = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key.startswith(_ALLOWED_PREFIXES):
            continue
        value = value.strip().strip('"').strip("'")
        os.environ[key] = value
        names.append(key)
    return names


class _Recorder:
    """A tenant_tx stand-in that records SQL and answers like an empty DB."""

    def __init__(self):
        self.statements: list[tuple[str, tuple]] = []

    def __call__(self, ctx):
        rec = self

        class _Conn:
            async def execute(self, sql, *args):
                rec.statements.append((" ".join(sql.split()), args))
                return "UPDATE 1"

            async def fetchrow(self, sql, *args):
                rec.statements.append((" ".join(sql.split()), args))
                if "INSERT INTO capture_sessions" in sql:
                    return {"id": "00000000-0000-0000-0000-00000000c0de"}
                return None

            async def fetchval(self, sql, *args):
                rec.statements.append((" ".join(sql.split()), args))
                return 0 if "MAX(sort_order)" in sql else None

            async def fetch(self, sql, *args):
                rec.statements.append((" ".join(sql.split()), args))
                return []

        class _Tx:
            async def __aenter__(self):
                return _Conn()

            async def __aexit__(self, *exc):
                return False

        return _Tx()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--images", required=True, type=Path)
    ap.add_argument("--env", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--check-only", action="store_true")
    ap.add_argument("--max-images", type=int, default=300)
    args = ap.parse_args()

    names = load_env(args.env)
    print("env loaded (names only):", sorted(names))

    import capture_quality
    import reconstruction_worker as worker
    from reconstruction_providers import get_provider
    from tenancy import Role, TenantContext

    images = sorted(p for p in args.images.iterdir()
                    if p.suffix.lower() in (".jpg", ".jpeg", ".png"))[: args.max_images]
    upload_bytes = sum(p.stat().st_size for p in images)
    print(f"capture: {len(images)} images, {upload_bytes / 1e6:.1f} MB")

    t0 = time.monotonic()
    verdict = capture_quality.assess(images)
    print("quality gate:", json.dumps(verdict.as_diagnostics()),
          f"({time.monotonic() - t0:.1f}s)")
    print("guidance:", verdict.guidance)

    provider = get_provider()
    ok, why = provider.available()
    print(f"provider: {provider.name} available={ok}" + ("" if ok else f" reason={why}"))
    if args.check_only or not ok or verdict.refused:
        return 0 if ok else 2

    args.out.mkdir(parents=True, exist_ok=True)
    objects = args.out / "objects"
    objects.mkdir(exist_ok=True)

    import media_storage
    import object_storage

    def put_file(key, path, content_type):
        dest = objects / key
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(Path(path).read_bytes())
        return key

    def put_bytes(key, data, content_type="application/octet-stream"):
        dest = objects / key
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(bytes(data))
        return key

    def get_bytes(key):
        return (objects / key).read_bytes()

    media_storage.storage_available = lambda: True
    object_storage.put_file = put_file
    object_storage.put_bytes = put_bytes
    object_storage.get_bytes = get_bytes
    recorder = _Recorder()
    worker.tenant_tx = recorder
    worker.get_provider = lambda: provider

    async def _gather(job, dest):
        return list(images), {"photos": len(images), "videos": 0, "frames": 0}

    worker._gather_source_images = _gather

    async def _no_broadcast(*a, **k):
        return None

    worker.ws_hub.broadcast = _no_broadcast

    job = worker.ReconstructionJob(
        ctx=TenantContext(agent_id="space-real-capture@neoh.test",
                          tenant_id="00000000-0000-0000-0000-000000000000",
                          role=Role.PLATFORM_ADMIN),
        job_id="5ace0000-0000-4000-8000-000000000059",
        lead_id="5ace0000-0000-4000-8000-0000000000aa", listing_id=None,
    )
    started = time.monotonic()
    error = None
    try:
        asyncio.run(worker._process(job))
    except Exception as exc:  # noqa: BLE001 — report, the pod is already released
        error = exc
    wall = time.monotonic() - started

    pods = [args for sql, args in recorder.statements if "SET provider_job_id" in sql]
    pod_id = pods[0][1] if pods else (getattr(provider, "last_metrics", None) or {}).get("provider_job_id")
    stages = [a[2] for s, a in recorder.statements
              if s.startswith("UPDATE reconstruction_jobs SET status = $2") and "stage = $3" in s]
    diag = {}
    for sql, a in recorder.statements:
        if "jsonb_build_object($2::text, $3::jsonb)" in sql:
            diag[a[1]] = json.loads(a[2])
    published = any("stage = 'ready'" in s for s, _ in recorder.statements)
    final = [a for s, a in recorder.statements if s.startswith("UPDATE reconstruction_jobs SET status = $2")]

    report = {
        "wall_seconds": round(wall, 1),
        "error": None if error is None else f"{type(error).__name__}: {str(error)[:400]}",
        "stages": stages,
        "final_status": final[-1][1] if final else ("succeeded" if published else None),
        "published_atomically": published,
        "provider_job_id": pod_id,
        "cost": (getattr(provider, "last_metrics", None) or {}).get("cost"),
        "phases": (getattr(provider, "last_metrics", None) or {}).get("phases"),
        "diagnostics": diag,
        "upload_bytes": upload_bytes,
    }
    if pod_id:
        report["pod_after_run"] = _pod_state(pod_id)
    sog = sorted(objects.rglob("*.sog"))
    if sog:
        report["sog"] = str(sog[0])
        report["sog_bytes"] = sog[0].stat().st_size
        scene = Path(str(sog[0]) + ".scene.json")
        if scene.exists():
            report["scene"] = json.loads(scene.read_text())
    (args.out / "report.json").write_text(json.dumps(report, indent=2, default=str))
    print(json.dumps({k: v for k, v in report.items() if k != "scene"}, indent=2, default=str))
    return 0 if error is None else 1


def _pod_state(pod_id: str) -> dict:
    """Ask RunPod directly whether the pod still exists. 404 = terminated."""
    import requests

    key = os.environ.get("RUNPOD_API_KEY", "")
    try:
        r = requests.get(f"https://rest.runpod.io/v1/pods/{pod_id}",
                         headers={"Authorization": f"Bearer {key}"}, timeout=30)
        listing = requests.get("https://rest.runpod.io/v1/pods",
                               headers={"Authorization": f"Bearer {key}"}, timeout=30)
        pods = listing.json() if listing.ok else []
        if isinstance(pods, dict):
            pods = pods.get("data") or []
        ours = [p.get("id") for p in pods if str(p.get("name", "")).startswith("neoh-recon-")]
        body = r.json() if r.ok and r.content else None
        return {
            "get_status_code": r.status_code,
            "desiredStatus": (body or {}).get("desiredStatus") if isinstance(body, dict) else None,
            "neoh_pods_still_listed": ours,
        }
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


if __name__ == "__main__":
    sys.exit(main())
