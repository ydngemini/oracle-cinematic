"""Durable intent for billable provider purchases (phone numbers).

Lifecycle (provider_purchases.state):
    intended  → written before the provider is asked; one in flight per agent
    confirmed → the provider returned the number's id; not yet on the route
    attached  → the route points at it (normal end state)
    failed    → the provider definitely refused; nothing was bought
    unknown   → the request may have succeeded (timeout, lost response):
                reconciliation.py looks for it at the provider and adopts or
                releases it; nothing more is bought for that agent meanwhile
    released  → given back to the provider (superseded or orphaned)

The next connect attempt reuses a `confirmed` number instead of buying again.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from db.connection import tenant_tx
from tenancy import TenantContext


class PurchaseInFlight(RuntimeError):
    """Another purchase for this agent and provider has not resolved yet."""


async def begin(ctx: TenantContext, provider: str) -> tuple[str, Optional[dict]]:
    """Return (purchase_id, reusable) — reusable is {ref, phone_number} of a
    number already bought for this agent but never attached, else None."""
    import asyncpg

    async with tenant_tx(ctx) as conn:
        row = await conn.fetchrow(
            "SELECT id, provider_ref, detail FROM provider_purchases WHERE tenant_id=$1 "
            "AND lower(agent_id)=lower($2) AND provider=$3 AND kind='phone_number' AND state='confirmed' "
            "AND NOT COALESCE((detail->>'superseded')::boolean, false) ORDER BY created_at DESC LIMIT 1",
            ctx.tenant_id, ctx.agent_id, provider)
        if row is not None:
            detail = json.loads(row["detail"]) if isinstance(row["detail"], str) else dict(row["detail"] or {})
            return str(row["id"]), {"ref": row["provider_ref"], "phone_number": detail.get("phone_number")}
        try:
            new = await conn.fetchval(
                "INSERT INTO provider_purchases (tenant_id, agent_id, provider, kind) "
                "VALUES ($1,$2,$3,'phone_number') RETURNING id", ctx.tenant_id, ctx.agent_id, provider)
        except asyncpg.exceptions.UniqueViolationError as exc:
            raise PurchaseInFlight("A number purchase is already in progress — try again in a minute.") from exc
    return str(new), None


async def _set(ctx: TenantContext, purchase_id: str, state: str, *, ref: Optional[str] = None,
               detail: Optional[dict] = None, error: Optional[str] = None) -> None:
    async with tenant_tx(ctx) as conn:
        await conn.execute(
            "UPDATE provider_purchases SET state=$2, provider_ref=COALESCE($3, provider_ref), "
            "detail = detail || $4::jsonb, error=$5, updated_at=now() WHERE id=$1",
            purchase_id, state, ref, json.dumps(detail or {}), error)


async def confirmed(ctx, purchase_id: str, ref: str, phone_number: str) -> None:
    await _set(ctx, purchase_id, "confirmed", ref=ref, detail={"phone_number": phone_number})


async def attached(ctx, purchase_id: Optional[str]) -> None:
    if purchase_id:
        await _set(ctx, purchase_id, "attached")


async def failed(ctx, purchase_id: str, error: str) -> None:
    await _set(ctx, purchase_id, "failed", error=error[:500])


async def unknown(ctx, purchase_id: str, error: str) -> None:
    await _set(ctx, purchase_id, "unknown", error=error[:500])


async def record_superseded(ctx: TenantContext, provider: str, ref: str, phone_number: Optional[str]) -> None:
    """A route switching provider leaves the old provider's number behind;
    record it so the sweep releases it rather than letting it bill forever."""
    async with tenant_tx(ctx) as conn:
        await conn.execute(
            "INSERT INTO provider_purchases (tenant_id, agent_id, provider, kind, state, provider_ref, detail) "
            "VALUES ($1,$2,$3,'phone_number','confirmed',$4,$5::jsonb) ON CONFLICT DO NOTHING",
            ctx.tenant_id, ctx.agent_id, provider, ref,
            json.dumps({"phone_number": phone_number, "superseded": True}))


async def buy(ctx: TenantContext, provider: str, purchase_fn) -> tuple[str, str, Optional[str], bool]:
    """Run one guarded purchase. Returns (inbound_did, provider_ref, purchase_id, newly_bought).

    purchase_fn() performs the provider call and returns a ProviderResult."""
    from command_providers import ProviderConfigurationError, ProviderRejectedError

    purchase_id, reuse = await begin(ctx, provider)
    if reuse and reuse.get("ref") and reuse.get("phone_number"):
        return str(reuse["phone_number"]), str(reuse["ref"]), purchase_id, True
    try:
        result = await purchase_fn()
    except (ProviderConfigurationError, ProviderRejectedError) as exc:
        await failed(ctx, purchase_id, str(exc))
        raise
    except BaseException as exc:
        await unknown(ctx, purchase_id, f"{type(exc).__name__}: {exc}"[:500])
        raise PurchaseUnconfirmed(
            "We could not confirm the number purchase. No other number will be bought "
            "until it has been checked — try again in a few minutes.") from exc
    phone = str(result.detail.get("phone_number") or "")
    await confirmed(ctx, purchase_id, result.reference, phone)
    return phone, result.reference, purchase_id, True


class PurchaseUnconfirmed(RuntimeError):
    """The provider may have sold us a number; reconciliation will find out."""


def describe_states() -> dict[str, Any]:
    return {"in_flight": ["intended", "unknown", "confirmed"], "terminal": ["attached", "failed", "released"]}
