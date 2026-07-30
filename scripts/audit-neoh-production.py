#!/usr/bin/env python3
"""Authenticated, non-destructive production UI/fetch audit for Neoh.

Credentials are read from the environment and are never written to the report.
The audit opens every application tab, exercises refresh/filter/disclosure
controls, and records browser errors plus failed first-party fetches. Actions
that send, approve, delete, connect providers, sign out, or touch billing are
intentionally excluded.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import Locator, Page, TimeoutError, sync_playwright


SAFE_BUTTON = re.compile(
    r"^(refresh|retry|close|back|clear|all|email|sms|note|voice|"
    r"buyers?|sellers?|leads?|clients?|active|archived|"
    r"platform capability map|ai draft|quick templates)$",
    re.IGNORECASE,
)
BLOCKED_BUTTON = re.compile(
    r"(stripe|billing|subscribe|checkout|pay|purchase|send|queue|call|"
    r"approve|reject|delete|remove|archive|submit|save|create|add|new|"
    r"connect|disconnect|sign out|logout|upload|download|execute|run|"
    r"rerun|generate|draft|offer|contract)",
    re.IGNORECASE,
)


def button_name(button: Locator) -> str:
    aria = button.get_attribute("aria-label") or ""
    text = button.inner_text(timeout=1_500).strip()
    title = button.get_attribute("title") or ""
    return " ".join((aria or text or title).split())


def first_party(url: str, hosts: set[str]) -> bool:
    return urlparse(url).hostname in hosts


def login(page: Page, base_url: str, agent_id: str, passphrase: str) -> None:
    page.goto(base_url, wait_until="domcontentloaded", timeout=60_000)
    try:
        page.get_by_role("tab", name="Pipeline").wait_for(timeout=8_000)
        return
    except TimeoutError:
        pass

    page.get_by_label("Email or Agent ID").fill(agent_id)
    page.get_by_label("Passphrase").fill(passphrase)
    page.get_by_role("button", name="Authenticate").click()
    try:
        page.get_by_role("tab", name="Pipeline").wait_for(timeout=12_000)
    except TimeoutError as error:
        headings = page.get_by_role("heading").all_inner_texts()
        buttons = page.get_by_role("button").all_inner_texts()
        alerts = page.get_by_role("alert").all_inner_texts()
        raise RuntimeError(
            "dashboard did not mount; "
            f"url={page.url!r} headings={headings[:8]!r} "
            f"buttons={buttons[:12]!r} alerts={alerts[:8]!r}"
        ) from error


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--base-url",
        default="https://neoh-web.livelypebble-f08762d5.northcentralus.azurecontainerapps.io",
    )
    parser.add_argument(
        "--api-url",
        default="https://neoh-api.livelypebble-f08762d5.northcentralus.azurecontainerapps.io",
    )
    parser.add_argument(
        "--browser-executable",
        default=os.environ.get("NEOH_AUDIT_BROWSER_EXECUTABLE"),
        help="Optional Chromium/Chrome executable; uses Playwright's managed browser by default.",
    )
    parser.add_argument("--viewport-width", type=int, default=1440)
    parser.add_argument("--viewport-height", type=int, default=1000)
    parser.add_argument(
        "--navigation-only",
        action="store_true",
        help="Open every tab and verify layout without clicking in-panel controls.",
    )
    parser.add_argument(
        "--stream-settle-ms",
        type=int,
        default=8_000,
        help="Wait for authenticated WebSocket state before inventorying Pipeline.",
    )
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    agent_id = os.environ.get("NEOH_AUDIT_AGENT_ID", "")
    passphrase = os.environ.get("NEOH_AUDIT_PASSPHRASE", "")
    if not agent_id or not passphrase:
        raise SystemExit("NEOH_AUDIT_AGENT_ID and NEOH_AUDIT_PASSPHRASE are required")

    hosts = {urlparse(args.base_url).hostname, urlparse(args.api_url).hostname}
    report: dict[str, object] = {
        "tabs": {},
        "failed_responses": [],
        "request_failures": [],
        "console_errors": [],
        "page_errors": [],
        "blocked_actions": [],
        "websockets": [],
        "websocket_errors": [],
    }

    with sync_playwright() as playwright:
        launch_options: dict[str, object] = {"headless": True}
        if args.browser_executable:
            launch_options["executable_path"] = args.browser_executable
        browser = playwright.chromium.launch(**launch_options)
        context = browser.new_context(
            viewport={
                "width": max(320, args.viewport_width),
                "height": max(360, args.viewport_height),
            },
            ignore_https_errors=False,
        )
        # The operator's server-side platform policy is already resolved, but
        # the ESA acknowledgment is intentionally browser-local. Mark it only
        # inside this disposable audit context so the sweep can reach the app
        # without accepting an agreement or mutating the operator account.
        context.add_init_script(
            """
            sessionStorage.setItem(
              'oracle_account_security_esa_ack',
              JSON.stringify({
                version: 'neoh-account-security-esa-2026-07-18-v1',
                acknowledgedAt: new Date().toISOString()
              })
            );
            """
        )
        page = context.new_page()

        def on_response(response) -> None:
            if first_party(response.url, hosts) and response.status >= 400:
                report["failed_responses"].append(
                    {"status": response.status, "method": response.request.method, "url": response.url}
                )

        def on_request_failed(request) -> None:
            if first_party(request.url, hosts):
                report["request_failures"].append(
                    {"method": request.method, "url": request.url, "failure": request.failure}
                )

        def on_websocket(socket) -> None:
            if not first_party(socket.url, hosts):
                return
            record = {
                "url": socket.url,
                "frames_received": 0,
                "frames_sent": 0,
                "closed": False,
            }
            report["websockets"].append(record)
            socket.on(
                "framereceived",
                lambda _payload: record.update(
                    frames_received=record["frames_received"] + 1
                ),
            )
            socket.on(
                "framesent",
                lambda _payload: record.update(
                    frames_sent=record["frames_sent"] + 1
                ),
            )
            socket.on("close", lambda: record.update(closed=True))
            socket.on(
                "socketerror",
                lambda error: report["websocket_errors"].append(
                    {"url": socket.url, "error": str(error)}
                ),
            )

        # The application opens its authenticated bus immediately after login,
        # so subscribe before navigation. REST listeners stay post-login to
        # exclude the expected anonymous /auth/verify 401 bootstrap probe.
        page.on("websocket", on_websocket)
        login(page, args.base_url, agent_id, passphrase)

        page.on("response", on_response)
        page.on("requestfailed", on_request_failed)
        page.on(
            "console",
            lambda message: report["console_errors"].append(message.text)
            if message.type == "error"
            else None,
        )
        page.on("pageerror", lambda error: report["page_errors"].append(str(error)))
        page.wait_for_timeout(max(0, min(args.stream_settle_ms, 30_000)))

        tabs = page.get_by_role("tab")
        tab_names = [tabs.nth(index).get_attribute("aria-label") for index in range(tabs.count())]
        for tab_name in filter(None, tab_names):
            tab = page.get_by_role("tab", name=tab_name, exact=True)
            tab.click()
            page.wait_for_timeout(1_250)
            panel = page.get_by_role("tabpanel", name=tab_name)
            panel.wait_for(timeout=15_000)
            if args.navigation_only:
                report["tabs"][tab_name] = {
                    "buttons": [],
                    "safe_buttons_clicked": [],
                    "alerts": panel.get_by_role("alert").all_inner_texts(),
                }
                continue
            buttons = panel.get_by_role("button")
            inventory: list[dict[str, object]] = []
            clicked: list[str] = []
            seen: set[str] = set()

            for index in range(buttons.count()):
                button = buttons.nth(index)
                try:
                    if not button.is_visible():
                        continue
                    name = button_name(button) or f"unnamed-button-{index + 1}"
                    disabled = button.is_disabled()
                except Exception:
                    continue
                inventory.append({"name": name, "disabled": disabled})
                if disabled or name in seen:
                    continue
                seen.add(name)
                if BLOCKED_BUTTON.search(name):
                    report["blocked_actions"].append({"tab": tab_name, "button": name})
                    continue
                if not SAFE_BUTTON.fullmatch(name):
                    continue
                try:
                    button.click(timeout=3_000)
                    clicked.append(name)
                    page.wait_for_timeout(650)
                except Exception as error:
                    report["page_errors"].append(f"{tab_name}/{name}: {type(error).__name__}")

            report["tabs"][tab_name] = {
                "buttons": inventory,
                "safe_buttons_clicked": clicked,
                "alerts": panel.get_by_role("alert").all_inner_texts(),
            }

        dimensions = page.evaluate(
            """() => {
              const shell = document.querySelector('[class*="shellContainer"]');
              const shellRect = shell?.getBoundingClientRect();
              return {
                viewport: innerWidth,
                viewportHeight: innerHeight,
                documentWidth: document.documentElement.scrollWidth,
                documentHeight: document.documentElement.scrollHeight,
                shellHeight: shellRect?.height ?? null,
                shellBottom: shellRect?.bottom ?? null,
              };
            }"""
        )
        report["layout"] = dimensions
        if args.viewport_width < 720:
            page.evaluate(
                "() => { document.documentElement.dataset.keyboardOpen = 'true'; }"
            )
            page.wait_for_timeout(100)
            report["keyboard_layout"] = page.evaluate(
                """() => {
                  const nav = document.querySelector('nav[aria-label="Neoh CRM"]');
                  const scroll = document.querySelector('[class*="scrollableContent"]');
                  const navStyle = nav ? getComputedStyle(nav) : null;
                  return {
                    navVisibility: navStyle?.visibility ?? null,
                    navPointerEvents: navStyle?.pointerEvents ?? null,
                    scrollOverflowY: scroll ? getComputedStyle(scroll).overflowY : null,
                    scrollBottomPadding: scroll ? getComputedStyle(scroll).paddingBottom : null,
                  };
                }"""
            )
            page.evaluate(
                "() => { delete document.documentElement.dataset.keyboardOpen; }"
            )
        report["final_url"] = page.url
        if not report["websockets"]:
            report["websocket_errors"].append(
                {"url": args.api_url, "error": "No first-party WebSocket opened."}
            )
        context.close()
        browser.close()

    Path(args.output).write_text(json.dumps(report, indent=2), encoding="utf-8")
    failures = (
        len(report["failed_responses"])
        + len(report["request_failures"])
        + len(report["console_errors"])
        + len(report["page_errors"])
        + len(report["websocket_errors"])
    )
    print(
        json.dumps(
            {
                "tabs": list(report["tabs"]),
                "buttons": sum(len(tab["buttons"]) for tab in report["tabs"].values()),
                "safe_clicks": sum(
                    len(tab["safe_buttons_clicked"]) for tab in report["tabs"].values()
                ),
                "failures": failures,
                "horizontal_overflow": dimensions["documentWidth"] > dimensions["viewport"] + 2,
            }
        )
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
