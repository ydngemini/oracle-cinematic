"""Production-bundle smoke test for the public Neoh architectural reel."""

from pathlib import Path
import os
import shutil

from playwright.sync_api import expect, sync_playwright


BASE_URL = os.environ.get("REEL_BASE_URL", "http://127.0.0.1:4173").rstrip("/")
SCREENSHOT = Path("/tmp/neoh-vite-reel.png")


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

    page.goto(f"{BASE_URL}/reel", wait_until="networkidle")
    expect(page.get_by_role("main")).to_be_visible()
    expect(page.locator("[data-reel-project]")).to_have_count(3)
    first_image = page.locator("[data-reel-project] img").first
    assert first_image.evaluate("image => image.complete && image.naturalWidth > 0")
    expect(page.get_by_role("button", name="02 / Threshold / Water Side")).to_be_visible()
    page.get_by_role("button", name="02 / Threshold / Water Side").click()
    expect(page.get_by_text("02 / 03")).to_be_visible(timeout=6000)
    page.screenshot(path=SCREENSHOT, full_page=True)

    mobile = browser.new_page(viewport={"width": 390, "height": 844})
    mobile.goto(f"{BASE_URL}/reel", wait_until="networkidle")
    trigger = mobile.get_by_role("button", name="Projects")
    trigger.click()
    navigation = mobile.get_by_role("navigation", name="Project navigation")
    expect(navigation).to_be_visible()
    mobile.keyboard.press("Escape")
    expect(navigation).to_be_hidden()
    expect(trigger).to_be_focused()

    reduced = browser.new_page(viewport={"width": 1280, "height": 800})
    reduced.emulate_media(reduced_motion="reduce")
    reduced.goto(f"{BASE_URL}/reel", wait_until="networkidle")
    projects = reduced.locator("[data-reel-project]")
    first_box = projects.nth(0).bounding_box()
    second_box = projects.nth(1).bounding_box()
    assert first_box and second_box and second_box["y"] > first_box["y"] + 500
    assert not errors, f"Browser runtime failures: {errors}"
    browser.close()

print(f"Vite reel browser validation passed. Screenshot: {SCREENSHOT}")
