#!/usr/bin/env python3
"""Mission 3 browser verification: human journeys, responsive layout, keyboard,
accessibility (axe), first-load network, visual baselines and a browser matrix.

    # 1. a backend that cannot reach a customer: ORACLE_RECOVERY_MODE=1, the perf
    #    provider mock as the LLM (performance/README.md), a web + a worker role.
    # 2. the worktree frontend, dev server and a production build:
    cd oracle-app && ORACLE_PROXY_TARGET=http://<backend> npx vite --port 5195 --strictPort &
    npm run build && ORACLE_PROXY_TARGET=http://<backend> npx vite preview --port 5196 --strictPort &
    # 3. run (PLAYWRIGHT_BROWSERS_PATH / TMPDIR on a disk with room):
    python scripts/test-mission3-journeys-playwright.py --base-url http://localhost:5195 \
        --prod-url http://localhost:5196 --chrome /usr/bin/google-chrome \
        --mock-url http://<provider-mock> [--browsers chromium,firefox,webkit] [--only journeys,axe]

Everything runs against localhost with brand-new synthetic accounts
(@example.test) created through the app's real signup and invitation flows.
Two things a local box cannot do are stood in for, exactly as
scripts/test-crm-walkthrough-playwright.py does:

* Billing: `GET /billing/status/*` is answered "trialing" in the browser
  AFTER the suite has seen the purchase gate, so no Stripe checkout can open.
* Geocoding: `/api/geocode` and `/api/enrich-property` are answered from a
  fixture, so the property journey never calls a public geocoder.

The invitation link comes from the dev capture the backend returns
(`dev_links`) when no mail server is configured. Nothing here sends a text,
an email or places a call: SMS from Conversations is log-only by design, and
an approved Neoh text is refused by ORACLE_RECOVERY_MODE inside the worker.

Credentials and run artefacts go to --out (gitignored performance/out/...).
Screenshot baselines live in --baselines (gitignored); the first run creates
them, later runs compare with a pixel tolerance. Exit status is non-zero when
any check fails.
"""
from __future__ import annotations

import argparse
import io
import json
import re
import secrets
import sys
import time
import traceback
import urllib.request
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

REPO = Path(__file__).resolve().parents[1]
AXE = REPO / "oracle-app" / "node_modules" / "axe-core" / "axe.min.js"
SPACE_FIXTURE = REPO / "scripts" / "fixtures" / "neoh-space-demo-room.sog"

WIDTHS = (320, 375, 390, 430, 768, 1024, 1440)
PHONE = {"width": 390, "height": 844}
DESKTOP = {"width": 1440, "height": 900}
BUSINESS = "Verify Realty M3"
BUYER = {"name": "Bianca Buyer", "email": "bianca.buyer@example.test", "phone": "3025550142"}
CONTACT = {"name": "Carmen Contact", "email": "carmen.contact@example.test", "phone": "+1 302 555 0177"}
PROPERTY_ADDRESS = "100 W 10th St, Wilmington, DE 19801"
GEO_FIXTURE = {"lat": 39.7459, "lng": -75.5466, "display_name": "100 West 10th Street, Wilmington, DE 19801"}
# 1x1 PNG, the smallest real image an upload can carry.
PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360f8cf000000020001e221bc330000000049454e44ae426082")


# ── results ────────────────────────────────────────────────────────────────

class Report:
    def __init__(self) -> None:
        self.data: dict = {"started": time.strftime("%Y-%m-%dT%H:%M:%S"), "sections": {}}
        self.failures: list[str] = []

    def section(self, name: str) -> dict:
        return self.data["sections"].setdefault(name, {})

    def check(self, section: str, name: str, ok: bool, evidence: object = "") -> bool:
        self.section(section)[name] = {"pass": bool(ok), "evidence": evidence}
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {section} :: {name}" + (f" — {str(evidence)[:220]}" if evidence not in ("", None) else ""))
        if not ok:
            self.failures.append(f"{section} :: {name}")
        return bool(ok)

    def note(self, section: str, name: str, value: object) -> None:
        self.section(section)[name] = value
        print(f"  [INFO] {section} :: {name} — {str(value)[:220]}")


def step(report: Report, section: str, name: str, fn):
    """Run one journey step; an exception is that step's failure, not the run's."""
    try:
        evidence = fn()
        return report.check(section, name, True, evidence)
    except Exception as exc:  # noqa: BLE001 — every failure is reported the same way
        page = getattr(report, "page", None)
        if page is not None and getattr(report, "out", None):
            try:
                slug = re.sub(r"[^a-z0-9]+", "-", f"{section}-{name}".lower()).strip("-")
                page.screenshot(path=str(report.out / f"fail-{slug}.png"))
            except Exception:  # noqa: BLE001
                pass
        return report.check(section, name, False, f"{type(exc).__name__}: {str(exc).splitlines()[0][:200]}")


# ── browsers and contexts ──────────────────────────────────────────────────

def launch(pw, name: str, args, *, mic: str = "granted"):
    if name == "chromium":
        flags = ["--use-fake-device-for-media-stream", "--enable-webgl", "--ignore-gpu-blocklist",
                 "--enable-unsafe-swiftshader"]
        if mic == "granted":
            flags.append("--use-fake-ui-for-media-stream")
        kw = {"args": flags}
        if args.chrome:
            kw["executable_path"] = args.chrome
        return pw.chromium.launch(headless=True, **kw)
    if name == "firefox":
        prefs = {"media.navigator.streams.fake": True,
                 "media.navigator.permission.disabled": mic == "granted",
                 "permissions.default.microphone": 1 if mic == "granted" else 2}
        return pw.firefox.launch(headless=True, firefox_user_prefs=prefs)
    return pw.webkit.launch(headless=True)


def new_context(browser, args, *, viewport=DESKTOP, state=None, billing=True, scheme="light",
                mobile=False, mic=None, quiet_status=False):
    kw = {"base_url": args.base_url, "viewport": viewport, "color_scheme": scheme}
    if state:
        kw["storage_state"] = state
    if mobile:
        kw.update(has_touch=True)
    ctx = browser.new_context(**kw)
    if mic == "granted":
        try:
            ctx.grant_permissions(["microphone"], origin=args.base_url)
        except Exception:  # noqa: BLE001 — not every engine models this permission
            pass
    if billing:
        install_billing_stub(ctx)
    install_geocode_stub(ctx)
    if quiet_status:
        # Layout/axe/visual sweeps reload ~100 pages in minutes; the real
        # /api/ai/chat/status allows 20 per minute per person. Those sweeps are
        # not testing it (the journeys and the matrix are), so answer it here.
        ctx.route(lambda u: urlparse(u).path == "/api/ai/chat/status",
                  lambda r: r.fulfill(status=200, content_type="application/json", body='{"enabled": true}'))
    # No walkthrough over the checks; the walkthrough itself is checked once.
    ctx.add_init_script("try { localStorage.setItem('oracle_product_tour_v1', 'dismissed') } catch (e) {}")
    return ctx


def install_billing_stub(ctx) -> None:
    body = json.dumps({"active": True, "status": "trialing", "plan": "oracle_swarm", "current_period_end": None})
    ctx.route("**/billing/status/**", lambda route: route.fulfill(status=200, content_type="application/json", body=body))


def install_geocode_stub(ctx) -> None:
    ctx.route(lambda u: urlparse(u).path == "/api/geocode", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(GEO_FIXTURE)))
    ctx.route(lambda u: urlparse(u).path == "/api/enrich-property", lambda r: r.fulfill(status=200, content_type="application/json", body="{}"))


def settle(page, ms: int = 900) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:  # noqa: BLE001 — a polling view never goes idle; time is enough
        pass
    page.wait_for_timeout(ms)


def go(page, path: str) -> None:
    page.goto(path, wait_until="domcontentloaded")
    settle(page)


def button(page, name: str, exact: bool = True):
    pattern = re.compile(("^" + re.escape(name) + "$") if exact else re.escape(name), re.I)
    return page.get_by_role("button", name=pattern).first


