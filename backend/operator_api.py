"""Operator API — running the first ten brokerages without SQL.

The admin console (admin_ops.py) answers "what is the platform doing". This
module answers the questions an operator asks about CUSTOMERS:

  * /brokerages               who is live, what each can use, who needs action
  * /brokerages/{id}/diagnostics  one tenant's support bundle (§11)
  * /billing/exceptions       no subscription, payment issue, canceled, webhook
                              refusals, usage-metering backlog (§20)
  * /mls/feeds                every feed, licensed or not, and who is entitled (§21)
  * /comms                    voice + texting state per brokerage (§22)
  * /ai                       providers, model, failures, latency, tool failures (§23)
  * /pilot-metrics            the small set of product metrics (§18, §19)
  * /release                  API build vs worker builds vs migration head

Same prefix and the same platform-admin gate as admin_ops: this extends the
existing admin surface rather than standing up a second one.

Three rules hold for every route here.

**Never customer content.** Every response is built from named columns — never
``dict(row)`` — so a column added to a table later cannot leak through. Free
text a provider or a customer could have written (job errors, carrier
rejection prose, SMS bodies, transcripts, mailbox addresses) is either left
out or reduced to a code. ``test_operator_api`` seeds sentinel secrets into
every fake row and asserts none reach the JSON.

**Never duplicate the RLS predicate.** Cross-tenant reads run inside
``tenant_tx(ctx)`` with the admin's own verified context; the platform login's
policy is what makes them legal. Per-tenant filters here are explicit
``tenant_id = $1`` / ``= ANY($1)`` on the tenant being described — never
``app_current_tenant()``, which would hide every other brokerage.

**Bounded.** One connection, ``SET LOCAL statement_timeout``, time windows and
LIMITs on every query, and each section in its own SAVEPOINT so one missing
table degrades one section (reported in ``unavailable``) instead of the page.
The per-tenant setup derivation is paginated because it is the only part whose
cost grows with the number of brokerages.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from admin_ops import require_platform_admin
from db.connection import tenant_tx
from tenancy import Role, TenantContext

logger = logging.getLogger("oracle.operator_api")

router = APIRouter(prefix="/api/admin", tags=["Platform Operator"])

#: Per-statement ceiling. component_health uses 2.5 s for its probes; these are
#: operator reads that may scan a day of rows, so a little more headroom.
STATEMENT_TIMEOUT_MS = 3000

# Capability vocabulary for operators. Deliberately coarser than the setup
# screen's: an operator triaging ten customers needs "fine / not done yet /
# broken / not used", not the seven-state onboarding machine.
READY = "READY"
NEEDS_SETUP = "NEEDS_SETUP"
NEEDS_ATTENTION = "NEEDS_ATTENTION"
NOT_USED = "NOT_USED"

OPERATOR_CAPABILITIES = ("phone", "messaging", "email", "calendar", "mls", "billing")

_PLATFORM_TENANT_ID = os.getenv(
    "ORACLE_PLATFORM_TENANT_ID", "00000000-0000-0000-0000-000000000000"
)

_PAYMENT_ISSUE = ("past_due", "unpaid", "incomplete", "incomplete_expired", "paused")
_ENTITLED = ("active", "trialing", "past_due")

# Things an operator might expect here that the product does not record. Said
# out loud so an empty column is never mistaken for "zero".
NOT_TRACKED = {
    "usage_limits": "No plan limits exist: billing is one flat plan and usage is metered "
                    "for history only (billing_usage.py), so nobody can approach a limit.",
    "support_issues": "Support runs by email; no ticket table exists in the product.",
    "cancel_at_period_end": "Stripe holds scheduled cancellations; the subscription stays "
                            "'active' here until it actually ends.",
}

_MIGRATIONS_DIR = Path(__file__).resolve().parent / "db" / "migrations"


# ---------------------------------------------------------------------------
# Safety helpers — what may leave this module
# ---------------------------------------------------------------------------

_CODE = re.compile(r"^[A-Za-z0-9_.:/\-]{1,96}$")
_SECRETISH = re.compile(
    r"(?i)(bearer\s+\S+|(access_?token|api_?key|token|secret|password|signature|sig|key)=\S+"
    r"|sk_(live|test)_\S+|whsec_\S+|[A-Za-z0-9+/_\-]{24,})"
)
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_DIGITS = re.compile(r"\+?\d[\d\s().\-]{5,}\d")


def safe_code(value: Any) -> Optional[str]:
    """An error CODE, or 'unclassified'. Free text never passes.

    Job and tool errors carry a code column and a prose column; the prose can
    quote a customer's message or a provider's echo of it. Only something that
    looks like an identifier (``mls_auth_failed``, ``RATE_LIMIT``) is reported.
    """
    if value is None:
        return None
    text = str(value).strip()
    return text if _CODE.fullmatch(text) else "unclassified"


def scrub(value: Any, limit: int = 160) -> Optional[str]:
    """Provider prose an operator genuinely needs (a 10DLC rejection reason),
    with anything shaped like a credential, an email address or a phone/EIN
    number removed, then truncated."""
    if value is None:
        return None
    text = " ".join(str(value).split())
    if not text:
        return None
    text = _SECRETISH.sub("[redacted]", text)
    text = _EMAIL.sub("[email]", text)
    text = _DIGITS.sub("[number]", text)
    return text[:limit] + ("…" if len(text) > limit else "")


def mask_e164(value: Any) -> Optional[str]:
    """A business number reduced to its last four digits."""
    if not value:
        return None
    digits = re.sub(r"\D", "", str(value))
    return f"•••{digits[-4:]}" if len(digits) >= 4 else "•••"


def _iso(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _uuid_or_422(raw: str) -> str:
    try:
        return str(uuid.UUID(str(raw)))
    except ValueError:
        raise HTTPException(422, "tenant_id must be a UUID") from None


class _Sections:
    """Runs each read in its own SAVEPOINT and remembers which ones failed.

    A failed statement poisons a Postgres transaction; without the savepoint,
    one un-migrated table would take every later section down with it.
    """

    def __init__(self, conn) -> None:
        self.conn = conn
        self.unavailable: list[str] = []

    async def run(self, name: str, fn: Callable[[], Awaitable[Any]], default: Any) -> Any:
        try:
            async with self.conn.transaction():
                return await fn()
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001 — one section, not the page
            logger.warning("operator section %s unavailable: %s", name, type(exc).__name__)
            self.unavailable.append(name)
            return default


async def _start(conn) -> None:
    await conn.execute(f"SET LOCAL statement_timeout = {int(STATEMENT_TIMEOUT_MS)}")


def _db_unavailable(exc: Exception) -> HTTPException:
    logger.error("Operator query failed (Memory Core offline?): %s", type(exc).__name__)
    return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Memory Core offline.")


# ---------------------------------------------------------------------------
# Capability mapping (pure — unit tested without a database)
# ---------------------------------------------------------------------------

def _cap(state: str, reason: str) -> dict[str, str]:
    return {"state": state, "reason": reason}


def map_capabilities(
    setup: Optional[dict],
    *,
    subscription: Optional[dict],
    credentials: Optional[dict],
    registration: Optional[dict],
) -> dict[str, dict[str, str]]:
    """Operator states for {phone, messaging, email, calendar, mls, billing}.

    Phone, texting, billing and MLS come from compute_setup_state, which
    derives them from the live tables. Email and calendar come from
    provider_credentials (existence and validity only), falling back to what
    the brokerage reported, labelled as self-reported.
    """
    caps = (setup or {}).get("capabilities") or {}
    out: dict[str, dict[str, str]] = {}
    unread = _cap(NEEDS_ATTENTION, "Setup state could not be read — open diagnostics")

    phone = caps.get("phone")
    out["phone"] = unread if setup is None else {
        "READY": _cap(READY, "Business number verified for calls"),
        "IN_PROGRESS": _cap(NEEDS_SETUP, "Caller ID verification pending"),
        "NEEDS_ACTION": _cap(NEEDS_ATTENTION, "Business number connected but caller ID not verified"),
    }.get(phone, _cap(NEEDS_SETUP, "No business number connected"))

    reg = registration or {}
    texting = (setup or {}).get("messaging")
    if setup is None and not reg.get("rejected"):
        out["messaging"] = unread
    elif reg.get("rejected"):
        out["messaging"] = _cap(NEEDS_ATTENTION, "Texting registration (10DLC) was rejected")
    elif texting == "READY":
        out["messaging"] = _cap(READY, "Texting active")
    elif texting == "IN_PROGRESS":
        out["messaging"] = _cap(NEEDS_SETUP, "Texting number setup in progress")
    elif texting == "NEEDS_ACTION":
        out["messaging"] = _cap(NEEDS_ATTENTION, "Texting number needs action from the brokerage")
    elif reg.get("pending"):
        out["messaging"] = _cap(NEEDS_SETUP, "Texting registration (10DLC) pending")
    else:
        out["messaging"] = _cap(NOT_USED, "Texting not set up")

    creds = credentials or {}
    self_reported = caps.get("email_calendar") == "READY"
    for name, noun in (("email", "Mailbox"), ("calendar", "Calendar")):
        c = creds.get(name) or {}
        if _int(c.get("broken")):
            out[name] = _cap(NEEDS_ATTENTION, f"{noun} connection needs re-authorization")
        elif _int(c.get("active")):
            out[name] = _cap(READY, f"{noun} connected")
        elif self_reported:
            out[name] = _cap(READY, f"{noun} reported connected by the brokerage (self-reported)")
        else:
            out[name] = _cap(NOT_USED, f"No {noun.lower()} connected")

    mls = (setup or {}).get("mls") or {}
    mls_status = mls.get("status") or caps.get("mls")
    out["mls"] = unread if setup is None else {
        "READY": _cap(READY, "Licensed MLS feed synced and fresh"),
        "BLOCKED": _cap(NEEDS_SETUP, "Only developer/reference listing data — licensed feed awaiting approval"),
        "IN_PROGRESS": _cap(NEEDS_SETUP, "MLS feed backfill running"),
        "ERROR": _cap(NEEDS_ATTENTION, mls.get("detail") or "Licensed MLS feed failing or stale"),
    }.get(mls_status, _cap(NOT_USED, "No MLS feed entitled"))

    sub_status = (subscription or {}).get("status")
    if sub_status is None:
        out["billing"] = _cap(NEEDS_SETUP, "No subscription")
    elif sub_status in ("active", "trialing"):
        out["billing"] = _cap(READY, "Subscription active" if sub_status == "active" else "Trial active")
    elif sub_status == "past_due":
        out["billing"] = _cap(NEEDS_ATTENTION, "Payment past due — still usable while Stripe retries")
    elif sub_status == "canceled":
        out["billing"] = _cap(NEEDS_ATTENTION, "Subscription canceled")
    elif sub_status in _PAYMENT_ISSUE:
        out["billing"] = _cap(NEEDS_ATTENTION, f"Payment not completed ({sub_status})")
    else:
        out["billing"] = _cap(NEEDS_ATTENTION, f"Unrecognised subscription status ({safe_code(sub_status)})")
    return out


_SETUP_LABELS = {
    "brokerage_profile": "business profile", "agent_invites": "agents",
    "phone": "phone", "billing": "billing",
}


def compute_needs_action(
    *,
    lifecycle_state: Optional[str],
    setup: Optional[dict],
    capabilities: dict[str, dict[str, str]],
    subscription: Optional[dict],
    work: Optional[dict],
    is_platform: bool = False,
) -> list[dict[str, str]]:
    """What an operator should do something about, most urgent first."""
    items: list[dict[str, str]] = []
    state = lifecycle_state or "active"
    if state != "active":
        items.append({"code": f"lifecycle_{state}", "message": {
            "suspended": "Account suspended",
            "closing": "Account closing — data will be erased after the grace period",
            "erasing": "Account erasure in progress",
            "erased": "Account erased",
        }.get(state, f"Account {state}")})
        if state in ("erasing", "erased"):
            return items

    w = work or {}
    if _int(w.get("unresolved_side_effects")):
        n = _int(w["unresolved_side_effects"])
        items.append({"code": "side_effects_unresolved",
                      "message": f"{n} call/text/email{'s need' if n != 1 else ' needs'} reconciliation"})
    if _int(w.get("failed_jobs_24h")):
        n = _int(w["failed_jobs_24h"])
        items.append({"code": "jobs_failed",
                      "message": f"{n} background job{'s' if n != 1 else ''} failed in the last 24h"})

    if not is_platform:
        sub_status = (subscription or {}).get("status")
        if sub_status is None:
            items.append({"code": "billing_none", "message": "No subscription"})
        elif sub_status == "canceled":
            items.append({"code": "billing_canceled", "message": "Subscription canceled"})
        elif sub_status in _PAYMENT_ISSUE:
            items.append({"code": "billing_payment", "message": f"Payment issue ({sub_status.replace('_', ' ')})"})

    seen: set[str] = set()
    for name in ("mls", "messaging", "email", "calendar", "phone"):
        cap = capabilities.get(name) or {}
        reason = cap.get("reason") or f"{name} needs attention"
        if cap.get("state") == NEEDS_ATTENTION and reason not in seen:
            seen.add(reason)
            items.append({"code": f"{name}_attention", "message": reason})

    caps = (setup or {}).get("capabilities") or {}
    if setup and not is_platform and caps.get("readiness") != "READY":
        outstanding = [label for key, label in _SETUP_LABELS.items()
                       if caps.get(key) not in (None, "READY")
                       and not (key == "billing" and is_platform)]
        if outstanding:
            items.append({"code": "setup_incomplete",
                          "message": "Setup incomplete: " + ", ".join(outstanding)})
    return items


def brokerage_status(lifecycle_state: Optional[str], setup: Optional[dict],
                     subscription: Optional[dict]) -> str:
    state = lifecycle_state or "active"
    if state != "active":
        return state
    if (subscription or {}).get("status") == "canceled":
        return "canceled"
    caps = (setup or {}).get("capabilities") or {}
    return "live" if caps.get("readiness") == "READY" else "onboarding"


# ---------------------------------------------------------------------------
# Batched per-tenant reads (one query per concern for a whole page of tenants)
# ---------------------------------------------------------------------------

_LATEST_SUBSCRIPTION_SQL = """
    SELECT DISTINCT ON (tenant_id) tenant_id, status, plan, current_period_end, updated_at
      FROM subscriptions
     WHERE tenant_id = ANY($1::uuid[])
     ORDER BY tenant_id, created_at DESC
