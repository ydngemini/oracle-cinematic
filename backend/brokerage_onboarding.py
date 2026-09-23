"""Brokerage onboarding — business profile, agent invitations, setup state.

What this module is for: a real brokerage owner signs up, says who they are,
invites their agents, and those agents land in the right tenant. No developer,
no manual SQL, no tenant id copied by hand.

Three boundaries worth stating, because each one was a temptation:

  * A tenant IS a brokerage. 0001 named them "Domains (brokerages)" and this
    module extends `tenants` rather than introducing an organization table
    beside it. Two tables claiming to be the same thing disagree eventually.

  * Membership is `team_memberships` (0027), which already had an unused
    'invited' status waiting for exactly this flow. Invitations are separate
    only because team_memberships.user_id is NOT NULL REFERENCES users(id) and
    an invitee has no account yet.

  * Capability status is DERIVED from the real tables wherever a real table
    exists — phone from telephony_routes, texts from messaging_routes, billing
    from subscriptions. Only genuinely unknowable intent ("we already imported
    contacts", "skip this") is stored. A cached copy of "phone is ready" is a
    copy that can be wrong, and being wrong about that is worse than being
    slow.

Tenant safety: every tenant id comes from the verified JWT via TenantContext.
Nothing here reads a tenant from a request body or query string.
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from db.connection import tenant_tx
from tenancy import Role, TenantContext, require_context, require_role

log = logging.getLogger("oracle.brokerage_onboarding")

router = APIRouter(prefix="/api/brokerage", tags=["Brokerage Onboarding"])

INVITE_TTL_DAYS = 14
MAX_BULK_INVITES = 25

# The capability order is also the order we recommend working through them.
CAPABILITIES = (
    "brokerage_profile",
    "agent_invites",
    "contact_import",
    "email_calendar",
    "phone",
    "mls",
    "billing",
    "readiness",
)

# Capabilities Neoh genuinely does not need in order to be useful. These never
# become the recommended next step and never block readiness — §17: a missing
# MLS feed must not stop someone managing contacts.
OPTIONAL_CAPABILITIES = frozenset({"contact_import", "email_calendar", "mls"})

# Capabilities with no live source of truth; their status is whatever the
# operator last told us.
# mls used to live here. It now has a real source of truth — feed health and
# licence classification — so an operator can no longer type "mls: READY" and
# have the setup screen agree.
PROGRESS_BACKED = frozenset({"contact_import", "email_calendar"})

Status = Literal[
    "NOT_STARTED", "NEEDS_ACTION", "IN_PROGRESS", "READY", "BLOCKED", "ERROR", "OPTIONAL"
]


# ---------------------------------------------------------------------------
# Invitation tokens
# ---------------------------------------------------------------------------

def new_invitation_token() -> tuple[str, str]:
    """Return (raw_token, sha256_hex).

    The raw token exists only long enough to build the email link. Only the
    digest is ever stored, so a database read — a backup, a dump, a support
    query — cannot reconstruct a working invitation.
    """
    raw = secrets.token_urlsafe(32)
    return raw, hash_invitation_token(raw)


def hash_invitation_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def invitation_link(raw_token: str) -> str:
    """The link that goes in the email.

    ORACLE_PUBLIC_BASE_URL is the codebase's existing, documented public-origin
    variable (commands_api and plivo_call_handler already build URLs from it).
    Inventing a second one would both duplicate it and fail the env-var
    documentation test, which is a ratchet precisely against that.
    """
    base = (os.getenv("ORACLE_PUBLIC_BASE_URL") or "").strip().rstrip("/")
    path = f"/accept-invite?token={raw_token}"
    return f"{base}{path}" if base else path


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class BrokerageProfileUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: Optional[str] = Field(default=None, min_length=2, max_length=160)
    org_type: Optional[Literal["brokerage", "team", "independent_agent"]] = None
    primary_state: Optional[str] = Field(default=None, pattern=r"^[A-Za-z]{2}$")
    website: Optional[str] = Field(default=None, max_length=300)


class InviteCreate(BaseModel):
    """One or many — the same endpoint, because a UI that lets you paste three
    addresses should not need a different route than one that lets you paste
    one."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    emails: list[str] = Field(min_length=1, max_length=MAX_BULK_INVITES)

    @field_validator("emails")
    @classmethod
    def _valid_addresses(cls, value: list[str]) -> list[str]:
        """Same shape check /auth/register uses, deliberately.

        pydantic's EmailStr would need the email_validator package, which this
        backend does not install — and an invitation that passes a stricter
        check here than signup applies later would be an invitation nobody can
        accept. Deduplicated case-insensitively so pasting the same address
        twice sends one email, not two.
        """
        seen: dict[str, str] = {}
        for raw in value:
            email = (raw or "").strip().lower()
            local, _, domain = email.rpartition("@")
            # /auth/register checks only for "@" and a dotted domain, which
            # also accepts "@example.com". Requiring a local part here is
            # strictly narrower, so nothing this accepts can fail there — and
            # it stops an invitation being addressed to nobody.
            if not local or "." not in domain or len(email) > 128:
                raise ValueError(f"{raw!r} is not a valid email address.")
            seen.setdefault(email, email)
        if not seen:
            raise ValueError("At least one email address is required.")
        return list(seen.values())
    # The invitee never picks this; the authorized inviter does, and the server
    # re-checks it below. platform_admin is not in the Literal at all, so an
    # invitation can never mint god-mode.
    role: Literal["agent", "broker_owner"] = "agent"


class SetupProgressUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    capability: Literal["contact_import", "email_calendar"]
    status: Literal["NOT_STARTED", "IN_PROGRESS", "READY", "BLOCKED", "ERROR", "OPTIONAL"]
    detail: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Capability derivation
# ---------------------------------------------------------------------------

async def _count_or_absent(conn, sql: str, tenant_id: str) -> Optional[dict]:
    """Run a capability count, tolerating a table that is not there yet.

    A brokerage's setup screen must not 500 because one carrier rail's
    migration has not been applied. §17: Neoh stays useful while integrations
    are incomplete, and that has to include the screen that reports on them.

    The miss is logged at error level rather than swallowed — a missing table
    IS a deployment problem, it is just not this screen's problem to die of.
    """
    import asyncpg
    try:
        row = await conn.fetchrow(sql, tenant_id)
        return dict(row) if row else None
    except asyncpg.UndefinedTableError:
        log.error("Capability table missing — is the database behind on migrations? %s",
                  sql.split("FROM", 1)[-1].strip()[:60])
        return None


async def _phone_status(conn, tenant_id: str) -> Status:
    """READY when some active route in this brokerage has a server-verified
    caller id. That is inbound_voice's own definition of a number Neoh may
    speak from — reproducing a looser one here would let setup claim ready for
    a number the call path will refuse."""
    row = await _count_or_absent(
        conn,
        "SELECT count(*) FILTER (WHERE voice_caller_id_verified "
        "                         AND voice_caller_id_e164 IS NOT NULL "
        "                         AND outbound_verification_status = 'verified') AS verified, "
        "       count(*) FILTER (WHERE outbound_verification_status = 'pending') AS pending, "
        "       count(*) AS total "
        "  FROM telephony_routes WHERE tenant_id = $1::uuid AND active",
        tenant_id,
    )
    if not row or not row["total"]:
        return "NOT_STARTED"
    if row["verified"]:
        return "READY"
    if row["pending"]:
        return "IN_PROGRESS"
    return "NEEDS_ACTION"


async def _messaging_status(conn, tenant_id: str) -> Status:
    """Texts are tracked separately from calls on purpose: a brokerage can be
    taking calls while its 10DLC registration is still pending, and conflating
    them would report one capability that is true of neither."""
    row = await _count_or_absent(
        conn,
        "SELECT count(*) FILTER (WHERE hosted_order_status = 'active') AS active, "
        "       count(*) FILTER (WHERE hosted_order_status NOT IN ('active','not_started')) AS working, "
        "       count(*) AS total "
        "  FROM messaging_routes WHERE tenant_id = $1::uuid AND active",
        tenant_id,
    )
    if not row or not row["total"]:
        return "NOT_STARTED"
    if row["active"]:
        return "READY"
    return "IN_PROGRESS" if row["working"] else "NEEDS_ACTION"


