"""A brokerage's monthly budget for hosted model tokens (prompt 18, T18.07;
prompt 07, T07.07; migration 0170).

One channel, end to end: the hosted model calls a Neoh chat turn makes. A turn
could make up to seven of them (six tool rounds and a forced answer, each
carrying up to ~40k characters of history, record and persona) plus its
semantic-frame call, and nothing capped a brokerage's total. Admission limits
are per agent and count messages, not spend.

The unit is tokens as the provider reports them (prompt + completion), not
money: there is no per-token price table and prices are an owner decision
(D-2026-10-12-02). The period is the calendar month in UTC. **No cap is the
default** — a brokerage is limited only once an owner or platform admin sets
one; no budget number ships in code.

    used      billing_usage_events ai_prompt_tokens + ai_completion_tokens this
              month (the 0084 ledger, written by settle() for every call)
    reserved  ai_token_reservations not yet lapsed: estimates of calls in flight

reserve() runs just before every hosted call (llm_gateway.spend_scope): it
refuses — before anything is sent — when the platform is halted, the brokerage
paused, or used + reserved + this call's estimate would pass the cap
("exhausted" at the cap, "insufficient" while some room is left but less than
this call needs).
Reservations of one brokerage serialise on an advisory lock and both sums are
read in one statement, so the cap holds at admission. What can still overshoot
is a call whose reported usage beat its estimate, or one whose settle failed;
both are stated limits, not hidden.

An unreadable budget holds the call (SpendUnavailable, retryable) — never a
pass. A hold that is never settled lapses after HOLD_SECONDS, so nothing here
can block a brokerage for good. A call already sent to a provider is not
cancelled by any switch.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Optional

import billing_usage
from db.connection import tenant_tx
from llm_gateway import SpendRefused, SpendUnavailable
from tenancy import Role, TenantContext

logger = logging.getLogger("oracle.ai_budget")

CHANNEL = "ai_tokens"
#: How long a call's estimate is held without a settle. A turn has a 90 s
#: deadline and one gateway call at most 120 s. A call made in a worker thread
#: (Foundry, Bedrock) is held until its thread returns, past the turn's
#: deadline. A Foundry request is bounded below this, with no retry
#: (ai_chat_agent._FOUNDRY_REQUEST_SECONDS); a Bedrock call that outlives it
#: lapses first and is still metered when it returns. Otherwise only a dead
#: worker's hold lives this long.
HOLD_SECONDS = 600

CODES = {
    "exhausted": "AI_BUDGET_EXHAUSTED",
    "insufficient": "AI_BUDGET_INSUFFICIENT",
    "paused": "AI_SPEND_PAUSED",
    "platform_halted": "AI_SPEND_HALTED",
    "unknown": "AI_BUDGET_UNAVAILABLE",
}

#: What "used" is, and what it is not — returned beside the number.
COUNTS = ("Hosted model calls made by Neoh chat turns (including each turn's request "
          "parsing), in tokens as the provider reported them.")
NOT_COUNTED = (
    "local-model calls",
    "calls whose provider reported no usage",
    "calls cut off before they answered (a provider timeout, or a request parse past its time limit)",
    "phone-call voice replies",
    "mission drafting and planning",
    "client automation, CMA, comms-draft and command-parse routes",
)

_OWNER_MESSAGES = {
    "exhausted": ("This month's Neoh AI budget for your brokerage is used up, so Neoh {stopped}. "
                  "Raise it in Settings under AI budget; it renews on the 1st (UTC)."),
    "insufficient": ("Too little of this month's Neoh AI budget for your brokerage is left for "
                     "this request, so Neoh {stopped}. Raise it in Settings "
                     "under AI budget; it renews on the 1st (UTC)."),
    "paused": "Neoh's AI is paused for your brokerage. Turn it back on in Settings under AI budget.",
}
_AGENT_MESSAGES = {
    "exhausted": ("This month's Neoh AI budget for your brokerage is used up, so Neoh {stopped}. "
                  "Ask your broker to raise it; it renews on the 1st (UTC)."),
    "insufficient": ("Too little of this month's Neoh AI budget for your brokerage is left for "
                     "this request, so Neoh {stopped}. Ask your broker to "
                     "raise it; it renews on the 1st (UTC)."),
    "paused": "Your broker has paused Neoh's AI for the brokerage. Ask them to turn it back on.",
}
_SHARED_MESSAGES = {
    "platform_halted": ("Neoh's AI is paused across the platform right now. Nothing was sent to "
                        "a model and your work is saved."),
    "unknown": ("Neoh couldn't check your brokerage's AI budget just now, so it didn't ask a "
                "model. Your work is saved — try again in a moment."),
}
#: The same, once the turn has already made a hosted call (its request parse
#: or an earlier round): that call may have reached a provider, so nothing
#: here says no model was asked.
_SHARED_AFTER_CALLS = {
    "platform_halted": ("Neoh's AI is paused across the platform right now, so Neoh stopped "
                        "before its next model call. Your work is saved."),
    "unknown": ("Neoh couldn't check your brokerage's AI budget just now, so it stopped before "
                "its next model call. Your work is saved — try again in a moment."),
}
_STOPPED = "stopped before asking a model"
_STOPPED_AFTER_CALLS = "stopped before its next model call"


def deploy_halted() -> bool:
    """The deploy-time kill switch: refuses without reading anything, so it
    works while the database does not."""
    return os.getenv("ORACLE_AI_INFERENCE_HALT", "").strip().lower() in {"1", "true", "yes", "on"}


def refusal(state: str, ctx: TenantContext, *, after_calls: bool = False) -> SpendRefused:
    """The refusal for `state`, worded for who is asking: an agent is told to
    ask their broker, an owner where to change it. `after_calls`: this turn
    has already made a hosted call, so it never says no model was asked."""
    owner = ctx.role in (Role.BROKER_OWNER, Role.PLATFORM_ADMIN)
    message = ((_SHARED_AFTER_CALLS if after_calls else _SHARED_MESSAGES).get(state)
               or (_OWNER_MESSAGES if owner else _AGENT_MESSAGES)[state].format(
                   stopped=_STOPPED_AFTER_CALLS if after_calls else _STOPPED))
    kind = SpendUnavailable if state == "unknown" else SpendRefused
    return kind(state, CODES[state], message)


def _own(ctx: TenantContext) -> TenantContext:
    """ctx as its brokerage's own context. A platform admin's context opens
    every brokerage's rows under RLS; narrowed to the owner of the tenant it
    names (as ai_chat_agent._worker_role narrows a turn), RLS alone scopes
    every read and write here, with no tenant predicate in SQL (C3.3)."""
    if ctx.is_platform_admin:
        return TenantContext(agent_id=ctx.agent_id, tenant_id=ctx.tenant_id, role=Role.BROKER_OWNER)
    return ctx


@dataclass(frozen=True)
class Hold:
    call_key: str
    tokens: int
    #: unlimited | ok | warning, as of this reservation.
    state: str
    #: Whether a reservation row exists (none is written when there is no cap).
    held: bool


# The brokerage's controls and the platform switch, in one read. The tenant
# row is the one RLS shows (tenant_action_budgets is keyed by tenant). A
# missing switch row reads as not halted: the application can neither insert
# nor delete it, so the switch cannot have been turned on.
_CONTROLS = """
    SELECT b.ai_tokens_monthly_cap AS cap,
           coalesce(b.ai_tokens_warn_pct, 80) AS warn_pct,
           coalesce(b.ai_inference_paused, false) AS paused,
           coalesce((SELECT halted FROM platform_spend_controls WHERE channel = 'ai_tokens'),
                    false) AS halted
      FROM (SELECT 1) AS one
      LEFT JOIN tenant_action_budgets b ON true
