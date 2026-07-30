#!/usr/bin/env python3
"""Authenticated, non-destructive browser audit for the NEOH agent workspace."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from typing import Any

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright


def _visible_control_names(page: Page, role: str) -> list[str]:
    locator = page.get_by_role(role)
    names: list[str] = []
    for index in range(locator.count()):
        control = locator.nth(index)
        if not control.is_visible():
            continue
        name = control.evaluate(
            """element => (
                element.getAttribute('aria-label')
                || (element.labels && element.labels[0] && element.labels[0].innerText)
                || element.getAttribute('placeholder')
                || element.innerText
                || ''
            ).trim()"""
        )
        names.append(" ".join(name.split()) if name else "<unnamed>")
    return names


def _login(page: Page, agent_id: str, passphrase: str) -> None:
    page.goto("/", wait_until="domcontentloaded")
    authenticate = page.get_by_role("button", name="Authenticate")
    if authenticate.is_visible():
        page.get_by_label("Email or Agent ID").fill(agent_id)
        page.get_by_label("Passphrase").fill(passphrase)
        authenticate.click()

    try:
        page.locator('nav[aria-label="Neoh CRM"]').wait_for(state="visible", timeout=20_000)
    except PlaywrightTimeoutError as exc:
        policy = page.get_by_role("dialog", name="Before you enter NEOH™")
        if policy.count() and policy.is_visible():
            raise RuntimeError(
                "The audit account requires a personal policy acknowledgement; "
                "the test will not accept legal terms on a user's behalf."
            ) from exc
        alert = page.get_by_role("alert")
        detail = alert.inner_text().strip() if alert.count() and alert.is_visible() else "workspace did not load"
        raise RuntimeError(detail) from exc


def _audit_viewport(browser, base_url: str, viewport: dict[str, int], agent_id: str, passphrase: str) -> dict[str, Any]:
    context = browser.new_context(base_url=base_url, viewport=viewport, reduced_motion="reduce")
    context.add_init_script("sessionStorage.setItem('oracle_onboarding_dismissed', '1');")
    page = context.new_page()
    page.route(
        "**/*",
        lambda route: route.abort()
        if route.request.resource_type in {"image", "media"}
        else route.continue_(),
    )

    console_errors: list[str] = []
    page_errors: list[str] = []
    api_failures: list[dict[str, Any]] = []

    def record_console(message) -> None:
        if message.type != "error":
            return
        # The audit deliberately aborts image/media requests so 48 property
        # cards do not consume browser resources. Chromium logs that harness
        # action as ERR_FAILED; it is not an application exception.
        if message.text == "Failed to load resource: net::ERR_FAILED":
            return
        console_errors.append(message.text)

    page.on("console", record_console)
    page.on("pageerror", lambda error: page_errors.append(str(error)))

    def record_response(response) -> None:
        if response.status < 400:
            return
        if response.request.resource_type not in {"document", "fetch", "xhr", "script", "stylesheet"}:
            return
        api_failures.append({"status": response.status, "url": response.url.split("?", 1)[0]})

    page.on("response", record_response)
    _login(page, agent_id, passphrase)

    deck = page.locator('nav[aria-label="Neoh CRM"]')
    tabs = deck.get_by_role("tab")
    tab_names = [tabs.nth(index).get_attribute("aria-label") or "" for index in range(tabs.count())]

    # WAI-ARIA tab keyboard behavior: arrows move focus; Enter activates.
    first = tabs.first
    first.focus()
    first.press("ArrowRight")
    second = tabs.nth(1)
    keyboard_focus = second.evaluate("element => document.activeElement === element")
    second.press("Enter")
    keyboard_activation = second.get_attribute("aria-selected") == "true"

    tab_results: list[dict[str, Any]] = []
    ready_targets = {
        "Listings": ('section[aria-label="Marketplace"]', None),
        "Clients": ('section[aria-label="Client book"]', None),
        "Comms": ('section[aria-label="Comms — all conversations"]', None),
        "Personal AI": (None, "Personal AI"),
        "Contracts": (None, "PDF documents"),
        "Me": ('section[aria-label="Me"]', None),
        "Ops": ('section[aria-label="Platform operations console"]', None),
    }
    for tab_name in tab_names:
        tab = deck.get_by_role("tab", name=tab_name, exact=True)
        tab.click()
        panel = page.get_by_role("tabpanel", name=tab_name, exact=True)
        panel.wait_for(state="visible", timeout=15_000)
        selector, heading = ready_targets[tab_name]
        if selector:
            panel.locator(selector).wait_for(state="visible", timeout=15_000)
        else:
            panel.get_by_role("heading", name=heading, exact=True).wait_for(
                state="visible", timeout=15_000
            )

        if tab_name == "Listings":
            panel.get_by_text("properties", exact=False).first.wait_for(state="visible", timeout=15_000)
            if panel.get_by_text("No pipeline properties match", exact=True).count():
                raise AssertionError("Listings pipeline rendered the empty state")

        footer = panel.locator('[data-neoh-footer="true"]')
        footer.scroll_into_view_if_needed()
        footer_text = " ".join(footer.inner_text().split())
        dimensions = page.evaluate(
            "() => ({ viewport: innerWidth, width: document.documentElement.scrollWidth })"
        )
        buttons = _visible_control_names(panel, "button")
        unnamed_buttons = [name for name in buttons if name == "<unnamed>"]
        tab_results.append(
            {
                "tab": tab_name,
                "selected": tab.get_attribute("aria-selected") == "true",
                "footer_visible": footer.is_visible(),
                "footer_legal_text": "NEOH" in footer_text and "YDN LLC" in footer_text,
                "horizontal_overflow_px": max(0, dimensions["width"] - dimensions["viewport"]),
                "visible_buttons": buttons,
                "unnamed_buttons": unnamed_buttons,
                "visible_textboxes": _visible_control_names(panel, "textbox"),
                "visible_comboboxes": _visible_control_names(panel, "combobox"),
            }
        )

    result = {
        "viewport": viewport,
        "tabs": tab_names,
        "keyboard_focus": keyboard_focus,
        "keyboard_activation": keyboard_activation,
        "tab_results": tab_results,
        "console_errors": sorted(set(console_errors)),
        "page_errors": sorted(set(page_errors)),
        "api_failures": api_failures,
    }
    context.close()
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:5173")
    args = parser.parse_args()

    agent_id = os.environ.get("ORACLE_ADMIN_ID", "")
    passphrase = os.environ.get("ORACLE_ADMIN_PASSPHRASE", "")
    if not agent_id or not passphrase:
        print("ORACLE_ADMIN_ID and ORACLE_ADMIN_PASSPHRASE are required.", file=sys.stderr)
        return 2

    viewports = (
        {"width": 390, "height": 844},
        {"width": 1440, "height": 1000},
    )
    with sync_playwright() as playwright:
        executable = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE") or shutil.which("google-chrome")
        browser = playwright.chromium.launch(headless=True, executable_path=executable)
        try:
            results = [
                _audit_viewport(browser, args.base_url, viewport, agent_id, passphrase)
                for viewport in viewports
            ]
        finally:
            browser.close()

    violations: list[str] = []
    for result in results:
        label = f"{result['viewport']['width']}x{result['viewport']['height']}"
        if not result["keyboard_focus"] or not result["keyboard_activation"]:
            violations.append(f"{label}: tab keyboard behavior failed")
        if result["console_errors"]:
            violations.append(f"{label}: browser console errors")
        if result["page_errors"]:
            violations.append(f"{label}: uncaught page errors")
        if result["api_failures"]:
            violations.append(f"{label}: API/resource failures")
        for tab in result["tab_results"]:
            if not tab["selected"]:
                violations.append(f"{label} {tab['tab']}: tab did not activate")
            if not tab["footer_visible"] or not tab["footer_legal_text"]:
                violations.append(f"{label} {tab['tab']}: legal footer missing")
            if tab["horizontal_overflow_px"] > 2:
                violations.append(f"{label} {tab['tab']}: horizontal overflow")
            if tab["unnamed_buttons"]:
                violations.append(f"{label} {tab['tab']}: unnamed buttons")

    print(json.dumps({"ok": not violations, "violations": violations, "results": results}, indent=2))
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