"""

_AGENT_COUNTS_SQL = """
    SELECT tenant_id,
           count(*)::int                           AS total,
           count(*) FILTER (WHERE is_active)::int  AS active
      FROM users
     WHERE tenant_id = ANY($1::uuid[]) AND role <> 'platform_admin'
     GROUP BY tenant_id
"""

_WORK_SQL = """
    SELECT tenant_id,
           count(*) FILTER (WHERE state IN ('failed','dead_letter')
                            AND updated_at > now() - interval '24 hours')::int AS failed_jobs_24h,
           count(*) FILTER (WHERE state = 'queued' AND attempt_count > 0)::int  AS retrying_jobs
      FROM automation_jobs
     WHERE tenant_id = ANY($1::uuid[])
       AND state IN ('failed','dead_letter','queued')
       AND updated_at > now() - interval '7 days'
     GROUP BY tenant_id
"""

_SIDE_EFFECTS_SQL = """
    SELECT tenant_id,
           count(*) FILTER (WHERE state = 'reconciliation_required')::int AS unresolved_side_effects,
           count(*) FILTER (WHERE state = 'failed'
                            AND updated_at > now() - interval '24 hours')::int AS failed_side_effects_24h
      FROM command_executions
     WHERE tenant_id = ANY($1::uuid[])
       AND state IN ('reconciliation_required','failed')
     GROUP BY tenant_id
"""

_CREDENTIALS_SQL = """
    SELECT tenant_id, provider,
           count(*) FILTER (WHERE disabled_at IS NULL
                            AND validation_status NOT IN ('invalid','expired'))::int AS active,
           count(*) FILTER (WHERE disabled_at IS NULL
                            AND validation_status IN ('invalid','expired'))::int     AS broken,
           bool_or(disabled_at IS NULL AND EXISTS (
               SELECT 1 FROM unnest(scopes) AS s WHERE s ILIKE '%calendar%'))         AS calendar_scope
      FROM provider_credentials
     WHERE tenant_id = ANY($1::uuid[])
       AND provider IN ('google','smtp','ses','acs')
     GROUP BY tenant_id, provider
"""

_REGISTRATION_SQL = """
    SELECT b.tenant_id, b.status AS brand_status,
           (SELECT count(*) FROM tenant_messaging_campaigns c
             WHERE c.tenant_id = b.tenant_id AND c.status IN ('rejected','failed'))::int AS campaigns_rejected,
           (SELECT count(*) FROM tenant_messaging_campaigns c
             WHERE c.tenant_id = b.tenant_id AND c.status = 'pending')::int             AS campaigns_pending
      FROM tenant_messaging_brands b
     WHERE b.tenant_id = ANY($1::uuid[])
"""

_LAST_ACTIVITY_SQL = """
    SELECT tenant_id, max(at) AS last_activity_at FROM (
        SELECT tenant_id, max(created_at) AS at FROM interaction_logs
         WHERE tenant_id = ANY($1::uuid[]) AND created_at > now() - interval '90 days'
         GROUP BY tenant_id
        UNION ALL
        SELECT tenant_id, max(created_at) FROM ai_chat_messages
         WHERE tenant_id = ANY($1::uuid[]) AND role = 'user' AND created_at > now() - interval '90 days'
         GROUP BY tenant_id
    ) a GROUP BY tenant_id
