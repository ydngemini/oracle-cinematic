#!/usr/bin/env python3
"""Durable desktop/mobile WebGL smoke and pixel checks for Oracle spatial UI."""

from __future__ import annotations

import argparse
import base64
import io
import os
import sys
from pathlib import Path

from PIL import Image, ImageStat
from playwright.sync_api import Locator, Page, sync_playwright


VIEWPORTS = {
    "desktop": {"width": 1440, "height": 900},
    "mobile": {"width": 390, "height": 844},
}


def assert_no_horizontal_overflow(page: Page, label: str) -> None:
    dimensions = page.evaluate(
        """() => ({
          viewport: window.innerWidth,
          body: document.body.scrollWidth,
          document: document.documentElement.scrollWidth,
          offenders: [...document.querySelectorAll('*')]
            .map((element) => {
              const rect = element.getBoundingClientRect();
              return {
                tag: element.tagName.toLowerCase(),
                className: typeof element.className === 'string' ? element.className : '',
                left: Math.round(rect.left),
                right: Math.round(rect.right),
                width: Math.round(rect.width),
              };
            })
            .filter((item) => item.left < -2 || item.right > window.innerWidth + 2)
            .slice(0, 8),
        })"""
    )
    widest = max(dimensions["body"], dimensions["document"])
    assert widest <= dimensions["viewport"] + 2, f"{label}: horizontal overflow {dimensions}"


def assert_nonblank_canvas(canvas: Locator, output: Path, label: str) -> None:
    assert canvas.count() == 1, f"{label}: expected one canvas"
    box = canvas.bounding_box()
    assert box and box["width"] >= 100 and box["height"] >= 100, f"{label}: collapsed canvas"
    data_url = canvas.evaluate("canvas => canvas.toDataURL('image/png')")
    assert data_url.startswith("data:image/png;base64,"), f"{label}: canvas PNG unavailable"
    raw = base64.b64decode(data_url.split(",", 1)[1])
    (output / f"{label}-canvas.png").write_bytes(raw)
    image = Image.open(io.BytesIO(raw)).convert("RGB").resize((64, 64))
    statistics = ImageStat.Stat(image)
    unique_colors = len(set(image.getdata()))
    assert max(statistics.stddev) > 0.5, f"{label}: canvas pixel variance is blank"
    assert unique_colors >= 8, f"{label}: canvas has only {unique_colors} sampled colors"


def check_home(page: Page, output: Path, label: str) -> None:
    page.goto("/", wait_until="domcontentloaded", timeout=30_000)
    page.wait_for_timeout(3_000)
    assert_no_horizontal_overflow(page, f"{label}-home")
    assert_nonblank_canvas(page.locator("canvas").first, output, f"{label}-home")

    neural_heading = page.get_by_role("heading", name="Multi-Agent Neural Topology")
    neural_heading.scroll_into_view_if_needed()
    page.wait_for_timeout(1_500)
    neural_canvas = page.locator("[data-testid='neural-visualization'] canvas")
    assert_nonblank_canvas(neural_canvas, output, f"{label}-neural")


def assert_inside_viewport(page: Page, selector: str, label: str) -> None:
    locator = page.locator(selector)
    assert locator.count() == 1, f"{label}: expected one element"
    box = locator.bounding_box()
    viewport = page.viewport_size
    assert box and viewport, f"{label}: missing bounding box"
    assert box["x"] >= -2 and box["y"] >= -2, f"{label}: starts outside viewport: {box}"
    assert box["x"] + box["width"] <= viewport["width"] + 2, f"{label}: clipped horizontally: {box}"
    assert box["y"] + box["height"] <= viewport["height"] + 2, f"{label}: clipped vertically: {box}"