async def _billing_status(conn, tenant_id: str) -> Status:
    row = await _count_or_absent(
        conn,
        "SELECT status FROM subscriptions WHERE tenant_id = $1::uuid "
        " ORDER BY created_at DESC LIMIT 1",
        tenant_id,
    )
    if not row:
        return "NOT_STARTED"
    # Matches billing.py: past_due is Stripe's retry window, still usable.
    return "READY" if row["status"] in ("active", "trialing", "past_due") else "NEEDS_ACTION"


async def _profile_status(tenant: dict) -> Status:
    if tenant.get("profile_completed_at"):
        return "READY"
    if tenant.get("primary_state") or tenant.get("org_type") != "brokerage":
        return "IN_PROGRESS"
    return "NEEDS_ACTION"


async def _invites_status(conn, tenant_id: str) -> tuple[Status, dict]:
    counts = await conn.fetchrow(
        "SELECT "
        "  (SELECT count(*) FROM users WHERE tenant_id = $1::uuid AND is_active) AS members, "
        "  (SELECT count(*) FROM brokerage_invitations WHERE tenant_id = $1::uuid "
        "     AND consumed_at IS NULL AND revoked_at IS NULL AND expires_at > now()) AS pending, "
        "  (SELECT count(*) FROM brokerage_invitations WHERE tenant_id = $1::uuid "
        "     AND consumed_at IS NOT NULL) AS accepted",
        tenant_id,
    )
    members = int(counts["members"] or 0)
    pending = int(counts["pending"] or 0)
    accepted = int(counts["accepted"] or 0)
    summary = {"active_members": members, "pending_invitations": pending, "accepted_invitations": accepted}
    if accepted or members > 1:
        return "READY", summary
    if pending:
        return "IN_PROGRESS", summary
    return "NEEDS_ACTION", summary


async def compute_setup_state(conn, ctx: TenantContext) -> dict[str, Any]:
    """The one place that answers "what can this brokerage use right now?"."""
    tenant = await conn.fetchrow(
        "SELECT id, slug, name, org_type, primary_state, website, profile_completed_at "
        "  FROM tenants WHERE id = $1::uuid",
        ctx.tenant_id,
    )
    if not tenant:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Brokerage not found.")
    tenant = dict(tenant)

    stored = {
        r["capability"]: {"status": r["status"], "detail": dict(r["detail"] or {})}
        for r in await conn.fetch(
            "SELECT capability, status, detail FROM brokerage_setup_progress "
            " WHERE tenant_id = $1::uuid",
            ctx.tenant_id,
        )
    }

    invites_status, team_summary = await _invites_status(conn, ctx.tenant_id)
    phone = await _phone_status(conn, ctx.tenant_id)
    messaging = await _messaging_status(conn, ctx.tenant_id)

    # MLS comes from real feed health and licence classification. Developer or
    # reference datasets can never report READY, however green their sync is.
    try:
        from mls_health import mls_capability
        mls_state = await mls_capability(conn, ctx)
    except Exception as exc:  # noqa: BLE001
        log.debug("MLS capability unavailable: %s", exc)
        mls_state = {"status": "NOT_STARTED", "detail": "", "feeds": []}

    capabilities: dict[str, Status] = {
        "brokerage_profile": await _profile_status(tenant),
        "agent_invites": invites_status,
        "phone": phone,
        "billing": await _billing_status(conn, ctx.tenant_id),
        "mls": mls_state["status"],
    }
    for cap in PROGRESS_BACKED:
        capabilities[cap] = stored.get(cap, {}).get("status", "NOT_STARTED")

    # readiness is the summary, not an input: it is READY only once everything
    # non-optional is, and BLOCKED while anything required is outstanding.
    required = [c for c in CAPABILITIES
                if c not in OPTIONAL_CAPABILITIES and c != "readiness"]
    outstanding = [c for c in required if capabilities.get(c) != "READY"]
    capabilities["readiness"] = "READY" if not outstanding else "BLOCKED"

    recommended = outstanding[0] if outstanding else None

    return {
        "brokerage": {
            "id": str(tenant["id"]),
            "name": tenant["name"],
            "slug": tenant["slug"],
            "org_type": tenant["org_type"],
            "primary_state": tenant["primary_state"],
            "website": tenant["website"],
            "profile_completed_at": tenant["profile_completed_at"],
        },
        "capabilities": capabilities,
        "recommended_next": recommended,
        "team": team_summary,
        # Calls and texts are reported separately — see _messaging_status.
        "messaging": messaging,
        # Why MLS is where it is, and which feeds back it. An operator should
        # not need SQL to learn that the only connected feed is a sample set.
        "mls": mls_state,
        "optional": sorted(OPTIONAL_CAPABILITIES),
    }


