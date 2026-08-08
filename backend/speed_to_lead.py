"""
Speed-to-lead — the first-response automation and its latency ledger.

Why this module exists
----------------------
Migration 0061 built signed lead intake and deterministic routing, and routing
*ends* at "assigned to an agent". Nothing then contacts the lead. The market
research in the vault (``Research/Deep/2026-08-06 — proptech-revenue-patterns``)
found that first-response latency is the only conversion lever in this market
with a measured effect attached to it, and that every component needed to act on
it already exists here: the command staging path (``commands_api.create_command``),
the SMS/voice providers, and the TCPA/BIPA gate in ``outreach_compliance``.
Only the trigger was missing. This is that trigger.

Two design commitments
----------------------
**Staged, not sent.** The job creates a command in ``awaiting_approval`` — the
same state Smart Plans use. Every major CRM in the category ships human-in-the-loop
review as the default and autonomous send as an opt-in add-on; matching that is
both the market norm and the defensible posture when the message is going to a
consumer under TCPA. Turning this into autonomous send is a product decision with
legal weight, not a constant to flip in here.

**A block is an outcome, not an error.** When ``guard_outreach`` denies the
attempt (calling window closed, prior opt-out, missing consent) the ledger
records ``disposition='blocked'`` with the reason and the job *succeeds*. A
retry loop against a compliance denial would be both useless and abusive, and
dropping the row entirely would make the latency metric survivorship-biased —
it would report only the leads we were allowed to contact.

The feature is off unless ``ORACLE_FEATURE_SPEED_TO_LEAD`` is truthy, matching
every other outbound-capable subsystem in this codebase.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from automation_jobs import ActionRisk, enqueue_job, register_handler
from billing_usage import record_usage
from db.connection import tenant_tx
from outreach_compliance import Channel, VoiceMode, guard_outreach
from platform_policy import Feature, feature_enabled
from tenancy import Role, TenantContext

logger = logging.getLogger("oracle.speed_to_lead")

JOB_TYPE = "crm:speed_to_lead"

# Priority is an ASC sort in the job poller, so a low number wins. Speed-to-lead
# is the one job type whose entire value is its latency: if it queues behind a
# batch of client-AI reconciles (priority 45) the feature has already failed.
JOB_PRIORITY = 5

# One attempt, then stop. Retrying a first-response send is close to meaningless
# — by the time a retry lands the "first response" window has closed — and a
# retry against a compliance block would be an unwanted repeat contact.
JOB_MAX_ATTEMPTS = 1

# Fall back to SMS when the contact expressed no preference: it is the channel
# with the fastest median human response and the one the sub-90-second finding
# was measured on. 'none'/unknown lands here too.
_DEFAULT_CHANNEL = "sms"
_CHANNEL_ALIASES = {"text": "sms", "phone": "voice", "call": "voice"}


def _enabled() -> bool:
    # default=False: an outbound automation that turns itself on during a
    # deploy because nobody set an env var is exactly the failure mode
    # COUNTY_HARVEST_ENABLED and SPATIAL_ALLOW_WEB_SCRAPE were gated to prevent.
    return feature_enabled(Feature.SPEED_TO_LEAD, default=False)


def _resolve_channel(contact: dict[str, Any]) -> Optional[str]:
    """Pick the outreach channel from the contact's stated preference, degrading
    to whatever contact detail actually exists. Returns None when there is
    nothing to reach the lead on."""
    preferred = str(contact.get("preferred_channel") or "").strip().lower()
    preferred = _CHANNEL_ALIASES.get(preferred, preferred)
    has_phone = bool(contact.get("phone"))
    has_email = bool(contact.get("email"))

    if preferred in {"sms", "voice"} and has_phone:
        return preferred
    if preferred == "email" and has_email:
        return preferred
    # Stated preference is unusable (no matching contact detail) — fall through
    # to what we can actually reach, rather than staging a message to nowhere.
    if has_phone:
        return _DEFAULT_CHANNEL
    if has_email:
        return "email"
    return None


def _opening_draft(contact: dict[str, Any], channel: str, agent_name: str) -> dict[str, Any]:
    """The first-touch draft an agent reviews before it goes out.

    Intentionally plain and short. This is a first contact with a consumer who
    submitted an inquiry seconds ago; anything that reads as a marketing blast
    invites the STOP that ends the relationship. The agent edits before sending.
    """
    first_name = str(contact.get("full_name") or "").strip().split(" ")[0] or "there"
    if channel == "email":
        return {
            "subject": "Following up on your enquiry",
            "body": (
                f"Hi {first_name},\n\n"
                f"Thanks for reaching out — this is {agent_name}. I saw your enquiry "
                "come through and wanted to get you a real answer quickly.\n\n"
                "What's the best number and time to reach you?\n\n"
                f"— {agent_name}"
            ),
        }
    if channel == "voice":
        return {
            "script": (
                f"Hi, is this {first_name}? This is {agent_name} — you just enquired "
                "about a property and I wanted to reach you straight away rather than "
                "leave you waiting. Do you have two minutes?"
            )
        }
    return {
        "body": (
            f"Hi {first_name}, this is {agent_name} — thanks for your enquiry. "
            "Happy to answer questions or set up a viewing. What works for you?"
        )
    }


async def _record_response_event(
    ctx: TenantContext,
    *,
    conn: Any = None,
    intake_event_id: Optional[str],
    contact_id: Optional[str],
    client_id: Optional[str],
    lead_id: Optional[str],
    channel: Optional[str],
    disposition: str,
    blocked_reason: Optional[str] = None,
    command_id: Optional[str] = None,
    lead_created_at: datetime,
    origin: str = "speed_to_lead",
) -> None:
    """Append to the first-response ledger.

    ON CONFLICT DO NOTHING against the partial unique indexes from 0067: the
    metric is time-to-FIRST-response, so a later attempt must never overwrite
    the first one. Ledger failure is logged, never raised — a metric that can
    roll back the outreach it measures has the dependency backwards.
    """
    sql = """
        INSERT INTO lead_response_events (
            tenant_id,intake_event_id,contact_id,client_id,lead_id,
            origin,channel,disposition,blocked_reason,command_id,lead_created_at
        ) VALUES ($1::uuid,$2::uuid,$3::uuid,$4::uuid,$5::uuid,$6,$7,$8,$9,$10::uuid,$11)
        ON CONFLICT DO NOTHING
    """
    args = (
        ctx.tenant_id, intake_event_id, contact_id, client_id, lead_id,
        origin, channel, disposition, (blocked_reason or None), command_id,
        lead_created_at,
    )
    try:
        if conn is not None:
            await conn.execute(sql, *args)
        else:
            async with tenant_tx(ctx) as c:
                await c.execute(sql, *args)
    except Exception:  # noqa: BLE001 — see docstring
        logger.warning(
            "First-response ledger write failed: intake=%s contact=%s disposition=%s",
            intake_event_id, contact_id, disposition, exc_info=True,
        )


async def enqueue_speed_to_lead(
    ctx: TenantContext,
    *,
    contact_id: Optional[str] = None,
    client_id: Optional[str] = None,
    intake_event_id: Optional[str] = None,
    lead_id: Optional[str] = None,
    state_code: Optional[str] = None,
    lead_created_at: Optional[datetime] = None,
    reason: str = "lead_received",
) -> dict[str, Any]:
    """Queue a first-response attempt. Never raises into the caller's path.

    Callers are lead-creation handlers whose own success must not depend on this
    — a lead that was captured but not auto-contacted is a degraded outcome; a
    lead that was rejected because the automation failed is a lost one.
    """
    if not _enabled():
        return {"state": "disabled", "created": False}
    if not (contact_id or client_id):
        return {"state": "skipped", "created": False, "reason": "no_contact_anchor"}

    anchor = str(contact_id or client_id)
    try:
        uuid.UUID(anchor)
    except (ValueError, AttributeError, TypeError):
        logger.warning("Speed-to-lead skipped: malformed contact anchor %r", anchor)
        return {"state": "skipped", "created": False, "reason": "bad_anchor"}

    created_at = lead_created_at or datetime.now(timezone.utc)
    try:
        job, created = await enqueue_job(
            ctx,
            job_type=JOB_TYPE,
            payload={
                "tenant_id": str(ctx.tenant_id),
                "contact_id": str(contact_id) if contact_id else None,
                "client_id": str(client_id) if client_id else None,
                "intake_event_id": str(intake_event_id) if intake_event_id else None,
                "lead_id": str(lead_id) if lead_id else None,
                "state_code": state_code,
                "lead_created_at": created_at.isoformat(),
                "reason": str(reason)[:120],
            },
            # Keyed on the anchor with NO time bucket, unlike client-AI reconcile.
            # A lead gets exactly one first response, ever; a second enqueue for
            # the same lead is a duplicate to swallow, not a burst to coalesce.
            idempotency_key=f"speed-to-lead:{intake_event_id or anchor}",
            created_by=ctx.agent_id,
            priority=JOB_PRIORITY,
            max_attempts=JOB_MAX_ATTEMPTS,
            # No scheduled_at — "as soon as a worker frees up" is the whole point.
            risk=ActionRisk.INTERNAL_EDIT,
        )
        return {"state": "queued", "created": created, "job_id": str(job["id"])}
    except Exception:  # noqa: BLE001 — see docstring
        logger.warning(
            "Speed-to-lead enqueue deferred: contact=%s intake=%s",
            contact_id, intake_event_id, exc_info=True,
        )
        return {"state": "deferred", "created": False}


async def _speed_to_lead_job(payload: dict[str, Any], reporter: Any) -> dict[str, Any]:
    """Resolve the contact, clear the compliance gate, stage the opening touch."""
    tenant_id = str(reporter.job["tenant_id"])
    worker_ctx = TenantContext(
        agent_id="speed-to-lead-worker", tenant_id=tenant_id, role=Role.PLATFORM_ADMIN
    )
    contact_id = payload.get("contact_id")
    client_id = payload.get("client_id")
    intake_event_id = payload.get("intake_event_id")
    lead_id = payload.get("lead_id")
    lead_created_at = datetime.fromisoformat(payload["lead_created_at"])
    if lead_created_at.tzinfo is None:
        lead_created_at = lead_created_at.replace(tzinfo=timezone.utc)

    ledger = dict(
        intake_event_id=intake_event_id, contact_id=contact_id,
        client_id=client_id, lead_id=lead_id, lead_created_at=lead_created_at,
    )

    if not _enabled():
        # The flag can flip between enqueue and execution. Recording the skip
        # keeps the ledger honest about why a lead was never contacted.
        await _record_response_event(
            worker_ctx, channel=None, disposition="skipped", **ledger
        )
        return {"state": "disabled"}

    # Imported here, not at module scope: contacts_api and commands_api both sit
    # above this module in the import graph, and a top-level import would make
    # the cycle real.
    from contacts_api import _CONTACT_SELECT, _contact_json
    from commands_api import CommandCreate, CommandType, create_command

    await reporter.progress(10, "Resolving lead contact")
    async with tenant_tx(worker_ctx) as conn:
        if contact_id:
            row = await conn.fetchrow(
                _CONTACT_SELECT + " WHERE contact.id=$1::uuid AND contact.deleted_at IS NULL",
                str(contact_id),
            )
        else:
            row = await conn.fetchrow(
                _CONTACT_SELECT + " WHERE contact.legacy_client_id=$1::uuid AND contact.deleted_at IS NULL",
                str(client_id),
            )
        if row is None:
            await _record_response_event(
                worker_ctx, conn=conn, channel=None, disposition="skipped", **ledger
            )
            return {"state": "skipped", "reason": "contact_not_found"}
        contact = await _contact_json(conn, worker_ctx, row)
        agent_name = str(contact.get("assigned_agent_id") or "your agent")

    channel = _resolve_channel(contact)
    if channel is None:
        await _record_response_event(
            worker_ctx, channel=None, disposition="skipped", **ledger
        )
        return {"state": "skipped", "reason": "no_reachable_channel"}

    destination = contact["email"] if channel == "email" else contact["phone"]
    state_code = payload.get("state_code") or contact.get("state_code")

    await reporter.progress(40, "Checking outreach compliance")
    decision = await guard_outreach(
        worker_ctx,
        channel=Channel(channel),
        contact=destination,
        state_code=state_code,
        tz_name=contact.get("timezone"),
        # An AI-staged opening touch that a human reviews and sends is still an
        # AI-authored contact; VoiceMode.AI keeps the artificial-voice disclosure
        # attached to the draft rather than quietly dropping it. recording stays
        # off — this stages a message, it does not open a recorded call.
        voice_mode=VoiceMode.AI,
        recording_enabled=False,
    )
    if not decision.allowed:
        reason = decision.blockers[0] if decision.blockers else "blocked_by_compliance"
        await _record_response_event(
            worker_ctx, channel=channel, disposition="blocked",
            blocked_reason=reason[:500], **ledger,
        )
        await reporter.progress(100, f"First response withheld: {reason}")
        # Deliberately a success. A compliance denial is a correct outcome, and
        # failing the job would retry it or page someone about lawful behaviour.
        return {"state": "blocked", "reason": reason, "channel": channel}

    await reporter.progress(70, "Staging first-response draft")
    actor_ctx = TenantContext(
        agent_id=str(contact.get("assigned_agent_id") or worker_ctx.agent_id),
        tenant_id=tenant_id,
        role=Role.AGENT,
    )
    draft = _opening_draft(contact, channel, agent_name)
    if decision.required_disclosures:
        # Obligations the gate returned ride WITH the draft. An agent approving
        # a send must see what has to be said, not discover it in an audit.
        draft["required_disclosures"] = list(decision.required_disclosures)

    command_type = {
        "sms": CommandType.SMS, "email": CommandType.EMAIL, "voice": CommandType.CALL,
    }[channel]
    target = {
        "contact_id": contact["id"],
        "client_id": contact.get("legacy_client_id"),
        "state_code": state_code,
        "timezone": contact.get("timezone"),
    }
    target["email" if channel == "email" else "phone"] = destination

    try:
        staged = await create_command(
            CommandCreate(
                command_type=command_type,
                target=target,
                draft=draft,
                idempotency_key=f"speed-to-lead:{intake_event_id or contact['id']}",
                context={
                    "source": "speed-to-lead",
                    "intake_event_id": intake_event_id,
                    "contact_id": contact["id"],
                    "reason": payload.get("reason"),
                },
            ),
            actor_ctx,
        )
    except Exception as exc:  # noqa: BLE001 — recorded, then surfaced as a failure
        await _record_response_event(
            worker_ctx, channel=channel, disposition="failed", **ledger
        )
        logger.warning("Speed-to-lead staging failed: %s", exc, exc_info=True)
        return {"state": "failed", "error": (str(exc).strip() or exc.__class__.__name__)[:500]}

    command_id = str(staged["command"]["id"])
    await _record_response_event(
        worker_ctx, channel=channel, disposition="staged",
        command_id=command_id, **ledger,
    )

    # Meter the engagement, not the arrival. A lead we were blocked from
    # contacting consumed no billable work, so metering on intake would charge
    # for leads the compliance gate correctly refused to touch.
    await record_usage(
        worker_ctx,
        metric="lead_engaged",
        quantity=1,
        idempotency_key=f"lead-engaged:{intake_event_id or contact['id']}",
    )

    latency = (datetime.now(timezone.utc) - lead_created_at).total_seconds()
    logger.info(
        "Speed-to-lead staged: contact=%s channel=%s latency=%.1fs command=%s",
        contact["id"], channel, latency, command_id,
    )
    await reporter.progress(100, f"First response staged in {latency:.0f}s")
    return {
        "state": "staged",
        "channel": channel,
        "command_id": command_id,
        "latency_seconds": round(latency, 1),
        "requires_approval": True,
    }


register_handler(JOB_TYPE, _speed_to_lead_job)
