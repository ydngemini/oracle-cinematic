from pathlib import Path
import re
import shutil

from playwright.sync_api import expect, sync_playwright


BASE_URL = "http://127.0.0.1:3100"
ARTIFACT_DIR = Path("/tmp/oracle-reel-validation")
ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
AXE_PATH = Path("node_modules/axe-core/axe.min.js").resolve()


def assert_no_runtime_errors(errors: list[str]) -> None:
    meaningful = [
        error
        for error in errors
        if "favicon.ico" not in error
        and "Download the React DevTools" not in error
        and not error.startswith("Failed to load resource:")
    ]
    assert not meaningful, f"Browser runtime errors: {meaningful}"


def assert_no_serious_accessibility_violations(page) -> None:
    page.add_script_tag(path=AXE_PATH)
    violations = page.evaluate(
        """async () => {
          const results = await axe.run(document, {
            resultTypes: ['violations']
          });
          return results.violations
            .filter(({ impact }) => impact === 'serious' || impact === 'critical')
            .map(({ id, impact, description, nodes }) => ({
              id,
              impact,
              description,
              targets: nodes.map(({ target }) => target)
            }));
        }"""
    )
    assert not violations, f"Accessibility violations: {violations}"


with sync_playwright() as playwright:
    chrome_path = shutil.which("google-chrome") or shutil.which("chromium")
    browser = playwright.chromium.launch(
        headless=True,
        executable_path=chrome_path,
    )

    desktop = browser.new_page(viewport={"width": 1440, "height": 900})
    desktop_errors: list[str] = []
    desktop.on("console", lambda message: desktop_errors.append(message.text) if message.type == "error" else None)
    desktop.on("pageerror", lambda error: desktop_errors.append(str(error)))
    desktop.on(
        "response",
        lambda response: desktop_errors.append(
            f"HTTP {response.status}: {response.url}"
        )
        if response.status >= 400
        else None,
    )
    desktop.goto(f"{BASE_URL}/reel", wait_until="networkidle")

    assert desktop.get_by_role("heading", name="NEOH architectural studies").count() == 1
    assert desktop.locator("[data-reel-project]").count() == 3
    assert desktop.locator("[data-reel-project] img").first.evaluate(
        "(image) => image.complete && image.naturalWidth > 0"
    )

    skip_link_reached = False
    focus_sequence: list[str] = []
    for _ in range(4):
        desktop.keyboard.press("Tab")
        focused_text = desktop.locator(":focus").inner_text().strip()
        focus_sequence.append(focused_text)
        if focused_text.casefold() == "skip to project sequence":
            skip_link_reached = True
            break
    assert skip_link_reached, f"Skip link missing from keyboard order: {focus_sequence}"

    desktop.screenshot(path=ARTIFACT_DIR / "desktop-first-project.png")
    desktop.get_by_role(
        "button", name=re.compile(r"Go to project 2:")
    ).click()
    expect(desktop.locator("header p[aria-live='polite']")).to_contain_text(
        "02", timeout=6000
    )
    desktop.screenshot(path=ARTIFACT_DIR / "desktop-second-project.png")
    assert_no_serious_accessibility_violations(desktop)
    assert_no_runtime_errors(desktop_errors)

    mobile = browser.new_page(viewport={"width": 390, "height": 844})
    mobile_errors: list[str] = []
    mobile.on("console", lambda message: mobile_errors.append(message.text) if message.type == "error" else None)
    mobile.on("pageerror", lambda error: mobile_errors.append(str(error)))
    mobile.on(
        "response",
        lambda response: mobile_errors.append(
            f"HTTP {response.status}: {response.url}"
        )
        if response.status >= 400
        else None,
    )
    mobile.goto(f"{BASE_URL}/reel", wait_until="networkidle")

    menu_trigger = mobile.get_by_role("button", name=re.compile(r"Index"))
    menu_trigger.click()
    project_index = mobile.get_by_role("navigation", name="Project index")
    assert project_index.is_visible()
    assert project_index.get_by_role("button").count() == 3
    mobile.keyboard.press("Escape")
    assert project_index.is_hidden()
    assert menu_trigger.evaluate("(element) => document.activeElement === element")
    assert mobile.locator("[data-reel-project]").nth(1).bounding_box()["y"] > 700
    mobile.screenshot(path=ARTIFACT_DIR / "mobile-project.png")
    assert_no_serious_accessibility_violations(mobile)
    assert_no_runtime_errors(mobile_errors)

    reduced = browser.new_page(viewport={"width": 1280, "height": 800})
    reduced.emulate_media(reduced_motion="reduce")
    reduced.goto(f"{BASE_URL}/reel", wait_until="networkidle")
    project_boxes = [
        reduced.locator("[data-reel-project]").nth(index).bounding_box()
        for index in range(3)
    ]
    assert all(box is not None for box in project_boxes)
    assert project_boxes[1]["y"] > project_boxes[0]["y"] + 500
    assert project_boxes[2]["y"] > project_boxes[1]["y"] + 500
    assert reduced.locator(".pin-spacer").count() == 0

    home = browser.new_page(viewport={"width": 1280, "height": 800})
    home.goto(BASE_URL, wait_until="networkidle")
    assert home.locator("[data-reel-backdrop]").count() == 1
    assert home.get_by_role("heading", name="ORACLE").count() == 1

    legacy = browser.new_page(viewport={"width": 1280, "height": 800})
    legacy.goto(f"{BASE_URL}/legacy", wait_until="networkidle")
    assert legacy.locator("[data-reel-backdrop]").count() == 0
    assert legacy.get_by_role("heading", name="ORACLE").count() == 1

    browser.close()

print(f"Reel browser validation passed. Screenshots: {ARTIFACT_DIR}")