"""


async def _by_tenant(sections: _Sections, name: str, sql: str, ids: list[str]) -> dict[str, dict]:
    async def go():
        rows = await sections.conn.fetch(sql, ids)
        return {str(r["tenant_id"]): r for r in rows}
    return await sections.run(name, go, {})


def _credential_summary(rows: list) -> dict[str, dict[str, int]]:
    out = {"email": {"active": 0, "broken": 0}, "calendar": {"active": 0, "broken": 0}}
    for r in rows:
        out["email"]["active"] += _int(r["active"])
        out["email"]["broken"] += _int(r["broken"])
        if r["provider"] == "google" and r["calendar_scope"]:
            out["calendar"]["active"] += _int(r["active"])
            out["calendar"]["broken"] += _int(r["broken"])
    return out


def _registration_summary(row) -> dict[str, bool]:
    if not row:
        return {"rejected": False, "pending": False}
    return {
        "rejected": row["brand_status"] == "failed" or _int(row["campaigns_rejected"]) > 0,
        "pending": row["brand_status"] in ("pending", "unverified") or _int(row["campaigns_pending"]) > 0,
    }


def _subscription_out(row) -> Optional[dict]:
    if not row:
        return None
    return {
        "status": row["status"],
        "plan": row["plan"],
        "current_period_end": _iso(row["current_period_end"]),
        "updated_at": _iso(row["updated_at"]),
    }


async def _setup_for(sections: _Sections, ctx: TenantContext, tenant_id: str) -> Optional[dict]:
    """compute_setup_state for another tenant, on the admin's connection.

    It reads only ctx.tenant_id, and filters every statement on it explicitly,
    so a context naming the target tenant — never used to open a transaction —
    gives exactly the brokerage's own setup screen.
    """
    import brokerage_onboarding

    target = TenantContext(agent_id=ctx.agent_id, tenant_id=tenant_id, role=Role.PLATFORM_ADMIN)

    async def go():
        try:
            return await brokerage_onboarding.compute_setup_state(sections.conn, target)
        except HTTPException:
            # Its 404 means the tenant vanished mid-page; that is one row's
            # problem, not a reason to fail the whole list.
            return None

    return await sections.run(f"setup:{tenant_id}", go, None)


# ---------------------------------------------------------------------------
# GET /api/admin/brokerages — §12
# ---------------------------------------------------------------------------

@router.get("/brokerages")
async def list_brokerages(
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0, le=100_000),
    ctx: TenantContext = Depends(require_platform_admin),
) -> dict[str, Any]:
    try:
        async with tenant_tx(ctx) as conn:
            await _start(conn)
            sections = _Sections(conn)
            tenants = await conn.fetch(
                """
                SELECT t.id, t.name, t.slug, t.created_at, t.lifecycle_state,
                       t.erase_after, count(*) OVER () AS total
                  FROM tenants t
                 ORDER BY t.created_at, t.id
                 LIMIT $1 OFFSET $2
                """,
                limit, offset,
            )
            ids = [str(t["id"]) for t in tenants]
            subs = await _by_tenant(sections, "subscriptions", _LATEST_SUBSCRIPTION_SQL, ids)
            agents = await _by_tenant(sections, "agents", _AGENT_COUNTS_SQL, ids)
            work = await _by_tenant(sections, "jobs", _WORK_SQL, ids)
            side = await _by_tenant(sections, "side_effects", _SIDE_EFFECTS_SQL, ids)
            regs = await _by_tenant(sections, "messaging_registration", _REGISTRATION_SQL, ids)
            activity = await _by_tenant(sections, "activity", _LAST_ACTIVITY_SQL, ids)

            async def creds_go():
                grouped: dict[str, list] = {}
                for r in await conn.fetch(_CREDENTIALS_SQL, ids):
                    grouped.setdefault(str(r["tenant_id"]), []).append(r)
                return grouped
            creds = await sections.run("credentials", creds_go, {})

            rows = []
            for t in tenants:
                tid = str(t["id"])
                setup = await _setup_for(sections, ctx, tid)
                rows.append(_brokerage_row(
                    t, setup=setup, subscription=subs.get(tid), agents=agents.get(tid),
                    work={**dict(work.get(tid) or {}), **dict(side.get(tid) or {})},
                    credentials=_credential_summary(creds.get(tid, [])),
                    registration=_registration_summary(regs.get(tid)),
                    last_activity=(activity.get(tid) or {}).get("last_activity_at") if activity.get(tid) else None,
                ))
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _db_unavailable(exc) from None

    total = _int(tenants[0]["total"]) if tenants else 0
    summary: dict[str, int] = {}
    for r in rows:
        summary[r["status"]] = summary.get(r["status"], 0) + 1
    summary["needs_action"] = sum(1 for r in rows if r["needs_action"])
    # Most urgent first: the operator reads the top of this list.
    rows.sort(key=lambda r: (not r["needs_action"], -len(r["needs_action"]), r["name"] or ""))
    return {
        "brokerages": rows,
        "summary": summary,
        "page": {"limit": limit, "offset": offset, "total": total,
                 "has_more": offset + len(rows) < total},
        "not_tracked": {k: NOT_TRACKED[k] for k in ("usage_limits", "support_issues")},
        "unavailable": sorted({u.split(":")[0] for u in sections.unavailable}),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def _brokerage_row(t, *, setup, subscription, agents, work, credentials, registration,
                   last_activity) -> dict[str, Any]:
    tid = str(t["id"])
    is_platform = tid == _PLATFORM_TENANT_ID
    sub = _subscription_out(subscription)
    caps = map_capabilities(setup, subscription=sub, credentials=credentials,
                            registration=registration)
    if is_platform and caps["billing"]["state"] != READY:
        caps["billing"] = _cap(NOT_USED, "Platform tenant — not billed")
    work_out = {
        "failed_jobs_24h": _int(work.get("failed_jobs_24h")),
        "retrying_jobs": _int(work.get("retrying_jobs")),
        "unresolved_side_effects": _int(work.get("unresolved_side_effects")),
        "failed_side_effects_24h": _int(work.get("failed_side_effects_24h")),
    }
    lifecycle = t["lifecycle_state"]
    return {
        "id": tid,
        "name": t["name"],
        "slug": t["slug"],
        "is_platform": is_platform,
        "created_at": _iso(t["created_at"]),
        "lifecycle_state": lifecycle,
        "erase_after": _iso(t["erase_after"]),
        "status": "platform" if is_platform else brokerage_status(lifecycle, setup, sub),
        "subscription": sub or {"status": "none"},
        "agents": {"total": _int((agents or {}).get("total")), "active": _int((agents or {}).get("active"))},
        "capabilities": caps,
        "setup_readiness": ((setup or {}).get("capabilities") or {}).get("readiness"),
        "recommended_next": (setup or {}).get("recommended_next"),
        "last_activity_at": _iso(last_activity),
        "work": work_out,
        "needs_action": compute_needs_action(
            lifecycle_state=lifecycle, setup=setup, capabilities=caps,
            subscription=sub, work=work_out, is_platform=is_platform),
    }


# ---------------------------------------------------------------------------
# GET /api/admin/brokerages/{tenant_id}/diagnostics — §10, §11
# ---------------------------------------------------------------------------

#: What the bundle never contains. Stated in the response so a support
#: engineer pasting it into a ticket knows what they are (not) sharing.
EXCLUDED_FROM_DIAGNOSTICS = (
    "message bodies", "call transcripts and summaries", "documents and attachments",
    "password hashes", "tokens and credentials", "contact phone numbers and emails",
    "free-text provider or job errors (codes only)", "audit payloads",
)


@router.get("/brokerages/{tenant_id}/diagnostics")
async def brokerage_diagnostics(
    tenant_id: str,
    ctx: TenantContext = Depends(require_platform_admin),
) -> dict[str, Any]:
    tid = _uuid_or_422(tenant_id)
    try:
        async with tenant_tx(ctx) as conn:
            await _start(conn)
            s = _Sections(conn)
            tenant = await conn.fetchrow(
                "SELECT id, name, slug, created_at, lifecycle_state, lifecycle_changed_at, "
                "       closure_requested_at, erase_after, erased_at "
                "  FROM tenants WHERE id = $1::uuid",
                tid,
            )
            if tenant is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Brokerage not found.")
            setup = await _setup_for(s, ctx, tid)
            bundle = {
                "subscription": await s.run("subscription", lambda: _diag_subscription(conn, tid), None),
                "agents": await s.run("agents", lambda: _diag_agents(conn, tid), None),
                "credentials_rows": await s.run("email_calendar", lambda: _diag_credentials(conn, tid), []),
                "voice": await s.run("voice", lambda: _diag_voice(conn, tid), None),
                "messaging": await s.run("messaging", lambda: _diag_messaging(conn, tid), None),
                "mls": await s.run("mls", lambda: _diag_mls(conn, tid), []),
                "jobs": await s.run("jobs", lambda: _diag_jobs(conn, tid), None),
                "side_effects": await s.run("side_effects", lambda: _diag_side_effects(conn, tid), None),
                "provider_errors": await s.run("provider_errors", lambda: _diag_provider_errors(conn, tid), None),
                "audit": await s.run("audit", lambda: _diag_audit(conn, tid), []),
                "release": await s.run("release", lambda: _release(conn), None),
            }
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _db_unavailable(exc) from None

    cred_rows = bundle.pop("credentials_rows")
    sub = bundle["subscription"]
    caps = map_capabilities(
        setup, subscription=sub,
        credentials=_credential_summary_from_diag(cred_rows),
        registration=(bundle["messaging"] or {}).get("registration_summary"),
    )
    work = {
        "failed_jobs_24h": _int((bundle["jobs"] or {}).get("failed_24h")),
        "unresolved_side_effects": _int((bundle["side_effects"] or {}).get("unresolved")),
    }
    is_platform = tid == _PLATFORM_TENANT_ID
    if is_platform and caps["billing"]["state"] != READY:
        caps["billing"] = _cap(NOT_USED, "Platform tenant — not billed")
    return {
        "tenant": {
            "id": tid,
            "name": tenant["name"],
            "slug": tenant["slug"],
            "created_at": _iso(tenant["created_at"]),
            "lifecycle_state": tenant["lifecycle_state"],
            "lifecycle_changed_at": _iso(tenant["lifecycle_changed_at"]),
            "closure_requested_at": _iso(tenant["closure_requested_at"]),
            "erase_after": _iso(tenant["erase_after"]),
            "erased_at": _iso(tenant["erased_at"]),
            "is_platform": is_platform,
        },
        "plan": sub or {"status": "none"},
        "agents": bundle["agents"],
        "capabilities": caps,
        "setup": {
            "capabilities": (setup or {}).get("capabilities"),
            "recommended_next": (setup or {}).get("recommended_next"),
            "team": (setup or {}).get("team"),
        } if setup else None,
        "needs_action": compute_needs_action(
            lifecycle_state=tenant["lifecycle_state"], setup=setup, capabilities=caps,
            subscription=sub, work=work, is_platform=is_platform),
        "email_calendar": {
            "connections": cred_rows,
            "self_reported_status": ((setup or {}).get("capabilities") or {}).get("email_calendar"),
        },
        "voice": bundle["voice"],
        "messaging": {k: v for k, v in (bundle["messaging"] or {}).items()
                      if k != "registration_summary"} or None,
        "mls": bundle["mls"],
        "jobs": bundle["jobs"],
        "side_effects": bundle["side_effects"],
        "provider_errors": bundle["provider_errors"],
        "recent_audit": bundle["audit"],
        "release": bundle["release"],
        "excluded": list(EXCLUDED_FROM_DIAGNOSTICS),
        "unavailable": sorted({u.split(":")[0] for u in s.unavailable}),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


async def _diag_subscription(conn, tid: str) -> Optional[dict]:
    row = await conn.fetchrow(
        "SELECT status, plan, current_period_end, updated_at, created_at, stripe_customer_id "
        "  FROM subscriptions WHERE tenant_id = $1::uuid ORDER BY created_at DESC LIMIT 1",
        tid,
    )
    if not row:
        return None
    out = _subscription_out(row)
    out["created_at"] = _iso(row["created_at"])
    # A Stripe customer id is a lookup key for the Stripe dashboard, not a
    # credential; support needs it to find the customer without SQL.
    out["stripe_customer_ref"] = row["stripe_customer_id"]
    return out


async def _diag_agents(conn, tid: str) -> dict:
    row = await conn.fetchrow(
        "SELECT count(*) FILTER (WHERE role <> 'platform_admin')::int AS total, "
        "       count(*) FILTER (WHERE is_active AND role <> 'platform_admin')::int AS active, "
        "       count(*) FILTER (WHERE is_active AND role = 'broker_owner')::int AS owners "
        "  FROM users WHERE tenant_id = $1::uuid",
        tid,
    )
    return {"total": _int(row["total"]), "active": _int(row["active"]), "owners": _int(row["owners"])}


async def _diag_credentials(conn, tid: str) -> list[dict]:
    """Existence and validity only. The token columns are never selected."""
    rows = await conn.fetch(
        "SELECT provider, validation_status, disabled_at, expires_at, last_validated_at, "
        "       EXISTS (SELECT 1 FROM unnest(scopes) s WHERE s ILIKE '%calendar%') AS calendar_scope "
        "  FROM provider_credentials "
        " WHERE tenant_id = $1::uuid AND provider IN ('google','smtp','ses','acs') "
        " ORDER BY provider LIMIT 20",
        tid,
    )
    return [{
        "provider": r["provider"],
        "validation_status": r["validation_status"],
        "disabled": r["disabled_at"] is not None,
        "expires_at": _iso(r["expires_at"]),
        "last_validated_at": _iso(r["last_validated_at"]),
        "calendar_scope": bool(r["calendar_scope"]),
    } for r in rows]


def _credential_summary_from_diag(rows: list[dict]) -> dict[str, dict[str, int]]:
    out = {"email": {"active": 0, "broken": 0}, "calendar": {"active": 0, "broken": 0}}
    for r in rows or []:
        if r["disabled"]:
            continue
        broken = r["validation_status"] in ("invalid", "expired")
        out["email"]["broken" if broken else "active"] += 1
        if r["provider"] == "google" and r["calendar_scope"]:
            out["calendar"]["broken" if broken else "active"] += 1
    return out


async def _diag_voice(conn, tid: str) -> dict:
    rows = await conn.fetch(
        "SELECT agent_id, provider, inbound_did, voice_caller_id_e164, active, "
        "       outbound_verification_status, outbound_verification_failure_reason, "
        "       inbound_forwarding_status, inbound_forwarding_failure_reason, forwarding_mode, updated_at "
        "  FROM telephony_routes WHERE tenant_id = $1::uuid ORDER BY active DESC, updated_at DESC LIMIT 50",
        tid,
    )
    calls = await conn.fetchrow(
        "SELECT count(*)::int AS total, "
        "       count(*) FILTER (WHERE provider_status IN ('failed','busy','no-answer','canceled'))::int AS unsuccessful, "
        "       count(*) FILTER (WHERE provider_status = 'failed')::int AS failed "
        "  FROM inbound_voice_calls WHERE tenant_id = $1::uuid AND created_at > now() - interval '24 hours'",
        tid,
    )
    return {
        "routes": [{
            "agent_id": r["agent_id"],
            "provider": r["provider"],
            "business_number": mask_e164(r["inbound_did"]),
            "caller_id": mask_e164(r["voice_caller_id_e164"]),
            "active": bool(r["active"]),
            "outbound_verification": r["outbound_verification_status"],
            "outbound_failure": scrub(r["outbound_verification_failure_reason"]),
            "inbound_forwarding": r["inbound_forwarding_status"],
            "inbound_failure": scrub(r["inbound_forwarding_failure_reason"]),
            "forwarding_mode": r["forwarding_mode"],
            "updated_at": _iso(r["updated_at"]),
        } for r in rows],
        "inbound_calls_24h": {"total": _int(calls["total"]), "failed": _int(calls["failed"]),
                              "unsuccessful": _int(calls["unsuccessful"])},
    }


async def _diag_messaging(conn, tid: str) -> dict:
    routes = await conn.fetch(
        "SELECT agent_id, provider, active, eligibility_status, hosted_order_status, "
        "       hosted_order_failure_reason, loa_document_state, invoice_document_state, updated_at "
        "  FROM messaging_routes WHERE tenant_id = $1::uuid ORDER BY active DESC, updated_at DESC LIMIT 50",
        tid,
    )
    brand = await conn.fetchrow(
        "SELECT provider, status, failure_reason, submitted_at, approved_at "
        "  FROM tenant_messaging_brands WHERE tenant_id = $1::uuid",
        tid,
    )
    campaigns = await conn.fetch(
        "SELECT provider, status, usecase, failure_reason, submitted_at, approved_at "
        "  FROM tenant_messaging_campaigns WHERE tenant_id = $1::uuid ORDER BY created_at DESC LIMIT 10",
        tid,
    )
    sms = await conn.fetchrow(
        "SELECT count(*) FILTER (WHERE direction = 'outbound')::int AS outbound, "
        "       count(*) FILTER (WHERE direction = 'outbound' AND status IN ('failed','undelivered'))::int AS failed "
        "  FROM sms_messages WHERE tenant_id = $1::uuid AND created_at > now() - interval '24 hours'",
        tid,
    )
    reg = {
        "rejected": bool(brand and brand["status"] == "failed")
                    or any(c["status"] in ("rejected", "failed") for c in campaigns),
        "pending": bool(brand and brand["status"] in ("pending", "unverified"))
                   or any(c["status"] == "pending" for c in campaigns),
    }
    return {
        "routes": [{
            "agent_id": r["agent_id"],
            "provider": r["provider"],
            "active": bool(r["active"]),
            "eligibility": r["eligibility_status"],
            "hosting": r["hosted_order_status"],
            "hosting_failure": scrub(r["hosted_order_failure_reason"]),
            "loa_document": r["loa_document_state"],
            "invoice_document": r["invoice_document_state"],
            "updated_at": _iso(r["updated_at"]),
        } for r in routes],
        "brand_10dlc": {
            "provider": brand["provider"], "status": brand["status"],
            "failure_reason": scrub(brand["failure_reason"]),
            "submitted_at": _iso(brand["submitted_at"]), "approved_at": _iso(brand["approved_at"]),
        } if brand else None,
        "campaigns_10dlc": [{
            "provider": c["provider"], "status": c["status"], "usecase": safe_code(c["usecase"]),
            "failure_reason": scrub(c["failure_reason"]),
            "submitted_at": _iso(c["submitted_at"]), "approved_at": _iso(c["approved_at"]),
        } for c in campaigns],
        "outbound_sms_24h": {"total": _int(sms["outbound"]), "failed": _int(sms["failed"])},
        "registration_summary": reg,
    }


_MLS_FEED_COLUMNS = (
    "mls_id, mls_name, provider, dataset, feed_type, license_classification, "
    "last_success_at, last_attempt_at, last_sync_at, last_error, last_error_class, "
    "consecutive_failures, backfill_complete, backfill_cursor_key, backfill_records, "
    "listings_synced, stale_after_minutes, notes, updated_at"
)


def _rejections(notes: Any) -> dict[str, Any]:
    """Rejection counts from the status row's notes (text holding JSON)."""
    try:
        data = notes if isinstance(notes, dict) else json.loads(notes or "{}")
    except (TypeError, ValueError):
        return {"validation": 0, "on_write": 0, "by_reason": {}}
    if not isinstance(data, dict):
        return {"validation": 0, "on_write": 0, "by_reason": {}}
    by_reason = data.get("rejected") if isinstance(data.get("rejected"), dict) else {}
    clean = {safe_code(k): _int(v) for k, v in list(by_reason.items())[:20]}
    return {"validation": sum(clean.values()), "on_write": _int(data.get("rejected_on_write")),
            "by_reason": clean}