"""

# Used and held, in ONE statement: one snapshot, so a settle committing
# between two reads can never drop a call from both sums.
_TOTALS = """
    SELECT p.start_at, p.end_at, u.prompt, u.completion, h.reserved
      FROM (SELECT date_trunc('month', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC' AS start_at,
                   (date_trunc('month', now() AT TIME ZONE 'UTC') + interval '1 month')
                       AT TIME ZONE 'UTC' AS end_at) AS p
     CROSS JOIN LATERAL (
           SELECT coalesce(sum(quantity) FILTER (WHERE metric = 'ai_prompt_tokens'), 0)::bigint AS prompt,
                  coalesce(sum(quantity) FILTER (WHERE metric = 'ai_completion_tokens'), 0)::bigint
                      AS completion
             FROM billing_usage_events
            WHERE metric IN ('ai_prompt_tokens', 'ai_completion_tokens')
              AND occurred_at >= p.start_at) AS u
     CROSS JOIN LATERAL (
           SELECT coalesce(sum(tokens), 0)::bigint AS reserved
             FROM ai_token_reservations
            WHERE expires_at > now()) AS h
"""


def _gate(controls: Any) -> Optional[str]:
    if deploy_halted() or controls["halted"]:
        return "platform_halted"
    if controls["paused"]:
        return "paused"
    return None


def _level(cap: Optional[int], warn_pct: int, total: int) -> str:
    """The state with `total` tokens spent or held (a call's estimate included)."""
    if cap is None:
        return "unlimited"
    if total > cap:
        return "exhausted"
    if total * 100 >= cap * warn_pct:
        return "warning"
    return "ok"


def _standing(cap: Optional[int], warn_pct: int, total: int, floor: int = 0) -> str:
    """The state between calls: at the cap no further call can fit, and with
    less left than `floor` (the smallest request a turn makes) no turn can."""
    if cap is not None and total >= cap:
        return "exhausted"
    if cap is not None and cap - total < floor:
        return "insufficient"
    return _level(cap, warn_pct, total)


async def _reserve(ctx: TenantContext, call_key: str, estimate: int) -> tuple[Optional[str], Optional[Hold]]:
    async with tenant_tx(_own(ctx)) as conn:
        controls = await conn.fetchrow(_CONTROLS)
        gate = _gate(controls)
        if gate is not None:
            return gate, None
        cap = controls["cap"]
        if cap is None:
            # No cap: nothing to hold against. The call is still metered.
            return None, Hold(call_key=call_key, tokens=estimate, state="unlimited", held=False)
        await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                           f"ai-budget:{ctx.tenant_id}")
        totals = await conn.fetchrow(_TOTALS)
        standing = int(totals["prompt"]) + int(totals["completion"]) + int(totals["reserved"])
        total = standing + estimate
        if total > cap:
            return ("exhausted" if standing >= cap else "insufficient"), None
        await conn.execute("DELETE FROM ai_token_reservations WHERE expires_at <= now()")
        await conn.execute(
            """INSERT INTO ai_token_reservations (tenant_id, call_key, tokens, expires_at)
               VALUES ($1::uuid, $2, $3, now() + make_interval(secs => $4))
               ON CONFLICT (tenant_id, call_key) DO NOTHING""",
            ctx.tenant_id, call_key[:240], estimate, float(HOLD_SECONDS),
        )
        return None, Hold(call_key=call_key[:240], tokens=estimate,
                          state=_level(cap, int(controls["warn_pct"]), total), held=True)


async def reserve(ctx: TenantContext, call_key: str, estimate: int) -> Hold:
    """Hold `estimate` tokens for one hosted call, or refuse it before it is
    sent. Raises SpendRefused (exhausted, insufficient, paused,
    platform_halted) or SpendUnavailable when the budget cannot be read."""
    if deploy_halted():
        raise refusal("platform_halted", ctx)
    estimate = max(1, int(estimate))
    try:
        refused, hold = await _reserve(ctx, call_key, estimate)
    except Exception as exc:  # noqa: BLE001 — unreadable is held, never passed
        logger.warning("ai budget: unreadable, call held (%s)", type(exc).__name__)
        raise refusal("unknown", ctx) from exc
    if refused is not None:
        logger.info("ORACLE_METRIC ai_budget_refused tenant=%s state=%s", ctx.tenant_id, refused)
        raise refusal(refused, ctx)
    return hold


async def settle(ctx: TenantContext, hold: Hold, response: Any) -> None:
    """Record the call's reported usage and drop its hold, in one transaction.
    Never raises: the answer already came back. A transaction that fails
    leaves the hold to lapse (its estimate stands in for the usage until
    then); a usage row refused inside it is logged by record_usage and is not
    counted — a stated gap, as is a call whose provider reported no usage."""
    try:
        async with tenant_tx(_own(ctx)) as conn:
            await billing_usage.record_inference(_own(ctx), response, idempotency_key=hold.call_key, conn=conn)
            if hold.held:
                await conn.execute("DELETE FROM ai_token_reservations WHERE call_key = $1", hold.call_key)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ai budget: usage not recorded (%s); the hold lapses", type(exc).__name__)


async def release_hold(ctx: TenantContext, hold: Hold) -> None:
    """Drop the hold of a call that raised. Never raises; a hold left behind lapses."""
    if not hold.held:
        return
    try:
        async with tenant_tx(_own(ctx)) as conn:
            await conn.execute("DELETE FROM ai_token_reservations WHERE call_key = $1", hold.call_key)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ai budget: hold not released (%s); it lapses", type(exc).__name__)


async def _read(ctx: TenantContext, *, totals: bool = True) -> tuple[Any, Any]:
    async with tenant_tx(_own(ctx)) as conn:
        controls = await conn.fetchrow(_CONTROLS)
        # With no cap (the default) admission needs no sums.
        if not totals and controls["cap"] is None:
            return controls, None
        return controls, await conn.fetchrow(_TOTALS)


async def admission(ctx: TenantContext, *, floor: int = 0) -> str:
    """Before a chat turn is accepted: its budget state, or a refusal (nothing
    is persisted or queued for a refused turn). `floor` is the room this kind
    of turn needs before its first answering call — its request parse, then
    that call (ai_chat_agent.smallest_request_hold): with less left than that
    the turn cannot be answered, so it is refused here rather than after its
    request parse has been spent. The real stop is per call."""
    if deploy_halted():
        raise refusal("platform_halted", ctx)
    try:
        controls, totals = await _read(ctx, totals=False)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ai budget: unreadable at admission (%s)", type(exc).__name__)
        raise refusal("unknown", ctx) from exc
    gate = _gate(controls)
    if gate is None:
        if totals is None:
            return "unlimited"
        total = int(totals["prompt"]) + int(totals["completion"]) + int(totals["reserved"])
        level = _standing(controls["cap"], int(controls["warn_pct"]), total, floor)
        if level not in ("exhausted", "insufficient"):
            return level
        gate = level
    logger.info("ORACLE_METRIC ai_budget_refused tenant=%s state=%s at=admission", ctx.tenant_id, gate)
    raise refusal(gate, ctx)


async def state(ctx: TenantContext, *, floor: int = 0) -> dict:
    """The brokerage's budget as GET /api/billing/budgets/ai-tokens reports it,
    judged as admission() judges a turn (`floor` likewise). Raises
    SpendUnavailable when it cannot be read — never reads as unlimited."""
    try:
        controls, totals = await _read(ctx)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ai budget: unreadable for its owner (%s)", type(exc).__name__)
        raise refusal("unknown", ctx) from exc
    prompt, completion, reserved = int(totals["prompt"]), int(totals["completion"]), int(totals["reserved"])
    cap = controls["cap"]
    halted = bool(controls["halted"]) or deploy_halted()
    return {
        "channel": CHANNEL,
        "unit": "tokens",
        "period": {"start": totals["start_at"].isoformat(), "end": totals["end_at"].isoformat(),
                   "basis": "calendar month, UTC"},
        "used": {"prompt": prompt, "completion": completion, "total": prompt + completion},
        "reserved": reserved,
        "cap": int(cap) if cap is not None else None,
        "warn_pct": int(controls["warn_pct"]),
        "paused": bool(controls["paused"]),
        "platform_halted": halted,
        "state": _gate(controls) or _standing(cap, int(controls["warn_pct"]),
                                              prompt + completion + reserved, floor),
        # The room the smallest turn needs through its first answering call
        # (its request parse included): an estimate.
        "smallest_request_estimate": floor,
        "counts": COUNTS,
        "not_counted": list(NOT_COUNTED),
        "evidence_status": "observed",
    }


async def settings(ctx: TenantContext) -> dict:
    """The three values an owner sets, as stored (defaults when no row)."""
    async with tenant_tx(_own(ctx)) as conn:
        row = await conn.fetchrow(_CONTROLS)
    return {"monthly_cap_tokens": int(row["cap"]) if row["cap"] is not None else None,
            "warn_pct": int(row["warn_pct"]), "paused": bool(row["paused"])}


async def set_budget(ctx: TenantContext, *, cap: Optional[int], warn_pct: int, paused: bool) -> None:
    """Write the brokerage's AI token budget. The route admits a broker owner or
    platform admin; the 0170 trigger refuses anyone else's connection (42501).
    The missions cap on the same row is left as it is."""
    async with tenant_tx(_own(ctx)) as conn:
        await conn.execute(
            """INSERT INTO tenant_action_budgets
                   (tenant_id, ai_tokens_monthly_cap, ai_tokens_warn_pct, ai_inference_paused,
                    updated_at, updated_by)
               VALUES ($1::uuid, $2, $3, $4, now(), $5)
               ON CONFLICT (tenant_id) DO UPDATE
                  SET ai_tokens_monthly_cap = EXCLUDED.ai_tokens_monthly_cap,
                      ai_tokens_warn_pct = EXCLUDED.ai_tokens_warn_pct,
                      ai_inference_paused = EXCLUDED.ai_inference_paused,
                      updated_at = now(), updated_by = EXCLUDED.updated_by""",
            ctx.tenant_id, cap, warn_pct, paused, ctx.agent_id,
        )


async def platform_control(ctx: TenantContext) -> dict:
    """The platform switch, for a platform admin."""
    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            "SELECT halted, reason, updated_by, updated_at FROM platform_spend_controls WHERE channel = $1",
            CHANNEL)
    return {
        "channel": CHANNEL,
        "halted": bool(row["halted"]) if row else False,
        "reason": row["reason"] if row else None,
        "updated_by": row["updated_by"] if row else None,
        "updated_at": row["updated_at"].isoformat() if row and row["updated_at"] else None,
        # The deploy-time switch overrides this row and is changed by a deploy.
        "deploy_halted": deploy_halted(),
    }