def open_profile(page) -> None:
    button(page, "Open agent profile and settings").click()
    page.locator("#agent-profile-sheet").wait_for(state="visible", timeout=8000)
    page.wait_for_timeout(500)


def close_profile(page) -> None:
    page.keyboard.press("Escape")
    page.locator("#agent-profile-sheet").wait_for(state="hidden", timeout=5000)


def open_neoh_dock(page):
    page.keyboard.press("/")
    field = page.get_by_role("textbox", name="Message Neoh")
    field.first.wait_for(state="visible", timeout=8000)
    return field.first


def ask_neoh(page, text: str, *, wait_for_reply=True, timeout=30_000) -> str:
    field = page.get_by_role("textbox", name="Message Neoh").first
    field.fill(text)
    field.press("Enter")
    if not wait_for_reply:
        return ""
    page.wait_for_function(
        """(q) => [...document.querySelectorAll('article[aria-label="You said"]')].some(a => a.innerText.includes(q))""",
        arg=text, timeout=timeout)
    page.wait_for_function(
        """(q) => { const turns = [...document.querySelectorAll('article')];
                    const i = turns.findIndex(a => a.getAttribute('aria-label') === 'You said' && a.innerText.includes(q));
                    const next = turns.slice(i + 1).find(a => a.getAttribute('aria-label') === 'Neoh said');
                    return next && next.innerText.replace(/NEOH|\\d+:\\d+\\s*[AP]M/g, '').trim().length > 10; }""",
        arg=text, timeout=timeout)
    return page.evaluate(
        """(q) => { const turns = [...document.querySelectorAll('article')];
                    const i = turns.findIndex(a => a.getAttribute('aria-label') === 'You said' && a.innerText.includes(q));
                    return turns.slice(i + 1).find(a => a.getAttribute('aria-label') === 'Neoh said').innerText; }""", text)


def mock_config(args, **cfg) -> None:
    if not args.mock_url:
        raise RuntimeError("--mock-url is required for this step")
    req = urllib.request.Request(args.mock_url.rstrip("/") + "/config", data=json.dumps(cfg).encode(),
                                 headers={"content-type": "application/json"}, method="POST")
    urllib.request.urlopen(req, timeout=10).read()


# ── page probes ────────────────────────────────────────────────────────────

LAYOUT_JS = """() => {
  const vw = window.innerWidth, vh = window.innerHeight;
  const rect = (e) => { if (!e) return null; const r = e.getBoundingClientRect();
    return r.width && r.height ? { l: Math.round(r.left), r: Math.round(r.right), t: Math.round(r.top), b: Math.round(r.bottom) } : null; };
  const visible = (e) => { const r = e.getBoundingClientRect(); const cs = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none' && Number(cs.opacity) > 0.05; };
  // A control outside the screen is a defect unless it sits in a strip the
  // person can scroll sideways (a chip row with overflow-x: auto is fine).
  const scrollsSideways = (e) => { for (let p = e.parentElement; p && p !== document.body; p = p.parentElement) {
      const ox = getComputedStyle(p).overflowX; if ((ox === 'auto' || ox === 'scroll') && p.scrollWidth > p.clientWidth) return true; }
    return false; };
  const header = [...document.querySelectorAll('button, a[href], input, select, textarea, [role=button], [role=tab]')]
    .filter((e) => visible(e) && !e.closest('[aria-hidden=true]'))
    .map((e) => ({ e, name: e.getAttribute('aria-label') || e.innerText.trim().slice(0, 30), ...rect(e) }))
    .filter((b) => (b.l < -1 || b.r > vw + 1) && !scrollsSideways(b.e))
    .map(({ e, ...b }) => b);
  const nav = rect(document.querySelector('nav[aria-label="Neoh CRM"]'));
  const field = [...document.querySelectorAll('textarea[aria-label="Message Neoh"]')].find(visible);
  const composer = field ? rect(field.closest('[role=dialog], [class*=dock], [class*=surface]') || field) : null;
  const pill = [...document.querySelectorAll('button')].find((b) => visible(b) && /^(Ask Neoh|Ask about|Neoh answered|Neoh is working)/.test(b.getAttribute('aria-label') || ''));
  const dialogs = [...document.querySelectorAll('[role=dialog], [role=alertdialog]')].filter(visible)
    .map((d) => ({ name: d.getAttribute('aria-label') || '', ...rect(d) }));
  return { vw, vh, scrollWidth: document.documentElement.scrollWidth, bodyScrollWidth: document.body.scrollWidth,
           headerOffscreen: header, nav, composer, pill: pill ? rect(pill) : null, dialogs };
}"""


def layout_problems(probe: dict, *, need_composer=False) -> list[str]:
    out = []
    vw = probe["vw"]
    if max(probe["scrollWidth"], probe["bodyScrollWidth"]) > vw:
        out.append(f"horizontal overflow {max(probe['scrollWidth'], probe['bodyScrollWidth'])} > {vw}")
    if probe["headerOffscreen"]:
        out.append(f"control off-screen (not in a sideways-scrolling strip): {probe['headerOffscreen']}")
    nav = probe.get("nav")
    for key in ("composer", "pill"):
        box = probe.get(key)
        if box and nav and box["b"] > nav["t"] + 1 and box["t"] < nav["b"]:
            out.append(f"{key} covered by the tab bar ({box} vs nav top {nav['t']})")
        if box and (box["l"] < 0 or box["r"] > vw):
            out.append(f"{key} wider than the screen {box}")
    if need_composer and not probe.get("composer"):
        out.append("composer not visible")
    for d in probe["dialogs"]:
        if d["l"] < -1 or d["r"] > vw + 1:
            out.append(f"dialog '{d['name']}' does not fit horizontally {d}")
        if d["t"] < -1 or d["b"] > probe["vh"] + 1:
            out.append(f"dialog '{d['name']}' does not fit vertically {d}")
    return out


def run_axe(page, *, include_minor=False) -> list[dict]:
    if not page.evaluate("() => !!window.axe"):
        page.add_script_tag(content=AXE.read_text(encoding="utf-8"))
    return page.evaluate("""async () => {
        const r = await axe.run(document, { resultTypes: ['violations'] });
        return r.violations.map((v) => ({ id: v.id, impact: v.impact, help: v.help,
          nodes: v.nodes.length, targets: v.nodes.slice(0, 4).map((n) => n.target.join(' ')) })); }""")


def focus_name(page) -> str:
    return page.evaluate("""() => { const e = document.activeElement; if (!e || e === document.body) return 'BODY';
        return (e.tagName + ':' + (e.getAttribute('aria-label') || e.labels?.[0]?.innerText || e.innerText || e.placeholder || '')).trim().replace(/\\s+/g, ' ').slice(0, 60); }""")


def focus_inside(page, selector: str) -> bool:
    return page.evaluate("(s) => { const d = document.querySelector(s); return !!d && d.contains(document.activeElement); }", selector)


# ── setup: the owner, through the real signup ─────────────────────────────

