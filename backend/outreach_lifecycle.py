"""One definition of where an outbound message is in its life (CONTRACT C6, audit A05).

Every outbound store spoke its own dialect: ``command_executions.state``
(``awaiting_approval`` / ``queued`` / ``succeeded``), ``email_outbox.status``
(``queued`` / ``sent``), ``sms_messages.status`` (``queued`` / ``sent`` /
``delivered``), carrier call statuses (``answered`` / ``completed``) and the
first-response ledger's ``lead_response_events.disposition``. Speed-to-lead read
"a command exists" as an engagement and metered ``lead_engaged`` the moment a
draft was STAGED, before a human approved it or any provider saw it
(``speed_to_lead.py`` at 745f2fa, :387). This module is the shared vocabulary
those stores map into, and the one place that says which stage is billable.

The stages, in order (C6):

    staged    — drafted; awaiting a human decision
    approved  — a human approved the exact content; queued for the provider
    accepted  — the provider acknowledged the submission (SMTP 250, carrier API
                2xx, call placed). NOT proof that a person received anything.
    delivered — the provider or carrier reported delivery to the recipient:
                a carrier delivery receipt for a text, an answered call. SMTP has
                no such receipt, so an email never reaches this stage today.
    responded — the recipient replied. Not tracked by this module yet.

Terminal stages that never became a delivery: failed, cancelled, blocked,
skipped. ``unknown`` is a submission whose outcome nobody knows (a crash between
provider submission and acknowledgement) — never treated as sent.

Billing: ``lead_engaged`` is metered once per lead at ``BILLABLE_ENGAGEMENT_STAGE``
and nowhere else. Pricing is a hypothesis (D-2026-10-12-02); moving the billable
stage is a pricing-contract decision, and 0133's CHECK on billing_usage_events
has to move with it.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger("oracle.outreach_lifecycle")


class Stage(str, Enum):
    STAGED = "staged"
    APPROVED = "approved"
    ACCEPTED = "accepted"
    DELIVERED = "delivered"
    RESPONDED = "responded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"
    SKIPPED = "skipped"
    UNKNOWN = "unknown"


#: The forward path. A stage may only move to a later one on this list.
PROGRESSION: tuple[Stage, ...] = (
    Stage.STAGED, Stage.APPROVED, Stage.ACCEPTED, Stage.DELIVERED, Stage.RESPONDED,
)

#: The only stage at which a lead engagement is billable (AC18.04: a billable
#: usage row references an approved AND delivered event).
BILLABLE_ENGAGEMENT_STAGE = Stage.DELIVERED


def is_billable_engagement(stage: Optional[Stage]) -> bool:
    return stage is BILLABLE_ENGAGEMENT_STAGE


# ── Store → stage maps. Each covers every value its table's CHECK allows ────

#: command_executions.state (0027). `reconciliation_required` is a submission
#: whose outcome is unknown; it must never read as sent.
COMMAND_STATE_STAGE: dict[str, Stage] = {
    "draft": Stage.STAGED,
    "awaiting_approval": Stage.STAGED,
    "approved": Stage.APPROVED,
    "queued": Stage.APPROVED,
    "executing": Stage.APPROVED,
    "succeeded": Stage.ACCEPTED,
    "failed": Stage.FAILED,
    "cancelled": Stage.CANCELLED,
    "reconciliation_required": Stage.UNKNOWN,
}

#: email_outbox.status (0012 chk_email_status). The agent pressed send on their
#: own composer email, so `queued` is already approved. `sent` is what
#: email_outbox.py says it is — the SMTP server accepted it — not delivery.
EMAIL_OUTBOX_STAGE: dict[str, Stage] = {
    "queued": Stage.APPROVED,
    "sending": Stage.APPROVED,
    "sent": Stage.ACCEPTED,
    "bounced": Stage.FAILED,
    "failed": Stage.FAILED,
    "cancelled": Stage.CANCELLED,
}

#: Outbound sms_messages.status (0108). A row is written only after the
#: provider's API accepted the message, so even `queued` is accepted.
SMS_STATUS_STAGE: dict[str, Stage] = {
    "queued": Stage.ACCEPTED,
    "sent": Stage.ACCEPTED,
    "delivered": Stage.DELIVERED,
    "failed": Stage.FAILED,
    "undelivered": Stage.FAILED,
}

#: lead_response_events.disposition (0067, widened by 0133). `sent` is the
#: pre-0133 spelling the command worker wrote at provider acceptance; it means
#: accepted and is still accepted by the CHECK so a pre-0133 worker can write it.
DISPOSITION_STAGE: dict[str, Stage] = {
    "staged": Stage.STAGED,
    "approved": Stage.APPROVED,
    "accepted": Stage.ACCEPTED,
    "sent": Stage.ACCEPTED,
    "delivered": Stage.DELIVERED,
    "cancelled": Stage.CANCELLED,
    "blocked": Stage.BLOCKED,
    "skipped": Stage.SKIPPED,
    "failed": Stage.FAILED,
}

#: The ledger dispositions that count as "reached at least accepted".
ACCEPTED_DISPOSITIONS: tuple[str, ...] = ("accepted", "sent", "delivered")


def call_status_stage(status: str, duration_seconds: Optional[int] = None) -> Stage:
    """A carrier call status → stage. Only an answer is delivery.

    Plivo reports a never-answered call as ``completed`` with zero billed time,
    so ``completed`` is delivery only with a positive duration; with no duration
    at all nobody knows, and unknown is not delivered.
    """
    value = (status or "").strip().lower()
    if value in {"in-progress", "answered"}:
        return Stage.DELIVERED
    if value == "completed":
        return Stage.DELIVERED if duration_seconds and duration_seconds > 0 else Stage.FAILED
    if value in {"busy", "no-answer", "timeout", "failed", "canceled", "cancel"}:
        return Stage.FAILED
    if value in {"ringing", "queued", "initiated"}:
        return Stage.ACCEPTED
    return Stage.UNKNOWN


def _rank(stage: Stage) -> int:
    return PROGRESSION.index(stage) if stage in PROGRESSION else -1


def _earlier_dispositions(target: Stage) -> list[str]:
    """Ledger dispositions a row may move FROM to reach ``target``."""
    if target is Stage.CANCELLED:
        return ["staged", "approved"]
    target_rank = _rank(target)
    return [
        disposition for disposition, stage in DISPOSITION_STAGE.items()
        if stage in PROGRESSION and _rank(stage) < target_rank
    ]


#: The timestamp column each forward stage stamps on lead_response_events.
_STAGE_COLUMN = {
    Stage.APPROVED: "approved_at",
    Stage.ACCEPTED: "accepted_at",
    Stage.DELIVERED: "delivered_at",
}


# One literal statement per stage (no SQL is assembled at run time). Accepted
# and delivered are claims about a provider: neither is recorded unless the
# command itself says a provider took it, so a stray or forged signal for a
# command that failed or was cancelled moves nothing.
_ADVANCE_SQL = {
    Stage.APPROVED: """
        UPDATE lead_response_events
           SET disposition = $3, approved_at = COALESCE(approved_at, $5), updated_at = now()
         WHERE tenant_id = $1::uuid AND command_id = $2::uuid
           AND disposition = ANY($4::text[])""",
    Stage.ACCEPTED: """
        UPDATE lead_response_events
           SET disposition = $3, accepted_at = COALESCE(accepted_at, $5), updated_at = now()
         WHERE tenant_id = $1::uuid AND command_id = $2::uuid
           AND disposition = ANY($4::text[])
           AND EXISTS (SELECT 1 FROM command_executions c
                        WHERE c.id = lead_response_events.command_id
                          AND c.tenant_id = lead_response_events.tenant_id
                          AND c.state = 'succeeded')""",
    Stage.DELIVERED: """
        UPDATE lead_response_events
           SET disposition = $3, delivered_at = COALESCE(delivered_at, $5), updated_at = now()
         WHERE tenant_id = $1::uuid AND command_id = $2::uuid
           AND disposition = ANY($4::text[])
           AND EXISTS (SELECT 1 FROM command_executions c
                        WHERE c.id = lead_response_events.command_id
                          AND c.tenant_id = lead_response_events.tenant_id
                          AND c.state = 'succeeded')""",
    Stage.CANCELLED: """
        UPDATE lead_response_events
           SET disposition = $3, updated_at = now()
         WHERE tenant_id = $1::uuid AND command_id = $2::uuid
           AND disposition = ANY($4::text[])""",
}


async def advance_first_response(
    conn: Any,
    *,
    tenant_id: str,
    command_id: str,
    stage: Stage,
    occurred_at: Optional[datetime] = None,
) -> bool:
    """Move the first-response row for ``command_id`` forward to ``stage``.

    Monotonic: a late or duplicate event never moves a row backwards, and a
    terminal row (blocked, skipped, failed, cancelled) never moves at all.
    At ``BILLABLE_ENGAGEMENT_STAGE`` the engagement is metered — idempotently,
    so a redelivered receipt or a retried worker cannot count it twice.

    ``conn`` is the caller's in-flight transaction. Everything here runs in a
    SAVEPOINT and never raises: the provider event that triggered it has
    already happened, and the caller's own write must stay committable.
    Returns True when the row moved.
    """
    if stage not in (Stage.APPROVED, Stage.ACCEPTED, Stage.DELIVERED, Stage.CANCELLED):
        raise ValueError(f"the first-response ledger does not record {stage.value!r}")
    at = occurred_at or datetime.now(timezone.utc)
    column = _STAGE_COLUMN.get(stage)
    moved = False
    try:
        async with conn.transaction():
            result = await conn.execute(
                _ADVANCE_SQL[stage],
                str(tenant_id), str(command_id), stage.value,
                _earlier_dispositions(stage), *((at,) if column else ()),
            )
            moved = not str(result).endswith(" 0")
    except Exception:  # noqa: BLE001 — see docstring
        logger.warning(
            "first-response stage %s not recorded for command %s", stage.value, command_id,
            exc_info=True,
        )
        return False

    if is_billable_engagement(stage):
        await meter_delivered_engagement(conn, tenant_id=tenant_id, command_id=command_id)
    return moved


async def meter_delivered_engagement(conn: Any, *, tenant_id: str, command_id: str) -> bool:
    """Meter ``lead_engaged`` for a speed-to-lead first response that was
    approved, accepted and delivered. Returns True only for a NEW billable row.

    Re-reads the durable evidence rather than trusting the caller: the command
    must be ``succeeded`` (a human approved it and the provider accepted it) and
    its ledger row must be ``delivered``. Safe to call any number of times.
    """
    from billing_usage import record_lead_engagement
    from tenancy import Role, TenantContext

    try:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT r.intake_event_id, r.contact_id, r.client_id, r.lead_id, r.delivered_at
                  FROM lead_response_events r
                  JOIN command_executions c
                    ON c.id = r.command_id AND c.tenant_id = r.tenant_id
                 WHERE r.tenant_id = $1::uuid AND r.command_id = $2::uuid
                   AND r.origin = 'speed_to_lead' AND r.disposition = 'delivered'
                   AND c.state = 'succeeded'
                """,
                str(tenant_id), str(command_id),
            )
    except Exception:  # noqa: BLE001 — metering must never fail the caller
        logger.warning("engagement evidence unreadable for command %s", command_id, exc_info=True)
        return False
    if row is None:
        return False
    anchor = row["intake_event_id"] or row["contact_id"] or row["client_id"] or row["lead_id"]
    ctx = TenantContext(
        agent_id="engagement-meter", tenant_id=str(tenant_id), role=Role.PLATFORM_ADMIN,
    )
    return await record_lead_engagement(
        ctx,
        conn=conn,
        # The pre-0133 key, unchanged: one engagement per lead, ever. A row the
        # old code wrote at the staged step is promoted, not duplicated.
        idempotency_key=f"lead-engaged:{anchor}",
        command_id=str(command_id),
        occurred_at=row["delivered_at"],
    )