def _feed_out(row: dict) -> dict[str, Any]:
    import mls_health
    from mls_licensing import LICENSED

    health = mls_health.compute_health(row)
    rej = _rejections(row.get("notes"))
    return {
        "mls_id": row["mls_id"],
        "mls_name": row.get("mls_name") or row["mls_id"],
        "provider": row.get("provider"),
        "feed_type": row.get("feed_type"),
        "licensed": row.get("license_classification") == LICENSED,
        "license_classification": row.get("license_classification"),
        "health": health,
        "last_success_at": _iso(row.get("last_success_at")),
        "last_attempt_at": _iso(row.get("last_attempt_at")),
        "age_seconds": mls_health.feed_age_seconds(row),
        "stale_after_minutes": _int(row.get("stale_after_minutes")) or 1440,
        "error_class": row.get("last_error_class"),
        "error_detail": scrub(row.get("last_error"), 120),
        "consecutive_failures": _int(row.get("consecutive_failures")),
        "backfill": {
            "complete": bool(row.get("backfill_complete")),
            "in_progress": bool(row.get("backfill_cursor_key")) and not row.get("backfill_complete"),
            "records": _int(row.get("backfill_records")),
        },
        "listings_synced": _int(row.get("listings_synced")),
        "rejections": rej,
    }