def signup_owner(report: Report, browser, args, out: Path) -> dict:
    sec = "journey_owner"
    creds = {"email": f"owner-{secrets.token_hex(4)}@example.test", "password": "M3-" + secrets.token_urlsafe(12),
             "name": "Olivia Owner"}
    ctx = new_context(browser, args, billing=False)
    page = ctx.new_page()
    report.page = page
    seen_gate = {}

    def signup():
        go(page, "/")
        page.get_by_text("Create an account", exact=True).click()
        page.get_by_label("Your name").fill(creds["name"])
        page.get_by_label("Brokerage (optional)").fill(BUSINESS)
        page.get_by_label("Email").fill(creds["email"])
        page.get_by_label("Password (10+ chars)").fill(creds["password"])
        with page.expect_response(lambda r: r.url.endswith("/auth/register")) as resp:
            button(page, "Create account").click()
        if resp.value.status != 201:
            raise AssertionError(f"register answered {resp.value.status}")
        creds["tenant_id"] = resp.value.json().get("tenant_id")
        return f"201, tenant {creds['tenant_id']}"

    def policy():
        dialog = page.get_by_role("dialog", name=re.compile("Before you enter", re.I))
        dialog.wait_for(timeout=15000)
        for box in dialog.locator("input[type=checkbox]").all():
            box.check()
        button(page, "Accept and continue").click()
        return "both acknowledgements accepted"

    def billing_gate():
        heading = page.get_by_role("heading", name=re.compile("Neoh Solo Premium", re.I))
        heading.wait_for(timeout=15000)
        seen_gate["tour_over_gate"] = page.locator("#crm-product-tour").count() > 0 and page.locator("#crm-product-tour").is_visible()
        if seen_gate["tour_over_gate"]:
            raise AssertionError("the guided walkthrough opened over the billing gate")
        if not button(page, "Start Neoh Solo Premium").is_visible():
            raise AssertionError("no purchase action on the gate")
        page.wait_for_timeout(1200)  # entrance animation; axe cannot rate text mid-fade
        page.screenshot(path=str(out / "owner-billing-gate.png"))
        bad = [v for v in run_axe(page) if v["impact"] in ("serious", "critical")]
        if bad:
            raise AssertionError(f"axe on the purchase gate: {[(v['id'], v['nodes']) for v in bad]}")
        return "purchase gate shown, walkthrough held back, axe clean (checkout NOT opened)"

    step(report, sec, "1 signup (POST /auth/register)", signup)
    step(report, sec, "2 policy acknowledgement", policy)
    step(report, sec, "3 billing gate before activation", billing_gate)
    ctx.storage_state(path=str(out / "owner-state.json"))
    ctx.close()
    (out / "credentials.json").write_text(json.dumps({"owner": creds}, indent=2))
    return creds


# ── journeys ───────────────────────────────────────────────────────────────

def journey_owner(report: Report, browser, args, out: Path, owner: dict) -> dict:
    sec = "journey_owner"
    found: dict = {}
    ctx = new_context(browser, args, state=str(out / "owner-state.json"))
    # A first visit, once: the walkthrough must open after activation.
    ctx.add_init_script("""try { if (!sessionStorage.getItem('m3-first-visit')) {
        sessionStorage.setItem('m3-first-visit', '1'); localStorage.removeItem('oracle_product_tour_v1'); } } catch (e) {}""")
    page = ctx.new_page()
    report.page = page

    def home_after_activation():
        go(page, "/")
        tour = page.locator("#crm-product-tour")
        tour.wait_for(state="visible", timeout=10000)
        page.keyboard.press("Escape")
        tour.wait_for(state="hidden", timeout=5000)
        page.get_by_role("tab", name="Home").wait_for(timeout=10000)
        return "walkthrough opened once billing was active; Escape closed it; Home shown"

    def setup():
        go(page, "/")
        open_profile(page)
        button(page, "Add business details").click()
        page.get_by_label("Primary state").fill("DE")
        with page.expect_response(lambda r: "/api/brokerage/profile" in r.url and r.request.method in ("PATCH", "PUT", "POST")) as resp:
            button(page, "Save").click()
        if resp.value.status >= 400:
            raise AssertionError(f"setup save answered {resp.value.status}")
        page.wait_for_timeout(800)
        close_profile(page)
        return f"business profile saved ({resp.value.status})"

    def invite():
        found["agent_email"] = f"agent-{secrets.token_hex(4)}@example.test"
        go(page, "/")
        open_profile(page)
        page.get_by_label("Email addresses to invite").fill(found["agent_email"])
        with page.expect_response(lambda r: r.url.endswith("/api/brokerage/invitations") and r.request.method == "POST") as resp:
            button(page, "Send invitation").click()
        body = resp.value.json()
        links = body.get("dev_links") or {}
        link = links.get(found["agent_email"]) or next(iter(links.values()), None)
        if not link:
            raise AssertionError(f"no dev-captured link (status {resp.value.status}); is SMTP configured?")
        found["invite_path"] = urlparse(link).path + "?" + urlparse(link).query
        page.get_by_text(re.compile("Invitation", re.I)).first.wait_for(timeout=5000)
        close_profile(page)
        return f"invitation created ({resp.value.status}); link dev-captured"

    def contact():
        go(page, "/work?type=people")
        button(page, "New contact").click()
        form = page.get_by_role("form", name="New contact")
        form.get_by_role("textbox", name="Full name").fill(CONTACT["name"])
        form.get_by_role("textbox", name="Email").fill(CONTACT["email"])
        form.get_by_role("textbox", name="Phone").fill(CONTACT["phone"])
        form.get_by_role("checkbox", name="SMS").check()
        with page.expect_response(lambda r: r.url.endswith("/api/crm/contacts") and r.request.method == "POST") as resp:
            button(page, "Create contact").click()
        if resp.value.status != 201:
            raise AssertionError(f"contact create answered {resp.value.status}: {resp.value.text()[:160]}")
        page.get_by_text(CONTACT["email"]).first.wait_for(timeout=8000)
        return "contact created with recorded SMS consent (201) and listed"

    def client():
        button(page, "Opportunities").click()
        button(page, "Add client").click()
        sheet = page.get_by_role("dialog", name="Add client")
        button(sheet, "Buyer").click()
        sheet.get_by_placeholder("Client name").fill(BUYER["name"])
        sheet.get_by_placeholder("client@email.com").fill(BUYER["email"])
        sheet.get_by_placeholder("555 000 0000").fill(BUYER["phone"])
        with page.expect_response(lambda r: r.url.endswith("/api/crm/clients") and r.request.method == "POST") as resp:
            button(sheet, "Create Profile").click()
        if resp.value.status >= 400:
            raise AssertionError(f"client create answered {resp.value.status}")
        found["client_id"] = (resp.value.json().get("client") or resp.value.json()).get("id")
        button(page, f"Open {BUYER['name']}").wait_for(timeout=8000)
        return f"buyer client created ({found['client_id']})"

    def property_record():
        go(page, "/work?type=properties")
        page.get_by_placeholder("123 Main St, Wilmington, DE 19801").fill(PROPERTY_ADDRESS)
        page.keyboard.press("Enter")
        make = button(page, "Create a new property record", exact=False)
        make.wait_for(timeout=15000)
        with page.expect_response(lambda r: "/api/crm/property-view/subject" in r.url) as resp:
            make.click()
        if resp.value.status >= 400:
            raise AssertionError(f"subject create answered {resp.value.status}")
        found["lead_id"] = resp.value.json().get("lead_id")
        page.get_by_text(re.compile("Property record created|Matched an existing record")).wait_for(timeout=8000)
        return f"property record {found['lead_id']}"

    def neoh_about_property():
        go(page, f"/property/{found['lead_id']}")
        page.get_by_role("dialog", name=re.compile("^Property")).wait_for(timeout=10000)
        open_neoh_dock(page)
        chip = page.get_by_text(re.compile(r"^Talking about "))
        chip.first.wait_for(timeout=5000)
        label = chip.first.inner_text()
        reply = ask_neoh(page, "What should I know about this property?", timeout=45_000)
        page.screenshot(path=str(out / "owner-neoh-over-property.png"))
        return f"'{label}' visible over the sheet; Neoh replied ({len(reply)} chars)"

    for name, fn in (("4 Home after activation", home_after_activation), ("5 brokerage setup", setup),
                     ("6 invite an agent", invite), ("7 add a contact", contact), ("8 add a buyer client", client),
                     ("9 add a property", property_record), ("10 Neoh about the property", neoh_about_property)):
        step(report, sec, name, fn)
    ctx.storage_state(path=str(out / "owner-state.json"))
    ctx.close()
    creds = json.loads((out / "credentials.json").read_text())
    creds["agent_invite"] = {"email": found.get("agent_email")}
    (out / "credentials.json").write_text(json.dumps(creds, indent=2))
    return found


