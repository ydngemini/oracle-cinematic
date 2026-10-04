#!/usr/bin/env python3
"""Seed (or top up) the killer-demo brokerage "Northstar Realty (Demo)".

    # dry run (default): says what it would create, writes nothing
    python scripts/seed-demo-tenant.py --base-url https://neoh-staging-….ondigitalocean.app
    # do it
    python scripts/seed-demo-tenant.py --base-url … --execute

Idempotent: every step looks for what it would create first, so a second run
creates nothing. Product paths are used wherever the product has one — the
same HTTP API the browser calls, signed in as the demo users:

    /auth/register, /auth/policy-acceptance, /auth/accept-invite,
    PATCH /api/brokerage/profile, POST /api/agents/profile,
    POST /api/crm/clients, /api/crm/contacts, /api/crm/listings,
    /api/crm/showings, /api/crm/clients/{id}/notes,
    POST /api/compliance/outreach/consent

and, in-process, the product's own service functions:

    brokerage_onboarding.create_invitations   (the link is otherwise e-mailed)
    reconstruction_worker._store_splat         (writes the space to object storage)

Raw SQL only where NO product path exists, each marked `RAW SQL:` below:

    * tenants.is_demo + the fixed slug — nothing in the product sets them;
    * the subscription row — only Stripe's webhook writes one, and a demo
      tenant must never create a real Stripe subscription;
    * the Plivo caller-ID route — the product's connect flow BUYS a number and
      sends a verification OTP; the demo uses the number the Plivo account
      already owns, checked first with a read-only Plivo API call;
    * the 3D asset's property_media row — the scan upload accepts only PLY/SPZ
      and marks it a capture of THIS home; the demo space is a generated room
      (provenance 'synthetic'), which the product then labels "Demo space
      (not this home)".

Environment (never printed): ORACLE_DB_HOST/PORT/NAME/ADMIN_USER/
ADMIN_PASSWORD + ORACLE_DB_CA_CERT (admin connection), ORACLE_STORAGE_BACKEND
and ORACLE_S3_* (the staging bucket), PLIVO_AUTH_ID/PLIVO_AUTH_TOKEN and
DEMO_CALLER_ID (the Plivo-owned number calls are placed from).
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent))
import demo_tenant_common as common  # noqa: E402
from demo_tenant_common import (  # noqa: E402
    AGENT, CLIENTS, DEMO_RECIPIENT, LISTINGS, OWNER, SHOWINGS, SUBJECT_ADDRESS,
    TENANT_NAME, TENANT_SLUG, Api, ApiError,
)

FIXTURE_SPACE = common.REPO / "scripts" / "fixtures" / "neoh-space-demo-room.sog"
CONSENT_PROOF = (
    "Demo data: +13024078981 is the operator's own phone. Its owner authorised "
    "test texts and AI calls to it for the Neoh sales demo on 2026-10-04. "
    "No other recipient is allowlisted."
)


class Plan:
    """Collects what was (or would be) done, for the report."""

    def __init__(self, execute: bool):
        self.execute = execute
        self.lines: list[str] = []

    def note(self, line: str) -> None:
        self.lines.append(line)
        print(("  " if self.execute else "  [dry-run] ") + line)


def _plivo_owns(number: str) -> bool:
    """Read-only Plivo check that the account owns `number` (voice enabled)."""
    auth_id = os.getenv("PLIVO_AUTH_ID", "")
    token = os.getenv("PLIVO_AUTH_TOKEN", "")
    if not (auth_id and token and number):
        return False
    req = urllib.request.Request(
        f"https://api.plivo.com/v1/Account/{auth_id}/Number/{number.lstrip('+')}/",
        headers={"Authorization": "Basic " + base64.b64encode(f"{auth_id}:{token}".encode()).decode()},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            body = json.loads(r.read() or b"{}")
    except Exception:  # noqa: BLE001 — "cannot prove ownership" is the answer
        return False
    return bool(body.get("voice_enabled")) and str(body.get("number", "")).lstrip("+") == number.lstrip("+")


async def seed(base_url: str, execute: bool, *, with_space: bool = True) -> dict:
    common.refuse_production(base_url)
    plan = Plan(execute)
    creds = common.load_or_create_credentials()
    state: dict = {"base_url": base_url, "tenant_slug": TENANT_SLUG}
    conn = await common.connect_admin()
    api = Api(base_url)
    try:
        # ── 1. the brokerage and its owner (product: /auth/register) ────────
        owner_row = await conn.fetchrow(
            "SELECT u.tenant_id::text AS tenant_id, t.slug, t.is_demo FROM users u "
            "JOIN tenants t ON t.id = u.tenant_id WHERE lower(u.agent_id) = $1",
            OWNER["email"],
        )
        if owner_row is None:
            plan.note(f"register {OWNER['email']} → new brokerage {TENANT_NAME!r}")
            if execute:
                api.post("/auth/register", {
                    "email": OWNER["email"], "password": creds[OWNER["email"]],
                    "full_name": OWNER["full_name"], "company": TENANT_NAME,
                })
                owner_row = await conn.fetchrow(
                    "SELECT u.tenant_id::text AS tenant_id, t.slug, t.is_demo FROM users u "
                    "JOIN tenants t ON t.id = u.tenant_id WHERE lower(u.agent_id) = $1",
                    OWNER["email"],
                )
        if owner_row is None:
            print("  (dry run stops here: the brokerage does not exist yet)")
            return state
        tenant_id = owner_row["tenant_id"]
        state["tenant_id"] = tenant_id
        existing_demo = await common.find_demo_tenant(conn)
        if existing_demo and existing_demo["id"] != tenant_id:
            raise common.DemoSafetyError(
                f"another tenant ({existing_demo['id']}) already holds the demo slug")

        # RAW SQL: the demo marker (no product path sets it — by design).
        if not (owner_row["is_demo"] and owner_row["slug"] == TENANT_SLUG):
            plan.note(f"mark tenant {tenant_id} is_demo + slug {TENANT_SLUG}")
            if execute:
                await conn.execute(
                    "UPDATE tenants SET is_demo = true, slug = $2 WHERE id = $1::uuid",
                    tenant_id, TENANT_SLUG,
                )

        # ── 2. sign in as Jordan, the way the browser does ──────────────────
        if not execute:
            return state
        api.login(OWNER["email"], creds[OWNER["email"]])
        api.accept_policies()
        api.request("PATCH", "/api/brokerage/profile", json_body={
            "name": TENANT_NAME, "org_type": "brokerage", "primary_state": "DE",
        })
        api.post("/api/agents/profile", {
            "experience_level": "closer", "target_zips": ["19801", "19803", "19806"],
            "monthly_deal_target": "3-5",
        })
        plan.note("Jordan signed in; policies accepted; brokerage + agent profile set")

        # RAW SQL: demo billing. Only Stripe's webhook writes subscriptions; a
        # demo must never create a real Stripe subscription. The ids say demo.
        sub = await conn.fetchval(
            "SELECT status FROM subscriptions WHERE tenant_id = $1::uuid "
            "ORDER BY created_at DESC LIMIT 1", tenant_id)
        if sub != "active":
            plan.note("demo subscription row (status active, ids demo_…)")
            await conn.execute(
                """INSERT INTO subscriptions (tenant_id, stripe_customer_id,
                       stripe_subscription_id, status, current_period_end)
                   VALUES ($1::uuid, 'demo_cus_northstar', $2, 'active', now() + interval '365 days')
                   ON CONFLICT (stripe_subscription_id) DO UPDATE SET status = 'active',
                       current_period_end = now() + interval '365 days'""",
                tenant_id, f"demo_sub_northstar_{tenant_id[:8]}",
            )

        # ── 3. the second agent (service function + /auth/accept-invite) ───
        if not await conn.fetchval("SELECT 1 FROM users WHERE lower(agent_id) = $1", AGENT["email"]):
            common.backend_on_path()
            import brokerage_onboarding
            from tenancy import Role, TenantContext

            ctx = TenantContext(agent_id=OWNER["email"], tenant_id=tenant_id, role=Role.BROKER_OWNER)
            result = await brokerage_onboarding.create_invitations(conn, ctx, [AGENT["email"]], "agent")
            token = dict(result["_deliveries"])[AGENT["email"]]
            invitee = Api(base_url)
            try:
                invitee.post("/auth/accept-invite", {
                    "token": token, "password": creds[AGENT["email"]],
                    "full_name": AGENT["full_name"],
                })
                invitee.accept_policies()
            finally:
                invitee.close()
            plan.note(f"second agent {AGENT['full_name']} joined through an invitation")

        # ── 4. clients (POST /api/crm/clients) + notes ──────────────────────
        client_ids: dict[str, str] = {}
        for c in CLIENTS:
            cid = await conn.fetchval(
                "SELECT id::text FROM clients WHERE tenant_id = $1::uuid AND lower(email) = $2 "
                "AND archived_at IS NULL", tenant_id, c["email"])
            if cid is None:
                created = api.post("/api/crm/clients", {
                    "full_name": c["full_name"], "email": c["email"], "phone": c["phone"],
                    "client_type": c["client_type"], "stage": c["stage"],
                    "lead_score": c["lead_score"], "tags": c["tags"],
                    "preferences": c["preferences"],
                })
                cid = str((created.get("client") or created)["id"])
                plan.note(f"client {c['full_name']}")
            client_ids[c["key"]] = cid
            for body in c.get("notes") or []:
                if not await conn.fetchval(
                        "SELECT 1 FROM client_notes WHERE client_id = $1::uuid AND body = $2", cid, body):
                    api.post(f"/api/crm/clients/{cid}/notes", {"body": body, "pinned": False})
            # Contact record: state + timezone (quiet hours) and, for Sarah only,
            # the consent the operator gave for their own phone.
            contact = c.get("contact") or {}
            has_contact = await conn.fetchval(
                "SELECT contact_id IS NOT NULL FROM clients WHERE id = $1::uuid", cid)
            if contact and not has_contact:
                now = datetime.now(timezone.utc).isoformat()
                granted = bool(contact.get("consent"))
                grant = {"granted": granted, "captured_at": now if granted else None,
                         "source": "demo_operator_attestation" if granted else None}
                api.post("/api/crm/contacts", {
                    "full_name": c["full_name"], "email": c["email"], "phone": c["phone"],
                    "timezone": contact["timezone"], "state_code": contact["state_code"],
                    "preferred_channel": contact.get("preferred_channel", "none"),
                    "consent": {"email": {"granted": False}, "sms": grant, "voice": grant},
                    "source": "demo_seed", "client_id": cid,
                })
        state["clients"] = client_ids

        # The TCPA ledger the send gate reads (POST /api/compliance/outreach/consent).
        for channel in ("sms", "voice"):
            if not await conn.fetchval(
                    "SELECT 1 FROM outreach_consent WHERE tenant_id = $1::uuid AND contact = $2 "
                    "AND channel = $3 AND revoked_at IS NULL", tenant_id, DEMO_RECIPIENT, channel):
                api.post("/api/compliance/outreach/consent", {
                    "contact": DEMO_RECIPIENT, "channel": channel,
                    "consent_type": "express_written", "state_code": "DE",
                    "proof_source": "demo_operator_attestation", "proof_text": CONSENT_PROOF,
                    "client_id": client_ids["sarah"],
                })
                plan.note(f"{channel} consent for Sarah's number (operator's own phone)")

        # ── 5. listings (POST /api/crm/listings) ────────────────────────────
        listing_ids: dict[str, dict] = {}
        for item in LISTINGS:
            row = await conn.fetchrow(
                "SELECT id::text AS id, lead_id::text AS lead_id FROM listings "
                "WHERE tenant_id = $1::uuid AND address = $2", tenant_id, item["address"])
            if row is None:
                body = {k: item[k] for k in ("address", "price", "beds", "baths", "sqft",
                                             "status", "features", "property_type")}
                if item.get("seller"):
                    body["seller_client_id"] = client_ids[item["seller"]]
                created = api.post("/api/crm/listings", body)["listing"]
                row = {"id": created["id"], "lead_id": created["lead_id"]}
                plan.note(f"listing {item['address']} ({item['status']})")
            listing_ids[item["key"]] = dict(row)
        state["listings"] = listing_ids
        state["subject"] = {"address": SUBJECT_ADDRESS, **listing_ids["main"]}

        # ── 6. Sarah's recent interest (POST /api/crm/showings) ─────────────
        for s in SHOWINGS:
            cid, lid = client_ids[s["client"]], listing_ids[s["listing"]]["id"]
            if not await conn.fetchval(
                    "SELECT 1 FROM showings WHERE client_id = $1::uuid AND listing_id = $2::uuid",
                    cid, lid):
                api.post("/api/crm/showings", {
                    "client_id": cid, "listing_id": lid,
                    "shown_at": (datetime.now(timezone.utc) - timedelta(days=s["days_ago"])).isoformat(),
                    "feedback": s["feedback"], "outcome": s["outcome"],
                })
                plan.note(f"showing: Sarah at {s['listing']} ({s['outcome']})")

        # ── 7. Jordan's calling number ──────────────────────────────────────
        caller_id = os.getenv("DEMO_CALLER_ID", "").strip()
        route = await conn.fetchrow(
            "SELECT provider, voice_caller_id_e164, outbound_verification_status FROM telephony_routes "
            "WHERE tenant_id = $1::uuid AND agent_id = $2", tenant_id, OWNER["email"])
        if caller_id and not (route and route["outbound_verification_status"] == "verified"
                              and route["voice_caller_id_e164"] == caller_id):
            if not _plivo_owns(caller_id):
                plan.note("SKIPPED calling number: the Plivo account does not own DEMO_CALLER_ID")
            else:
                # RAW SQL: see the module docstring. Ownership was just checked
                # against Plivo's own Number API, which is the verification.
                await conn.execute(
                    """INSERT INTO telephony_routes (tenant_id, agent_id, inbound_did, provider,
                           provider_account_id, voice_caller_id_e164, voice_caller_id_verified,
                           outbound_verification_status, outbound_verification_last_tested_at,
                           intake_mode, forwarding_mode, forward_on_request,
                           forward_when_ai_unavailable, active)
                       VALUES ($1::uuid, $2, $3, 'plivo', $4, $3, true, 'verified', now(),
                               'auto', 'none', false, false, true)
                       ON CONFLICT (tenant_id, agent_id) DO UPDATE SET
                           inbound_did = EXCLUDED.inbound_did, provider = 'plivo',
                           provider_account_id = EXCLUDED.provider_account_id,
                           twilio_account_sid = NULL,
                           voice_caller_id_e164 = EXCLUDED.voice_caller_id_e164,
                           voice_caller_id_verified = true,
                           outbound_verification_status = 'verified',
                           outbound_verification_last_tested_at = now(), active = true,
                           updated_at = now()""",
                    tenant_id, OWNER["email"], caller_id, os.environ["PLIVO_AUTH_ID"],
                )
                plan.note("Jordan's calling number: the Plivo-owned DID (ownership checked read-only)")

        # ── 8. the Neoh Space (generated demo room, labelled as such) ───────
        lead_id = listing_ids["main"]["lead_id"]
        has_space = await conn.fetchval(
            "SELECT 1 FROM property_media WHERE lead_id = $1::uuid AND kind = 'splat' "
            "AND superseded_at IS NULL", lead_id)
        if with_space and not has_space:
            common.backend_on_path()
            import reconstruction_worker

            media_id = str(uuid4())
            url, s3_key = await reconstruction_worker._store_splat(
                FIXTURE_SPACE, media_id, provider="neoh-demo-room", address=lead_id,
                tenant_id=tenant_id,
                extra_manifest={"note": "Generated demo room (Neoh stub). Not a capture "
                                        "of any real property."},
            )
            # RAW SQL: see the module docstring (no product path for a
            # synthetic space outside development).
            await conn.execute(
                """INSERT INTO property_media (id, tenant_id, lead_id, kind, url, s3_key,
                       sort_order, provenance, generator, caption)
                   VALUES ($1::uuid, $2::uuid, $3::uuid, 'splat', $4, $5, 100,
                           'synthetic', 'neoh-demo-room',
                           'Generated demo space — not a capture of this home')""",
                media_id, tenant_id, lead_id, url, s3_key,
            )
            plan.note(f"Neoh Space: generated demo room stored at {s3_key} (synthetic)")

        state["seeded_at"] = datetime.now(timezone.utc).isoformat()
        common.save_state(state)
        return state
    finally:
        api.close()
        await conn.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--execute", action="store_true", help="write (default: dry run)")
    parser.add_argument("--no-space", action="store_true", help="skip the 3D asset")
    args = parser.parse_args(argv)
    try:
        state = asyncio.run(seed(args.base_url, args.execute, with_space=not args.no_space))
    except (common.DemoSafetyError, ApiError) as exc:
        print(f"REFUSED/FAILED: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({k: state.get(k) for k in ("tenant_id", "subject")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