async def _diag_mls(conn, tid: str) -> list[dict]:
    rows = await conn.fetch(
        f"SELECT {_MLS_FEED_COLUMNS} FROM mls_sync_status "
        " WHERE mls_id IN (SELECT mls_id FROM mls_feed_entitlements WHERE tenant_id = $1::uuid) "
        " ORDER BY mls_id LIMIT 50",
        tid,
    )
    return [_feed_out(dict(r)) for r in rows]


async def _diag_jobs(conn, tid: str) -> dict:
    groups = await conn.fetch(
        """
        SELECT job_type, state, last_error_code, count(*)::int AS n, max(updated_at) AS last_at
          FROM automation_jobs
         WHERE tenant_id = $1::uuid
           AND updated_at > now() - interval '7 days'
           AND (state IN ('failed','dead_letter') OR (state = 'queued' AND attempt_count > 0))
         GROUP BY job_type, state, last_error_code
         ORDER BY max(updated_at) DESC
         LIMIT 50
        """,
        tid,
    )
    out = [{
        "job_type": safe_code(g["job_type"]),
        "state": "retrying" if g["state"] == "queued" else g["state"],
        "error_code": safe_code(g["last_error_code"]),
        "count": _int(g["n"]),
        "last_at": _iso(g["last_at"]),
    } for g in groups]
    failed_24h = await conn.fetchval(
        "SELECT count(*)::int FROM automation_jobs WHERE tenant_id = $1::uuid "
        "   AND state IN ('failed','dead_letter') AND updated_at > now() - interval '24 hours'",
        tid,
    )
    return {"window_days": 7, "groups": out, "failed_24h": _int(failed_24h)}


async def _diag_side_effects(conn, tid: str) -> dict:
    groups = await conn.fetch(
        """
        SELECT command_type, state, provider, count(*)::int AS n, max(updated_at) AS last_at
          FROM command_executions
         WHERE tenant_id = $1::uuid
           AND (state = 'reconciliation_required'
                OR (state = 'failed' AND updated_at > now() - interval '7 days'))
         GROUP BY command_type, state, provider
         ORDER BY max(updated_at) DESC
         LIMIT 30
        """,
        tid,
    )
    return {
        "groups": [{
            "command_type": g["command_type"], "state": g["state"],
            "provider": safe_code(g["provider"]), "count": _int(g["n"]),
            "last_at": _iso(g["last_at"]),
        } for g in groups],
        "unresolved": sum(_int(g["n"]) for g in groups if g["state"] == "reconciliation_required"),
    }


async def _diag_provider_errors(conn, tid: str) -> dict:
    """Recent provider error CATEGORIES — counts by code, never messages."""
    chat = await conn.fetch(
        "SELECT error_code, count(*)::int AS n FROM ai_chat_messages "
        " WHERE tenant_id = $1::uuid AND role = 'assistant' AND status = 'failed' "
        "   AND created_at > now() - interval '7 days' "
        " GROUP BY error_code ORDER BY 2 DESC LIMIT 10",
        tid,
    )
    tools = await conn.fetch(
        "SELECT tool_name, error_code, count(*)::int AS n FROM ai_tool_operations "
        " WHERE tenant_id = $1::uuid AND status = 'failed' AND created_at > now() - interval '7 days' "
        " GROUP BY tool_name, error_code ORDER BY 3 DESC LIMIT 10",
        tid,
    )
    sms = await conn.fetch(
        "SELECT provider, provider_status, count(*)::int AS n FROM sms_messages "
        " WHERE tenant_id = $1::uuid AND status IN ('failed','undelivered') "
        "   AND created_at > now() - interval '7 days' "
        " GROUP BY provider, provider_status ORDER BY 3 DESC LIMIT 10",
        tid,
    )
    mail = await conn.fetchval(
        "SELECT count(*)::int FROM email_outbox WHERE tenant_id = $1::uuid AND status = 'failed' "
        "   AND updated_at > now() - interval '7 days'",
        tid,
    )
    return {
        "window_days": 7,
        "ai_responses": [{"code": safe_code(r["error_code"]) or "none", "count": _int(r["n"])} for r in chat],
        "ai_tools": [{"tool": safe_code(r["tool_name"]), "code": safe_code(r["error_code"]) or "none",
                      "count": _int(r["n"])} for r in tools],
        "sms": [{"provider": r["provider"], "code": safe_code(r["provider_status"]) or "none",
                 "count": _int(r["n"])} for r in sms],
        "email_failed": _int(mail),
    }


async def _diag_audit(conn, tid: str) -> list[dict]:
    """Action names and times. The metadata column is never selected."""
    rows = await conn.fetch(
        "SELECT category, action, created_at FROM audit_ledger "
        " WHERE tenant_id = $1::uuid ORDER BY created_at DESC LIMIT 25",
        tid,
    )
    return [{"category": safe_code(r["category"]), "action": safe_code(r["action"]),
             "at": _iso(r["created_at"])} for r in rows]


# ---------------------------------------------------------------------------
# Release — API build vs worker builds vs migration head
# ---------------------------------------------------------------------------

def _expected_migration_head() -> Optional[str]:
    try:
        files = sorted(p.name for p in _MIGRATIONS_DIR.glob("[0-9][0-9][0-9][0-9]_*.sql"))
    except OSError:
        return None
    return files[-1] if files else None


async def _release(conn) -> dict[str, Any]:
    import config
    import process_heartbeat

    api_sha = os.environ.get("ORACLE_GIT_SHA", "unknown")
    head = None
    try:
        async with conn.transaction():
            head = await conn.fetchval(
                "SELECT filename FROM schema_migrations ORDER BY filename DESC LIMIT 1")
    except Exception:  # noqa: BLE001 — a ledger-less database reports None
        head = None
    rows = [dict(r) for r in await conn.fetch(process_heartbeat.READ_WORKERS_SQL)]
    workers = process_heartbeat.summarize(rows)
    expected = _expected_migration_head()
    live_shas = workers.get("live_git_shas") or []
    warnings = []
    if not workers.get("healthy"):
        warnings.append({"code": "no_worker", "message": "No background worker is alive"})
    if len(live_shas) > 1:
        warnings.append({"code": "mixed_workers", "message": "Workers are running more than one build"})
    if api_sha != "unknown" and live_shas and api_sha not in live_shas:
        warnings.append({"code": "worker_build_differs",
                         "message": "Workers are not running the same build as this API"})
    if expected and head and head < expected:
        warnings.append({"code": "migrations_behind",
                         "message": "Database migrations are behind this build"})
    if api_sha == "unknown":
        warnings.append({"code": "unknown_build", "message": "This API does not know its build (no ORACLE_GIT_SHA)"})
    return {
        "api": {
            "git_sha": api_sha,
            "app_version": os.environ.get("ORACLE_APP_VERSION", "unknown"),
            "built_at": os.environ.get("ORACLE_BUILD_TIMESTAMP", "unknown"),
            "environment": config.ORACLE_ENV or "unset",
            "process_role": config.PROCESS_ROLE,
        },
        "migration_head": head,
        "expected_migration_head": expected,
        "workers": {
            "healthy": bool(workers.get("healthy")),
            "live": _int(workers.get("live_workers")),
            "live_git_shas": live_shas,
        },
        "warnings": warnings,
    }


@router.get("/release")
async def release(ctx: TenantContext = Depends(require_platform_admin)) -> dict[str, Any]:
    """/version and /health/workers in one authenticated answer, with the
    comparisons an operator would otherwise do by eye."""
    try:
        async with tenant_tx(ctx) as conn:
            await _start(conn)
            return await _release(conn)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _db_unavailable(exc) from None


# ---------------------------------------------------------------------------
# GET /api/admin/billing/exceptions — §20
# ---------------------------------------------------------------------------