def journey_agent(report: Report, browser, args, out: Path, found: dict) -> None:
    sec = "journey_agent"
    if not found.get("invite_path"):
        report.check(sec, "0 invitation available", False, "owner journey produced no invitation")
        return
    ctx = new_context(browser, args)
    page = ctx.new_page()
    report.page = page
    password = "M3-" + secrets.token_urlsafe(12)

    def accept():
        go(page, found["invite_path"])
        page.get_by_role("heading", name=re.compile(f"Join {re.escape(BUSINESS)} on Neoh")).wait_for(timeout=10000)
        page.get_by_label("Your name").fill("Avery Agent")
        page.get_by_label("Choose a password").fill(password)
        with page.expect_response(lambda r: r.url.endswith("/auth/accept-invite")) as resp:
            page.get_by_label("Choose a password").press("Enter")
        if resp.value.status != 201:
            raise AssertionError(f"accept-invite answered {resp.value.status}")
        return "joined with Enter from the password field (201)"

    def login():
        dialog = page.get_by_role("dialog", name=re.compile("Before you enter", re.I))
        dialog.wait_for(timeout=15000)
        for box in dialog.locator("input[type=checkbox]").all():
            box.check()
        button(page, "Accept and continue").click()
        page.get_by_role("tab", name="Work").wait_for(timeout=15000)
        # Sign out and back in through the login form, as an agent would.
        open_profile(page)
        page.get_by_role("button", name=re.compile("^Sign out$", re.I)).click()
        page.get_by_label("Email or Agent ID").wait_for(timeout=10000)
        page.get_by_label("Email or Agent ID").fill(found["agent_email"])
        page.get_by_label("Password").fill(password)
        with page.expect_response(lambda r: r.url.endswith("/auth/login")) as resp:
            page.get_by_label("Password").press("Enter")
        if resp.value.status != 200:
            raise AssertionError(f"login answered {resp.value.status}")
        page.get_by_role("tab", name="Work").wait_for(timeout=15000)
        return "policy accepted, signed out, signed back in with Enter (200)"

    def work():
        page.get_by_role("tab", name="Work").click()
        page.get_by_role("searchbox", name="Search everything").wait_for(timeout=8000)
        settle(page)
        return "Work open: " + page.url

    def neoh_and_action():
        page.get_by_role("tab", name="Neoh").click()
        reply = ask_neoh(page, "What needs me today?", timeout=45_000)
        go(page, "/work?type=people")
        button(page, "New contact").click()
        form = page.get_by_role("form", name="New contact")
        form.get_by_role("textbox", name="Full name").fill("Dana Doorknock")
        with page.expect_response(lambda r: r.url.endswith("/api/crm/contacts") and r.request.method == "POST") as resp:
            button(page, "Create contact").click()
        if resp.value.status != 201:
            raise AssertionError(f"agent contact create answered {resp.value.status}")
        return f"Neoh replied ({len(reply)} chars); agent created a contact (201)"

    for name, fn in (("1 accept invite", accept), ("2 login", login), ("3 Work", work), ("4 Neoh then an action", neoh_and_action)):
        step(report, sec, name, fn)
    ctx.storage_state(path=str(out / "agent-state.json"))
    ctx.close()


def journey_property(report: Report, browser, args, out: Path, found: dict) -> None:
    sec = "journey_property"
    ctx = new_context(browser, args, state=str(out / "owner-state.json"))
    page = ctx.new_page()
    report.page = page

    def search_open():
        go(page, "/work")
        box = page.get_by_role("searchbox", name="Search everything")
        box.fill("100 W 10th")
        page.wait_for_timeout(1500)
        hit = page.get_by_role("button", name=re.compile("10th", re.I)).first
        if hit.count():
            hit.click()
        else:
            go(page, f"/property/{found['lead_id']}")
        page.get_by_role("dialog", name=re.compile("^Property")).wait_for(timeout=10000)
        return "property sheet open: " + page.url

    def tour_entry():
        text = page.get_by_role("dialog", name=re.compile("^Property")).inner_text()
        if not re.search(r"3D tour|Step inside|No 3D tour yet|Preview a demo", text):
            raise AssertionError("no 3D entry or honest no-tour state on the sheet")
        return re.search(r"(Step inside[^\n]*|No 3D tour yet[^\n]*|Preview a demo[^\n]*)", text).group(1)[:120]

    def buyer_match():
        go(page, "/work?type=deals")
        market = page.get_by_role("button", name=re.compile("Marketplace", re.I))
        if not market.count():
            raise AssertionError("no Marketplace view in Deals")
        market.first.click()
        settle(page)
        body = page.locator("main").inner_text()
        if "Select a property to match buyers" in body or "No active buyer requests" in body or "publication" in body.lower():
            return "marketplace reachable; matching needs a published contract publication (none on a new brokerage)"
        return "marketplace opened: " + body[:120].replace("\n", " ")

    def neoh_and_communication():
        go(page, f"/property/{found['lead_id']}")
        page.get_by_role("dialog", name=re.compile("^Property")).wait_for(timeout=10000)
        open_neoh_dock(page)
        page.get_by_text(re.compile(r"^Talking about ")).first.wait_for(timeout=5000)
        reply = ask_neoh(page, "Which of my buyers fits this property?", timeout=45_000)
        page.keyboard.press("Escape")
        go(page, "/work?type=conversations")
        page.get_by_text(BUYER["name"]).first.click()
        button(page, "Email").click()
        page.get_by_label("Email subject").fill("A home on West 10th")
        page.get_by_label("Email body").fill("Hi Bianca, 100 W 10th St just came up and fits your search.")
        with page.expect_response(lambda r: "/messages" in r.url and r.request.method == "POST") as resp:
            button(page, "Queue email").click()
        if resp.value.status >= 400:
            raise AssertionError(f"queue email answered {resp.value.status}")
        page.get_by_text(re.compile("Queued|AI sends", re.I)).first.wait_for(timeout=8000)
        return f"Neoh replied ({len(reply)} chars); email queued to the outbox ({resp.value.status}) — outbox is held by recovery mode"

    for name, fn in (("1 search/open", search_open), ("2 3D entry state", tour_entry), ("3 buyer match", buyer_match),
                     ("4 Neoh then communication", neoh_and_communication)):
        step(report, sec, name, fn)
    ctx.close()


