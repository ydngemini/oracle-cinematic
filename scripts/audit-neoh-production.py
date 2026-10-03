#!/usr/bin/env python3
"""Authenticated, non-destructive UI sweep of a running Neoh — the CURRENT UI.

    NEOH_AUDIT_AGENT_ID=… NEOH_AUDIT_PASSPHRASE=… \
      python3 scripts/audit-neoh-production.py --base-url https://<env host> --output audit.json

It signs in, then visits what an agent actually uses:

  Home · Work (and each Work view: Recent, People, Properties, Deals,
  Conversations) · Neoh · and, when the account is a platform admin, Admin
  (reached through the profile sheet — it is not a top-level tab).

It records failed first-party requests, browser errors, whether the realtime
WebSocket opened, horizontal overflow, and stale product names, and answers
PASS / WARN / BLOCKED for each, like scripts/neoh-launch-readiness.py. The
readiness script checks the platform; this checks that a person can use it.

Elements are found by ROLE and accessible NAME, never by CSS class or visible
copy, so wording and styling can change without breaking the sweep:
  nav "Neoh CRM" → tab "Home" | "Work" | "Neoh"     (CrmShell.jsx TABS, TabBar.jsx)
  tabpanel labelled by the active tab
  Work: tablist "Kind" → tab "Recent" | "People" | …  (UniversalWorkspace.jsx)
  button "Open agent profile and settings" → nav "Profile and administration"
    → button "Admin"                                  (platform admins only)

NOTHING is sent, approved, deleted, connected, purchased or uploaded: only
navigation and a short allow-list of read-only controls are clicked
(--navigation-only clicks none). Credentials come from the environment and
are never written to the report. There is deliberately no default URL —
an audit must name the environment it audits.

Exit code: 2 if anything is BLOCKED, 1 if only WARNs, 0 if all PASS.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

PASS, WARN, BLOCKED = "PASS", "WARN", "BLOCKED"

DESTINATIONS = ("Home", "Work", "Neoh")
WORK_VIEWS = ("Recent", "People", "Properties", "Deals", "Conversations")
# Visible product names that should no longer appear to an agent (mission §28).
STALE_IDENTITIES = re.compile(r"\b(ORCL|JARVIS|AI Closer|Mission Control)\b")

SAFE_BUTTON = re.compile(
    r"^(refresh|retry|close|back|clear|clear search|all|buyers?|sellers?|leads?|clients?|"
    r"active|archived)$",
    re.IGNORECASE,
)
BLOCKED_BUTTON = re.compile(
    r"(stripe|billing|subscribe|checkout|pay|purchase|send|queue|call|text|"
    r"approve|reject|delete|remove|archive|submit|save|create|add|new|invite|"
    r"connect|disconnect|sign out|logout|upload|download|execute|run|import|"
    r"rerun|generate|draft|offer|contract|erase|close account|offboard)",
    re.IGNORECASE,
)
# The SPA probes its session before login; that 401 is expected, not a fault.
EXPECTED_STATUS = {("/auth/verify", 401)}


def check(id_: str, title: str, status: str, reason: str) -> dict:
    return {"id": id_, "title": title, "status": status, "reason": reason}


def classify(report: dict) -> list[dict]:
    """Turn the raw sweep into PASS/WARN/BLOCKED checks. Pure — unit-tested."""
    out: list[dict] = []
    if report.get("login_error"):
        out.append(check("login", "Sign in", BLOCKED, report["login_error"]))
        return out
    out.append(check("login", "Sign in", PASS, "signed in and the Home tab appeared"))

    for name in DESTINATIONS:
        d = (report.get("destinations") or {}).get(name)
        if d is None:
            out.append(check(f"open:{name}", f"Open {name}", BLOCKED, "tab not found in the Neoh CRM navigation"))
        elif d.get("error"):
            out.append(check(f"open:{name}", f"Open {name}", BLOCKED, d["error"]))
        else:
            alerts = d.get("alerts") or []
            out.append(check(f"open:{name}", f"Open {name}", WARN if alerts else PASS,
                             f"opened; {len(alerts)} alert(s): {'; '.join(alerts)[:200]}" if alerts else "opened"))

    for name in WORK_VIEWS:
        v = (report.get("work_views") or {}).get(name)
        if v is None:
            out.append(check(f"work:{name}", f"Work → {name}", WARN, "view chip not found"))
        elif v.get("error"):
            out.append(check(f"work:{name}", f"Work → {name}", WARN, v["error"]))
        else:
            out.append(check(f"work:{name}", f"Work → {name}", PASS, "opened"))

    admin = report.get("admin") or {}
    if admin.get("skipped"):
        out.append(check("admin", "Admin (operator)", PASS, f"skipped: {admin['skipped']}"))
    elif admin.get("error"):
        out.append(check("admin", "Admin (operator)", WARN, admin["error"]))
    else:
        alerts = admin.get("alerts") or []
        out.append(check("admin", "Admin (operator)", WARN if alerts else PASS,
                         f"opened; {len(alerts)} alert(s)" if alerts else "opened"))

    failed = report.get("failed_responses") or []
    server = [f for f in failed if f["status"] >= 500]
    client = [f for f in failed if f["status"] < 500]
    out.append(check("server_errors", "No 5xx from Neoh", BLOCKED if server else PASS,
                     f"{len(server)} server error(s): " + ", ".join(f"{f['status']} {f['path']}" for f in server[:6])
                     if server else "none"))
    out.append(check("client_errors", "No unexpected 4xx", WARN if client else PASS,
                     f"{len(client)}: " + ", ".join(f"{f['status']} {f['path']}" for f in client[:8])
                     if client else "none"))
    rf = report.get("request_failures") or []
    out.append(check("request_failures", "No failed requests", WARN if rf else PASS,
                     f"{len(rf)} request(s) failed to complete" if rf else "none"))
    pe = report.get("page_errors") or []
    out.append(check("page_errors", "No uncaught page errors", BLOCKED if pe else PASS,
                     f"{len(pe)}: {'; '.join(pe)[:240]}" if pe else "none"))
    ce = report.get("console_errors") or []
    out.append(check("console_errors", "No console errors", WARN if ce else PASS,
                     f"{len(ce)}: {'; '.join(ce)[:240]}" if ce else "none"))
    ws = report.get("websockets") or []
    out.append(check("realtime", "Realtime connection opened",
                     PASS if ws and not report.get("websocket_errors") else BLOCKED,
                     f"{len(ws)} socket(s), {sum(w.get('frames_received', 0) for w in ws)} frame(s) received"
                     if ws else "no first-party WebSocket opened — live updates and Neoh voice cannot work"))
    over = report.get("overflow") or {}
    bad = [k for k, v in over.items() if v]
    out.append(check("overflow", "No horizontal overflow", WARN if bad else PASS,
                     f"overflows on: {', '.join(bad)}" if bad else "none"))
    stale = sorted(set(report.get("stale_identities") or []))
    out.append(check("identity", "Product says Neoh", WARN if stale else PASS,
                     f"visible stale names: {', '.join(stale)}" if stale else "no stale product names visible"))
    return out


def verdict(checks: list[dict]) -> str:
    st = {c["status"] for c in checks}
    return BLOCKED if BLOCKED in st else (WARN if WARN in st else PASS)


def exit_code(checks: list[dict]) -> int:
    return {PASS: 0, WARN: 1, BLOCKED: 2}[verdict(checks)]


# ── Browser sweep ──────────────────────────────────────────────────────────

def _login(page, base_url: str, agent_id: str, passphrase: str, otp: str) -> str | None:
    """None on success, else a one-line reason."""
    from playwright.sync_api import TimeoutError as PwTimeout

    home = page.get_by_role("navigation", name="Neoh CRM").get_by_role("tab", name="Home", exact=True)
    page.goto(base_url, wait_until="domcontentloaded", timeout=60_000)
    try:
        home.wait_for(timeout=6_000)
        return None
    except PwTimeout:
        pass
    try:
        page.get_by_label("Email or Agent ID").fill(agent_id, timeout=15_000)
        page.get_by_label("Passphrase").fill(passphrase)
        page.get_by_role("button", name="Authenticate").click()
        code = page.get_by_label("Sign-in code")
        try:
            home.wait_for(timeout=12_000)
            return None
        except PwTimeout:
            if not code.is_visible():
                raise
        if not otp:
            return "the account needs a sign-in code — set NEOH_AUDIT_OTP (TOTP or emailed code)"
        code.fill(otp)
        page.get_by_role("button", name="Authenticate").click()
        home.wait_for(timeout=15_000)
        return None
    except Exception as exc:  # noqa: BLE001 — report what the page showed, never the credentials
        alerts = page.get_by_role("alert").all_inner_texts()[:3]
        return f"the Home tab never appeared ({type(exc).__name__}); url={urlparse(page.url).path!r} alerts={alerts!r}"


def _alerts(panel) -> list[str]:
    try:
        return [" ".join(t.split())[:160] for t in panel.get_by_role("alert").all_inner_texts()]
    except Exception:  # noqa: BLE001
        return []


def _overflow(page) -> bool:
    return bool(page.evaluate("() => document.documentElement.scrollWidth > innerWidth + 2"))


def _click_safe_buttons(page, panel, where: str, report: dict) -> None:
    buttons = panel.get_by_role("button")
    seen: set[str] = set()
    for i in range(min(buttons.count(), 60)):
        b = buttons.nth(i)
        try:
            if not b.is_visible() or b.is_disabled():
                continue
            name = " ".join((b.get_attribute("aria-label") or b.inner_text(timeout=1_000) or "").split())
        except Exception:  # noqa: BLE001
            continue
        if not name or name in seen:
            continue
        seen.add(name)
        if BLOCKED_BUTTON.search(name):
            report["not_clicked"].append({"where": where, "button": name})
            continue
        if SAFE_BUTTON.fullmatch(name):
            try:
                b.click(timeout=3_000)
                page.wait_for_timeout(500)
                report["clicked"].append({"where": where, "button": name})
            except Exception as exc:  # noqa: BLE001
                report["page_errors"].append(f"{where}/{name}: {type(exc).__name__}")


def sweep(args, agent_id: str, passphrase: str, otp: str) -> dict:
    from playwright.sync_api import sync_playwright

    host = urlparse(args.base_url).hostname
    report: dict[str, Any] = {
        "base_url": args.base_url, "viewport": [args.viewport_width, args.viewport_height],
        "destinations": {}, "work_views": {}, "admin": {}, "overflow": {},
        "failed_responses": [], "request_failures": [], "console_errors": [], "page_errors": [],
        "websockets": [], "websocket_errors": [], "clicked": [], "not_clicked": [],
        "stale_identities": [],
    }

    def first_party(url: str) -> bool:
        return urlparse(url).hostname == host

    with sync_playwright() as pw:
        opts: dict[str, Any] = {"headless": True}
        if args.browser_executable:
            opts["executable_path"] = args.browser_executable
        browser = getattr(pw, args.browser).launch(**opts)
        context = browser.new_context(viewport={"width": max(320, args.viewport_width),
                                                "height": max(360, args.viewport_height)})
        page = context.new_page()

        def on_ws(sock):
            if not first_party(sock.url):
                return
            rec = {"path": urlparse(sock.url).path, "frames_received": 0, "closed": False}
            report["websockets"].append(rec)
            sock.on("framereceived", lambda _p: rec.update(frames_received=rec["frames_received"] + 1))
            sock.on("close", lambda: rec.update(closed=True))
            sock.on("socketerror", lambda e: report["websocket_errors"].append(str(e)[:200]))

        page.on("websocket", on_ws)
        err = _login(page, args.base_url, agent_id, passphrase, otp)
        if err:
            report["login_error"] = err
            context.close()
            browser.close()
            return report

        def on_response(resp):
            if not first_party(resp.url) or resp.status < 400:
                return
            path = urlparse(resp.url).path
            if (path, resp.status) in EXPECTED_STATUS:
                return
            report["failed_responses"].append({"status": resp.status, "method": resp.request.method, "path": path})

        page.on("response", on_response)
        page.on("requestfailed", lambda req: report["request_failures"].append(
            {"method": req.method, "path": urlparse(req.url).path}) if first_party(req.url) else None)
        page.on("console", lambda m: report["console_errors"].append(m.text[:300]) if m.type == "error" else None)
        page.on("pageerror", lambda e: report["page_errors"].append(str(e)[:300]))
        page.wait_for_timeout(max(0, min(args.settle_ms, 30_000)))

        nav = page.get_by_role("navigation", name="Neoh CRM")
        for name in DESTINATIONS:
            tab = nav.get_by_role("tab", name=name, exact=True)
            if tab.count() == 0:
                continue
            try:
                tab.click()
                panel = page.get_by_role("tabpanel", name=name)
                panel.wait_for(timeout=15_000)
                page.wait_for_timeout(1_200)
                report["destinations"][name] = {"alerts": _alerts(panel)}
                report["overflow"][name] = _overflow(page)
                text = panel.inner_text(timeout=5_000)
                report["stale_identities"] += STALE_IDENTITIES.findall(text)
                if name == "Work":
                    kinds = panel.get_by_role("tablist", name="Kind")
                    for view in WORK_VIEWS:
                        chip = kinds.get_by_role("tab", name=re.compile(rf"^{view}\b"))
                        if chip.count() == 0:
                            continue
                        try:
                            chip.first.click()
                            page.wait_for_timeout(1_000)
                            report["work_views"][view] = {"alerts": _alerts(panel)}
                            report["overflow"][f"Work/{view}"] = _overflow(page)
                            if not args.navigation_only:
                                _click_safe_buttons(page, panel, f"Work/{view}", report)
                        except Exception as exc:  # noqa: BLE001
                            report["work_views"][view] = {"error": f"{type(exc).__name__} opening the view"}
                elif not args.navigation_only:
                    _click_safe_buttons(page, panel, name, report)
            except Exception as exc:  # noqa: BLE001
                report["destinations"][name] = {"error": f"{type(exc).__name__}: panel did not appear"}

        # Admin lives in the profile sheet, for platform admins only.
        try:
            page.get_by_role("button", name="Open agent profile and settings").click(timeout=5_000)
            admin_btn = page.get_by_role("navigation", name="Profile and administration") \
                .get_by_role("button", name="Admin", exact=True)
            try:
                admin_btn.wait_for(timeout=4_000)
            except Exception:  # noqa: BLE001
                report["admin"] = {"skipped": "this account is not a platform admin"}
            else:
                admin_btn.click()
                page.wait_for_timeout(2_500)
                sheet = page.locator("#agent-profile-sheet")
                report["admin"] = {"alerts": _alerts(sheet)}
                report["overflow"]["Admin"] = _overflow(page)
            page.get_by_role("button", name="Close profile").click(timeout=3_000)
        except Exception as exc:  # noqa: BLE001
            report["admin"] = {"error": f"{type(exc).__name__}: the profile sheet did not open"}

        context.close()
        browser.close()
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description="Non-destructive UI sweep of a running Neoh.")
    ap.add_argument("--base-url", required=True, help="the environment to audit, e.g. https://<staging host>")
    ap.add_argument("--output", required=True, help="JSON report path")
    ap.add_argument("--browser", choices=["chromium", "firefox", "webkit"], default="chromium")
    ap.add_argument("--browser-executable", default=os.environ.get("NEOH_AUDIT_BROWSER_EXECUTABLE"))
    ap.add_argument("--viewport-width", type=int, default=1440)
    ap.add_argument("--viewport-height", type=int, default=1000)
    ap.add_argument("--navigation-only", action="store_true", help="open views only; click no in-panel controls")
    ap.add_argument("--settle-ms", type=int, default=5_000, help="wait for realtime state after sign-in")
    args = ap.parse_args()
    if not re.match(r"^https://|^http://(localhost|127\.0\.0\.1)(:\d+)?", args.base_url):
        ap.error("--base-url must be https:// (or http://localhost for a local stack)")

    agent_id = os.environ.get("NEOH_AUDIT_AGENT_ID", "")
    passphrase = os.environ.get("NEOH_AUDIT_PASSPHRASE", "")
    if not agent_id or not passphrase:
        raise SystemExit("NEOH_AUDIT_AGENT_ID and NEOH_AUDIT_PASSPHRASE are required")

    report = sweep(args, agent_id, passphrase, os.environ.get("NEOH_AUDIT_OTP", ""))
    checks = classify(report)
    out = {"summary": {"verdict": verdict(checks),
                       **{s: sum(c["status"] == s for c in checks) for s in (PASS, WARN, BLOCKED)}},
           "checks": checks, "raw": report}
    text = json.dumps(out, indent=2)
    for secret in (passphrase, os.environ.get("NEOH_AUDIT_OTP", "")):
        if secret:
            text = text.replace(secret, "[redacted]")
    Path(args.output).write_text(text + "\n", encoding="utf-8")
    for c in checks:
        print(f"  {c['status']:<7} {c['title']}: {c['reason']}")
    print(f"VERDICT: {out['summary']['verdict']}  ({args.base_url})")
    return exit_code(checks)


if __name__ == "__main__":
    raise SystemExit(main())
