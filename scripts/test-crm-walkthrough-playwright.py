#!/usr/bin/env python3
"""Authenticated desktop/mobile smoke test for the CRM guided walkthrough.

The local operator credentials come from the environment. Billing is stubbed
active so this smoke test can never open live Stripe checkout, and the policy
status response is adapted to represent an identity that already acknowledged
the ESA. The login, CRM APIs, routes, lazy chunks, and tour itself remain live.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
from pathlib import Path

from playwright.sync_api import Page, Route, expect, sync_playwright


STEPS = (
    ("Today", "/today", None),
    ("People", "/people", None),
    ("Inbox", "/inbox", None),
    ("Deals", "/deals", None),
    ("Our AI · Cowork", "/our-ai", "cowork"),
    ("Our AI · Sales", "/our-ai/sales", "sales"),
    ("Sales Agent", "/our-ai/sales/agent", None),
    ("Lead Routing", "/our-ai/sales/routing", None),
    ("Power Dialer", "/our-ai/sales/dialer", None),
    ("Smart Plans", "/our-ai/sales/plans", None),
    ("Providers", "/our-ai/sales/providers", None),
    ("Our AI · Social", "/our-ai", "social"),
    ("Our AI · Homeowners", "/our-ai", "homeowners"),
    ("Our AI · Automations", "/our-ai", "automations"),
    ("Our AI · Sites", "/our-ai", "sites"),
)


def _policy_as_previously_acknowledged(route: Route) -> None:
    if route.request.method != "GET":
        route.continue_()
        return
    response = route.fetch()
    if not response.ok:
        route.fulfill(response=response)
        return
    payload = response.json()
    payload["account_security_required"] = False
    route.fulfill(response=response, json=payload)


def _install_safe_test_routes(page: Page) -> None:
    page.route(
        "**/billing/status/**",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "active": True,
                    "status": "active",
                    "plan": "e2e-safe",
                    "current_period_end": None,
                }
            ),
        ),
    )
    page.route("**/auth/policy-acceptance", _policy_as_previously_acknowledged)


def _assert_viewport_width(page: Page, label: str) -> None:
    dimensions = page.evaluate(
        """() => ({
          viewport: window.innerWidth,
          body: document.body.scrollWidth,
          document: document.documentElement.scrollWidth,
        })"""
    )
    widest = max(dimensions["body"], dimensions["document"])
    assert widest <= dimensions["viewport"] + 2, (
        f"{label}: horizontal overflow {dimensions}"
    )


def _run_walkthrough(
    page: Page,
    *,
    base_url: str,
    agent_id: str,
    passphrase: str,
    label: str,
    screenshot: Path | None,
) -> None:
    console_errors: list[str] = []
    page_errors: list[str] = []
    failed_requests: list[str] = []

    def record_console_error(message) -> None:
        if message.type != "error":
            return
        location = message.location or {}
        source = location.get("url", "unknown source")
        console_errors.append(f"{message.text} @ {source}")

    page.on("console", record_console_error)
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    page.on(
        "requestfailed",
        lambda request: failed_requests.append(
            f"{request.method} {request.url}: {request.failure}"
        ),
    )
    _install_safe_test_routes(page)

    page.goto(base_url, wait_until="domcontentloaded", timeout=30_000)
    page.get_by_label("Email or Agent ID").fill(agent_id)
    page.get_by_label("Passphrase").fill(passphrase)
    page.get_by_role("button", name="Authenticate").click()

    tour_button = page.get_by_role(
        "button", name="Start CRM guided walkthrough"
    )
    expect(tour_button).to_be_visible(timeout=30_000)

    later = page.get_by_role("button", name="Later", exact=True)
    if later.count() and later.is_visible():
        later.click()

    tour_button.click()
    dialog = page.get_by_role("dialog", name=re.compile("next best work", re.I))
    expect(dialog).to_be_visible()

    for index, (step_label, expected_path, workspace) in enumerate(STEPS, start=1):
        dialog = page.get_by_role("dialog")
        step_pattern = re.compile(
            rf"Step {index} of {len(STEPS)}\s*[·-]\s*{re.escape(step_label)}"
        )
        expect(dialog.get_by_text(step_pattern)).to_be_visible()
        expect(dialog.get_by_role("progressbar")).to_have_attribute(
            "aria-valuenow", str(index)
        )
        expect(page).to_have_url(re.compile(rf"{re.escape(expected_path)}(?:[?#].*)?$"))

        if workspace is not None:
            actual_workspace = page.evaluate(
                "() => window.sessionStorage.getItem('oracle_ai_workspace')"
            )
            assert actual_workspace == workspace, (
                f"{label} step {index}: expected workspace {workspace!r}, "
                f"found {actual_workspace!r}"
            )

        _assert_viewport_width(page, f"{label}-step-{index}")
        if screenshot is not None and step_label == "Lead Routing":
            page.screenshot(path=str(screenshot), full_page=True)

        if index < len(STEPS):
            # The tour uses history.pushState for client-side route changes.
            # Dispatch the already-visible/enabled control directly so
            # Playwright cannot mistake that same-document transition for a
            # full navigation and wait on unrelated background API requests.
            # The next loop explicitly verifies both URL and rendered step.
            next_button = dialog.get_by_role("button", name="Next", exact=True)
            expect(next_button).to_be_enabled()
            next_button.dispatch_event("click")

    finish_button = dialog.get_by_role("button", name="Finish tour", exact=True)
    expect(finish_button).to_be_enabled()
    finish_button.dispatch_event("click")
    expect(page.get_by_role("dialog")).to_be_hidden()
    expect(tour_button).to_be_focused()
    completed = page.evaluate(
        "() => window.localStorage.getItem('oracle_product_tour_v1')"
    )
    assert completed == "complete", f"{label}: tour completion was not persisted"

    assert not page_errors, f"{label}: page errors: {page_errors}"
    assert not failed_requests, f"{label}: failed requests: {failed_requests}"
    assert not console_errors, f"{label}: console errors: {console_errors}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:5173")
    parser.add_argument("--screenshot", default="/tmp/oracle-lead-routing-tour.png")
    args = parser.parse_args()

    agent_id = os.environ.get("ORACLE_ADMIN_ID", "")
    passphrase = os.environ.get("ORACLE_ADMIN_PASSPHRASE", "")
    if not agent_id or not passphrase:
        raise RuntimeError(
            "ORACLE_ADMIN_ID and ORACLE_ADMIN_PASSPHRASE are required"
        )

    with sync_playwright() as playwright:
        system_chrome = shutil.which("google-chrome") or shutil.which("chromium")
        launch_options = {"headless": True}
        if system_chrome:
            launch_options["executable_path"] = system_chrome
        browser = playwright.chromium.launch(**launch_options)
        try:
            desktop = browser.new_page(viewport={"width": 1440, "height": 1000})
            _run_walkthrough(
                desktop,
                base_url=args.base_url,
                agent_id=agent_id,
                passphrase=passphrase,
                label="desktop",
                screenshot=Path(args.screenshot),
            )
            print("Desktop CRM walkthrough passed", flush=True)
            desktop.close()

            mobile = browser.new_page(viewport={"width": 390, "height": 844})
            _run_walkthrough(
                mobile,
                base_url=args.base_url,
                agent_id=agent_id,
                passphrase=passphrase,
                label="mobile",
                screenshot=None,
            )
            print("Mobile CRM walkthrough passed", flush=True)
            mobile.close()
        finally:
            browser.close()

    print(f"CRM walkthrough passed on desktop and mobile; screenshot={args.screenshot}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