def check_tour(page: Page, output: Path, label: str) -> None:
    page.goto("/tour", wait_until="domcontentloaded", timeout=30_000)
    enter = page.get_by_role("button", name="Enter the 3D tour")
    assert enter.count() == 1, f"{label}: tour entry button missing"
    enter.evaluate("element => element.click()")
    page.wait_for_timeout(5_000)
    assert page.locator("[data-testid='tour-mode-controls']").count() == 1, (
        f"{label}: tour controls missing"
    )
    assert page.locator("[data-testid='tour-loading-veil']").count() == 0, (
        f"{label}: tour renderer did not become ready"
    )

    assert_no_horizontal_overflow(page, f"{label}-tour")
    assert_inside_viewport(page, "[data-testid='tour-mode-controls']", f"{label}-tour-modes")
    assert_inside_viewport(page, "[data-testid='tour-minimap']", f"{label}-tour-minimap")
    assert_inside_viewport(page, "[data-testid='tour-guide-controls']", f"{label}-tour-guide")
    assert_nonblank_canvas(page.locator("canvas").first, output, f"{label}-tour")

    dollhouse = page.get_by_role("button", name="Dollhouse")
    dollhouse.evaluate("element => element.click()")
    assert dollhouse.get_attribute("aria-pressed") == "true", f"{label}: dollhouse did not activate"
    floor_plan = page.get_by_role("button", name="Floor Plan")
    floor_plan.evaluate("element => element.click()")
    page.get_by_role("dialog", name="Interactive property floor plan").wait_for(
        state="visible", timeout=5_000
    )
    page.get_by_role("button", name="Close floor plan").evaluate("element => element.click()")

    canvas = page.locator("canvas").first
    box = canvas.bounding_box()
    assert box
    page.mouse.move(box["x"] + box["width"] * 0.55, box["y"] + box["height"] * 0.5)
    page.mouse.down()
    page.mouse.move(box["x"] + box["width"] * 0.68, box["y"] + box["height"] * 0.55, steps=8)
    page.mouse.up()
    assert_nonblank_canvas(canvas, output, f"{label}-tour-interacted")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:3000")
    parser.add_argument("--output-dir", default="/tmp/oracle-spatial-playwright")
    parser.add_argument("--viewport", choices=["desktop", "mobile"])
    parser.add_argument("--scene", choices=["home", "tour"])
    args = parser.parse_args()
    if not args.viewport or not args.scene:
        parser.error("--viewport and --scene are required; use run-spatial-playwright.py for the full matrix")
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)

    failures: list[str] = []
    playwright = sync_playwright().start()
    browser = playwright.chromium.launch(
        headless=True,
        args=["--enable-webgl", "--ignore-gpu-blocklist", "--use-angle=swiftshader"],
    )
    selected = {args.viewport: VIEWPORTS[args.viewport]}
    scenes = {
        "home": check_home,
        "tour": check_tour,
    }
    selected_scenes = {args.scene: scenes[args.scene]}
    for label, viewport in selected.items():
        for scene, check in selected_scenes.items():
            context = browser.new_context(base_url=args.base_url, viewport=viewport)
            page = context.new_page()
            page.set_default_timeout(10_000)
            # A listening Next.js socket can precede route readiness while the
            # production server finishes loading its manifests.
            page.wait_for_timeout(5_000)
            page_errors: list[str] = []
            page.on("pageerror", lambda error, bucket=page_errors: bucket.append(str(error)))
            try:
                check(page, output, label)
                assert not page_errors, f"{label}-{scene}: browser errors: {page_errors}"
            except Exception as exc:  # collect both viewports before failing
                diagnostics: list[str] = []
                try:
                    page.screenshot(
                        path=str(output / f"{label}-{scene}-failure.png"),
                        full_page=False,
                        timeout=3_000,
                    )
                except Exception as capture_error:
                    diagnostics.append(f"screenshot={capture_error}")
                try:
                    body = page.locator("body").inner_text(timeout=2_000).replace("\n", " ")[:500]
                except Exception as body_error:
                    body = f"<unavailable: {body_error}>"
                failures.append(
                    f"{label}-{scene}: {exc}; browser errors={page_errors}; body={body!r}; "
                    f"diagnostics={diagnostics}"
                )

    if failures:
        print("Spatial Playwright checks failed:\n- " + "\n- ".join(failures), file=sys.stderr)
        sys.stderr.flush()
        os._exit(1)
    print(
        f"Spatial Playwright checks passed for {', '.join(selected)} "
        f"({', '.join(selected_scenes)}); pixels: {output}",
        flush=True,
    )
    # Chromium can deadlock while synchronously destroying live SwiftShader
    # WebGL contexts. The test process owns the browser, so exiting after all
    # assertions have flushed provides deterministic cleanup to the OS.
    os._exit(0)


if __name__ == "__main__":
    main()
