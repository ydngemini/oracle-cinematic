#!/usr/bin/env python3
"""Is the killer demo ready to show? READY / READY WITH LIMITATIONS / BLOCKED.

    python scripts/demo-preflight.py --base-url https://… --tenant-id <uuid> \
        [--expected-sha <git sha>] [--json out.json]

Read-only. It signs in as the demo owner (the same login the presenter
uses), reads what the product would show, and checks the configuration the
live moments depend on. It never sends a text, places a call, or writes a
row; the one "real" request it can make is a single chat turn
(--chat-check), which is an ordinary Neoh question with no tools approved.

A check is BLOCKING when the demo cannot honestly run without it (the app is
down, the wrong release, no demo tenant, Neoh cannot answer, the property or
its 3D space is missing, actions left pending from a previous run). It is a
LIMITATION when the demo runs and the presenter must say so (texting not set
up, calendar not connected, outside calling hours, no realtime voice).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import demo_tenant_common as common  # noqa: E402

READY = "READY"
LIMITED = "READY WITH LIMITATIONS"
BLOCKED = "BLOCKED"

PASS, WARN, FAIL = "pass", "limitation", "blocked"


@dataclass
class Check:
    name: str
    status: str
    detail: str


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str, *, blocking: bool = True) -> None:
        self.checks.append(Check(name, PASS if ok else (FAIL if blocking else WARN), detail))

    @property
    def verdict(self) -> str:
        return verdict([c.status for c in self.checks])


def verdict(statuses: list[str]) -> str:
    if FAIL in statuses:
        return BLOCKED
    if WARN in statuses:
        return LIMITED
    return READY


def within_calling_hours(now_utc: datetime, tz: str = "America/New_York") -> bool:
    local = now_utc.astimezone(ZoneInfo(tz))
    return 8 <= local.hour < 20


def allowlist_is_exactly_the_demo_phone(raw: str) -> bool:
    entries = [e.strip() for e in (raw or "").split(",") if e.strip()]
    return entries == [common.DEMO_RECIPIENT]


def _env_map(component: dict) -> dict:
    return {e.get("key"): e for e in component.get("envs") or []}


def live_voice_from_spec(spec: dict | None) -> tuple[bool, str]:
    """Does the DEPLOYED api hold a live call voice? Read from the app spec, not
    this machine's environment (which says nothing about the deployment).

    Mirrors plivo_call_handler.plivo_qwen_enabled: with the elevenlabs provider
    both ELEVENLABS_API_KEY and ELEVENLABS_AGENT_ID must be set; otherwise the
    Qwen flag must be on and its key set. A SECRET is set when the spec carries
    an encrypted value for it.
    """
    if not spec:
        return False, "deployed app spec unreadable (doctl) — live voice unverified"
    api = next((c for c in spec.get("services") or [] if c.get("name") == "api"), None)
    if api is None:
        return False, "no api service in the deployed spec"
    envs = _env_map(api)

    def value(key: str) -> str:
        return str((envs.get(key) or {}).get("value") or "").strip()

    provider = value("ORACLE_PLIVO_REALTIME_PROVIDER").lower() or "qwen"
    if provider == "elevenlabs":
        missing = [k for k in ("ELEVENLABS_API_KEY", "ELEVENLABS_AGENT_ID") if not value(k)]
        if missing:
            return False, f"provider elevenlabs, but {', '.join(missing)} not set on the api"
        return True, "ElevenLabs Agents voices answered calls (key and agent id set on the api)"
    if value("ORACLE_PLIVO_QWEN_REALTIME_ENABLED").lower() in ("", "0", "false", "no", "off"):
        return False, "provider qwen, realtime flag off"
    if not value("DASHSCOPE_API_KEY"):
        return False, "provider qwen, DASHSCOPE_API_KEY not set on the api"
    return True, "Qwen realtime voices answered calls"


def _deployed_spec(base_url: str) -> dict | None:
    """The spec of the App Platform app serving base_url, or None."""
    import yaml
    from urllib.parse import urlparse

    host = urlparse(base_url).hostname or ""
    cmd = ["doctl", "apps", "list", "-o", "json"]
    env = {k: v for k, v in os.environ.items() if k != "DOCKER_HOST"}
    try:
        apps = json.loads(subprocess.run(cmd, capture_output=True, text=True, timeout=60,
                                         env=env, check=True).stdout or "[]")
        app = next((a for a in apps if urlparse(a.get("live_url") or "").hostname == host), None)
        if app is None:
            return None
        out = subprocess.run(["doctl", "apps", "spec", "get", app["id"]], capture_output=True,
                             text=True, timeout=60, env=env, check=True).stdout
        return yaml.safe_load(out)
    except Exception:  # noqa: BLE001 — unverifiable reads as "not live", never as live
        return None


def _head_sha() -> str:
    try:
        return subprocess.run(["git", "-C", str(common.REPO), "rev-parse", "HEAD"],
                              capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


async def run(base_url: str, tenant_id: str, expected_sha: str, *, chat_check: bool) -> Report:
    import httpx

    common.refuse_production(base_url)
    rep = Report()
    http = httpx.Client(base_url=base_url, timeout=30)

    # ── the app is up, and it is the release we think it is ────────────────
    try:
        web = http.get("/")
        rep.add("frontend", web.status_code == 200 and "<div id" in web.text,
                f"GET / → {web.status_code}")
    except Exception as exc:  # noqa: BLE001
        rep.add("frontend", False, f"unreachable: {type(exc).__name__}")
    try:
        health = http.get("/health").json()
        rep.add("api", health.get("status") == "ok", f"/health status={health.get('status')}")
        outbound = health.get("outbound") or {}
        rep.add("recovery mode (staging safety)", outbound.get("side_effects") == "blocked",
                f"outbound side effects: {outbound.get('side_effects')}")
        count = outbound.get("demo_recipient_allowlist_count")
        rep.add("demo recipient allowlist (live)", count == 1,
                f"{count} allowlisted recipient(s) honoured by the running instance")
    except Exception as exc:  # noqa: BLE001
        rep.add("api", False, f"/health unreadable: {type(exc).__name__}")
    source = os.getenv("DEMO_ALLOWLIST_SOURCE")
    if source is not None:
        rep.add("demo recipient allowlist (configured)", allowlist_is_exactly_the_demo_phone(source),
                "configured value is exactly the operator's own phone"
                if allowlist_is_exactly_the_demo_phone(source)
                else "configured allowlist is NOT exactly the operator's phone")
    try:
        version = http.get("/version").json()
        sha = version.get("git_sha", "")
        rep.add("release", bool(expected_sha) and sha == expected_sha,
                f"running {sha[:12]} (expected {expected_sha[:12] or '?'}), "
                f"migrations at {version.get('migration_head')}")
        workers = http.get("/health/workers")
        wjson = workers.json() if workers.status_code in (200, 503) else {}
        rep.add("worker", workers.status_code == 200, f"/health/workers → {workers.status_code}"
                + (f" ({wjson.get('reason')})" if wjson.get("reason") else ""))
    except Exception as exc:  # noqa: BLE001
        rep.add("release", False, f"/version unreadable: {type(exc).__name__}")

    # ── the demo tenant and its data (read-only DB) ─────────────────────────
    conn = await common.connect_admin()
    try:
        row = await conn.fetchrow(
            "SELECT id::text AS id, slug, is_demo, name FROM tenants WHERE id = $1::uuid", tenant_id)
        try:
            common.assert_demo_tenant(dict(row) if row else None, tenant_id)
            rep.add("demo tenant", True, f"{row['name']} ({row['slug']}, is_demo)")
        except common.DemoSafetyError as exc:
            rep.add("demo tenant", False, str(exc))
            return rep
        pending = await conn.fetch(
            """SELECT state, count(*) AS n FROM command_executions
                WHERE tenant_id = $1::uuid
                  AND state IN ('awaiting_approval','queued','executing','reconciliation_required')
                GROUP BY state""", tenant_id)
        rep.add("no stale pending actions", not pending,
                "none" if not pending else ", ".join(f"{r['n']} {r['state']}" for r in pending)
                + " — run reset-demo-tenant.py")
        subject = await conn.fetchrow(
            """SELECT l.id::text AS listing_id, l.lead_id::text AS lead_id, l.status, l.price
                 FROM listings l WHERE l.tenant_id = $1::uuid AND l.address = $2""",
            tenant_id, common.SUBJECT_ADDRESS)
        rep.add("demo property", bool(subject and subject["status"] == "active"),
                f"{common.SUBJECT_ADDRESS}: "
                + (f"{subject['status']}, ${float(subject['price']):,.0f}" if subject else "missing"))
        sarah = await conn.fetchrow(
            """SELECT c.id::text AS id, c.phone, ac.state_code, ac.timezone
                 FROM clients c LEFT JOIN agent_contacts ac ON ac.id = c.contact_id
                WHERE c.tenant_id = $1::uuid AND c.full_name = 'Sarah Johnson'
                  AND c.archived_at IS NULL""", tenant_id)
        rep.add("buyer Sarah Johnson", bool(sarah and sarah["phone"] == common.DEMO_RECIPIENT
                                              and sarah["state_code"]),
                "phone is the operator's own number; state/timezone on file"
                if sarah else "missing")
        consent = await conn.fetch(
            """SELECT channel, consent_type FROM outreach_consent
                WHERE tenant_id = $1::uuid AND contact = $2 AND revoked_at IS NULL""",
            tenant_id, common.DEMO_RECIPIENT)
        channels = {r["channel"]: r["consent_type"] for r in consent}
        rep.add("consent on file (sms + written voice)",
                channels.get("sms") is not None and channels.get("voice") == "express_written",
                f"channels: {sorted(channels)}")
        route = await conn.fetchrow(
            """SELECT provider, outbound_verification_status FROM telephony_routes
                WHERE tenant_id = $1::uuid AND agent_id = $2 AND active""",
            tenant_id, common.OWNER["email"])
        rep.add("calling number (voice)", bool(route and route["outbound_verification_status"] == "verified"),
                f"{route['provider']} route {route['outbound_verification_status']}" if route
                else "Jordan has no calling number — 'Call Sarah' will fail honestly",
                blocking=False)
        messaging = await conn.fetchrow(
            "SELECT hosted_order_status FROM messaging_routes WHERE tenant_id = $1::uuid LIMIT 1",
            tenant_id)
        rep.add("texting (SMS)", bool(messaging and messaging["hosted_order_status"] == "active"),
                "business number ready for texts" if messaging and messaging["hosted_order_status"] == "active"
                else "no registered texting number — an approved text will be refused before any "
                     "carrier request (say so; use the call)", blocking=False)
        calendar = await conn.fetchval(
            """SELECT count(*) FROM provider_credentials WHERE tenant_id = $1::uuid
                AND provider = 'google' AND disabled_at IS NULL""", tenant_id)
        rep.add("calendar", bool(calendar), "Google Calendar connected" if calendar
                else "no calendar connected — 'schedule a showing' stages an approval that "
                     "cannot execute; show the honest next step instead", blocking=False)
        space = None
        if subject:
            space = await conn.fetchrow(
                """SELECT id::text AS id, provenance FROM property_media
                    WHERE lead_id = $1::uuid AND kind = 'splat' AND superseded_at IS NULL
                    ORDER BY created_at DESC LIMIT 1""", subject["lead_id"])
    finally:
        await conn.close()

    rep.add("calling hours (recipient local time)", within_calling_hours(datetime.now(timezone.utc)),
            "inside 8am–8pm ET" if within_calling_hours(datetime.now(timezone.utc))
            else "outside 8am–8pm ET — the compliance gate will refuse a text or call (correctly)",
            blocking=False)

    # ── signed in as the presenter: what the product actually shows ─────────
    creds = common.load_or_create_credentials()
    api = common.Api(base_url)
    try:
        api.login(common.OWNER["email"], creds[common.OWNER["email"]])
        brief = api.get("/api/command-center")
        cards = (brief.get("attention") or {}).get("opportunities") or []
        hit = next((c for c in cards if c.get("headline") == "May fit Sarah Johnson"), None)
        rep.add("Home shows the opportunity", hit is not None,
                f"Home card: {hit['subject']} — {hit['headline']}" if hit
                else f"{len(cards)} card(s), none for Sarah")
        ai = api.get("/api/ai/chat/status")
        ai_ok = bool(ai.get("available", ai.get("enabled", ai.get("ok"))))
        rep.add("Neoh (AI) available", ai_ok, f"chat status: {json.dumps(ai)[:160]}")
        if subject:
            dossier = api.get(f"/api/leads/{subject['lead_id']}/dossier")
            names = [m["name"] for m in dossier.get("buyer_matches") or []]
            rep.add("property shows the buyer match", "Sarah Johnson" in names,
                    f"buyers who may fit: {names}")
            tour = api.get(f"/api/crm/property-tour?lead_id={subject['lead_id']}")
            stream = tour.get("splat_stream_url") or tour.get("splat_url")
            ok_bytes = 0
            if stream:
                r = api.http.get(stream, headers={"Range": "bytes=0-1023"})
                ok_bytes = len(r.content) if r.status_code in (200, 206) else 0
            rep.add("Neoh Space asset loads", bool(stream and ok_bytes),
                    f"{'demo space (labelled not this home)' if tour.get('is_this_property') is False else 'space'}"
                    f"; first KB → {ok_bytes} bytes" if stream else "no 3D asset on the property")
            if space and space["provenance"] != "captured":
                rep.add("3D is a real capture of the home", False,
                        "the Space is a generated demo room, labelled 'not this home' — say so",
                        blocking=False)
        voice_live, voice_detail = live_voice_from_spec(_deployed_spec(base_url))
        rep.add("realtime AI voice on calls", voice_live,
                voice_detail if voice_live else
                f"{voice_detail} — an answered call speaks the AI disclosure and says the agent "
                "will follow up; there is no live AI conversation on this environment", blocking=False)
        if chat_check:
            rep.add("chat turn", True, "not run here — the Playwright E2E exercises a real turn",
                    blocking=False)
    except common.ApiError as exc:
        rep.add("presenter session", False, str(exc))
    finally:
        api.close()
        http.close()
    return rep


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--expected-sha", default="")
    parser.add_argument("--json", type=Path)
    parser.add_argument("--chat-check", action="store_true")
    args = parser.parse_args(argv)
    expected = args.expected_sha or _head_sha()
    report = asyncio.run(run(args.base_url, args.tenant_id, expected, chat_check=args.chat_check))
    width = max(len(c.name) for c in report.checks)
    for c in report.checks:
        mark = {"pass": "ok  ", "limitation": "LIM ", "blocked": "FAIL"}[c.status]
        print(f"  {mark} {c.name.ljust(width)}  {c.detail}")
    print(f"\n{report.verdict}")
    if args.json:
        args.json.write_text(json.dumps({"verdict": report.verdict,
                                         "checks": [asdict(c) for c in report.checks]}, indent=2))
    return 0 if report.verdict != BLOCKED else 1


if __name__ == "__main__":
    raise SystemExit(main())