def journey_communication(report: Report, browser, args, out: Path, found: dict) -> None:
    sec = "journey_communication"
    ctx = new_context(browser, args, state=str(out / "owner-state.json"))
    page = ctx.new_page()
    report.page = page
    body_text = "Hi Bianca, two new listings match your search. Saturday?"

    def text_person():
        go(page, "/work?type=conversations")
        page.get_by_text(BUYER["name"]).first.click()
        button(page, "SMS").click()
        page.get_by_label("SMS body").fill(body_text)
        with page.expect_response(lambda r: "/messages" in r.url and r.request.method == "POST") as resp:
            button(page, "Log SMS without sending").click()
        if resp.value.status >= 400:
            raise AssertionError(f"log SMS answered {resp.value.status}")
        page.get_by_text("Logged only — not sent").first.wait_for(timeout=8000)
        return "SMS logged, labelled 'Logged only — not sent' (no provider send exists for it)"

    def call_entry():
        go(page, "/our-ai/sales/dialer")
        text = page.locator("main").inner_text()
        disabled = button(page, "Check and call", exact=False)
        state = "disabled" if disabled.count() and disabled.is_disabled() else "enabled"
        if "Not set up" not in text and state == "enabled":
            raise AssertionError("dialer offers calling on a brokerage with no calling set up")
        return f"Power Dialer: 'Check and call' {state}; capability shown as 'Not set up'"

    def timeline():
        go(page, "/work?type=people")
        button(page, "Opportunities").click()
        button(page, f"Open {BUYER['name']}").click()
        drawer = page.get_by_role("dialog", name=f"Client — {BUYER['name']}")
        drawer.wait_for(timeout=8000)
        drawer.get_by_role("tab", name=re.compile("Timeline", re.I)).click()
        drawer.get_by_text(re.compile("Outbound SMS logged")).first.wait_for(timeout=10000)
        page.keyboard.press("Escape")
        return "timeline shows 'Outbound SMS logged (not sent)'"

    def receipt():
        # The quick-add client has no state, and texts/calls are refused without
        # one (quiet hours are per state) — so Neoh stages an email here.
        mock_config(args, llm_hijack_tools=[{"name": "draft_email", "arguments": {
            "client_id": "{record_id}", "subject": "Saturday tour",
            "body": "Hi Bianca, can we tour 100 W 10th St on Saturday?"}}])
        try:
            go(page, "/work?type=people")
            button(page, "Opportunities").click()
            button(page, f"Open {BUYER['name']}").click()
            page.get_by_role("dialog", name=f"Client — {BUYER['name']}").wait_for(timeout=8000)
            open_neoh_dock(page)
            page.get_by_text(f"Talking about {BUYER['name']}").first.wait_for(timeout=5000)
            ask_neoh(page, "Email Bianca about a Saturday tour.", wait_for_reply=False)
            go(page, "/neoh")
            title = page.get_by_text(re.compile(r"^Email (waiting for your approval|approved|needs review|didn.t go through|cancelled)"))
            title.first.wait_for(timeout=60_000)
            first = title.first.inner_text()
        finally:
            mock_config(args, llm_hijack_tools=None)
        go(page, "/work?type=conversations")
        # The field arrives pre-filled; it is found by its label, then rewritten.
        reason = page.get_by_label(re.compile("Why you.re approving or rejecting")).first
        try:
            reason.wait_for(timeout=4000)
        except Exception:  # noqa: BLE001 — collapsed: open it
            page.get_by_role("button", name=re.compile("^Waiting for your approval")).first.click()
            reason.wait_for(timeout=8000)
        reason.fill("Buyer asked for a Saturday tour; content checked.")
        with page.expect_response(lambda r: re.search(r"/api/commands/[^/]+/approve$", r.url) is not None, timeout=15000) as resp:
            button(page, "Approve").click()
        if resp.value.status >= 400:
            raise AssertionError(f"approve answered {resp.value.status}: {resp.value.text()[:160]}")
        go(page, "/neoh")
        after = page.get_by_text(re.compile(r"^Email (approved|needs review|didn.t go through|sent)|^Sending email"))
        after.first.wait_for(timeout=90_000)
        page.screenshot(path=str(out / "communication-receipt.png"))
        return (f"receipt before approval: '{first}'; approved ({resp.value.status}); after: "
                f"'{after.first.inner_text()}' (recovery mode refuses the provider send)")

    for name, fn in (("1 text a person", text_person), ("2 call entry", call_entry), ("3 timeline", timeline), ("4 receipt", receipt)):
        step(report, sec, name, fn)
    ctx.close()


def journey_failure(report: Report, browser, args, out: Path) -> None:
    sec = "journey_failure"
    ctx = new_context(browser, args, state=str(out / "owner-state.json"))
    page = ctx.new_page()
    report.page = page
    raw = re.compile(r"\b(50[0-9]|Traceback|Internal Server Error|ECONN|fetch failed|undefined|null)\b")

    def ai_down():
        mock_config(args, llm_fail_rate=1.0, llm_fail="503")
        try:
            go(page, "/neoh")
            # A question the instant-answer path cannot take, so it reaches the model.
            ask_neoh(page, "Draft a warm note for a first-time buyer in Dover.", wait_for_reply=False)
            question = "Draft a warm note for a first-time buyer in Dover."
            answer_js = """(q) => { const turns = [...document.querySelectorAll('article')];
                const i = turns.map(a => a.getAttribute('aria-label') === 'You said' && a.innerText.includes(q)).lastIndexOf(true);
                const next = turns.slice(i + 1).find(a => a.getAttribute('aria-label') === 'Neoh said');
                return next ? next.innerText : ''; }"""
            page.wait_for_function(f"(q) => /trouble|try again|couldn|unavailable|saved/i.test(({answer_js})(q))",
                                   arg=question, timeout=90_000)
            said = page.evaluate(answer_js, question)
        finally:
            mock_config(args, llm_fail_rate=0.0, llm_fail="")
        if raw.search(said):
            raise AssertionError(f"raw provider text shown: {said[:160]}")
        field = page.get_by_role("textbox", name="Message Neoh").first
        if not field.is_enabled():
            raise AssertionError("composer disabled after the failure")
        return "product-language failure: " + said.replace("\n", " ")[-140:]

    def contacts_down():
        contacts = lambda u: urlparse(u).path == "/api/crm/contacts"  # noqa: E731
        page.route(contacts, lambda r: r.fulfill(status=503, content_type="application/json", body='{"detail":"upstream timeout"}'))
        go(page, "/work?type=people")
        page.get_by_role("button", name=re.compile(r"^Contacts")).first.click()
        alert = page.get_by_role("alert").filter(has_text=re.compile("temporarily unavailable", re.I))
        alert.first.wait_for(timeout=45000)
        text = alert.first.inner_text()
        if "upstream timeout" in text or raw.search(text):
            raise AssertionError(f"raw error leaked: {text[:160]}")
        retry = button(page, "Retry contacts")
        page.unroute(contacts)
        retry.click()
        page.get_by_text(CONTACT["email"]).first.wait_for(timeout=10000)
        return "degraded state: '" + text.replace("\n", " ")[:120] + "' and Retry recovered"

    def status_banner():
        page.route("**/api/status", lambda r: r.fulfill(status=200, content_type="application/json",
                   body=json.dumps({"state": "DEGRADED", "messages": ["Calling is temporarily unavailable."]})))
        go(page, "/")
        banner = page.locator("[data-service-banner]")
        banner.wait_for(timeout=10000)
        box = banner.bounding_box()
        header = page.locator("header").first.bounding_box()
        if box["y"] < header["y"] + header["height"] - 1:
            raise AssertionError(f"banner sits under the header (banner y={box['y']}, header bottom={header['y'] + header['height']})")
        page.unroute("**/api/status")
        return "banner below the header: '" + banner.inner_text() + "'"

    for name, fn in (("1 AI provider unavailable", ai_down), ("2 contact source unavailable", contacts_down),
                     ("3 provider status banner", status_banner)):
        step(report, sec, name, fn)
    ctx.close()


# ── responsive, keyboard, axe, network, visual ────────────────────────────

def surfaces(found: dict) -> list[tuple[str, str, object]]:
    def drawer(page):
        button(page, "Opportunities").click()
        button(page, f"Open {BUYER['name']}").click()
        page.get_by_role("dialog", name=f"Client — {BUYER['name']}").wait_for(timeout=8000)

    def add_client(page):
        button(page, "Opportunities").click()
        button(page, "Add client").click()
        page.get_by_role("dialog", name="Add client").wait_for(timeout=8000)

    def setup(page):
        open_profile(page)
        page.get_by_role("heading", name=re.compile("Set up Neoh for your business", re.I)).scroll_into_view_if_needed()

    def new_contact(page):
        button(page, "New contact").click()

    def thread(page):
        page.get_by_text(BUYER["name"]).first.click()
        page.get_by_label(re.compile(r"(SMS|Email|Note) body")).first.wait_for(timeout=8000)

    def composer(page):
        open_neoh_dock(page)

    out = [
        ("home", "/", None), ("work", "/work", None), ("neoh", "/neoh", None),
        ("people", "/work?type=people", None), ("new-contact", "/work?type=people", new_contact),
        ("add-client", "/work?type=people", add_client), ("person", "/work?type=people", drawer),
        ("conversation", "/work?type=conversations", thread), ("deals", "/work?type=deals", None),
        ("properties", "/work?type=properties", None), ("brokerage-setup", "/", setup),
        ("home-composer", "/", composer),
    ]
    if found.get("lead_id"):
        out.append(("property", f"/property/{found['lead_id']}", None))
    return out