@router.get("/billing/exceptions")
async def billing_exceptions(ctx: TenantContext = Depends(require_platform_admin)) -> dict[str, Any]:
    import billing
    import billing_usage

    try:
        async with tenant_tx(ctx) as conn:
            await _start(conn)
            s = _Sections(conn)
            rows = await s.run("subscriptions", lambda: conn.fetch(
                """
                WITH latest AS (
                    SELECT DISTINCT ON (tenant_id) tenant_id, status, plan,
                           current_period_end, updated_at
                      FROM subscriptions
                     ORDER BY tenant_id, created_at DESC
                )
                SELECT t.id, t.name, t.lifecycle_state, t.created_at,
                       l.status, l.plan, l.current_period_end, l.updated_at
                  FROM tenants t
                  LEFT JOIN latest l ON l.tenant_id = t.id
                 WHERE (l.status IS NULL OR l.status NOT IN ('active','trialing'))
                   AND t.lifecycle_state <> 'erased'
                 ORDER BY l.updated_at DESC NULLS LAST, t.created_at
                 LIMIT 500
                """), [])
            reactivated = await s.run("reactivated", lambda: conn.fetch(
                """
                SELECT t.id, t.name, cur.status, cur.created_at AS reactivated_at,
                       prev.canceled_at
                  FROM tenants t
                  JOIN LATERAL (
                       SELECT status, created_at FROM subscriptions s
                        WHERE s.tenant_id = t.id ORDER BY created_at DESC LIMIT 1) cur ON true
                  JOIN LATERAL (
                       SELECT max(updated_at) AS canceled_at FROM subscriptions c
                        WHERE c.tenant_id = t.id AND c.status = 'canceled') prev ON true
                 WHERE cur.status IN ('active','trialing')
                   AND prev.canceled_at IS NOT NULL
                   AND cur.created_at > prev.canceled_at - interval '1 minute'
                   AND cur.created_at > now() - interval '90 days'
                 ORDER BY cur.created_at DESC
                 LIMIT 100
                """), [])
            events = await s.run("webhook_events", lambda: conn.fetch(
                "SELECT event_type, count(*)::int AS n, max(received_at) AS last_at "
                "  FROM stripe_webhook_events WHERE received_at > now() - interval '30 days' "
                " GROUP BY event_type ORDER BY max(received_at) DESC LIMIT 20"), [])
            last_event = await s.run("webhook_last", lambda: conn.fetchval(
                "SELECT max(received_at) FROM stripe_webhook_events"), None)
            reportable = sorted(billing_usage._STRIPE_REPORTED)
            usage = await s.run("usage", lambda: conn.fetchrow(
                "SELECT count(*)::int AS backlog, min(occurred_at) AS oldest, "
                "       count(*) FILTER (WHERE report_error IS NOT NULL)::int AS erroring "
                "  FROM billing_usage_events "
                " WHERE reported_at IS NULL AND metric = ANY($1::text[])", reportable), None)
            usage_tenants = await s.run("usage_tenants", lambda: conn.fetch(
                "SELECT u.tenant_id, t.name, count(*)::int AS backlog, min(u.occurred_at) AS oldest, "
                "       count(*) FILTER (WHERE u.report_error IS NOT NULL)::int AS erroring "
                "  FROM billing_usage_events u LEFT JOIN tenants t ON t.id = u.tenant_id "
                " WHERE u.reported_at IS NULL AND u.metric = ANY($1::text[]) "
                " GROUP BY u.tenant_id, t.name ORDER BY min(u.occurred_at) LIMIT 20", reportable), [])
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _db_unavailable(exc) from None

    no_sub, payment, canceled, other = [], [], [], []
    for r in rows:
        tid = str(r["id"])
        if tid == _PLATFORM_TENANT_ID:
            continue
        item = {"tenant_id": tid, "name": r["name"], "lifecycle_state": r["lifecycle_state"],
                "status": r["status"] or "none", "plan": r["plan"],
                "current_period_end": _iso(r["current_period_end"]),
                "since": _iso(r["updated_at"] or r["created_at"])}
        if r["status"] is None:
            no_sub.append(item)
        elif r["status"] in _PAYMENT_ISSUE:
            payment.append(item)
        elif r["status"] == "canceled":
            canceled.append(item)
        else:
            other.append(item)

    metering_on = billing_usage.metering_configured()
    backlog = _int((usage or {}).get("backlog")) if usage else 0
    webhook = billing.webhook_diagnostics()
    return {
        "no_subscription": no_sub,
        "payment_issue": payment,
        "canceled": canceled,
        "other_status": other,
        "reactivated_90d": [{
            "tenant_id": str(r["id"]), "name": r["name"], "status": r["status"],
            "reactivated_at": _iso(r["reactivated_at"]), "previously_canceled_at": _iso(r["canceled_at"]),
        } for r in reactivated],
        "webhooks": {
            **webhook,
            "last_processed_at": _iso(last_event),
            "processed_30d": [{"event_type": e["event_type"], "count": _int(e["n"]),
                               "last_at": _iso(e["last_at"])} for e in events],
        },
        "usage_metering": {
            "configured": metering_on,
            # With metering off, unreported rows are the design (history only),
            # not a backlog — saying "backlog" there would page someone for nothing.
            "state": ("off" if not metering_on else "backlog" if backlog else "ok"),
            "unreported": backlog,
            "oldest_unreported_at": _iso((usage or {}).get("oldest")) if usage else None,
            "erroring": _int((usage or {}).get("erroring")) if usage else 0,
            "by_tenant": [{"tenant_id": str(u["tenant_id"]), "name": u["name"],
                           "unreported": _int(u["backlog"]), "erroring": _int(u["erroring"]),
                           "oldest_unreported_at": _iso(u["oldest"])} for u in usage_tenants],
        },
        "counts": {"no_subscription": len(no_sub), "payment_issue": len(payment),
                   "canceled": len(canceled), "reactivated_90d": len(reactivated)},
        "not_tracked": {"cancel_at_period_end": NOT_TRACKED["cancel_at_period_end"]},
        "unavailable": s.unavailable,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# GET /api/admin/mls/feeds — §21
# ---------------------------------------------------------------------------

@router.get("/mls/feeds")
async def mls_feeds(ctx: TenantContext = Depends(require_platform_admin)) -> dict[str, Any]:
    try:
        async with tenant_tx(ctx) as conn:
            await _start(conn)
            rows = [dict(r) for r in await conn.fetch(
                f"SELECT {_MLS_FEED_COLUMNS} FROM mls_sync_status ORDER BY mls_id LIMIT 500")]
            ents = await conn.fetch(
                """
                SELECT e.mls_id, count(*)::int AS n,
                       (array_agg(json_build_object('tenant_id', e.tenant_id, 'name', t.name)
                                  ORDER BY t.name))[1:50] AS tenants
                  FROM mls_feed_entitlements e LEFT JOIN tenants t ON t.id = e.tenant_id
                 GROUP BY e.mls_id
                 LIMIT 500
                """
            )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _db_unavailable(exc) from None

    by_feed = {}
    for e in ents:
        tenants = []
        for item in e["tenants"] or []:
            obj = json.loads(item) if isinstance(item, str) else dict(item)
            tenants.append({"tenant_id": str(obj.get("tenant_id")), "name": obj.get("name")})
        by_feed[e["mls_id"]] = {"count": _int(e["n"]), "tenants": tenants}

    feeds = []
    for row in rows:
        out = _feed_out(row)
        out["entitled_tenants"] = by_feed.pop(row["mls_id"], {"count": 0, "tenants": []})
        feeds.append(out)
    # Entitlements naming a feed with no status row: granted access to a feed
    # that has never been configured here. Worth seeing, never a crash.
    orphans = [{"mls_id": k, **v} for k, v in sorted(by_feed.items())]
    states: dict[str, int] = {}
    for f in feeds:
        states[f["health"]] = states.get(f["health"], 0) + 1
    return {
        "feeds": feeds,
        "summary": {"feeds": len(feeds), "licensed": sum(1 for f in feeds if f["licensed"]),
                    "by_health": states},
        "entitlements_without_feed": orphans,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# GET /api/admin/comms — §22
# ---------------------------------------------------------------------------

def _platform_providers() -> dict[str, bool]:
    """Whether each carrier has platform credentials configured. Booleans only."""
    def present(*names: str) -> bool:
        return all((os.getenv(n) or "").strip() for n in names)
    return {
        "twilio": present("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN"),
        "plivo": present("PLIVO_AUTH_ID", "PLIVO_AUTH_TOKEN"),
        "telnyx": present("TELNYX_API_KEY"),
    }


@router.get("/comms")
async def comms(ctx: TenantContext = Depends(require_platform_admin)) -> dict[str, Any]:
    try:
        async with tenant_tx(ctx) as conn:
            await _start(conn)
            s = _Sections(conn)
            voice = await s.run("voice", lambda: conn.fetch(
                """
                SELECT tenant_id,
                       count(*) FILTER (WHERE active)::int AS routes,
                       array_agg(DISTINCT provider) FILTER (WHERE active) AS providers,
                       min(inbound_did) FILTER (WHERE active) AS sample_number,
                       count(*) FILTER (WHERE active AND outbound_verification_status = 'verified')::int AS verified,
                       count(*) FILTER (WHERE active AND outbound_verification_status = 'pending')::int AS pending,
                       count(*) FILTER (WHERE active AND (outbound_verification_status = 'failed'
                                         OR inbound_forwarding_status = 'failed'))::int AS routing_errors
                  FROM telephony_routes GROUP BY tenant_id LIMIT 1000
                """), [])
            texting = await s.run("messaging", lambda: conn.fetch(
                """
                SELECT tenant_id,
                       count(*) FILTER (WHERE active)::int AS routes,
                       array_agg(DISTINCT provider) FILTER (WHERE active) AS providers,
                       count(*) FILTER (WHERE active AND hosted_order_status = 'active')::int AS live,
                       count(*) FILTER (WHERE active AND hosted_order_status IN
                           ('pending_verification','pending_documents','processing'))::int AS pending,
                       count(*) FILTER (WHERE active AND hosted_order_status IN
                           ('failed','manual_action_required'))::int AS needs_action
                  FROM messaging_routes GROUP BY tenant_id LIMIT 1000
                """), [])
            brands = await s.run("registration", lambda: conn.fetch(
                """
                SELECT b.tenant_id, b.status AS brand_status,
                       (SELECT array_agg(c.status) FROM tenant_messaging_campaigns c
                         WHERE c.tenant_id = b.tenant_id) AS campaign_statuses
                  FROM tenant_messaging_brands b LIMIT 1000
                """), [])
            sms = await s.run("sms_24h", lambda: conn.fetch(
                """
                SELECT tenant_id, count(*)::int AS outbound,
                       count(*) FILTER (WHERE status IN ('failed','undelivered'))::int AS failed
                  FROM sms_messages
                 WHERE direction = 'outbound' AND created_at > now() - interval '24 hours'
                 GROUP BY tenant_id
                """), [])
            calls = await s.run("calls_24h", lambda: conn.fetch(
                """
                SELECT tenant_id, count(*)::int AS inbound,
                       count(*) FILTER (WHERE provider_status = 'failed')::int AS failed
                  FROM inbound_voice_calls
                 WHERE created_at > now() - interval '24 hours'
                 GROUP BY tenant_id
                """), [])
            commands = await s.run("commands_24h", lambda: conn.fetch(
                """
                SELECT tenant_id, command_type, count(*)::int AS failed
                  FROM command_executions
                 WHERE command_type IN ('CALL','SMS','EMAIL') AND state = 'failed'
                   AND updated_at > now() - interval '24 hours'
                 GROUP BY tenant_id, command_type
                """), [])
            ids = sorted({str(r["tenant_id"]) for group in (voice, texting, brands, sms, calls, commands)
                          for r in group})
            names = await s.run("names", lambda: conn.fetch(
                "SELECT id, name FROM tenants WHERE id = ANY($1::uuid[])", ids), [])
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _db_unavailable(exc) from None

    name_of = {str(r["id"]): r["name"] for r in names}
    out: dict[str, dict] = {}

    def entry(tid: str) -> dict:
        return out.setdefault(tid, {
            "tenant_id": tid, "name": name_of.get(tid),
            "voice": {"configured": False, "routes": 0, "providers": [], "business_number": None,
                      "verified": 0, "verification_pending": 0, "routing_errors": 0},
            "messaging": {"configured": False, "routes": 0, "providers": [], "live": 0,
                          "pending": 0, "needs_action": 0, "brand_10dlc": None, "campaigns_10dlc": {}},
            "failures_24h": {"sms_outbound": 0, "sms_failed": 0, "inbound_calls": 0,
                             "inbound_calls_failed": 0, "neoh_calls_failed": 0,
                             "neoh_texts_failed": 0, "neoh_emails_failed": 0},
        })

    for r in voice:
        v = entry(str(r["tenant_id"]))["voice"]
        v.update(configured=_int(r["routes"]) > 0, routes=_int(r["routes"]),
                 providers=sorted(p for p in (r["providers"] or []) if p),
                 business_number=mask_e164(r["sample_number"]), verified=_int(r["verified"]),
                 verification_pending=_int(r["pending"]), routing_errors=_int(r["routing_errors"]))
    for r in texting:
        m = entry(str(r["tenant_id"]))["messaging"]
        m.update(configured=_int(r["routes"]) > 0, routes=_int(r["routes"]),
                 providers=sorted(p for p in (r["providers"] or []) if p), live=_int(r["live"]),
                 pending=_int(r["pending"]), needs_action=_int(r["needs_action"]))
    for r in brands:
        m = entry(str(r["tenant_id"]))["messaging"]
        m["brand_10dlc"] = r["brand_status"]
        tally: dict[str, int] = {}
        for st in r["campaign_statuses"] or []:
            tally[st] = tally.get(st, 0) + 1
        m["campaigns_10dlc"] = tally
    for r in sms:
        f = entry(str(r["tenant_id"]))["failures_24h"]
        f.update(sms_outbound=_int(r["outbound"]), sms_failed=_int(r["failed"]))
    for r in calls:
        f = entry(str(r["tenant_id"]))["failures_24h"]
        f.update(inbound_calls=_int(r["inbound"]), inbound_calls_failed=_int(r["failed"]))
    key = {"CALL": "neoh_calls_failed", "SMS": "neoh_texts_failed", "EMAIL": "neoh_emails_failed"}
    for r in commands:
        entry(str(r["tenant_id"]))["failures_24h"][key[r["command_type"]]] = _int(r["failed"])

    for e in out.values():
        m, v = e["messaging"], e["voice"]
        verification = []
        if v["verification_pending"]:
            verification.append("caller ID verification pending")
        if m["pending"] or m["brand_10dlc"] in ("pending", "unverified") or m["campaigns_10dlc"].get("pending"):
            verification.append("texting registration pending")
        e["verification_pending"] = verification
        problems = []
        if v["routing_errors"]:
            problems.append("call routing or caller-ID verification failed")
        if m["needs_action"]:
            problems.append("texting number needs action")
        if m["brand_10dlc"] == "failed" or m["campaigns_10dlc"].get("rejected") or m["campaigns_10dlc"].get("failed"):
            problems.append("10DLC registration rejected")
        fails = e["failures_24h"]
        if fails["sms_failed"] and fails["sms_failed"] * 5 >= max(fails["sms_outbound"], 1):
            problems.append("many texts failing in the last 24h")
        if fails["neoh_calls_failed"] or fails["neoh_texts_failed"] or fails["neoh_emails_failed"]:
            problems.append("Neoh outreach failed in the last 24h")
        e["problems"] = problems

    rows = sorted(out.values(), key=lambda e: (not e["problems"], e["name"] or ""))
    return {
        "platform_providers": _platform_providers(),
        "brokerages": rows,
        "unavailable": s.unavailable,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# GET /api/admin/ai — §23
# ---------------------------------------------------------------------------

def _gateway_view() -> dict[str, Any]:
    """Configured providers/models and this replica's call counts. No keys:
    only Provider.name and Provider.model are read."""
    import llm_gateway

    ladders = {}
    for task in ("analysis", "fast"):
        try:
            ladders[task] = [{"provider": p.name, "model": p.model} for p in llm_gateway.providers_for(task)]
        except Exception:  # noqa: BLE001 — a broken builder must not hide the rest
            ladders[task] = []
    snap = llm_gateway.counter.snapshot()
    providers: dict[str, dict[str, int]] = {}
    for bucket, field in (("calls", "calls"), ("failures", "failures"), ("rate_limited", "rate_limited")):
        for key, n in (snap.get(bucket) or {}).items():
            name = key.split(":", 1)[-1]
            providers.setdefault(name, {"calls": 0, "failures": 0, "rate_limited": 0})[field] += _int(n)
    for p in providers.values():
        attempts = p["calls"] + p["failures"]
        p["failure_rate"] = round(p["failures"] / attempts, 3) if attempts else None
    return {
        "configured": any(ladders.values()),
        "ladders": ladders,
        "current": {task: (ladder[0] if ladder else None) for task, ladder in ladders.items()},
        "providers": providers,
        "counter_scope": "this API replica since it started",
        "window_seconds": snap.get("window_seconds"),
    }


@router.get("/ai")
async def ai_operations(ctx: TenantContext = Depends(require_platform_admin)) -> dict[str, Any]:
    import component_health

    gateway = _gateway_view()
    try:
        snap = await component_health.snapshot()
        ai_health = snap["components"].get("ai")
    except Exception:  # noqa: BLE001
        ai_health = {"state": "UNKNOWN", "summary": "health probe did not answer"}
    try:
        async with tenant_tx(ctx) as conn:
            await _start(conn)
            s = _Sections(conn)
            turns = await s.run("chat_latency", lambda: conn.fetchrow(
                """
                SELECT count(*) FILTER (WHERE status = 'completed')::int AS completed,
                       count(*) FILTER (WHERE status = 'failed')::int    AS failed,
                       percentile_cont(0.5) WITHIN GROUP (
                           ORDER BY EXTRACT(EPOCH FROM (updated_at - created_at)))
                           FILTER (WHERE status = 'completed') AS p50,
                       percentile_cont(0.95) WITHIN GROUP (
                           ORDER BY EXTRACT(EPOCH FROM (updated_at - created_at)))
                           FILTER (WHERE status = 'completed') AS p95
                  FROM ai_chat_messages
                 WHERE role = 'assistant' AND created_at > now() - interval '24 hours'
                """), None)
            errors = await s.run("chat_errors", lambda: conn.fetch(
                "SELECT error_code, count(*)::int AS n FROM ai_chat_messages "
                " WHERE role = 'assistant' AND status = 'failed' AND created_at > now() - interval '24 hours' "
                " GROUP BY error_code ORDER BY 2 DESC LIMIT 10"), [])
            models = await s.run("models", lambda: conn.fetch(
                "SELECT model_id, count(*)::int AS n FROM ai_chat_messages "
                " WHERE role = 'assistant' AND status = 'completed' AND created_at > now() - interval '24 hours' "
                " GROUP BY model_id ORDER BY 2 DESC LIMIT 10"), [])
            tools = await s.run("tools", lambda: conn.fetchrow(
                "SELECT count(*)::int AS total, count(*) FILTER (WHERE status = 'failed')::int AS failed "
                "  FROM ai_tool_operations WHERE created_at > now() - interval '24 hours'"), None)
            tool_failures = await s.run("tool_failures", lambda: conn.fetch(
                "SELECT tool_name, error_code, count(*)::int AS n FROM ai_tool_operations "
                " WHERE status = 'failed' AND created_at > now() - interval '24 hours' "
                " GROUP BY tool_name, error_code ORDER BY 3 DESC LIMIT 10"), [])
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _db_unavailable(exc) from None

    completed = _int((turns or {}).get("completed")) if turns else 0
    failed = _int((turns or {}).get("failed")) if turns else 0
    tool_total = _int((tools or {}).get("total")) if tools else 0
    tool_failed = _int((tools or {}).get("failed")) if tools else 0

    def secs(v):
        return round(float(v), 2) if v is not None else None

    rate_limited_codes = sum(_int(e["n"]) for e in errors
                             if "rate" in str(e["error_code"] or "").lower())
    return {
        "gateway": gateway,
        "health": ai_health,
        "chat_24h": {
            "completed": completed,
            "failed": failed,
            "failure_rate": round(failed / (completed + failed), 3) if completed + failed else None,
            # pending -> final update. A turn still streaming is not counted.
            "latency_seconds": {"p50": secs((turns or {}).get("p50")) if turns else None,
                                "p95": secs((turns or {}).get("p95")) if turns else None,
                                "n": completed},
            "error_codes": [{"code": safe_code(e["error_code"]) or "none", "count": _int(e["n"])} for e in errors],
            "rate_limited": rate_limited_codes,
            "models": [{"model": safe_code(m["model_id"]) or "unknown", "count": _int(m["n"])} for m in models],
        },
        "tools_24h": {
            "total": tool_total,
            "failed": tool_failed,
            "failure_rate": round(tool_failed / tool_total, 3) if tool_total else None,
            "top_failures": [{"tool": safe_code(t["tool_name"]), "code": safe_code(t["error_code"]) or "none",
                              "count": _int(t["n"])} for t in tool_failures],
        },
        "unavailable": s.unavailable,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# GET /api/admin/pilot-metrics — §18, §19
# ---------------------------------------------------------------------------

DEAL_ATTRIBUTION_NOTE = (
    "Associated with Neoh = the last Neoh decision or approved command on the same "
    "person or deal within 90 days before it closed (last touch). This is association, "
    "not causation. Deal value is the recorded purchase price, not commission revenue — "
    "commission is not recorded."
)


@router.get("/pilot-metrics")
async def pilot_metrics(
    days: int = Query(default=7, ge=1, le=90),
    deal_days: int = Query(default=90, ge=7, le=365),
    ctx: TenantContext = Depends(require_platform_admin),
) -> dict[str, Any]:
    try:
        async with tenant_tx(ctx) as conn:
            await _start(conn)
            s = _Sections(conn)
            active = await s.run("active_agents", lambda: conn.fetch(
                """
                SELECT tenant_id, count(DISTINCT actor)::int AS n FROM (
                    SELECT tenant_id, user_id AS actor FROM ai_chat_messages
                     WHERE role = 'user' AND created_at > now() - make_interval(days => $1)
                    UNION
                    SELECT tenant_id, decided_by FROM action_approvals
                     WHERE decided_by IS NOT NULL AND decided_at > now() - make_interval(days => $1)
                ) a GROUP BY tenant_id
                """, days), [])
            chats = await s.run("conversations", lambda: conn.fetch(
                """
                SELECT tenant_id, count(*)::int AS turns,
                       count(DISTINCT (user_id, date_trunc('day', created_at)))::int AS agent_days
                  FROM ai_chat_messages
                 WHERE role = 'user' AND created_at > now() - make_interval(days => $1)
                 GROUP BY tenant_id
                """, days), [])
            tools = await s.run("actions", lambda: conn.fetch(
                "SELECT tenant_id, count(*)::int AS n FROM ai_tool_operations "
                " WHERE status = 'completed' AND created_at > now() - make_interval(days => $1) "
                " GROUP BY tenant_id", days), [])
            commands = await s.run("outreach", lambda: conn.fetch(
                "SELECT tenant_id, command_type, count(*)::int AS n FROM command_executions "
                " WHERE state = 'succeeded' AND updated_at > now() - make_interval(days => $1) "
                " GROUP BY tenant_id, command_type", days), [])
            matches = await s.run("matches", lambda: conn.fetch(
                "SELECT tenant_id, count(*)::int AS n FROM marketplace_matches "
                " WHERE state IN ('contact_approved','contacted','offer') "
                "   AND updated_at > now() - make_interval(days => $1) GROUP BY tenant_id", days), [])
            deals = await s.run("deals", lambda: conn.fetch(
                """
                SELECT tenant_id,
                       count(*) FILTER (WHERE outcome_kind = 'transaction_closed')::int AS closed,
                       count(*) FILTER (WHERE outcome_kind = 'transaction_closed'
                                        AND attributed_at IS NOT NULL)::int AS examined,
                       count(*) FILTER (WHERE outcome_kind = 'transaction_closed'
                                        AND (attributed_trace_id IS NOT NULL
                                             OR attributed_decision_id IS NOT NULL))::int AS associated,
                       sum(outcome_value) FILTER (WHERE outcome_kind = 'transaction_closed'
                                        AND (attributed_trace_id IS NOT NULL
                                             OR attributed_decision_id IS NOT NULL)) AS associated_value,
                       count(*) FILTER (WHERE outcome_kind = 'transaction_closed'
                                        AND (attributed_trace_id IS NOT NULL
                                             OR attributed_decision_id IS NOT NULL)
                                        AND outcome_value IS NULL)::int AS associated_unpriced,
                       count(*) FILTER (WHERE outcome_kind IN ('appointment_booked','showing_held','offer_made')
                                        AND (attributed_trace_id IS NOT NULL
                                             OR attributed_decision_id IS NOT NULL))::int AS other_associated
                  FROM outcome_events
                 WHERE occurred_at > now() - make_interval(days => $1)
                 GROUP BY tenant_id
                """, deal_days), [])
            ids = sorted({str(r["tenant_id"]) for group in (active, chats, tools, commands, matches, deals)
                          for r in group})
            names = await s.run("names", lambda: conn.fetch(
                "SELECT id, name FROM tenants WHERE id = ANY($1::uuid[])", ids), [])
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _db_unavailable(exc) from None

    name_of = {str(r["id"]): r["name"] for r in names}
    per: dict[str, dict] = {}

    def entry(tid: str) -> dict:
        return per.setdefault(tid, {
            "tenant_id": tid, "name": name_of.get(tid),
            "weekly_active_agents": 0, "neoh_conversations": {"turns": 0, "agent_days": 0},
            "neoh_completed_actions": 0,
            "outreach_through_neoh": {"calls": 0, "texts": 0, "emails": 0, "calendar": 0},
            "matches_acted_on": 0,
            "deal_activity": {"closed": 0, "examined": 0, "associated": 0,
                              "associated_deal_value": 0.0, "associated_unpriced": 0,
                              "other_outcomes_associated": 0},
        })

    for r in active:
        entry(str(r["tenant_id"]))["weekly_active_agents"] = _int(r["n"])
    for r in chats:
        entry(str(r["tenant_id"]))["neoh_conversations"] = {"turns": _int(r["turns"]),
                                                            "agent_days": _int(r["agent_days"])}
    for r in tools:
        entry(str(r["tenant_id"]))["neoh_completed_actions"] += _int(r["n"])
    kind = {"CALL": "calls", "SMS": "texts", "EMAIL": "emails", "CALENDAR": "calendar"}
    for r in commands:
        e = entry(str(r["tenant_id"]))
        e["outreach_through_neoh"][kind.get(r["command_type"], "emails")] += _int(r["n"])
        e["neoh_completed_actions"] += _int(r["n"])
    for r in matches:
        entry(str(r["tenant_id"]))["matches_acted_on"] = _int(r["n"])
    for r in deals:
        entry(str(r["tenant_id"]))["deal_activity"] = {
            "closed": _int(r["closed"]), "examined": _int(r["examined"]),
            "associated": _int(r["associated"]),
            "associated_deal_value": float(r["associated_value"] or 0),
            "associated_unpriced": _int(r["associated_unpriced"]),
            "other_outcomes_associated": _int(r["other_associated"]),
        }

    rows = sorted(per.values(), key=lambda e: (-e["weekly_active_agents"], e["name"] or ""))
    total = {
        "active_agents": sum(e["weekly_active_agents"] for e in rows),
        "neoh_conversation_turns": sum(e["neoh_conversations"]["turns"] for e in rows),
        "neoh_completed_actions": sum(e["neoh_completed_actions"] for e in rows),
        "outreach_through_neoh": {k: sum(e["outreach_through_neoh"][k] for e in rows)
                                  for k in ("calls", "texts", "emails", "calendar")},
        "matches_acted_on": sum(e["matches_acted_on"] for e in rows),
        "deal_activity": {k: sum(e["deal_activity"][k] for e in rows)
                          for k in ("closed", "examined", "associated", "associated_deal_value",
                                    "associated_unpriced", "other_outcomes_associated")},
    }
    return {
        "window_days": days,
        "deal_window_days": deal_days,
        "total": total,
        "brokerages": rows,
        "definitions": {
            "active_agents": "Distinct people who sent Neoh a message or decided an approval in the window.",
            "neoh_conversations": "Messages agents sent Neoh (turns) and distinct agent-days with a conversation; "
                                  "chat sessions are not recorded separately.",
            "neoh_completed_actions": "Changes Neoh applied from chat (ai_tool_operations) plus approved "
                                      "calls/texts/emails/calendar events Neoh carried out.",
            "outreach_through_neoh": "Approved commands Neoh carried out successfully, by channel.",
            "matches_acted_on": "Marketplace buyer matches moved to contact-approved, contacted or offer.",
            "deal_activity": DEAL_ATTRIBUTION_NOTE,
        },
        "not_tracked": {
            "time_saved": "Not measured — no baseline of how long the work took without Neoh exists.",
            "property_buyer_matches_outside_marketplace": "CRM property suggestions are not recorded as acted on.",
            "commission_revenue": "Commission is not recorded; only purchase price on the transaction.",
        },
        "unavailable": s.unavailable,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