async def set_platform_halt(ctx: TenantContext, *, halted: bool, reason: Optional[str]) -> bool:
    """Turn the platform switch on or off. Returns whether a row was changed:
    False when this connection is not the platform login (RLS lets only a
    platform admin update it), which the route reports rather than hides."""
    async with tenant_tx(ctx) as conn:
        changed = await conn.fetchval(
            """UPDATE platform_spend_controls
                  SET halted = $2, reason = $3, updated_by = $4, updated_at = now()
                WHERE channel = $1
            RETURNING true""",
            CHANNEL, halted, reason, ctx.agent_id,
        )
    return bool(changed)


class SpendScope:
    """One chat turn's spend: what ai_chat_agent sets in llm_gateway.spend_scope.

    Each lane (provider) numbers its own calls, so every call in the turn has
    its own key — chat:{assistant id}:{lane}:{n} — which is both its hold and
    its usage idempotency key. `state` is the last reservation's state, for
    the turn's AI_CHAT_COMPLETE frame. `calls_made` counts the calls admitted
    so far (each may have reached its provider): once there is one, a
    refusal no longer says that no model was asked.
    """

    def __init__(self, ctx: TenantContext, turn_key: str) -> None:
        self.ctx = ctx
        self.turn_key = turn_key
        self.state: Optional[str] = None
        self.calls_made = 0
        self._calls: dict[str, int] = {}
        self._warned = False

    async def before(self, lane: str, estimate: int) -> Hold:
        n = self._calls.get(lane, 0)
        self._calls[lane] = n + 1
        try:
            hold = await reserve(self.ctx, f"{self.turn_key}:{lane}:{n}", estimate)
        except SpendRefused as refused:
            if not self.calls_made:
                raise
            raise refusal(refused.state, self.ctx, after_calls=True) from refused
        self.calls_made += 1
        self.state = hold.state
        if hold.state == "warning" and not self._warned:
            self._warned = True
            logger.info("ORACLE_METRIC ai_budget_warning tenant=%s", self.ctx.tenant_id)
        return hold

    async def after(self, hold: Hold, response: Any) -> None:
        await settle(self.ctx, hold, response)

    async def release(self, hold: Hold) -> None:
        await release_hold(self.ctx, hold)