# ---------------------------------------------------------------------------
# Invitation email
# ---------------------------------------------------------------------------

def build_invitation_email(*, brokerage: str, inviter: str, link: str, expires_at: datetime) -> tuple[str, str, str]:
    """(subject, text, html), mirroring missions/digest.py's parallel-list
    style. Inline markup only — there is no template engine in this codebase
    and introducing one for four paragraphs would be its own maintenance."""
    expiry = expires_at.strftime("%d %b %Y")
    subject = f"{inviter} invited you to {brokerage} on Neoh"
    text = (
        f"{inviter} has invited you to join {brokerage} on Neoh.\n\n"
        f"Neoh is the workspace {brokerage} uses to manage contacts, properties "
        f"and deals.\n\nAccept the invitation:\n{link}\n\n"
        f"This link is personal to you and stops working on {expiry}.\n"
        f"If you were not expecting this, you can ignore it.\n"
    )
    html = (
        f"<p>{inviter} has invited you to join <strong>{brokerage}</strong> on Neoh.</p>"
        f"<p>Neoh is the workspace {brokerage} uses to manage contacts, properties and deals.</p>"
        f'<p><a href="{link}">Accept the invitation</a></p>'
        f"<p style='color:#666;font-size:13px'>This link is personal to you and stops "
        f"working on {expiry}. If you were not expecting this, you can ignore it.</p>"
    )
    return subject, text, html


def _dev_capture_enabled() -> bool:
    """True when there is no configured mail server and we are not in prod.

    Deliberately keyed on existing configuration rather than a new env var:
    the repo has a test that fails on undocumented variables, and "SMTP is not
    set up and this is a dev box" is already an unambiguous signal. In that
    case the invite link is logged and returned to the caller so a developer
    can complete the flow; in prod, a missing mail server is an error, because
    an invitation nobody receives is not an invitation.
    """
    from smtp_mailer import is_configured
    if (os.getenv("ORACLE_ENV") or "dev").lower() in ("prod", "production"):
        return False
    try:
        return not is_configured()
    except Exception:
        return True


async def _send_invitation(*, email: str, brokerage: str, inviter: str,
                           raw_token: str, expires_at: datetime) -> Optional[str]:
    """Send the invite. Returns the link when dev-captured, else None.

    A send failure does NOT roll back the invitation: the row is already
    committed and the owner can resend. Losing the invitation because the mail
    server hiccuped would be the worse outcome.
    """
    import asyncio

    link = invitation_link(raw_token)
    if _dev_capture_enabled():
        log.warning("DEV: invitation for %s not emailed (no SMTP configured). Link: %s", email, link)
        return link

    subject, text, html = build_invitation_email(
        brokerage=brokerage, inviter=inviter, link=link, expires_at=expires_at
    )
    try:
        import smtp_mailer
        await asyncio.wait_for(
            asyncio.to_thread(smtp_mailer.send, recipient=email, subject=subject, text=text, html=html),
            timeout=40.0,
        )
    except Exception as exc:  # noqa: BLE001 — every failure is the same outcome here
        log.error("Invitation email to %s failed: %s", email, exc)
    return None


# ---------------------------------------------------------------------------
# Invitation lifecycle
# ---------------------------------------------------------------------------