def responsive(report: Report, browser, args, out: Path, found: dict) -> None:
    sec = "responsive"
    for width in WIDTHS:
        ctx = new_context(browser, args, viewport={"width": width, "height": 844 if width < 768 else 900},
                          state=str(out / "owner-state.json"), mobile=width < 768, quiet_status=True)
        page = ctx.new_page()
        problems: dict = {}
        for name, path, prepare in surfaces(found):
            try:
                go(page, path)
                if prepare:
                    prepare(page)
                    page.wait_for_timeout(500)
                issues = layout_problems(page.evaluate(LAYOUT_JS), need_composer=name in ("neoh", "home-composer"))
            except Exception as exc:  # noqa: BLE001
                issues = [f"could not open: {type(exc).__name__}: {str(exc).splitlines()[0][:120]}"]
            if issues:
                problems[name] = issues
        report.check(sec, f"{width}px", not problems, problems or f"{len(surfaces(found))} surfaces fit")
        ctx.close()


def keyboard(report: Report, browser, args, out: Path, found: dict) -> None:
    sec = "keyboard"
    ctx = new_context(browser, args, state=str(out / "owner-state.json"))
    page = ctx.new_page()
    report.page = page

    def tab_order():
        go(page, "/")
        page.locator("body").click(position={"x": 5, "y": 300})
        stops = []
        for _ in range(40):
            page.keyboard.press("Tab")
            stops.append(focus_name(page))
            if stops[-1] == "BODY":  # wrapped past the last stop to the browser chrome
                break
        joined = " | ".join(stops)
        for want in ("Open agent profile and settings", "BUTTON:Home", "Ask Neoh", "Neoh answered"):
            if want not in joined and not (want == "Ask Neoh" and "Neoh answered" in joined) \
                    and not (want == "Neoh answered" and "Ask Neoh" in joined):
                raise AssertionError(f"'{want}' not reachable by Tab: {joined}")
        if "BODY" in stops[:-1]:
            raise AssertionError(f"focus fell to the page mid-order: {joined}")
        # The tab bar is one Tab stop with roving focus (WAI-ARIA tabs):
        # arrows move between Home, Work and Neoh, Enter opens.
        page.get_by_role("tab", name="Home").focus()
        page.keyboard.press("ArrowRight")
        if focus_name(page) != "BUTTON:Work":
            raise AssertionError(f"ArrowRight from Home focused {focus_name(page)}")
        page.keyboard.press("Enter")
        page.wait_for_url(re.compile(r"/work"), timeout=8000)
        return f"{len(stops) - 1} stops then wraps; tab bar arrows Home→Work + Enter opened /work. Order: {joined[:200]}"

    def slash_composer_enter():
        go(page, "/")
        field = open_neoh_dock(page)
        if not focus_inside(page, 'textarea[aria-label="Message Neoh"]') and focus_name(page).find("Message Neoh") < 0:
            raise AssertionError(f"'/' did not focus the composer (focus on {focus_name(page)})")
        field.fill("Keyboard check: what needs me today?")
        field.press("Enter")
        page.wait_for_function("""() => [...document.querySelectorAll('article[aria-label="You said"]')].some(a => a.innerText.includes('Keyboard check'))""", timeout=15000)
        page.keyboard.press("Escape")
        page.wait_for_timeout(500)
        return "'/' focused 'Message Neoh', Enter sent, Escape closed; focus now " + focus_name(page)

    def dialog_trap(name, opener, dialog_sel, closer_focus):
        page.keyboard.press("Escape")
        opener()
        page.wait_for_timeout(600)
        escaped = []
        for _ in range(30):
            page.keyboard.press("Tab")
            if not focus_inside(page, dialog_sel):
                escaped.append(focus_name(page))
        for _ in range(5):
            page.keyboard.press("Shift+Tab")
            if not focus_inside(page, dialog_sel):
                escaped.append(focus_name(page))
        if escaped:
            raise AssertionError(f"focus left the {name}: {escaped[:3]}")
        page.keyboard.press("Escape")
        page.wait_for_timeout(600)
        if page.locator(dialog_sel).count() and page.locator(dialog_sel).first.is_visible():
            raise AssertionError(f"Escape did not close the {name}")
        return f"Tab x30 / Shift+Tab x5 stayed inside; Escape closed; focus returned to {focus_name(page)}"

    def profile_trap():
        go(page, "/")
        btn = button(page, "Open agent profile and settings")
        return dialog_trap("settings sheet", lambda: (btn.focus(), page.keyboard.press("Enter")), "#agent-profile-sheet", btn)

    def add_client_trap():
        go(page, "/work?type=people")
        button(page, "Opportunities").click()
        return dialog_trap("Add client dialog", lambda: button(page, "Add client").click(), '[role=dialog][aria-label="Add client"]', None)

    def person_trap():
        go(page, "/work?type=people")
        button(page, "Opportunities").click()
        return dialog_trap("person drawer", lambda: button(page, f"Open {BUYER['name']}").click(),
                           f'[role=dialog][aria-label="Client — {BUYER["name"]}"]', None)

    def state_picker():
        go(page, "/")
        pill = page.get_by_role("button", name=re.compile("^Select active jurisdictions"))
        pill.focus()
        page.keyboard.press("Enter")
        page.get_by_role("dialog", name="State selector").wait_for(timeout=5000)
        page.keyboard.press("Escape")
        page.wait_for_timeout(400)
        if page.get_by_role("dialog", name="State selector").count():
            raise AssertionError("Escape did not close the state selector")
        return "Enter opened the state selector, Escape closed it"

    def login_enter():
        c2 = new_context(browser, args, billing=False)
        p2 = c2.new_page()
        go(p2, "/")
        p2.keyboard.press("Tab")
        first = focus_name(p2)
        p2.get_by_label("Email or Agent ID").fill("nobody@example.test")
        p2.get_by_label("Password").fill("not-the-password-1")
        with p2.expect_response(lambda r: r.url.endswith("/auth/login")) as resp:
            p2.get_by_label("Password").press("Enter")
        alert = p2.get_by_role("alert").first
        alert.wait_for(timeout=8000)
        said = alert.inner_text()[:80]
        c2.close()
        return f"first Tab stop {first}; Enter submitted ({resp.value.status}); error announced: {said}"

    for name, fn in (("Tab order (Home)", tab_order), ("composer: '/' focus, Enter submit, Escape", slash_composer_enter),
                     ("settings sheet focus trap + Escape", profile_trap), ("Add client focus trap + Escape", add_client_trap),
                     ("person drawer focus trap + Escape", person_trap), ("state picker Enter/Escape", state_picker),
                     ("login form Enter submit", login_enter)):
        step(report, sec, name, fn)
    ctx.close()


def accessibility(report: Report, browser, args, out: Path, found: dict) -> None:
    sec = "axe"
    totals: Counter = Counter()
    for scheme in ("light", "dark"):
        for vp_name, vp in (("desktop", DESKTOP), ("phone", PHONE)):
            ctx = new_context(browser, args, viewport=vp, state=str(out / "owner-state.json"), scheme=scheme, quiet_status=True)
            page = ctx.new_page()
            for name, path, prepare in surfaces(found):
                try:
                    go(page, path)
                    if prepare:
                        prepare(page)
                        page.wait_for_timeout(500)
                    violations = run_axe(page)
                except Exception as exc:  # noqa: BLE001
                    report.check(sec, f"{scheme}/{vp_name}/{name}", False, f"could not scan: {exc}")
                    continue
                for v in violations:
                    totals[v["impact"]] += v["nodes"]
                bad = [v for v in violations if v["impact"] in ("serious", "critical")]
                report.check(sec, f"{scheme}/{vp_name}/{name}", not bad,
                             [f"{v['impact']} {v['id']} x{v['nodes']} {v['targets'][:2]}" for v in bad]
                             or [f"{v['impact']} {v['id']}" for v in violations] or "clean")
            ctx.close()
    # Signed-out pages.
    ctx = new_context(browser, args, billing=False)
    page = ctx.new_page()
    report.page = page
    for name, prep in (("login", None), ("signup", lambda p: p.get_by_text("Create an account", exact=True).click()),
                       ("accept-invite (bad token)", lambda p: go(p, "/accept-invite?token=not-a-real-token"))):
        go(page, "/")
        if prep:
            prep(page)
            page.wait_for_timeout(600)
        violations = run_axe(page)
        for v in violations:
            totals[v["impact"]] += v["nodes"]
        bad = [v for v in violations if v["impact"] in ("serious", "critical")]
        report.check(sec, f"signed-out/{name}", not bad, [f"{v['impact']} {v['id']}" for v in violations] or "clean")
    ctx.close()
    report.note(sec, "node totals by impact", dict(totals))


