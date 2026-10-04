#!/usr/bin/env python3
"""Neoh Space browser checks: load, entry view, fallback, error, mobile sizing,
frame rate, memory across open/close cycles (docs/neoh-space.md §Testing).

    # 1. serve the worktree frontend (dev server serves the harness page)
    cd oracle-app && npx vite --port 5199 --strictPort &
    # 2. run (fixture = generated demo room; --sog adds a real capture)
    python scripts/test-neoh-space-playwright.py --base-url http://127.0.0.1:5199 \
        --chrome /usr/bin/google-chrome [--sog path/model.sog --scene path/model.sog.scene.json]

Fixtures are controlled and legal: the default space is the stub provider's
generated demo room (written by backend code, no third-party content), as the
committed 29 KB `scripts/fixtures/neoh-space-demo-room.sog` (converted once
with splat-transform) and as legacy `.splat`. No GPU job runs in this test. A
real `.sog` is optional and only read locally.

Uses system Chrome through Playwright (no bundled browsers needed). Firefox and
WebKit need Playwright's patched builds; when they are not installed the
script says so instead of pretending they passed.
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "backend"))

VIEWPORTS = {
    "desktop": {"viewport": {"width": 1440, "height": 900}},
    # Mid-range phone profile: phone viewport + touch + 4x CPU slowdown.
    "mobile": {"viewport": {"width": 390, "height": 844}, "is_mobile": True,
               "has_touch": True, "device_scale_factor": 3},
}


FIXTURE_SOG = REPO / "scripts" / "fixtures" / "neoh-space-demo-room.sog"


def make_fixture(out: Path) -> tuple[Path, Path]:
    """The stub provider's demo room as .splat, plus a v2 scene.json for it."""
    import struct

    import numpy as np

    import scene_manifest
    from reconstruction_providers import write_demo_splat

    splat = write_demo_splat(out / "space.splat")
    raw = splat.read_bytes()
    n = len(raw) // 32
    xyz = np.array([struct.unpack_from("<3f", raw, i * 32) for i in range(n)])
    t = np.linspace(0, 2 * np.pi, 24, endpoint=False)
    cams = np.stack([1.2 * np.cos(t), np.full(24, 1.45), 1.2 * np.sin(t)], axis=1)
    manifest = scene_manifest.build(xyz, cams, asset_id="fixture", pipeline_version="fixture")
    scene = out / "scene.json"
    scene.write_text(json.dumps(manifest))
    return splat, scene


def nonblank(png: bytes) -> dict:
    from PIL import Image, ImageStat

    image = Image.open(io.BytesIO(png)).convert("RGB").resize((64, 64))
    stat = ImageStat.Stat(image)
    return {"stddev": round(max(stat.stddev), 2), "colors": len(set(image.tobytes()[i:i + 3] for i in range(0, 64 * 64 * 3, 3)))}


def overflow(page) -> int:
    return page.evaluate("() => Math.max(document.body.scrollWidth, document.documentElement.scrollWidth) - window.innerWidth")


def run(args) -> dict:
    from playwright.sync_api import sync_playwright

    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    splat, scene = make_fixture(out)
    fixtures = {
        "/space-fixture/space.splat": (splat.read_bytes(), "application/octet-stream"),
        "/space-fixture/space.sog": (FIXTURE_SOG.read_bytes(), "application/octet-stream"),
        "/space-fixture/scene.json": (scene.read_bytes(), "application/json"),
    }
    if args.sog:
        fixtures["/space-fixture/real.sog"] = (Path(args.sog).read_bytes(), "application/octet-stream")
        if args.scene:
            fixtures["/space-fixture/real.scene.json"] = (Path(args.scene).read_bytes(), "application/json")

    results: dict = {"browsers": {}, "fixture_gaussians": len(splat.read_bytes()) // 32}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            executable_path=args.chrome, headless=True,
            args=["--enable-webgl", "--ignore-gpu-blocklist", "--enable-gpu",
                  "--enable-unsafe-swiftshader", "--js-flags=--expose-gc",
                  "--enable-precise-memory-info"],
        )
        results["browsers"]["chrome"] = chrome = {"version": browser.version}
        for label, ctx_opts in VIEWPORTS.items():
            chrome[label] = check_viewport(browser, args.base_url, label, ctx_opts, fixtures, out, bool(args.sog))
        browser.close()
        for name in ("firefox", "webkit"):
            try:
                other = getattr(pw, name).launch(headless=True)
                other.close()
                results["browsers"][name] = {"status": "installed but not exercised by this run"}
            except Exception as exc:  # noqa: BLE001
                results["browsers"][name] = {"status": "unavailable", "reason": str(exc).splitlines()[0][:160]}
    return results


