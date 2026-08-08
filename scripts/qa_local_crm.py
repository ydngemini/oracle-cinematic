#!/usr/bin/env python3
"""Authenticated local UI smoke test for the Neoh five-destination CRM.

The script never prints credentials or persists browser storage state.  With
``--create-preview`` it creates one clearly labelled private Sites draft when
that draft does not already exist; it never publishes, sends, calls, or opens a
billing flow.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from playwright.sync_api import Page, expect, sync_playwright


ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "oracle-app" / "design"
FRONTEND = "http://localhost:5173"
PREVIEW_NAME = "Neoh Mountain Preview"
PREVIEW_HEADLINE = "Real estate with a clearer point of view."


def _login(page: Page, agent_id: str, passphrase: str) -> None:
    page.goto(FRONTEND, wait_until="networkidle")
    login = page.get_by_role("button", name="Authenticate")
    if login.count():
        page.get_by_label("Email or Agent ID").fill(agent_id)
        page.get_by_label("Passphrase").fill(passphrase)
        login.click()

    # Auth resolves asynchronously into either the CRM or a user-only legal
    # acknowledgement. Wait for that decision before inspecting either path;
    # the smoke test must never race the gate or accept an agreement itself.
    page.locator(
        "#today-title, #esa-only-title, #policy-title, #policy-error-title"
    ).first.wait_for(state="visible", timeout=20_000)

    policy_dialog = page.get_by_role("dialog", name=re.compile("Before you enter", re.I))
    if policy_dialog.count() and policy_dialog.is_visible():
        raise RuntimeError(
            "The account requires a policy acknowledgement; QA will not accept it automatically."
        )
    esa_dialog = page.get_by_role(
        "dialog", name=re.compile("Account Security Agreement|Acknowledge.*ESA", re.I)
    )
    if esa_dialog.count() and esa_dialog.is_visible():
        raise RuntimeError(
            "The account requires its one-time Account Security ESA acknowledgement; "
            "QA will not accept a legal agreement automatically."
        )

    expect(page.get_by_role("heading", name="Today", exact=True)).to_be_visible(timeout=20_000)


def _measure_frames(page: Page, duration_ms: int = 2_000) -> float:
    result = page.evaluate(
        """
        (duration) => new Promise((resolve) => {
          let frames = 0;
          const started = performance.now();
          const tick = (now) => {
            frames += 1;
            if (now - started >= duration) {
              resolve({ frames, elapsed: now - started });
              return;
            }
            requestAnimationFrame(tick);
          };
          requestAnimationFrame(tick);
        })
        """,
        duration_ms,
    )
    return round(float(result["frames"]) * 1_000 / float(result["elapsed"]), 1)


def _create_private_preview(page: Page) -> None:
    if page.get_by_text(PREVIEW_NAME, exact=True).count():
        return

    page.get_by_role("button", name="New site").click()
    expect(page.get_by_role("heading", name="Template", exact=True)).to_be_visible()
    page.get_by_role("radio", name=re.compile("Editorial", re.I)).check()
    page.get_by_role("button", name=re.compile("Continue")).click()

    page.get_by_label("Internal site name").fill(PREVIEW_NAME)
    page.get_by_label("Public brand name").fill("Neoh Property Advisory")
    page.get_by_label("Hero headline").fill(PREVIEW_HEADLINE)
    page.get_by_label("Hero support line").fill(
        "A direct path to thoughtful property guidance, verified sources, and your next conversation."
    )
    page.get_by_role("button", name=re.compile("Continue")).click()

    page.get_by_label("Service areas").fill("Delaware, Wilmington")
    page.get_by_role("button", name=re.compile("Continue")).click()

    page.get_by_label("Agent or team name").fill("YDN G")
    page.get_by_label("Short bio").fill(
        "Independent guidance with source-backed research and a direct, accountable process."
    )
    page.get_by_role("button", name=re.compile("Continue")).click()

    page.get_by_label("Search title").fill("Neoh Mountain Property Guidance")
    page.get_by_label("Search description").fill(
        "Source-backed real-estate guidance and a direct three-question path to an independent agent."
    )
    with page.expect_response(
        lambda response: response.url.endswith("/api/sites")
        and response.request.method == "POST"
    ) as response_info:
        page.get_by_role("button", name=re.compile("Save private draft")).click()
    response = response_info.value
    if response.status != 201:
        raise RuntimeError(f"Studio draft returned HTTP {response.status}")
    expect(page.get_by_text(PREVIEW_NAME, exact=True)).to_be_visible(timeout=15_000)


def _open_preview(page: Page, console_errors: list[str]) -> Page:
    row = page.get_by_role("listitem").filter(has_text=PREVIEW_NAME)
    with page.expect_popup() as popup_info:
        row.get_by_role("button", name=re.compile("Preview")).click()
    popup = popup_info.value
    _record_errors(popup, console_errors)
    popup.wait_for_load_state("networkidle")
    expect(popup.get_by_role("heading", name=PREVIEW_HEADLINE, exact=True)).to_be_visible()
    return popup


def _record_errors(page: Page, bucket: list[str]) -> None:
    page.on(
        "console",
        lambda message: bucket.append(message.text[:500])
        if message.type == "error"
        else None,
    )
    page.on("pageerror", lambda error: bucket.append(str(error)[:500]))


def run(create_preview: bool) -> dict[str, Any]:
    load_dotenv(ROOT / ".env", override=False)
    agent_id = os.getenv("ORACLE_ADMIN_ID", "")
    passphrase = os.getenv("ORACLE_ADMIN_PASSPHRASE", "")
    if not agent_id or not passphrase:
        raise RuntimeError("ORACLE_ADMIN_ID and ORACLE_ADMIN_PASSPHRASE are required in .env")

    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    console_errors: list[str] = []
    summary: dict[str, Any] = {}

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            executable_path="/usr/bin/google-chrome",
        )
        desktop = browser.new_context(
            viewport={"width": 1440, "height": 1000},
            color_scheme="dark",
        )
        page = desktop.new_page()
        _record_errors(page, console_errors)
        _login(page, agent_id, passphrase)

        nav = page.get_by_role("navigation", name="Neoh CRM")
        tab_names = ["Today", "People", "Inbox", "Deals", "Our AI"]
        for name in tab_names:
            expect(nav.get_by_role("tab", name=name, exact=True)).to_be_visible()
        summary["destinations"] = tab_names

        video = page.locator("video").first
        expect(video).to_be_visible()
        page.wait_for_function(
            """() => {
              const video = document.querySelector('video');
              return Boolean(video && video.readyState >= 2 && !video.paused);
            }""",
            timeout=15_000,
        )
        summary["waterfall_video"] = "playing"
        summary["measured_raf_fps"] = _measure_frames(page)

        page.screenshot(path=str(ARTIFACTS / "neoh-local-today-desktop.png"), full_page=True)

        nav.get_by_role("tab", name="People", exact=True).click()
        expect(page.get_by_role("heading", name="People", exact=True)).to_be_visible()
        expect(page.get_by_role("heading", name="Contacts", exact=True)).to_be_visible(timeout=15_000)
        contacts = page.locator("section[aria-labelledby='canonical-contacts-title'] ul li")
        expect(contacts.first).to_be_visible()
        contact_count = contacts.count()
        if contact_count < 5:
            raise AssertionError(f"Expected at least five migrated contacts, saw {contact_count}")
        summary["canonical_contacts_visible"] = contact_count

        people_tab = nav.get_by_role("tab", name="People", exact=True)
        people_tab.focus()
        people_tab.press("ArrowRight")
        inbox_tab = nav.get_by_role("tab", name="Inbox", exact=True)
        expect(inbox_tab).to_be_focused()
        inbox_tab.press("Enter")
        expect(page.get_by_role("heading", name="Inbox", exact=True)).to_be_visible()
        summary["keyboard_tab_navigation"] = "passed"

        nav.get_by_role("tab", name="Our AI", exact=True).click()
        expect(page.get_by_role("heading", name="Our AI", exact=True)).to_be_visible()
        ai_nav = page.get_by_role("navigation", name="Our AI capabilities")
        ai_workspaces = ["Cowork", "Sales", "Social", "Homeowners", "Automations", "Sites"]
        for name in ai_workspaces:
            expect(ai_nav.get_by_role("tab", name=name, exact=True)).to_be_visible()
        summary["ai_workspaces"] = ai_workspaces

        ai_nav.get_by_role("tab", name="Sales", exact=True).click()
        expect(page.get_by_role("heading", name="Sales capabilities", exact=True)).to_be_visible()
        ai_nav.get_by_role("tab", name="Social", exact=True).click()
        expect(page.get_by_text("Google LSA", exact=True)).to_be_visible()
        expect(page.get_by_text("Setup required", exact=True).first).to_be_visible()
        ai_nav.get_by_role("tab", name="Homeowners", exact=True).click()
        expect(page.get_by_role("heading", name="Homeowner capabilities", exact=True)).to_be_visible()
        ai_nav.get_by_role("tab", name="Automations", exact=True).click()
        expect(page.get_by_role("heading", name="Personal AI", exact=True)).to_be_visible(timeout=15_000)
        ai_nav.get_by_role("tab", name="Sites", exact=True).click()
        expect(page.get_by_role("heading", name="Sites & IDX", exact=True)).to_be_visible(timeout=15_000)
        if create_preview:
            _create_private_preview(page)
            popup = _open_preview(page, console_errors)
            popup.screenshot(
                path=str(ARTIFACTS / "neoh-site-preview-desktop.png"),
                full_page=True,
            )
            summary["private_preview"] = popup.url
            popup.close()
        page.screenshot(path=str(ARTIFACTS / "neoh-local-studio-desktop.png"), full_page=True)

        profile = page.get_by_role("button", name="Open agent profile and settings")
        profile.click()
        dialog = page.get_by_role("dialog", name=re.compile("Settings|AI controls|Admin"))
        expect(dialog).to_be_visible()
        page.keyboard.press("Escape")
        expect(dialog).to_be_hidden()
        expect(profile).to_be_focused()
        summary["profile_escape_and_focus_return"] = "passed"

        state = desktop.storage_state()
        mobile = browser.new_context(
            viewport={"width": 390, "height": 844},
            device_scale_factor=3,
            is_mobile=True,
            has_touch=True,
            reduced_motion="reduce",
            color_scheme="dark",
            storage_state=state,
        )
        mobile_page = mobile.new_page()
        _record_errors(mobile_page, console_errors)
        mobile_page.goto(FRONTEND, wait_until="networkidle")
        expect(mobile_page.get_by_role("heading", name="Today", exact=True)).to_be_visible(timeout=20_000)
        mobile_page.get_by_role("navigation", name="Neoh CRM").get_by_role(
            "tab", name="Our AI", exact=True
        ).click()
        expect(mobile_page.get_by_role("heading", name="Our AI", exact=True)).to_be_visible(timeout=20_000)
        summary["mobile_horizontal_overflow"] = mobile_page.evaluate(
            "document.documentElement.scrollWidth > window.innerWidth + 1"
        )
        if summary["mobile_horizontal_overflow"]:
            raise AssertionError("Mobile CRM has horizontal document overflow")
        if mobile_page.locator("video").count() != 0:
            raise AssertionError("Reduced-motion mode must render the waterfall poster, not autoplay video")
        expect(mobile_page.locator("img[aria-hidden='true']").first).to_be_visible()
        mobile_page.screenshot(
            path=str(ARTIFACTS / "neoh-local-mobile-reduced-motion.png"),
            full_page=True,
        )
        summary["reduced_motion_media"] = "poster"

        mobile.close()
        desktop.close()
        browser.close()

    unexpected = [
        message for message in console_errors
        if "favicon" not in message.lower() and "resizeobserver loop" not in message.lower()
    ]
    summary["unexpected_console_errors"] = unexpected
    if unexpected:
        raise AssertionError("Unexpected browser console errors: " + " | ".join(unexpected))
    return summary


def run_preview_only() -> dict[str, Any]:
    """Render the real saved Studio revision without bypassing API auth."""
    load_dotenv(ROOT / ".env", override=False)
    agent_id = os.getenv("ORACLE_ADMIN_ID", "")
    passphrase = os.getenv("ORACLE_ADMIN_PASSPHRASE", "")
    if not agent_id or not passphrase:
        raise RuntimeError("ORACLE_ADMIN_ID and ORACLE_ADMIN_PASSPHRASE are required in .env")

    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    console_errors: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            executable_path="/usr/bin/google-chrome",
        )
        context = browser.new_context(
            viewport={"width": 1440, "height": 1000},
            color_scheme="dark",
        )
        login = context.request.post(
            "http://localhost:8000/auth/login",
            data={"agent_id": agent_id, "passphrase": passphrase},
        )
        if not login.ok:
            raise RuntimeError(f"Local API login failed with HTTP {login.status}")
        sites_response = context.request.get("http://localhost:8000/api/sites?limit=100")
        if not sites_response.ok:
            raise RuntimeError(f"Studio list failed with HTTP {sites_response.status}")
        site = next(
            (
                item for item in sites_response.json().get("sites", [])
                if item.get("slug") == "neoh-mountain-preview"
            ),
            None,
        )
        if not site or not site.get("preview_revision_id"):
            raise RuntimeError("The Neoh Mountain Preview private revision does not exist")

        url = (
            f"{FRONTEND}/site-preview/neoh-mountain-preview"
            f"?revision={site['preview_revision_id']}"
        )
        page = context.new_page()
        _record_errors(page, console_errors)
        page.goto(url, wait_until="networkidle")
        expect(page.get_by_role("heading", name=PREVIEW_HEADLINE, exact=True)).to_be_visible()
        expect(page.locator("video").first).to_be_visible()
        page.wait_for_function(
            """() => {
              const video = document.querySelector('video');
              return Boolean(video && video.readyState >= 2 && !video.paused);
            }""",
            timeout=15_000,
        )
        measured_fps = _measure_frames(page)
        page.screenshot(
            path=str(ARTIFACTS / "neoh-site-preview-desktop.png"),
            full_page=True,
        )
        if page.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1"):
            raise AssertionError("Desktop site preview has horizontal document overflow")

        page.set_viewport_size({"width": 390, "height": 844})
        expect(page.get_by_role("heading", name=PREVIEW_HEADLINE, exact=True)).to_be_visible()
        if page.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1"):
            raise AssertionError("Mobile site preview has horizontal document overflow")
        page.screenshot(
            path=str(ARTIFACTS / "neoh-site-preview-mobile.png"),
            full_page=True,
        )
        page.emulate_media(reduced_motion="reduce")
        expect(page.locator("video")).to_have_count(0)
        expect(page.get_by_role("img", name="Architectural property in a mountain landscape")).to_be_visible()
        context.close()
        browser.close()

    unexpected = [
        message for message in console_errors
        if "favicon" not in message.lower() and "resizeobserver loop" not in message.lower()
    ]
    if unexpected:
        raise AssertionError("Unexpected browser console errors: " + " | ".join(unexpected))
    return {
        "private_preview": url,
        "desktop_screenshot": str(ARTIFACTS / "neoh-site-preview-desktop.png"),
        "mobile_screenshot": str(ARTIFACTS / "neoh-site-preview-mobile.png"),
        "measured_raf_fps": measured_fps,
        "waterfall_video": "playing",
        "reduced_motion_media": "poster",
        "unexpected_console_errors": unexpected,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--create-preview",
        action="store_true",
        help="Create the idempotent local-only private Studio draft when absent.",
    )
    parser.add_argument(
        "--preview-only",
        action="store_true",
        help="Render the real saved private site revision without entering the CRM shell.",
    )
    args = parser.parse_args()
    result = run_preview_only() if args.preview_only else run(create_preview=args.create_preview)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