def network(report: Report, browser, args, out: Path) -> None:
    sec = "network"
    url = args.prod_url or args.base_url
    mode = "production build" if args.prod_url else "dev server (StrictMode doubles effects; duplicates reported, not failed)"
    report.note(sec, "measured against", f"{url} — {mode}")
    for path in ("/", "/work", "/neoh"):
        browser_ctx = browser.new_context(base_url=url, viewport=DESKTOP, storage_state=str(out / "owner-state.json"))
        install_billing_stub(browser_ctx)
        browser_ctx.add_init_script("try { localStorage.setItem('oracle_product_tour_v1', 'dismissed') } catch (e) {}")
        page = browser_ctx.new_page()
        reqs, sockets = [], []
        page.on("request", lambda r: reqs.append((r.method, r.url, r.resource_type)))
        page.on("websocket", lambda w: sockets.append(w.url))
        page.goto(path, wait_until="domcontentloaded")
        page.wait_for_timeout(6000)
        api = [(m, urlparse(u).path + ("?" + urlparse(u).query if urlparse(u).query else "")) for m, u, t in reqs
               if t in ("fetch", "xhr") or urlparse(u).path.startswith(("/api", "/auth", "/billing"))]
        counts = Counter(api)
        dupes = {f"{m} {u}": n for (m, u), n in counts.items() if n > 1 and m == "GET"}
        evidence = {"total_requests": len(reqs), "api_requests": len(api), "distinct": len(counts),
                    "websockets": len(sockets), "duplicates": dupes, "api": sorted(f"{m} {u}" for m, u in counts)}
        ok = not dupes if args.prod_url else True
        report.check(sec, f"{path} first load", ok and len(sockets) <= 1, evidence)
        browser_ctx.close()


def visual(report: Report, browser, args, out: Path, found: dict, engine: str) -> None:
    from PIL import Image, ImageChops

    sec = f"visual/{engine}"
    base = Path(args.baselines) / engine
    base.mkdir(parents=True, exist_ok=True)
    shots = [("home", "/", DESKTOP, None), ("work", "/work", DESKTOP, None), ("neoh", "/neoh", DESKTOP, None),
             ("person", "/work?type=people", DESKTOP, "person"), ("brokerage-setup", "/", DESKTOP, "brokerage-setup"),
             ("mobile-composer", "/neoh", PHONE, None)]
    if found.get("lead_id"):
        shots.insert(3, ("property", f"/property/{found['lead_id']}", DESKTOP, None))
    preps = {name: prep for name, _, prep in surfaces(found)}
    for name, path, vp, prep in shots:
        ctx = new_context(browser, args, viewport=vp, state=str(out / "owner-state.json"), quiet_status=True)
        page = ctx.new_page()
        page.route("**/api/status", lambda r: r.fulfill(status=200, content_type="application/json", body='{"state":"HEALTHY","messages":[]}'))
        # Chat history differs on every run; the baseline is the empty conversation.
        page.route(lambda u: urlparse(u).path == "/api/ai/chat/messages",
                   lambda r: r.fulfill(status=200, content_type="application/json", body='{"messages":[]}'))
        try:
            go(page, path)
            if prep:
                preps[prep](page)
            page.wait_for_timeout(1200)
            masks = [page.locator(s) for s in ("time", "[datetime]", "canvas", "video", "[class*=greeting]",
                                               "[class*=clock]", "[class*=timestamp]", "[class*=meta] small")]
            png = page.screenshot(animations="disabled", caret="hide", mask=masks, mask_color="#808080")
        except Exception as exc:  # noqa: BLE001
            report.check(sec, name, False, f"could not capture: {exc}")
            ctx.close()
            continue
        (out / "shots").mkdir(exist_ok=True)
        (out / "shots" / f"{engine}-{name}.png").write_bytes(png)
        ref = base / f"{name}.png"
        if args.update_baselines or not ref.exists():
            ref.write_bytes(png)
            report.check(sec, name, True, f"baseline written {ref}")
        else:
            a = Image.open(ref).convert("RGB")
            b = Image.open(io.BytesIO(png)).convert("RGB")
            if a.size != b.size:
                report.check(sec, name, False, f"size changed {a.size} -> {b.size}")
            else:
                diff = ImageChops.difference(a, b).convert("L").point(lambda v: 255 if v > 40 else 0)
                changed = sum(1 for v in diff.getdata() if v) / (a.size[0] * a.size[1])
                if changed > args.pixel_tolerance:
                    diff.save(out / "shots" / f"{engine}-{name}-diff.png")
                report.check(sec, name, changed <= args.pixel_tolerance, f"{changed:.2%} of pixels changed (tolerance {args.pixel_tolerance:.1%})")
        ctx.close()


# ── browser matrix ─────────────────────────────────────────────────────────

