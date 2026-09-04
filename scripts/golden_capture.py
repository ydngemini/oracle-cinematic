#!/usr/bin/env python3
"""Golden Capture — push one real capture through the whole pipeline, loudly.

    python3 scripts/golden_capture.py --photos ~/capture --lead <uuid>

Uploads the photos, starts a reconstruction, then polls and prints what each
stage measured. The point is that the first real run says exactly where it
succeeded or failed rather than "status: failed".

Nothing here is a substitute for the product path — it drives the SAME
endpoints the UI does (POST /crm/leads/{id}/media, POST
/crm/reconstruction-jobs), so a success here is a success for the product.

Media never leaves your machine except to the Neoh API. Photos are not copied
into the repository, and no token is printed.
"""
from __future__ import annotations

import argparse
import getpass
import json
import mimetypes
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

BATCH = 30                       # the API's per-request file cap
MAX_IMAGES = 300                 # MAX_CAPTURE_IMAGES in the provider
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".HEIC"}


def _request(url: str, *, token: str = "", data: bytes = b"", method: str = "GET",
             content_type: str = "", timeout: int = 300) -> dict:
    req = urllib.request.Request(url, data=data or None, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if content_type:
        req.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode() or "{}"
            return json.loads(body) if body.strip().startswith(("{", "[")) else {"raw": body}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()[:600]
        raise SystemExit(f"\n  HTTP {exc.code} on {method} {url}\n  {detail}\n") from exc


def login(base: str, email: str, password: str) -> str:
    out = _request(
        f"{base}/auth/login", method="POST",
        data=json.dumps({"email": email, "password": password}).encode(),
        content_type="application/json",
    )
    token = out.get("token")
    if not token:
        raise SystemExit("login returned no token")
    print(f"  signed in as {out.get('agent_id')}")
    return token


def _multipart(files: list[Path]) -> tuple[bytes, str]:
    """Build a multipart body by hand — no third-party dependency for one form."""
    boundary = f"----neoh{uuid.uuid4().hex}"
    parts: list[bytes] = []
    for path in files:
        ctype = mimetypes.guess_type(path.name)[0] or "image/jpeg"
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="files"; filename="{path.name}"\r\n'
            f"Content-Type: {ctype}\r\n\r\n".encode()
            + path.read_bytes() + b"\r\n"
        )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def upload(base: str, token: str, lead_id: str, photos: list[Path]) -> int:
    uploaded = 0
    for i in range(0, len(photos), BATCH):
        chunk = photos[i:i + BATCH]
        body, ctype = _multipart(chunk)
        mb = len(body) / (1024 * 1024)
        out = _request(
            f"{base}/api/crm/leads/{lead_id}/media", token=token, method="POST",
            data=body, content_type=ctype, timeout=600,
        )
        n = len(out.get("media", out) if isinstance(out, dict) else out)
        uploaded += len(chunk)
        print(f"  batch {i // BATCH + 1}: {len(chunk)} photos, {mb:.1f} MB  → {n} rows")
    return uploaded


STAGE_ORDER = ["capture", "reconstruction", "delivery", "storage", "quality_gate", "failure"]


def poll(base: str, token: str, job_id: str, every: int = 20) -> dict:
    seen: set[str] = set()
    last_status = ""
    started = time.time()
    while True:
        job = _request(f"{base}/api/crm/reconstruction-jobs/{job_id}", token=token)
        status = job.get("status", "?")
        if status != last_status:
            print(f"\n  [{time.time() - started:6.0f}s] status: {status}"
                  f"  progress: {job.get('progress', 0)}%")
            last_status = status
        diagnostics = job.get("diagnostics") or {}
        if isinstance(diagnostics, str):
            diagnostics = json.loads(diagnostics)
        for stage in STAGE_ORDER:
            if stage in diagnostics and stage not in seen:
                seen.add(stage)
                print(f"    ── {stage} ──")
                for k, v in sorted(diagnostics[stage].items()):
                    if k != "at":
                        print(f"       {k:22} {v}")
        if status in ("succeeded", "failed", "failed_quality_gate"):
            if job.get("error"):
                print(f"\n  error: {job['error'][:800]}")
            return job
        time.sleep(every)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--photos", required=True, help="directory of capture photos")
    ap.add_argument("--lead", required=True, help="lead uuid to attach the capture to")
    ap.add_argument("--base", default="http://localhost:8000")
    ap.add_argument("--email", default="")
    ap.add_argument("--skip-upload", action="store_true",
                    help="photos are already attached; just start the job")
    args = ap.parse_args()

    photos = sorted(
        p for p in Path(args.photos).expanduser().iterdir()
        if p.suffix.lower() in {s.lower() for s in IMAGE_SUFFIXES}
    )
    if not photos and not args.skip_upload:
        raise SystemExit(f"no images found in {args.photos}")
    if len(photos) > MAX_IMAGES:
        raise SystemExit(
            f"{len(photos)} photos exceeds the provider's {MAX_IMAGES} cap — "
            f"trim the capture rather than letting the job fail after upload"
        )
    total_mb = sum(p.stat().st_size for p in photos) / (1024 * 1024)
    print(f"\ncapture: {len(photos)} photos, {total_mb:.0f} MB total")
    oversize = [p.name for p in photos if p.stat().st_size > 12 * 1024 * 1024]
    if oversize:
        raise SystemExit(f"over the 12 MB per-file limit: {', '.join(oversize[:5])}")

    email = args.email or input("Neoh email: ").strip()
    token = login(args.base, email, getpass.getpass("Password: "))

    if not args.skip_upload:
        print(f"\nuploading to lead {args.lead}")
        upload(args.base, token, args.lead, photos)

    print("\nstarting reconstruction (this rents a GPU)")
    job = _request(
        f"{args.base}/api/crm/reconstruction-jobs?lead_id={args.lead}",
        token=token, method="POST", data=b"{}", content_type="application/json",
    )
    job_id = job["job_id"]
    print(f"  job {job_id}")

    final = poll(args.base, token, job_id)
    print(f"\n{'=' * 62}")
    print(f"FINAL: {final.get('status')}   media_id: {final.get('media_id') or '(none)'}")
    if final.get("status") == "succeeded":
        print("\nOpen the property in Neoh — the tour should offer 'Full 3D'.")
    print(f"{'=' * 62}\n")


if __name__ == "__main__":
    main()
