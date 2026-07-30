"""Production-bundle smoke test for the public Neoh architectural reel."""

from pathlib import Path
import os
import shutil
import time

from playwright.sync_api import expect, sync_playwright


BASE_URL = os.environ.get("REEL_BASE_URL", "http://127.0.0.1:4173").rstrip("/")
SCREENSHOT = Path(
    os.environ.get("REEL_SCREENSHOT", "/tmp/neoh-vite-reel.png")
)
ROOT_SCREENSHOT = Path(
    os.environ.get(
        "REEL_ROOT_SCREENSHOT",
        str(SCREENSHOT.with_name(f"{SCREENSHOT.stem}-root{SCREENSHOT.suffix}")),
    )
)


with sync_playwright() as playwright:
    chrome_path = shutil.which("google-chrome") or shutil.which("chromium")
    browser = playwright.chromium.launch(headless=True, executable_path=chrome_path)
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on(
        "response",
        lambda response: errors.append(f"HTTP {response.status}: {response.url}")
        if response.status >= 400
        else None,
    )

    page.goto(f"{BASE_URL}/reel", wait_until="domcontentloaded", timeout=60_000)
    expect(page.get_by_role("main")).to_be_visible()
    expect(page.locator("[data-reel-project]")).to_have_count(3)
    estate_video = page.locator(
        "video[poster='/media/mountain-waterfall-estate-v1.webp']"
    )
    expect(estate_video).to_have_count(1)
    expect(
        estate_video.locator(
            "source[src='/media/mountain-waterfall-estate-v1.mp4']"
        )
    ).to_have_count(1)
    page.wait_for_function(
        "video => video.readyState >= 2 && !video.paused",
        arg=estate_video.element_handle(),
        timeout=15_000,
    )
    start_time = estate_video.evaluate("video => video.currentTime")
    time.sleep(0.75)
    assert estate_video.evaluate("video => video.currentTime") > start_time
    measured_fps = page.evaluate(
        """() => new Promise((resolve) => {
          let frames = 0;
          const startedAt = performance.now();
          const sample = (now) => {
            frames += 1;
            if (now - startedAt >= 1500) {
              resolve((frames * 1000) / (now - startedAt));
              return;
            }
            requestAnimationFrame(sample);
          };
          requestAnimationFrame(sample);
        })"""
    )
    assert measured_fps >= 50, f"Expected near-60fps compositing, measured {measured_fps:.1f}"
    first_image = page.locator("[data-reel-project] img").first
    assert first_image.evaluate("image => image.complete && image.naturalWidth > 0")
    expect(page.get_by_role("button", name="02 / Threshold / Water Side")).to_be_visible()
    page.get_by_role("button", name="02 / Threshold / Water Side").click()
    expect(page.get_by_text("02 / 03")).to_be_visible(timeout=6000)
    page.screenshot(path=SCREENSHOT, full_page=True)

    mobile = browser.new_page(viewport={"width": 390, "height": 844})
    mobile.goto(f"{BASE_URL}/reel", wait_until="domcontentloaded", timeout=60_000)
    mobile_video = mobile.locator(
        "video[poster='/media/mountain-waterfall-estate-v1.webp']"
    )
    expect(mobile_video).to_have_count(1)
    mobile.wait_for_function(
        "video => video.readyState >= 2",
        arg=mobile_video.element_handle(),
        timeout=15_000,
    )
    assert mobile_video.evaluate(
        "video => video.currentSrc.endsWith('/media/mountain-waterfall-estate-v1-mobile.mp4')"
    )
    trigger = mobile.get_by_role("button", name="Index")
    trigger.click()
    navigation = mobile.get_by_role("navigation", name="Project navigation")
    expect(navigation).to_be_visible()
    mobile.keyboard.press("Escape")
    expect(navigation).to_be_hidden()
    expect(trigger).to_be_focused()

    reduced = browser.new_page(viewport={"width": 1280, "height": 800})
    reduced.emulate_media(reduced_motion="reduce")
    reduced.goto(f"{BASE_URL}/reel", wait_until="domcontentloaded", timeout=60_000)
    expect(
        reduced.locator(
            "img[src='/media/mountain-waterfall-estate-v1.webp']"
        )
    ).to_have_count(1)
    expect(reduced.locator("video")).to_have_count(0)
    projects = reduced.locator("[data-reel-project]")
    first_box = projects.nth(0).bounding_box()
    second_box = projects.nth(1).bounding_box()
    assert first_box and second_box and second_box["y"] > first_box["y"] + 500

    reduced_data_context = browser.new_context(viewport={"width": 1280, "height": 800})
    reduced_data_context.add_init_script(
        """Object.defineProperty(navigator, 'connection', {
          configurable: true,
          value: { saveData: true }
        });"""
    )
    reduced_data = reduced_data_context.new_page()
    reduced_data.goto(
        f"{BASE_URL}/reel", wait_until="domcontentloaded", timeout=60_000
    )
    expect(reduced_data.locator("video")).to_have_count(0)
    expect(
        reduced_data.locator(
            "img[src='/media/mountain-waterfall-estate-v1.webp']"
        )
    ).to_have_count(1)
    reduced_data_context.close()

    home = browser.new_page(viewport={"width": 1440, "height": 900})
    home_errors: list[str] = []
    home.on("pageerror", lambda error: home_errors.append(str(error)))
    home.goto(BASE_URL, wait_until="domcontentloaded", timeout=60_000)
    expect(home.get_by_role("button", name="Authenticate")).to_be_visible(
        timeout=15_000
    )
    home_video = home.locator(
        "video[poster='/media/mountain-waterfall-estate-v1.webp']"
    )
    expect(home_video).to_have_count(1)
    home.wait_for_function(
        "video => video.readyState >= 2 && !video.paused",
        arg=home_video.element_handle(),
        timeout=15_000,
    )
    home.wait_for_timeout(1_200)
    home.screenshot(path=ROOT_SCREENSHOT, full_page=True)
    assert not home_errors, f"Home browser runtime failures: {home_errors}"
    assert not errors, f"Browser runtime failures: {errors}"
    browser.close()

print(
    f"Vite reel browser validation passed at {measured_fps:.1f}fps. "
    f"Screenshots: {SCREENSHOT}, {ROOT_SCREENSHOT}"
)