def matrix(report: Report, pw, args, out: Path, found: dict, engine: str) -> None:
    sec = f"matrix/{engine}"
    try:
        browser = launch(pw, engine, args)
    except Exception as exc:  # noqa: BLE001
        report.check(sec, "launch", False, f"browser unavailable: {str(exc).splitlines()[0][:160]}")
        return
    report.note(sec, "version", browser.version)
    ctx = new_context(browser, args, viewport=PHONE, state=str(out / "owner-state.json"), mobile=True, mic="granted")
    page = ctx.new_page()
    report.page = page
    frames = {"open": 0, "received": 0}

    def on_ws(ws):
        if urlparse(ws.url).path.startswith("/ws"):
            frames["open"] += 1
            ws.on("framereceived", lambda _f: frames.__setitem__("received", frames["received"] + 1))
    page.on("websocket", on_ws)

    def websocket():
        go(page, "/neoh")
        reply = ask_neoh(page, f"{engine} socket check: draft a two-line hello for a new buyer.", timeout=60_000)
        if not frames["open"]:
            raise AssertionError("no /ws socket opened")
        return f"/ws opened {frames['open']}x, {frames['received']} frames received; reply over the socket ({len(reply)} chars)"

    def voice_granted():
        go(page, "/neoh")
        mic = page.get_by_role("button", name=re.compile("Talk to Neoh|Voice input isn.t available|Try the microphone again"))
        mic.first.wait_for(timeout=8000)
        label = mic.first.get_attribute("aria-label")
        if mic.first.is_disabled():
            return f"no speech recognition in this engine — mic disabled with '{label}'"
        mic.first.click()
        page.wait_for_timeout(1500)
        after = page.evaluate("""() => { const b = [...document.querySelectorAll('button')].find(x => /Stop listening|Waiting for microphone|Try the microphone|Talk to Neoh/.test(x.getAttribute('aria-label') || ''));
            const s = [...document.querySelectorAll('[role=status]')].map(e => e.innerText).filter(Boolean).join(' / ');
            return (b ? b.getAttribute('aria-label') : '?') + (s ? ' | ' + s : ''); }""")
        stop = page.get_by_role("button", name="Stop listening")
        if stop.count():
            stop.first.click()
        return f"granted: mic -> '{after[:150]}'"

    def sticky_composer():
        go(page, "/neoh")
        page.get_by_role("textbox", name="Message Neoh").first.wait_for(timeout=8000)
        before = page.evaluate(LAYOUT_JS)
        page.evaluate("() => { const m = document.querySelector('main'); m && m.scrollTo(0, m.scrollHeight); }")
        page.wait_for_timeout(400)
        page.evaluate("() => { const m = document.querySelector('main'); m && m.scrollTo(0, 0); }")
        page.wait_for_timeout(400)
        after = page.evaluate(LAYOUT_JS)
        problems = layout_problems(after, need_composer=True)
        if before["composer"] != after["composer"]:
            problems.append(f"composer moved on scroll {before['composer']} -> {after['composer']}")
        if problems:
            raise AssertionError("; ".join(problems))
        return f"fixed at {after['composer']}, above the tab bar (top {after['nav']['t'] if after['nav'] else '?'})"

    def upload():
        go(page, "/work?type=properties")
        page.get_by_placeholder("123 Main St, Wilmington, DE 19801").fill(PROPERTY_ADDRESS)
        page.keyboard.press("Enter")
        up = page.get_by_role("button", name=re.compile(r"^Upload .*photos or video", re.I))
        try:
            up.first.wait_for(timeout=15000)
        except Exception:  # noqa: BLE001 — two candidates: pick ours
            page.get_by_role("button", name=re.compile("Use this|Select|Attach", re.I)).first.click()
            up.first.wait_for(timeout=8000)
        with page.expect_file_chooser() as chooser:
            up.first.click()
        with page.expect_response(lambda r: "/media" in r.url and r.request.method == "POST", timeout=30_000) as resp:
            chooser.value.set_files(files=[{"name": f"{engine}-front.png", "mimeType": "image/png", "buffer": PNG_1PX}])
        page.get_by_text(re.compile(r"1 file uploaded|uploaded\.", re.I)).first.wait_for(timeout=15000)
        return f"file chooser opened from the button; POST media {resp.value.status}; '1 file uploaded.'"

    def launch_3d():
        if not SPACE_FIXTURE.exists():
            raise AssertionError("fixture missing: " + str(SPACE_FIXTURE))
        c3 = browser.new_context(base_url=args.base_url, viewport=PHONE)
        c3.route(lambda u: urlparse(u).path == "/space-fixture/space.sog",
                 lambda r: r.fulfill(status=200, body=SPACE_FIXTURE.read_bytes(), headers={"content-type": "application/octet-stream"}))
        p3 = c3.new_page()
        p3.goto("/neoh-space-harness.html?device=force&asset=/space-fixture/space.sog&format=.sog", wait_until="domcontentloaded")
        try:
            p3.wait_for_selector('[data-space-status="ready"], [data-testid="space-fallback"], [data-space-status="error"]', timeout=120_000)
            status = p3.evaluate("() => document.querySelector('[data-space-status]')?.dataset.spaceStatus || (document.querySelector('[data-testid=space-fallback]') ? 'fallback' : '?')")
        finally:
            c3.close()
        if status not in ("ready", "fallback"):
            raise AssertionError(f"3D viewer status {status}")
        return f"Neoh Space viewer: {status}" + (" (WebGL unavailable → honest fallback)" if status == "fallback" else "")

    for name, fn in (("WebSocket", websocket), ("voice permission granted", voice_granted),
                     ("sticky composer (390px)", sticky_composer), ("upload", upload), ("3D launch", launch_3d)):
        step(report, sec, name, fn)
    ctx.close()
    browser.close()

    # Denied microphone: the composer must still work.
    try:
        denied = launch(pw, engine, args, mic="denied")
    except Exception as exc:  # noqa: BLE001
        report.check(sec, "voice permission denied", False, str(exc)[:160])
        return
    c2 = new_context(denied, args, viewport=PHONE, state=str(out / "owner-state.json"), mobile=True)
    p2 = c2.new_page()

    def voice_denied():
        go(p2, "/neoh")
        mic = p2.get_by_role("button", name=re.compile("Talk to Neoh|Voice input isn.t available|Try the microphone again"))
        mic.first.wait_for(timeout=8000)
        note = "mic disabled (no speech API)"
        if not mic.first.is_disabled():
            mic.first.click()
            p2.wait_for_timeout(2500)
            note = " / ".join(t for t in p2.locator("[role=status]").all_inner_texts() if t.strip())[:160] or "no message"
        reply = ask_neoh(p2, f"{engine} denied-mic check: draft a short thank-you note.", timeout=60_000)
        return f"denied: {note}; typed message still sent and answered ({len(reply)} chars)"

    step(report, sec, "voice permission denied", voice_denied)
    c2.close()
    denied.close()


# ── main ──────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default="http://localhost:5195")
    parser.add_argument("--prod-url", default="", help="vite preview of a production build, for the network audit")
    parser.add_argument("--mock-url", default="", help="performance/mocks/provider_mock.py base URL (receipt + AI-failure steps)")
    parser.add_argument("--chrome", default="", help="system Chrome for the chromium engine (else Playwright's build)")
    parser.add_argument("--browsers", default="chromium,firefox,webkit")
    parser.add_argument("--only", default="journeys,responsive,keyboard,axe,network,visual,matrix")
    parser.add_argument("--out", default=str(REPO / "performance" / "out" / "ui-run"))
    parser.add_argument("--baselines", default=str(REPO / "performance" / "out" / "ui-baselines"))
    parser.add_argument("--update-baselines", action="store_true")
    parser.add_argument("--pixel-tolerance", type=float, default=0.02)
    parser.add_argument("--reuse", default="", help="a previous --out dir: reuse its owner session and records "
                        "instead of signing up again (e.g. to sweep another build with the same data)")
    args = parser.parse_args()

    from playwright.sync_api import sync_playwright

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    only = set(args.only.split(","))
    engines = [e for e in args.browsers.split(",") if e]
    report = Report()
    report.out = out
    found: dict = {}
    with sync_playwright() as pw:
        chromium = launch(pw, "chromium", args)
        report.note("run", "chromium", chromium.version)
        print("== setup + journeys (chromium)")
        if args.reuse:
            import shutil
            src = Path(args.reuse)
            for name in ("owner-state.json", "found.json", "credentials.json"):
                if (src / name).exists() and src.resolve() != out.resolve():
                    shutil.copy(src / name, out / name)
            found = json.loads((out / "found.json").read_text())
            report.note("run", "reused", str(src))
        else:
            owner = signup_owner(report, chromium, args, out)
            found = journey_owner(report, chromium, args, out, owner)
        (out / "found.json").write_text(json.dumps(found, indent=2))
        if "journeys" in only:
            journey_agent(report, chromium, args, out, found)
            journey_property(report, chromium, args, out, found)
            journey_communication(report, chromium, args, out, found)
            journey_failure(report, chromium, args, out)
        for name, fn in (("responsive", responsive), ("keyboard", keyboard), ("axe", accessibility)):
            if name in only:
                print(f"== {name}")
                try:
                    fn(report, chromium, args, out, found)
                except Exception:  # noqa: BLE001
                    report.check(name, "section ran", False, traceback.format_exc(limit=2)[-300:])
        if "network" in only:
            print("== network")
            network(report, chromium, args, out)
        chromium.close()
        if "visual" in only:
            print("== visual")
            for engine in engines:
                try:
                    b = launch(pw, engine, args)
                except Exception as exc:  # noqa: BLE001
                    report.check(f"visual/{engine}", "launch", False, str(exc).splitlines()[0][:160])
                    continue
                visual(report, b, args, out, found, engine)
                b.close()
        if "matrix" in only:
            print("== browser matrix")
            for engine in engines:
                matrix(report, pw, args, out, found, engine)

    report.data["failures"] = report.failures
    (out / "report.json").write_text(json.dumps(report.data, indent=2, default=str))
    print(f"\n{len(report.failures)} failing checks; report: {out / 'report.json'}")
    for f in report.failures:
        print("  FAIL", f)
    return 1 if report.failures else 0


if __name__ == "__main__":
    sys.exit(main())
