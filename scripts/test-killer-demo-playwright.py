#!/usr/bin/env python3
"""The killer demo, end to end, in a clean browser, against the real product.

    python scripts/test-killer-demo-playwright.py --base-url https://… \
        --tenant-id <demo tenant uuid> [--reset] [--real-call] [--label rehearsal-1]

Every step is the presenter's: sign in as Jordan, read Home, open the
property, open its Neoh Space, ask Neoh "Who should I call about this?",
open Sarah, ask Neoh to text her, approve it in the approvals queue, read the
receipt, read Sarah's timeline. Nothing is injected: no localStorage, no
pre-existing session, no database writes during the run (--reset runs the
reset tool BEFORE the browser opens).

The deterministic part ends at the approved text's receipt. Texting is not set
up on staging (no registered number), so the product refuses the send before
any carrier request, with its own reason — that refusal IS the expected
receipt. --real-call adds the controlled live step: ask Neoh to call Sarah and
approve it, which places a REAL call to the only allowlisted number, the
operator's own phone (+13024078981). Off by default.

Timings (seconds) are recorded for the §92 measures: Home load, property open,
Space interactive, Neoh response, action completion.

Browsers: system Chrome via Playwright (PLAYWRIGHT_BROWSERS_PATH and TMPDIR on
the data disk; never the root disk).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import demo_tenant_common as common  # noqa: E402

OUT = common.REPO / "performance" / "out" / "demo"
QUESTION = "Who should I call about this?"
TEXT_ASK = ("Text Sarah about 123 Main Street — it has the updated kitchen and a den "
            "she could use as her office. Keep it short.")
CALL_ASK = "Call Sarah about 123 Main Street — she may want a showing."


class Step:
    def __init__(self, results: dict, name: str):
        self.results, self.name = results, name

    def __enter__(self):
        self.t0 = time.time()
        print(f"→ {self.name}", flush=True)
        return self

    def __exit__(self, exc_type, exc, tb):
        took = round(time.time() - self.t0, 2)
        self.results["steps"].append({"step": self.name, "ok": exc is None, "seconds": took,
                                      **({"error": f"{exc_type.__name__}: {str(exc)[:300]}"} if exc else {})})
        print(f"   {'ok' if exc is None else 'FAILED'} in {took}s" + (f" — {exc}" if exc else ""), flush=True)
        return False


def latest_assistant(page, after: int):
    """Text of the newest completed Neoh message once there are more than `after`."""
    sel = 'article[data-role="assistant"]'
    deadline = time.time() + 150
    while time.time() < deadline:
        items = page.locator(sel)
        n = items.count()
        if n > after:
            last = items.nth(n - 1)
            status = last.get_attribute("data-status") or ""
            text = last.inner_text()
            if status in ("completed", "failed", "error") and text.strip():
                return status, text
        page.wait_for_timeout(500)
    raise TimeoutError("Neoh did not finish answering within 150 s")


def ask(page, text: str) -> tuple[str, str, float]:
    before = page.locator('article[data-role="assistant"]').count()
    page.keyboard.press("/")
    field = page.locator("textarea:visible").last
    field.wait_for(state="visible", timeout=10_000)
    field.fill(text)
    t0 = time.time()
    field.press("Enter")
    status, answer = latest_assistant(page, before)
    return status, answer, round(time.time() - t0, 2)


def wait_receipt(page, pattern: str, timeout_s: float = 90) -> str:
    deadline = time.time() + timeout_s
    rx = re.compile(pattern, re.I)
    while time.time() < deadline:
        body = page.inner_text("body")
        m = rx.search(body)
        if m:
            return m.group(0)
        page.wait_for_timeout(1000)
    raise TimeoutError(f"no receipt matching /{pattern}/ within {timeout_s} s")


def approve_first_pending(page, kind_word: str) -> None:
    """In the approvals queue, approve the newest pending request of this kind."""
    # "Review" lands on Work → Automations; the queue is a collapsed section.
    queue = page.get_by_role("button", name=re.compile(r"^Waiting for your approval"))
    queue.first.wait_for(timeout=45_000)
    if not page.get_by_role("button", name="Approve").first.is_visible():
        queue.first.click()
    page.get_by_role("button", name="Approve").first.wait_for(timeout=20_000)
    page.get_by_role("button", name="Approve").first.scroll_into_view_if_needed()
    page.screenshot(path=str(OUT / "e2e" / "last-review.png"))
    cards = page.locator("li, article, section").filter(has=page.get_by_role("button", name="Approve"))
    target = cards.filter(has_text=re.compile(kind_word, re.I)).last
    if target.count() == 0:
        target = cards.last
    target.get_by_role("button", name="Approve").click()


def run(args, results: dict) -> dict:
    from playwright.sync_api import sync_playwright

    common.refuse_production(args.base_url)
    results.update({"label": args.label, "base_url": args.base_url, "steps": [], "console_errors": [],
               "observations": {}})
    if args.reset:
        with Step(results, "reset demo tenant"):
            subprocess.run([sys.executable, str(Path(__file__).with_name("reset-demo-tenant.py")),
                            "--tenant-id", args.tenant_id, "--base-url", args.base_url, "--execute"],
                           check=True, stdout=subprocess.DEVNULL)

    creds = common.load_or_create_credentials()
    shots = OUT / "e2e" / args.label
    shots.mkdir(parents=True, exist_ok=True)
    obs = results["observations"]

    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=args.chrome, headless=not args.headed,
            args=["--enable-gpu", "--ignore-gpu-blocklist", "--use-angle=default"])
        # A brand-new context = a clean profile: no storage, no cookies.
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        page.on("console", lambda m: m.type == "error" and results["console_errors"].append(m.text[:300]))
        page.on("pageerror", lambda e: results["console_errors"].append(f"pageerror: {str(e)[:300]}"))

        with Step(results, "login + Home shows the opportunity") as s:
            page.goto(args.base_url + "/", wait_until="domcontentloaded")
            page.fill("#agent-id", common.OWNER["email"])
            page.fill("#passphrase", creds[common.OWNER["email"]])
            t0 = time.time()
            page.get_by_role("button", name="Sign in").click()
            page.get_by_text("May fit Sarah Johnson").wait_for(timeout=45_000)
            obs["home_load_s"] = round(time.time() - t0, 2)
            page.keyboard.press("Escape")  # the first-visit walkthrough
            page.screenshot(path=str(shots / "01-home.png"))

        with Step(results, "open the property from Home"):
            t0 = time.time()
            page.get_by_role("button", name="123 Main Street, Wilmington", exact=True).click()
            page.get_by_text("Buyers who may fit").wait_for(timeout=30_000)
            page.get_by_text("$499,000").first.wait_for(timeout=10_000)
            obs["property_open_s"] = round(time.time() - t0, 2)
            assert "Sarah Johnson" in page.inner_text("body")
            page.screenshot(path=str(shots / "02-property.png"))

        with Step(results, "Neoh Space opens (labelled demo space)"):
            t0 = time.time()
            page.get_by_role("button", name=re.compile("demo 3D space", re.I)).click()
            page.locator("canvas").first.wait_for(state="visible", timeout=60_000)
            page.screenshot(path=str(shots / "03a-space-loading.png"))
            page.get_by_text(re.compile("not this home|demo space", re.I)).filter(visible=True).first.wait_for(timeout=30_000)
            # Interactive: the viewer reports ready or the canvas has been drawn
            # for two seconds without an error message appearing.
            page.wait_for_timeout(2000)
            body = page.inner_text("body")
            # The viewer's own status, not a guess at its wording: the old
            # regex missed "could not be loaded", so a Space that never loaded
            # on staging passed every rehearsal (2026-10-04..06).
            page.wait_for_function(
                "() => ['ready', 'error'].includes(document.querySelector('[data-space-status]')?.dataset.spaceStatus)",
                timeout=60_000)
            status = page.evaluate(
                "() => document.querySelector('[data-space-status]')?.dataset.spaceStatus || null")
            if status != "ready" or re.search(
                    r"couldn.t load|could not be loaded|failed to load|unavailable on this device", body, re.I):
                raise AssertionError(f"the Space viewer did not become ready (status={status})")
            obs["space_interactive_s"] = round(time.time() - t0, 2)
            page.screenshot(path=str(shots / "03-space.png"))
            page.keyboard.press("Escape")
            page.wait_for_timeout(800)
            if not page.get_by_text("Buyers who may fit").is_visible():
                page.get_by_role("button", name=re.compile("close|exit|back", re.I)).first.click()
                page.get_by_text("Buyers who may fit").wait_for(timeout=15_000)

        with Step(results, "ask Neoh: who should I call about this?"):
            status, answer, took = ask(page, QUESTION)
            obs["neoh_response_s"] = took
            obs["neoh_answer"] = answer[:1500]
            page.screenshot(path=str(shots / "04-who-to-call.png"))
            assert status == "completed", f"Neoh answered with status {status}: {answer[:200]}"
            assert "Sarah" in answer, "Neoh did not name Sarah"
            assert re.search(r"525|budget|Wilmington|bedroom", answer, re.I), "no evidence in the answer"

        with Step(results, "open Sarah and ask Neoh to text her"):
            page.keyboard.press("Escape")
            page.wait_for_timeout(500)
            if not page.get_by_text("Buyers who may fit").is_visible():
                page.get_by_role("button", name="123 Main Street, Wilmington", exact=True).click()
                page.get_by_text("Buyers who may fit").wait_for(timeout=30_000)
            page.locator("section[aria-label='Buyers who may fit']").get_by_role("button", name="Sarah Johnson", exact=True).click()
            page.wait_for_url(re.compile(r"/p/"), timeout=20_000)
            page.wait_for_timeout(1500)
            status, answer, took = ask(page, TEXT_ASK)
            obs["text_proposal_s"] = took
            obs["text_proposal"] = answer[:1200]
            receipt = wait_receipt(page, r"Text (waiting for your approval|approved)[^\n]*", 60)
            obs["text_receipt_staged"] = receipt
            page.screenshot(path=str(shots / "05-text-proposed.png"))

        with Step(results, "approve the text in the approvals queue"):
            t0 = time.time()
            page.get_by_role("button", name="Review").last.click()
            approve_first_pending(page, "text|sms")
            page.wait_for_timeout(1500)
            page.screenshot(path=str(shots / "06-approved.png"))
            obs["text_approval_click_s"] = round(time.time() - t0, 2)

        with Step(results, "the text's receipt says what actually happened"):
            t0 = time.time()
            # The conversation, with its receipts, lives on the Neoh tab.
            page.get_by_role("tab", name="Neoh", exact=True).click()
            outcome = wait_receipt(
                page, r"(Text sent|Text not sent|Text didn.t go through|Text needs review)[^\n]*\n?[^\n]*", 120)
            obs["text_receipt_final"] = outcome
            obs["action_completion_s"] = round(time.time() - t0, 2)
            page.screenshot(path=str(shots / "07-text-receipt.png"))

        if args.real_call:
            with Step(results, "REAL call: ask Neoh to call Sarah, approve it"):
                page.get_by_role("tab", name="Home", exact=True).click()
                page.get_by_role("button", name="123 Main Street, Wilmington", exact=True).click()
                page.get_by_text("Buyers who may fit").wait_for(timeout=30_000)
                page.locator("section[aria-label='Buyers who may fit']").get_by_role("button", name="Sarah Johnson", exact=True).click()
                page.wait_for_url(re.compile(r"/p/"), timeout=20_000)
                page.wait_for_timeout(1500)
                status, answer, took = ask(page, CALL_ASK)
                obs["call_proposal"] = answer[:800]
                wait_receipt(page, r"Call (waiting for your approval|approved)[^\n]*", 60)
                page.get_by_role("button", name="Review").last.click()
                approve_first_pending(page, "call")
                t0 = time.time()
                page.get_by_role("tab", name="Neoh", exact=True).click()
                obs["call_receipt"] = wait_receipt(
                    page, r"(Call placed|Placing call…|Call not sent|Call didn.t go through|Call needs review)[^\n]*\n?[^\n]*", 150)
                obs["call_submit_s"] = round(time.time() - t0, 2)
                page.screenshot(path=str(shots / "08-call-receipt.png"))

        with Step(results, "Sarah's timeline shows the outreach"):
            page.goto(args.base_url + f"/p/{_sarah_id(args)}", wait_until="domcontentloaded")
            page.get_by_role("tab", name=re.compile("^Timeline", re.I)).click(timeout=20_000)
            page.wait_for_timeout(3000)
            body = page.inner_text("body")
            # A real call's carrier outcome arrives by status callback once it ends.
            deadline = time.time() + (90 if args.real_call else 0)
            while args.real_call and "Call placed" not in body and time.time() < deadline:
                page.reload(wait_until="domcontentloaded")
                page.get_by_role("tab", name=re.compile("^Timeline", re.I)).click(timeout=20_000)
                page.wait_for_timeout(5000)
                body = page.inner_text("body")
            obs["timeline_excerpt"] = re.findall(
                r"(?:Text[^\n]{0,80}|Call[^\n]{0,80}|Showing[^\n]{0,80}|Note[^\n]{0,60})", body)[:12]
            page.screenshot(path=str(shots / "09-timeline.png"), full_page=True)

        browser.close()
    results["ok"] = all(s["ok"] for s in results["steps"])
    return results


def _sarah_id(args) -> str:
    state = common.load_state()
    return (state.get("clients") or {}).get("sarah", "")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--real-call", action="store_true",
                        help="place ONE real call to the allowlisted demo phone")
    parser.add_argument("--label", default="run")
    parser.add_argument("--chrome", default=os.getenv("CHROME", "/usr/bin/google-chrome"))
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args(argv)
    results: dict = {}
    try:
        run(args, results)
    except Exception as exc:  # noqa: BLE001 — the step already recorded it; keep the evidence
        results["ok"] = False
        results["aborted"] = f"{type(exc).__name__}: {str(exc)[:400]}"
    (OUT / "e2e").mkdir(parents=True, exist_ok=True)
    out = OUT / "e2e" / f"{args.label}.json"
    out.write_text(json.dumps(results, indent=2))
    print(json.dumps({k: results.get(k) for k in ("ok", "aborted", "observations")}, indent=2)[:5000])
    print("console errors:", len(results.get("console_errors") or []))
    return 0 if results["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
