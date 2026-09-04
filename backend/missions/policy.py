"""Whether a mission may perform one specific action, right now.

This is the gate between a plan and a person's phone. It answers in one shape
— `Verdict` — and it never sends anything itself.

The order matters and is deliberate:

    1. mission constraints   — is this mission even running, and in time?
    2. budget                — its own, and the tenant's monthly ceiling
    3. approval vs autonomy  — see below; this is the subtle one
    4. guard_outreach        — the compliance gate every send already passes
    5. outbound_ready        — is there a credential to send WITH?

**Step 3 is where the design lives.** Every outbound action requires an
approval row, always: `requires_approval` is True for anything that reaches a
person, and nothing here changes that. What the mission's grant decides is
whether the mission may RELEASE its own approval without a human clicking it.

So `autonomy.may_act()` is consulted and its answer recorded — this is that
function's first real caller — but the standing dial is not the release
authority for a mission, and the verdict says which authority applied. The
dial pins outbound to 'assist' at the database and will answer False; the
mission's consented grant is what permits release, for that mission's own
actions only, on the channels the operator ticked, revocably.

Step 5 last, on purpose: everything above is computed even when there is no
credential, so shadow mode and a dormant deployment exercise the identical
path and record the same decisions. The only difference is that the last step
withholds. That is why `would_have_done` can be trusted.

What step 5 is NOT is the off switch. See `outbound_ready`: a machine with no
credential ROWS can still have Twilio in its environment, and the local stack
does. Dormancy is Feature.MISSIONS, default off, enforced by the executor.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from tenancy import TenantContext

logger = logging.getLogger(__name__)

#: Categories on the standing dial, per channel. Used only to ask may_act()
#: the right question and to record its answer.
CHANNEL_CATEGORY = {"email": "emails", "sms": "texts", "voice": "calls", "task": "crm_hygiene"}

#: Which providers can carry which channel. A mission is "ready" on a channel
#: when a live credential exists for any of them, or the environment carries
#: the equivalent configuration.
CHANNEL_PROVIDERS = {
    "email": ("gmail", "microsoft", "sendgrid"),
    "sms": ("twilio",),
    "voice": ("twilio",),
    "task": (),   # a task is internal; it needs nobody's permission to exist
}

CHANNEL_ENV = {
    "email": ("ORACLE_SMTP_HOST", "SENDGRID_API_KEY"),
    "sms": ("TWILIO_ACCOUNT_SID",),
    "voice": ("TWILIO_ACCOUNT_SID",),
    "task": (),
}


@dataclass(frozen=True)
class Verdict:
    """One answer, with every reason that produced it.

    `may_stage` and `may_release` are separate because they are separate
    questions: whether to prepare the action at all, and whether it may go
    without a person. A verdict that permits staging but not release is the
    normal, healthy case — it puts the action in the approval queue.
    """

    may_stage: bool
    may_release: bool
    reason: str
    #: Set when the action must not proceed. Written to
    #: mission_actions.blocked_reason, which the schema requires.
    blocked_reason: Optional[str] = None
    #: What the standing dial said, recorded even though it is not the release
    #: authority here, so an audit can see it was asked.
    dial_allowed: bool = False
    dial_reason: str = ""
    #: Which authority permitted release, when one did.
    release_authority: str = ""
    missing: tuple[str, ...] = ()
    disclosures: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "may_stage": self.may_stage,
            "may_release": self.may_release,
            "reason": self.reason,
            "blocked_reason": self.blocked_reason,
            "dial_allowed": self.dial_allowed,
            "dial_reason": self.dial_reason,
            "release_authority": self.release_authority,
            "missing": list(self.missing),
            "disclosures": list(self.disclosures),
        }


def _withheld(reason: str, **extra: Any) -> Verdict:
    return Verdict(may_stage=False, may_release=False, reason=reason,
                   blocked_reason=reason, **extra)


async def outbound_ready(
    ctx: TenantContext, channels: list[str],
) -> tuple[bool, list[str]]:
    """Whether real sending is possible, and what is missing if not.

    This answers honestly, which means it can answer YES on a machine nobody
    expected it to. Measured on the local stack: zero provider_credentials
    rows, but TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN are set in the
    environment, so sms and voice both report ready. The plan for this feature
    assumed "no credential rows, therefore dormant, therefore no feature flag
    needed" — that assumption is false, and the env fallback is why.

    The fallback stays, because the existing send paths really do use
    env-configured Twilio: reporting "cannot send" while the deployment can
    would be the more dangerous lie. What changes is that dormancy is NOT a
    property of this function. It is Feature.MISSIONS, default off, checked by
    the executor. This function only says what is possible.
    """
    from db.connection import tenant_tx

    wanted = [c for c in channels if c in CHANNEL_PROVIDERS]
    if not wanted:
        return True, []

    providers_needed = {p for c in wanted for p in CHANNEL_PROVIDERS[c]}
    live: set[str] = set()
    if providers_needed:
        async with tenant_tx(ctx) as conn:
            # No tenant predicate: RLS scopes this, and duplicating half the
            # policy would hide rows from a platform admin.
            rows = await conn.fetch(
                """SELECT DISTINCT provider FROM provider_credentials
                    WHERE disabled_at IS NULL
                      AND (expires_at IS NULL OR expires_at > now())
                      AND provider = ANY($1::text[])""",
                sorted(providers_needed),
            )
        live = {r["provider"] for r in rows}

    missing: list[str] = []
    for channel in wanted:
        if _channel_ready(channel, live):
            continue
        options = list(CHANNEL_PROVIDERS[channel]) + [
            f"${name}" for name in CHANNEL_ENV.get(channel, ())
        ]
        missing.append(f"{channel} (needs one of: {', '.join(options)})")
    return not missing, missing


def _channel_ready(channel: str, live: set[str]) -> bool:
    if not CHANNEL_PROVIDERS[channel] and not CHANNEL_ENV.get(channel):
        return True   # internal-only channel
    if any(p in live for p in CHANNEL_PROVIDERS[channel]):
        return True
    return any(os.getenv(name) for name in CHANNEL_ENV.get(channel, ()))


async def spend_so_far(ctx: TenantContext, mission_id: str) -> int:
    """Cents already committed by this mission. Counts staged and executed
    actions — an action waiting in the approval queue has reserved its cost."""
    from db.connection import tenant_tx

    async with tenant_tx(ctx) as conn:
        total = await conn.fetchval(
            """SELECT coalesce(sum(cost_cents), 0)::int FROM mission_actions
                WHERE mission_id = $1::uuid
                  AND state IN ('staged', 'approved', 'executed')""",
            mission_id,
        )
    return int(total or 0)


async def monthly_spend(ctx: TenantContext) -> tuple[int, int]:
    """(spent this month, tenant cap). A cap of 0 means no ceiling is set."""
    from db.connection import tenant_tx

    async with tenant_tx(ctx) as conn:
        spent = await conn.fetchval(
            """SELECT coalesce(sum(cost_cents), 0)::int FROM mission_actions
                WHERE state IN ('staged', 'approved', 'executed')
                  AND created_at >= date_trunc('month', now())""",
        )
        cap = await conn.fetchval(
            "SELECT monthly_cap_cents FROM tenant_action_budgets LIMIT 1",
        )
    return int(spent or 0), int(cap or 0)


async def evaluate_action(
    ctx: TenantContext,
    mission: dict[str, Any],
    action: dict[str, Any],
    *,
    contact: Optional[str] = None,
    state_code: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Verdict:
    """The whole gate, in order. Never sends; only decides."""
    import autonomy
    from missions import costs

    now = now or datetime.now(timezone.utc)
    channel = action.get("channel") or "task"

    # 1. Is this mission running at all?
    status = mission.get("status")
    if status not in ("shadow", "active"):
        return _withheld(f"the mission is {status}, not running")
    if channel not in (mission.get("allowed_channels") or []):
        return _withheld(f"{channel} is not a channel this mission may use")
    deadline = mission.get("deadline")
    if deadline and _as_datetime(deadline) and _as_datetime(deadline) < now:
        return _withheld("the mission's deadline has passed")

    # 2. Budget: the mission's own, then the tenant's ceiling above it.
    cost = costs.unit_cost_cents(channel)
    budget = int(mission.get("budget_cents") or 0)
    if budget:
        spent = await spend_so_far(ctx, str(mission["id"]))
        if spent + cost > budget:
            return _withheld(
                f"this mission's budget is spent ({spent} of {budget} cents)")
    spent_month, cap = await monthly_spend(ctx)
    if cap and spent_month + cost > cap:
        return _withheld(
            f"the brokerage's monthly action budget is spent "
            f"({spent_month} of {cap} cents)")

    # 3. Approval always; release only under an authority that says so.
    #    The dial is asked and recorded even though it is not the authority
    #    for a mission — so an audit can see what it said.
    category = CHANNEL_CATEGORY.get(channel, "crm_hygiene")
    reversible = channel == "task"
    dial_allowed, dial_reason = await autonomy.may_act(
        ctx, category, reversible=reversible)

    granted = channel in (mission.get("auto_channels") or [])
    consented = bool(mission.get("consent_at"))
    if granted and consented:
        authority = f"mission grant, consented {mission.get('consent_at')}"
        may_release = True
    elif dial_allowed:
        authority = "standing autonomy dial"
        may_release = True
    else:
        authority = ""
        may_release = False

    # 4. Compliance. Not negotiable by any grant: a mission may be allowed to
    #    call, and this still blocks the specific contact who never gave
    #    express written consent for an AI voice call (FCC 24-17).
    disclosures: tuple[str, ...] = ()
    if contact and channel in ("email", "sms", "voice"):
        from outreach_compliance import guard_outreach

        decision = await guard_outreach(
            ctx, channel=channel, contact=contact, state_code=state_code,
            now_utc=now, log=False,
        )
        if not decision.allowed:
            return _withheld(
                "compliance: " + "; ".join(decision.blockers or ("not permitted",)),
                dial_allowed=dial_allowed, dial_reason=dial_reason,
            )
        disclosures = decision.required_disclosures

    # 5. Is there anything to send WITH? Last, so everything above is computed
    #    and recorded even on a deployment that can send nothing.
    ready, missing = await outbound_ready(ctx, [channel])
    if not ready:
        return Verdict(
            may_stage=False, may_release=False,
            reason="no outbound credential is configured",
            blocked_reason=f"no credential for {channel}: {'; '.join(missing)}",
            dial_allowed=dial_allowed, dial_reason=dial_reason,
            release_authority=authority, missing=tuple(missing),
            disclosures=disclosures,
        )

    if mission.get("mode") != "live":
        return Verdict(
            may_stage=False, may_release=False,
            reason="shadow mode",
            blocked_reason="shadow mode: recorded, not sent",
            dial_allowed=dial_allowed, dial_reason=dial_reason,
            release_authority=authority, disclosures=disclosures,
        )

    return Verdict(
        may_stage=True,
        may_release=may_release,
        reason=(
            f"released by {authority}" if may_release
            else "staged for approval — no grant covers this channel"
        ),
        dial_allowed=dial_allowed,
        dial_reason=dial_reason,
        release_authority=authority,
        disclosures=disclosures,
    )


def _as_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None
