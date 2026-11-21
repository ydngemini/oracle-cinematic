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

**Staging is not engagement.** The ledger row this job writes is ``staged``;
``outreach_lifecycle`` moves it to approved, accepted and delivered as the
command does, and meters ``lead_engaged`` only at delivered.

**A block is an outcome, not an error.** When ``guard_outreach`` denies the
attempt (calling window closed, prior opt-out, missing consent) the ledger
records ``disposition='blocked'`` with the reason and the job *succeeds*. A
retry loop against a compliance denial would be both useless and abusive, and
dropping the row entirely would make the latency metric survivorship-biased —
it would report only the leads we were allowed to contact.

**Only a deferral is retried.** When the gate cannot decide yet (the shared
store of pending spoken STOPs is unreachable, e.g. during a Valkey failover)
nothing is sent or recorded and the job tries again a few minutes later
(``JOB_MAX_ATTEMPTS``); every other failure ends the job on its first attempt.

The feature is off unless ``ORACLE_FEATURE_SPEED_TO_LEAD`` is truthy, matching
every other outbound-capable subsystem in this codebase.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import recovery_mode
from automation_jobs import ActionRisk, enqueue_job, is_final_attempt, register_handler
from db.connection import tenant_tx
from outreach_compliance import (
    Channel,
    OutreachDeferred,
    OutreachUndecided,
    Purpose,
    VoiceMode,
    guard_outreach,
    raise_if_deferred,
)
from platform_policy import Feature, feature_enabled
from tenancy import SPEED_TO_LEAD_REQUESTER, Role, TenantContext

logger = logging.getLogger("oracle.speed_to_lead")

JOB_TYPE = "crm:speed_to_lead"

# Priority is an ASC sort in the job poller, so a low number wins. Speed-to-lead
# is the one job type whose entire value is its latency: if it queues behind a
# batch of client-AI reconciles (priority 45) the feature has already failed.
JOB_PRIORITY = 5

# One attempt, except after a deferral. A retry after the "first response"
# window has closed is close to meaningless, and a compliance block is a
# successful job (never retried: a repeat against a denial would be an unwanted
# contact), so every failure ends the job on its first attempt
# (_speed_to_lead_job makes it terminal: SpeedToLeadAttemptFailed) — except
# OutreachDeferred. That one says the gate could not decide yet (the shared
# STOP store is unreachable, e.g. a Valkey failover): nothing was sent and
# nothing recorded, and a draft a few minutes late (it still waits for a
# human's approval) beats no first response at all. So it is retried, spaced
# by its job_retry_after_seconds (TRANSIENT_RETRY_SECONDS, 120 s): the third
# and last attempt runs about four minutes after the first. If the gate still
# cannot decide then, the job ends OutreachUndecided and the ledger records the
# lead as 'failed' (L09-11).
JOB_MAX_ATTEMPTS = 3

# Fall back to SMS when the contact expressed no preference: it is the channel
# with the fastest median human response and the one the sub-90-second finding
# was measured on. 'none'/unknown lands here too.
_DEFAULT_CHANNEL = "sms"
_CHANNEL_ALIASES = {"text": "sms", "phone": "voice", "call": "voice"}


class SpeedToLeadAttemptFailed(RuntimeError):
    """A speed-to-lead attempt that failed for any reason but a deferral:
    dead-lettered now (job_terminal), never retried (JOB_MAX_ATTEMPTS). Keeps
    the original error's code when it names one, and its class and text."""

    job_terminal = True

    def __init__(self, exc: Exception):
        detail = str(exc).strip() or repr(exc)
        super().__init__(f"First response not staged, not retried: {type(exc).__name__}: {detail}")
        self.job_error_code = str(getattr(exc, "job_error_code", None) or "JOB_HANDLER_ERROR")


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