def _invite_row(row) -> dict[str, Any]:
    """Public shape. token_hash is never in it."""
    now = datetime.now(timezone.utc)
    if row["revoked_at"]:
        state = "revoked"
    elif row["consumed_at"]:
        state = "accepted"
    elif row["expires_at"] <= now:
        state = "expired"
    else:
        state = "pending"
    return {
        "id": str(row["id"]),
        "email": row["email"],
        "role": row["invited_role"],
        "state": state,
        "invited_by": row["invited_by_agent_id"],
        "expires_at": row["expires_at"],
        "created_at": row["created_at"],
        "last_sent_at": row["last_sent_at"],
        "send_count": row["send_count"],
    }


_INVITE_COLUMNS = (
    "id, email, invited_role, invited_by_agent_id, expires_at, "
    "consumed_at, revoked_at, created_at, last_sent_at, send_count"
)


async def _resolve_inviter(conn, ctx: TenantContext) -> dict:
    row = await conn.fetchrow(
        "SELECT id, agent_id FROM users "
        " WHERE tenant_id = $1::uuid AND lower(agent_id) = lower($2) AND is_active",
        ctx.tenant_id, ctx.agent_id,
    )
    if not row:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Your account is not a member of this brokerage.",
        )
    return dict(row)


async def create_invitations(conn, ctx: TenantContext, emails: list[str], role: str) -> dict[str, Any]:
    """Issue invitations, one row per address. Returns created + skipped.

    Idempotent by construction: re-inviting an address that already has a live
    invitation revokes the old row and issues a fresh one inside the same
    transaction, carrying send_count forward. Double-clicking Send therefore
    produces one live invitation, not two — and never two memberships.
    """
    inviter = await _resolve_inviter(conn, ctx)
    tenant = await conn.fetchrow("SELECT name FROM tenants WHERE id = $1::uuid", ctx.tenant_id)
    brokerage = (tenant or {}).get("name") or "your brokerage"
    expires_at = datetime.now(timezone.utc) + timedelta(days=INVITE_TTL_DAYS)

    created: list[dict] = []
    skipped: list[dict] = []
    to_send: list[tuple[str, str]] = []

    for raw_email in emails:
        email = str(raw_email).strip().lower()

        # Already in this brokerage? Nothing to do — and say so rather than
        # sending a link that would immediately report "already a member".
        existing = await conn.fetchrow(
            "SELECT 1 FROM users WHERE tenant_id = $1::uuid AND lower(agent_id) = $2",
            ctx.tenant_id, email,
        )
        if existing:
            skipped.append({"email": email, "reason": "already_a_member"})
            continue

        prior = await conn.fetchrow(
            "SELECT token_hash, send_count FROM brokerage_invitations "
            " WHERE tenant_id = $1::uuid AND email = $2 "
            "   AND consumed_at IS NULL AND revoked_at IS NULL",
            ctx.tenant_id, email,
        )
        send_count = 1
        if prior:
            await conn.execute(
                "UPDATE brokerage_invitations SET revoked_at = now(), "
                "       revoked_by_agent_id = $2 WHERE token_hash = $1",
                prior["token_hash"], ctx.agent_id,
            )
            send_count = int(prior["send_count"]) + 1

        raw_token, token_hash = new_invitation_token()
        row = await conn.fetchrow(
            "INSERT INTO brokerage_invitations "
            " (token_hash, tenant_id, email, invited_role, invited_by, "
            "  invited_by_agent_id, expires_at, send_count) "
            " VALUES ($1, $2::uuid, $3, $4, $5, $6, $7, $8) "
            f"RETURNING {_INVITE_COLUMNS}",
            token_hash, ctx.tenant_id, email, role, inviter["id"],
            ctx.agent_id, expires_at, send_count,
        )
        created.append(_invite_row(row))
        to_send.append((email, raw_token))

    return {
        "created": created,
        "skipped": skipped,
        "_deliveries": to_send,
        "_brokerage": brokerage,
        "_expires_at": expires_at,
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/setup")
async def get_setup(ctx: TenantContext = Depends(require_context)) -> dict[str, Any]:
    """The whole setup screen in one read. Any member may see where their
    brokerage stands; only an owner may change it."""
    async with tenant_tx(ctx) as conn:
        return await compute_setup_state(conn, ctx)


@router.patch("/profile")
async def update_profile(
    body: BrokerageProfileUpdate,
    ctx: TenantContext = Depends(require_context),
) -> dict[str, Any]:
    require_role(ctx, Role.BROKER_OWNER, Role.PLATFORM_ADMIN)
    fields = body.model_dump(exclude_none=True)
    if not fields:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Nothing to update.")
    if "primary_state" in fields:
        fields["primary_state"] = fields["primary_state"].upper()

    sets = ", ".join(f"{col} = ${i + 2}" for i, col in enumerate(fields))
    async with tenant_tx(ctx) as conn:
        # The tenant id is the session's, never the body's. There is no code
        # path here that can be aimed at another brokerage.
        row = await conn.fetchrow(
            f"UPDATE tenants SET {sets}, "
            "       profile_completed_at = COALESCE(profile_completed_at, now()) "
            " WHERE id = $1::uuid RETURNING id",
            ctx.tenant_id, *fields.values(),
        )
        if not row:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Brokerage not found.")
        return await compute_setup_state(conn, ctx)


@router.get("/team")
async def get_team(ctx: TenantContext = Depends(require_context)) -> dict[str, Any]:
    """The roster: everyone with an account in this brokerage, plus everyone
    who has been invited and has not accepted yet. One list, because that is
    how the owner thinks about their team."""
    async with tenant_tx(ctx) as conn:
        members = await conn.fetch(
            "SELECT u.id, u.agent_id, u.email, u.full_name, u.role, u.is_active, u.created_at, "
            "       m.status AS membership_status, m.title "
            "  FROM users u "
            "  LEFT JOIN team_memberships m ON m.user_id = u.id AND m.tenant_id = u.tenant_id "
            " WHERE u.tenant_id = $1::uuid "
            " ORDER BY u.created_at ASC",
            ctx.tenant_id,
        )
        invites = await conn.fetch(
            f"SELECT {_INVITE_COLUMNS} FROM brokerage_invitations "
            " WHERE tenant_id = $1::uuid AND consumed_at IS NULL AND revoked_at IS NULL "
            " ORDER BY created_at DESC",
            ctx.tenant_id,
        )
        return {
            "members": [
                {
                    "id": str(m["id"]),
                    "agent_id": m["agent_id"],
                    "email": m["email"] or m["agent_id"],
                    "full_name": m["full_name"],
                    "role": m["role"],
                    "title": m["title"],
                    "status": "active" if m["is_active"] else "suspended",
                    "membership_status": m["membership_status"],
                    "joined_at": m["created_at"],
                }
                for m in members
            ],
            "pending_invitations": [_invite_row(r) for r in invites],
        }


@router.get("/invitations")
async def list_invitations(ctx: TenantContext = Depends(require_context)) -> dict[str, Any]:
    require_role(ctx, Role.BROKER_OWNER, Role.PLATFORM_ADMIN)
    async with tenant_tx(ctx) as conn:
        rows = await conn.fetch(
            f"SELECT {_INVITE_COLUMNS} FROM brokerage_invitations "
            " WHERE tenant_id = $1::uuid ORDER BY created_at DESC LIMIT 200",
            ctx.tenant_id,
        )
        return {"invitations": [_invite_row(r) for r in rows]}


@router.post("/invitations", status_code=status.HTTP_201_CREATED)
async def post_invitations(
    body: InviteCreate,
    ctx: TenantContext = Depends(require_context),
) -> dict[str, Any]:
    """Invite one agent or twenty-five. The role comes from this authenticated
    owner, never from the invitee."""
    require_role(ctx, Role.BROKER_OWNER, Role.PLATFORM_ADMIN)

    async with tenant_tx(ctx) as conn:
        result = await create_invitations(conn, ctx, [str(e) for e in body.emails], body.role)
        brokerage = result.pop("_brokerage")
        expires_at = result.pop("_expires_at")
        deliveries = result.pop("_deliveries")

    # Mail is sent AFTER the transaction commits. Sending inside it would mean
    # a slow mail server holds a database transaction open, and a rollback
    # after a successful send would leave a live link to a row that no longer
    # exists.
    dev_links: dict[str, str] = {}
    for email, raw_token in deliveries:
        link = await _send_invitation(
            email=email, brokerage=brokerage, inviter=ctx.agent_id,
            raw_token=raw_token, expires_at=expires_at,
        )
        if link:
            dev_links[email] = link
    if dev_links:
        result["dev_links"] = dev_links
    return result


@router.post("/invitations/{invitation_id}/resend")
async def resend_invitation(
    invitation_id: str,
    ctx: TenantContext = Depends(require_context),
) -> dict[str, Any]:
    """Resend issues a NEW link and retires the old one.

    It cannot do otherwise: only the digest of the original token was stored,
    so the original link is unrecoverable by design. Anyone holding the old
    email finds a dead link, which is the correct outcome for a credential
    that has just been reissued.
    """
    require_role(ctx, Role.BROKER_OWNER, Role.PLATFORM_ADMIN)
    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            "SELECT email, invited_role FROM brokerage_invitations "
            " WHERE id = $1::uuid AND tenant_id = $2::uuid "
            "   AND consumed_at IS NULL AND revoked_at IS NULL",
            invitation_id, ctx.tenant_id,
        )
        if not row:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No live invitation with that id.")
        result = await create_invitations(conn, ctx, [row["email"]], row["invited_role"])
        brokerage = result.pop("_brokerage")
        expires_at = result.pop("_expires_at")
        deliveries = result.pop("_deliveries")

    dev_links = {}
    for email, raw_token in deliveries:
        link = await _send_invitation(
            email=email, brokerage=brokerage, inviter=ctx.agent_id,
            raw_token=raw_token, expires_at=expires_at,
        )
        if link:
            dev_links[email] = link
    if dev_links:
        result["dev_links"] = dev_links
    return result