def check_viewport(browser, base_url, label, ctx_opts, fixtures, out: Path, has_real: bool) -> dict:
    context = browser.new_context(base_url=base_url, **ctx_opts)

    from urllib.parse import urlparse

    def handler(route):
        path = urlparse(route.request.url).path
        body, mime = fixtures.get(path, (None, None))
        if body is None:
            route.fulfill(status=404, body="not found")
        else:
            route.fulfill(status=200, body=body, headers={"content-type": mime, "accept-ranges": "bytes"})

    # A predicate on the PATH: a glob would also match the harness URL itself,
    # whose query string names the fixture.
    context.route(lambda url: urlparse(url).path.startswith("/space-fixture/"), handler)
    page = context.new_page()
    cdp = context.new_cdp_session(page)
    if label == "mobile":
        cdp.send("Emulation.setCPUThrottlingRate", {"rate": 4})
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text[:300]) if m.type == "error" else None)
    report: dict = {}

    # ── device probe as this browser reports it ─────────────────────────────
    page.goto("/neoh-space-harness.html?device=auto&asset=/space-fixture/space.splat&format=.splat",
              wait_until="domcontentloaded")
    page.wait_for_function("() => window.__neohSpace", timeout=90_000)
    report["probe"] = page.evaluate("() => window.__neohSpaceProbe || null")
    report["assessment"] = page.evaluate("() => window.__neohSpace.device")

    def load(asset, fmt, scene_path, tag):
        url = (f"/neoh-space-harness.html?device=force&asset={asset}&format={fmt}"
               + (f"&scene={scene_path}" if scene_path else ""))
        t0 = time.monotonic()
        page.goto(url, wait_until="domcontentloaded")
        try:
            page.wait_for_selector('[data-space-status="ready"]', timeout=120_000)
        except Exception:  # noqa: BLE001
            status = page.evaluate("() => document.querySelector('[data-space-status]')?.dataset.spaceStatus || null")
            return {"ready": False, "status": status, "errors": errors[-3:]}
        ready_s = time.monotonic() - t0
        page.wait_for_timeout(1500)
        canvas = page.locator("canvas")
        # Composited pixels, not toDataURL: the WebGL drawing buffer is not
        # preserved, so reading it back after a frame returns a cleared image.
        png = canvas.screenshot()
        (out / f"{label}-{tag}.png").write_bytes(png)
        page.screenshot(path=str(out / f"{label}-{tag}-page.png"))
        fps = page.evaluate("""() => new Promise((resolve) => {
            let frames = 0; const start = performance.now();
            const tick = () => { frames += 1;
              if (performance.now() - start < 3000) requestAnimationFrame(tick);
              else resolve(frames / ((performance.now() - start) / 1000)); };
            requestAnimationFrame(tick); })""")
        quality = page.evaluate("() => document.querySelector('[data-space-quality]')?.dataset.spaceQuality")
        return {"ready": True, "ready_seconds": round(ready_s, 2), "canvas": nonblank(png),
                "fps": round(fps, 1), "quality_after_3s": quality,
                "canvas_label": canvas.get_attribute("aria-label"),
                "overflow_px": overflow(page)}

    report["fixture"] = load("/space-fixture/space.sog", ".sog", "/space-fixture/scene.json", "fixture")
    # Legacy .splat goes to the gsplat engine (PlayCanvas has no parser for it).
    page.goto("/neoh-space-harness.html?device=force&asset=/space-fixture/space.splat&format=.splat",
              wait_until="domcontentloaded")
    page.wait_for_timeout(4000)
    report["legacy_splat"] = {
        "canvases": page.locator("canvas").count(),
        "no_parser_error": not any("No parser found" in e for e in errors),
    }
    if has_real:
        report["real_capture"] = load("/space-fixture/real.sog", ".sog",
                                      "/space-fixture/real.scene.json" if "/space-fixture/real.scene.json" in fixtures else None,
                                      "real")

    # ── memory across open/close cycles ────────────────────────────────────
    asset = "/space-fixture/real.sog" if has_real else "/space-fixture/space.sog"
    fmt = ".sog"
    page.goto(f"/neoh-space-harness.html?device=force&asset={asset}&format={fmt}", wait_until="domcontentloaded")
    page.wait_for_selector('[data-space-status="ready"]', timeout=120_000)
    heap = []
    for _ in range(5):
        page.evaluate("() => window.__neohSpace.close()")
        page.wait_for_timeout(600)
        page.evaluate("() => window.gc && window.gc()")
        heap.append(page.evaluate("() => performance.memory ? performance.memory.usedJSHeapSize : null"))
        canvases_closed = page.evaluate("() => window.__neohSpace.liveCanvases()")
        page.evaluate("() => window.__neohSpace.open()")
        page.wait_for_selector('[data-space-status="ready"]', timeout=120_000)
    report["memory"] = {
        "heap_after_each_close_mb": [round(h / 1e6, 1) for h in heap if h],
        "canvases_after_close": canvases_closed,
        "growth_first_to_last_mb": round((heap[-1] - heap[0]) / 1e6, 1) if all(heap) else None,
    }

    # ── fallback: incapable device keeps the property page ─────────────────
    page.goto("/neoh-space-harness.html?device=none&asset=/space-fixture/space.splat&format=.splat",
              wait_until="domcontentloaded")
    page.wait_for_selector('[data-testid="space-fallback"]', timeout=30_000)
    report["fallback"] = {
        "card": page.locator('[data-testid="space-fallback"]').inner_text()[:200],
        "property_page_visible": page.locator('[data-testid="property-photos"]').count() == 1,
        "canvases": page.locator("canvas").count(),
        "overflow_px": overflow(page),
    }
    page.screenshot(path=str(out / f"{label}-fallback.png"))

    # ── error: the asset cannot be fetched ─────────────────────────────────
    page.goto("/neoh-space-harness.html?device=force&asset=/space-fixture/missing.sog&format=.sog",
              wait_until="domcontentloaded")
    try:
        page.wait_for_selector('[data-space-status="error"]', timeout=60_000)
        alert = page.locator('[role="alert"]').first.inner_text()[:200]
        report["error_state"] = {"shown": True, "text": alert,
                                 "property_page_visible": page.locator('[data-testid="property-photos"]').count() == 1}
    except Exception:  # noqa: BLE001
        report["error_state"] = {"shown": False}
    page.screenshot(path=str(out / f"{label}-error.png"))
    report["page_errors"] = errors[:5]
    context.close()
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:5199")
    ap.add_argument("--chrome", default="/usr/bin/google-chrome")
    ap.add_argument("--output-dir", default="/tmp/neoh-space-playwright")
    ap.add_argument("--sog")
    ap.add_argument("--scene")
    args = ap.parse_args()
    results = run(args)
    print(json.dumps(results, indent=2, default=str))
    (Path(args.output_dir) / "results.json").write_text(json.dumps(results, indent=2, default=str))
    ok = all(results["browsers"]["chrome"][v]["fixture"].get("ready") for v in VIEWPORTS)
    ok = ok and all(results["browsers"]["chrome"][v]["fallback"]["property_page_visible"] for v in VIEWPORTS)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