async def _first_response_on_record(
    ctx: TenantContext,
    *,
    intake_event_id: Optional[str],
    lead_id: Optional[str],
    contact_id: Optional[str],
    client_id: Optional[str],
) -> Optional[str]:
    """The disposition of the ledger row that already answers for this lead,
    or None: the row 0067's partial unique indexes would make a new write
    collide with (ON CONFLICT DO NOTHING) — by intake event, else by lead —
    or, for a lead with neither (no unique index covers it), the row this job
    writes for the same contact and client."""
    if intake_event_id:
        where, anchors = "intake_event_id = $2::uuid", (intake_event_id,)
    elif lead_id:
        where, anchors = "lead_id = $2::uuid AND intake_event_id IS NULL", (lead_id,)
    else:
        where = ("origin = 'speed_to_lead' AND intake_event_id IS NULL AND lead_id IS NULL "
                 "AND contact_id IS NOT DISTINCT FROM $2::uuid AND client_id IS NOT DISTINCT FROM $3::uuid")
        anchors = (contact_id, client_id)
    async with tenant_tx(ctx) as conn:
        return await conn.fetchval(
            "SELECT disposition FROM lead_response_events WHERE tenant_id = $1::uuid AND "
            + where + " ORDER BY responded_at LIMIT 1",
            ctx.tenant_id, *anchors,
        )


async def _speed_to_lead_job(payload: dict[str, Any], reporter: Any) -> dict[str, Any]:
    """The registered handler: one attempt, retried only after a deferral.

    The job is enqueued with JOB_MAX_ATTEMPTS so that OutreachDeferred can be
    retried; every other exception is made terminal here, so it is
    dead-lettered on the attempt that raised it, exactly as when the job had
    one attempt. One already terminal keeps its own code (OutreachUndecided,
    billing.NotEntitled), and a recovery-mode refusal reaches the job worker
    unchanged (dead-lettered as RECOVERY_MODE_BLOCKED)."""
    try:
        return await _attempt_first_response(payload, reporter)
    except (OutreachDeferred, recovery_mode.RecoveryModeBlocked):
        raise
    except Exception as exc:
        if getattr(exc, "job_terminal", False):
            raise
        raise SpeedToLeadAttemptFailed(exc) from exc


async def _attempt_first_response(payload: dict[str, Any], reporter: Any) -> dict[str, Any]:
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

    if int(reporter.job.get("attempt_count") or 1) > 1:
        # Only a deferral is retried, and a deferral records nothing. A row
        # here means an earlier attempt finished its work and its worker
        # stopped before the job was marked done (a restart, an expired
        # lease): that outcome stands. A recorded block is never re-gated into
        # a draft, and no second draft or second row is ever staged.
        on_record = await _first_response_on_record(
            worker_ctx, intake_event_id=intake_event_id, lead_id=lead_id,
            contact_id=contact_id, client_id=client_id,
        )
        if on_record is not None:
            return {"state": "already_recorded", "disposition": on_record}

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
        # Purpose: a first touch to a new lead promotes the agent's services (26.5).
        purpose=Purpose.MARKETING,
    )
    # The gate could not decide yet (a Valkey outage hides pending spoken
    # STOPs): NOT a compliance outcome, so nothing is recorded as "blocked"
    # (that would withhold this lead's first reply for good). With attempts
    # left (JOB_MAX_ATTEMPTS) it raises OutreachDeferred: nothing is recorded
    # and the job worker retries it about two minutes later. On the last
    # attempt it raises OutreachUndecided — not sent, not retried — and the
    # ledger records the lead 'failed', so it reads as a failed first response
    # instead of vanishing from the metric. (0067's CHECK allows a reason on a
    # 'blocked' row only; the dead-lettered job keeps the cause,
    # OUTREACH_UNDECIDED.) Nothing can run a dead-lettered job again, so this
    # row never shadows a later draft (L09-11).
    try:
        raise_if_deferred(decision, final_attempt=is_final_attempt(reporter.job))
    except OutreachUndecided:
        await _record_response_event(
            worker_ctx, channel=channel, disposition="failed", **ledger
        )
        raise
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
    # Staged FOR the assigned agent when there is one: they are the requester,
    # and the message leaves only while they are an active member who may
    # still reach the contact. An unassigned (pool) contact is staged by the
    # pipeline on its own authority, under its reserved "system:" name — the
    # marker the command worker reads to judge it by the pipeline's rule
    # (pool records only) instead of membership (L09-12). Never the worker's
    # own "speed-to-lead-worker" name: that one carries no such marker.
    actor_ctx = TenantContext(
        agent_id=str(contact.get("assigned_agent_id") or SPEED_TO_LEAD_REQUESTER),
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

    # Nothing is metered here. A staged draft is not an engagement: no human
    # has approved it and no provider has seen it (audit A05 — this is where
    # lead_engaged used to be recorded). The engagement is metered once, by
    # outreach_lifecycle, when this command's message is approved, accepted
    # by the provider AND delivered (BILLABLE_ENGAGEMENT_STAGE).

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