@router.delete("/invitations/{invitation_id}", status_code=status.HTTP_200_OK)
async def revoke_invitation(
    invitation_id: str,
    ctx: TenantContext = Depends(require_context),
) -> dict[str, Any]:
    require_role(ctx, Role.BROKER_OWNER, Role.PLATFORM_ADMIN)
    async with tenant_tx(ctx) as conn:
        # The tenant predicate is redundant under RLS and kept anyway: if the
        # policy is ever loosened, this statement should still not reach
        # another brokerage's row.
        row = await conn.fetchrow(
            "UPDATE brokerage_invitations "
            "   SET revoked_at = now(), revoked_by_agent_id = $3 "
            " WHERE id = $1::uuid AND tenant_id = $2::uuid "
            "   AND consumed_at IS NULL AND revoked_at IS NULL "
            f"RETURNING {_INVITE_COLUMNS}",
            invitation_id, ctx.tenant_id, ctx.agent_id,
        )
        if not row:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                "No live invitation with that id.",
            )
        return {"invitation": _invite_row(row)}


@router.put("/setup/progress")
async def set_progress(
    body: SetupProgressUpdate,
    ctx: TenantContext = Depends(require_context),
) -> dict[str, Any]:
    """Record intent for the capabilities that have no live source yet.

    Only the two without a live source are accepted — the model rejects
    'phone', 'billing', 'mls' and the rest at the schema level, because letting
    an operator hand-write "phone: READY" would produce a setup screen that
    disagrees with the call path, and "mls: READY" would let a brokerage
    believe a reference dataset was live inventory.
    """
    require_role(ctx, Role.BROKER_OWNER, Role.PLATFORM_ADMIN)
    async with tenant_tx(ctx) as conn:
        import json
        await conn.execute(
            "INSERT INTO brokerage_setup_progress "
            " (tenant_id, capability, status, detail, updated_by_agent_id) "
            " VALUES ($1::uuid, $2, $3, $4::jsonb, $5) "
            " ON CONFLICT (tenant_id, capability) DO UPDATE "
            "   SET status = EXCLUDED.status, detail = EXCLUDED.detail, "
            "       updated_by_agent_id = EXCLUDED.updated_by_agent_id",
            ctx.tenant_id, body.capability, body.status,
            json.dumps(body.detail), ctx.agent_id,
        )
        return await compute_setup_state(conn, ctx)