async def reconcile_command_delivery(conn: Any, *, tenant_id: str, command_id: str) -> bool:
    """Apply delivery evidence that arrived BEFORE the acceptance was recorded.

    A Telnyx receipt can beat the worker's ``succeeded`` write; it then finds no
    accepted command and is dropped. Called right after acceptance, this looks
    for a delivery the message log already holds. Returns True if delivered.
    """
    try:
        async with conn.transaction():
            delivered = await conn.fetchval(
                """
                SELECT 1
                  FROM command_executions c
                  JOIN sms_messages m
                    ON m.tenant_id = c.tenant_id AND m.provider = c.provider
                   AND m.provider_message_id = c.provider_reference
                 WHERE c.tenant_id = $1::uuid AND c.id = $2::uuid
                   AND c.state = 'succeeded' AND m.direction = 'outbound'
                   AND m.status = 'delivered'
                """,
                str(tenant_id), str(command_id),
            )
    except Exception:  # noqa: BLE001
        logger.warning("delivery evidence unreadable for command %s", command_id, exc_info=True)
        return False
    if not delivered:
        return False
    await advance_first_response(
        conn, tenant_id=tenant_id, command_id=command_id, stage=Stage.DELIVERED,
    )
    return True


async def record_sms_delivered(conn: Any, *, provider: str, provider_message_id: str) -> int:
    """A carrier delivery receipt → the commands that sent that message.

    Returns how many first-response rows were advanced (0 or 1 in practice).
    Only an approved command the provider accepted (``succeeded``) can be
    delivered; a receipt for anything else is recorded on sms_messages alone.
    """
    try:
        async with conn.transaction():
            rows = await conn.fetch(
                """
                SELECT c.tenant_id, c.id
                  FROM sms_messages m
                  JOIN command_executions c
                    ON c.tenant_id = m.tenant_id AND c.provider = m.provider
                   AND c.provider_reference = m.provider_message_id
                 WHERE m.provider = $1 AND m.provider_message_id = $2
                   AND m.direction = 'outbound' AND m.status = 'delivered'
                   AND c.command_type = 'SMS' AND c.state = 'succeeded'
                """,
                provider, provider_message_id,
            )
    except Exception:  # noqa: BLE001
        logger.warning("delivery receipt not linked to a command", exc_info=True)
        return 0
    advanced = 0
    for row in rows:
        if await advance_first_response(
            conn, tenant_id=str(row["tenant_id"]), command_id=str(row["id"]),
            stage=Stage.DELIVERED,
        ):
            advanced += 1
    return advanced


async def record_call_answered(conn: Any, *, provider_call_id: str) -> int:
    """A carrier "answered" for a placed call → its command's first response."""
    try:
        async with conn.transaction():
            rows = await conn.fetch(
                """
                SELECT c.tenant_id, c.id
                  FROM live_call_sessions s
                  JOIN command_executions c
                    ON c.id = s.command_id AND c.tenant_id = s.tenant_id
                 WHERE s.provider_call_id = $1
                   AND c.command_type = 'CALL' AND c.state = 'succeeded'
                """,
                provider_call_id,
            )
    except Exception:  # noqa: BLE001
        logger.warning("call answer not linked to a command", exc_info=True)
        return 0
    advanced = 0
    for row in rows:
        if await advance_first_response(
            conn, tenant_id=str(row["tenant_id"]), command_id=str(row["id"]),
            stage=Stage.DELIVERED,
        ):
            advanced += 1
    return advanced
