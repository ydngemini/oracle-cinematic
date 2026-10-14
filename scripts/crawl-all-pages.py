#!/usr/bin/env python3
"""Open every page in the all-pages matrix and record how it renders (prompt 02 T02.11).

    python scripts/crawl-all-pages.py --base-url https://neoh-staging-… [--only ai:sales,deals:pipeline]

Signs in as the demo owner and the demo agent (scripts/demo_tenant_common.py,
the same accounts the killer-demo rehearsal uses) and opens each row of
docs/claude-prompts/all-pages.json at desktop (1440x900) and phone (390x844)
size. Pages outside the signed-in shell open in a fresh anonymous context with
fake tokens, which is exactly the state a stranger with a bad link sees.

What it records per page and run: load time to a settled network, uncaught
page errors, console errors, API responses >= 500 (and 4xx, separately),
horizontal overflow at phone width, an error-boundary message on screen, and a
screenshot (performance/out/crawl/<run>/, git-ignored).

It never sends, calls, publishes or spends: every non-GET request is aborted
except signing in, and each aborted write is reported, because a page that
writes just by being opened is itself a finding.

A clean crawl means "renders without errors for this role and size", not
"works": results go to docs/claude-prompts/all-pages-crawl.json and appear as
the matrix's Crawl column, never as a verdict.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import demo_tenant_common as common  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
MATRIX = REPO / "docs/claude-prompts/all-pages.json"
SUMMARY = REPO / "docs/claude-prompts/all-pages-crawl.json"
VIEWPORTS = {"desktop": {"width": 1440, "height": 900}, "phone": {"width": 390, "height": 844}}
ALLOWED_WRITES = {"/auth/login", "/auth/logout"}
API_PREFIXES = ("/api/", "/portal/", "/auth/", "/billing/")
# What a page SAYS when it is failing. "temporarily unavailable" is how the AI
# hub announced an outage; it caught a 403 being shown as one (L02-05).
BOUNDARY = re.compile(r"could not be (loaded|opened)|something went wrong|unexpected error"
                      r"|temporarily unavailable", re.I)
# Chrome logs every non-2xx response as "Failed to load resource: …". Those are
# already recorded (api_4xx / api_5xx) and are often handled by the page, so
# they are not counted again as console errors.
NETWORK_NOISE = re.compile(r"^Failed to load resource: the server responded with a status of")
SUBVIEW_NAV = {"ai": ("Neoh tools", "tab"), "deals": ("Deal workspace", "button"),
               "properties": ("Property workspace", "button")}
FAKE = "crawl-not-a-real-token"


def _sha() -> str:
    try:
        return subprocess.run(["git", "-C", str(REPO), "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


def _ids(page, base: str) -> dict:
    """Real record ids for the entity sheets, read through the signed-in session."""
    out = {}
    def first(path, *keys):
        r = page.request.get(base + path)
        if not r.ok:
            return None
        data = r.json()
        for key in keys:
            rows = data.get(key) if isinstance(data, dict) else None
            if isinstance(rows, list) and rows:
                return rows[0].get("id")
        return None
    out["person"] = first("/api/crm/clients?limit=1", "clients")
    out["deal"] = first("/api/portfolio/transactions?limit=1", "transactions", "items")
    state = common.load_state() if hasattr(common, "load_state") else {}
    out["property"] = (state.get("subject") or {}).get("lead_id") or state.get("lead_id")
    return out


def _target(row: dict, ids: dict) -> tuple[str | None, tuple | None]:
    """(url path, (nav label, role, item label)) for one matrix row; None url = not addressable."""
    path = row["path"]
    m = re.match(r"^(/work\?type=\w+) \((?:workspace|view): ([\w-]+)\)$", path)
    if m:
        family = row["id"].split(":")[0]
        nav, role = SUBVIEW_NAV[family]
        return m.group(1), (nav, role, row["label"])
    if path.startswith("profile menu"):
        return "/", ("Profile and administration", "button", row["label"])
    if row["family"] == "Entity sheets":
        kind = row["id"].split(":")[1]
        rid = ids.get(kind)
        return (re.sub(r"\{[a-z]+\}", str(rid), path) if rid else None), None
    path = (path.replace("{slug}", "crawl-site").replace("{brokerage}", "crawl-brokerage")
            .replace("{site}", "crawl-site").replace("{token}", FAKE))
    return path, None


def _sign_in(page, base, email, creds):
    # Types the owner's two-step code when asked (demo_tenant_common.ui_sign_in).
    common.ui_sign_in(page, base, email, creds,
                      ready_selector='[aria-label="Open agent profile and settings"]')
    page.keyboard.press("Escape")   # the first-visit walkthrough


def _visit(page, base, url, step, shots: Path, name: str) -> dict:
    events = {"page_errors": [], "console_errors": [], "api_5xx": [], "api_4xx": []}
    on_err = lambda e: events["page_errors"].append(str(e)[:200])  # noqa: E731
    on_console = lambda m: m.type == "error" and events["console_errors"].append(m.text[:200])  # noqa: E731

    def on_response(r):
        p = urlparse(r.url)
        if p.netloc == urlparse(base).netloc and p.path.startswith(API_PREFIXES):
            if r.status >= 500:
                events["api_5xx"].append(f"{r.status} {r.request.method} {p.path}")
            elif r.status >= 400:
                events["api_4xx"].append(f"{r.status} {r.request.method} {p.path}")

    page.on("pageerror", on_err)
    page.on("console", on_console)
    page.on("response", on_response)
    t0 = time.time()
    result = {"url": url}
    try:
        page.goto(base + url, wait_until="domcontentloaded", timeout=45_000)
        if step:
            nav, role, label = step
            if nav == "Profile and administration":
                page.get_by_role("button", name="Open agent profile and settings").click()
            container = page.get_by_role("navigation", name=nav)
            item = container.get_by_role(role, name=re.compile(rf"^\s*{re.escape(label)}\s*$", re.I)).first
            try:
                # Workspaces load lazily: wait for the item before deciding it is absent.
                item.wait_for(state="visible", timeout=15_000)
                item.click()
            except Exception:  # noqa: BLE001
                result["not_offered"] = True
        try:
            page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:  # noqa: BLE001 — a page that keeps a socket open never idles
            result["never_idle"] = True
        page.wait_for_timeout(800)
        result["load_ms"] = int((time.time() - t0) * 1000)
        result["overflow_px"] = page.evaluate(
            "Math.max(0, document.documentElement.scrollWidth - window.innerWidth)")
        body = page.inner_text("body")[:20_000]
        hit = BOUNDARY.search(body)
        result["boundary"] = hit.group(0) if hit else None
        shot = shots / f"{name}.png"
        page.screenshot(path=str(shot), full_page=False)
        result["screenshot"] = str(shot.relative_to(REPO))
    except Exception as exc:  # noqa: BLE001
        result["navigation_error"] = str(exc).splitlines()[0][:200]
    finally:
        page.remove_listener("pageerror", on_err)
        page.remove_listener("console", on_console)
        page.remove_listener("response", on_response)
    result.update(events)
    return result


def _issues(run: dict, viewport: str, public: bool) -> list[str]:
    out = []
    if run.get("navigation_error"):
        out.append(f"navigation: {run['navigation_error']}")
    out += [f"page error: {e}" for e in run["page_errors"]]
    out += [f"API {e}" for e in run["api_5xx"]]
    if run.get("boundary"):
        out.append(f"error message on screen: '{run['boundary']}'")
    if viewport == "phone" and (run.get("overflow_px") or 0) > 1:
        out.append(f"horizontal overflow {run['overflow_px']}px at phone width")
    if not public:
        real = [e for e in run["console_errors"] if not NETWORK_NOISE.match(e)]
        out += [f"console: {e}" for e in real[:3]]
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--only", default="", help="comma-separated page ids")
    ap.add_argument("--chrome", default="/usr/bin/google-chrome")
    args = ap.parse_args(argv)
    common.refuse_production(args.base_url)
    base = args.base_url.rstrip("/")
    pages = json.loads(MATRIX.read_text())["pages"]
    if args.only:
        wanted = set(args.only.split(","))
        pages = [p for p in pages if p["id"] in wanted]
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    shots = REPO / "performance/out/crawl" / run_id
    shots.mkdir(parents=True, exist_ok=True)
    creds = common.load_or_create_credentials()
    # Owed two-step setup is done through the API before the read-only browser
    # run (whose guard blocks writes), so the UI only meets the code prompt.
    common.prepare_demo_logins(base, creds)
    blocked: list[str] = []

    def guard(route):
        req = route.request
        p = urlparse(req.url)
        if req.method in ("GET", "HEAD", "OPTIONS") or p.path in ALLOWED_WRITES:
            return route.continue_()
        blocked.append(f"{req.method} {p.path}")
        return route.abort()

    from playwright.sync_api import sync_playwright

    results: dict[str, dict] = {p["id"]: {"runs": []} for p in pages}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=args.chrome, headless=True)
        try:
            # Signed-in pages, per role and size.
            for role, account in (("owner", common.OWNER), ("agent", common.AGENT)):
                for vp_name, vp in VIEWPORTS.items():
                    ctx = browser.new_context(viewport=vp, is_mobile=vp_name == "phone", has_touch=vp_name == "phone")
                    ctx.route("**/*", guard)
                    page = ctx.new_page()
                    _sign_in(page, base, account["email"], creds)
                    ids = _ids(page, base)
                    for row in pages:
                        if row["family"] == "Outside the signed-in shell" and row["id"] != "public:site-preview":
                            continue
                        url, step = _target(row, ids)
                        if url is None:
                            results[row["id"]]["runs"].append({"role": role, "viewport": vp_name, "skipped": "no record to open"})
                            continue
                        before = len(blocked)
                        run = _visit(page, base, url, step, shots, f"{row['id'].replace(':', '_')}-{role}-{vp_name}")
                        run.update(role=role, viewport=vp_name, blocked_writes=blocked[before:])
                        results[row["id"]]["runs"].append(run)
                        print(f"{row['id']:<28} {role:<5} {vp_name:<7} {run.get('load_ms', '-')!s:>6} ms"
                              f"{'  NOT OFFERED' if run.get('not_offered') else ''}"
                              f"{'  ISSUES' if _issues(run, vp_name, False) else ''}", flush=True)
                    ctx.close()
            # Pages outside the shell, as a stranger with a bad link.
            for vp_name, vp in VIEWPORTS.items():
                ctx = browser.new_context(viewport=vp, is_mobile=vp_name == "phone", has_touch=vp_name == "phone")
                ctx.route("**/*", guard)
                page = ctx.new_page()
                for row in pages:
                    if row["family"] != "Outside the signed-in shell" or row["id"] == "public:site-preview":
                        continue
                    url, _ = _target(row, {})
                    before = len(blocked)
                    run = _visit(page, base, url, None, shots, f"{row['id'].replace(':', '_')}-anonymous-{vp_name}")
                    run.update(role="anonymous", viewport=vp_name, blocked_writes=blocked[before:])
                    results[row["id"]]["runs"].append(run)
                    print(f"{row['id']:<28} anon  {vp_name:<7} {run.get('load_ms', '-')!s:>6} ms", flush=True)
                ctx.close()
        finally:
            browser.close()

    (shots / "results.json").write_text(json.dumps(results, indent=1))
    summary = {"// generated_by": "scripts/crawl-all-pages.py (prompt 02 T02.11) — renders, not functional sign-off",
               "env": "staging" if "staging" in base else "other", "base_sha": _sha(), "run": run_id,
               "date": datetime.now(timezone.utc).date().isoformat(), "pages": {}}
    for page_id, data in results.items():
        runs = [r for r in data["runs"] if not r.get("skipped")]
        issues = sorted({f"{r['role']}/{r['viewport']}: {i}" for r in runs
                         for i in _issues(r, r["viewport"], r["role"] == "anonymous")})
        writes = sorted({w for r in runs for w in r.get("blocked_writes", [])})
        offered = [r for r in runs if not r.get("not_offered")]
        loads = sorted(r["load_ms"] for r in offered if "load_ms" in r)
        if not runs:
            status = "not_run"
        elif not offered:
            status = "not_offered"
        else:
            status = "issues" if issues else "clean"
        summary["pages"][page_id] = {
            "status": status,
            "not_offered_to": sorted({r["role"] for r in runs if r.get("not_offered")}),
            "load_ms_median": loads[len(loads) // 2] if loads else None,
            "issues": issues[:12],
            "writes_attempted_on_open": writes,
            "runs": len(runs),
        }
    SUMMARY.write_text(json.dumps(summary, indent=1) + "\n")
    counts = {}
    for v in summary["pages"].values():
        counts[v["status"]] = counts.get(v["status"], 0) + 1
    print(f"\n{counts}  -> {SUMMARY.relative_to(REPO)}  (screenshots: {shots.relative_to(REPO)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
