"""
Disposition Enforcer — the failure-to-close worker that migration 0007 was
built for ("Temporal Listing Safeguards", client-management roadmap).

Hourly sweep, two passes, one transaction each:

  1. EXPIRE — expire_stale_contracts() (0007) bulk-flips overdue
     under_contract/marketing rows to 'expired'; each one is broadcast to its
     tenant's dashboards as CONTRACT_EXPIRED.

  2. DANGER ZONE — lead_contract_status rows with days_remaining at or below
     the threshold and dossier_status still 'under_contract' get Bedrock-
     generated disposition marketing (email blast + IG caption from the real
     underwriting numbers), stored in leads.marketing_payload (0010), and the
     row transitions to 'marketing'. The transition IS the idempotency guard —
     'marketing' rows never re-enter the sweep. If Bedrock fails, the row
     stays 'under_contract': the alert still fires (the agent must know the
     clock is bleeding) and asset generation retries next sweep.

Tenancy: the sweep runs under a PLATFORM_ADMIN system context — the RLS
policies' app_is_platform_admin() escape is the sanctioned cross-tenant path
(0001). Every broadcast still targets only the owning tenant's sockets.
"""

import asyncio
import json
import logging
import os
from typing import Optional

from db.connection import tenant_tx
from ml_forge.bedrock_client import invoke_bedrock_model, PRIMARY_MODEL, SECONDARY_MODEL
from tenancy import Role, TenantContext
import ws_hub

logger = logging.getLogger("oracle.disposition")

DANGER_ZONE_DAYS = int(os.getenv("ORACLE_DANGER_ZONE_DAYS", "15"))
SWEEP_INTERVAL_S = int(os.getenv("ORACLE_DISPOSITION_SWEEP_INTERVAL", "3600"))
FIRST_SWEEP_DELAY_S = 30  # let the pool + WS clients settle after boot

# The worker's identity for RLS purposes. agent_id is a label for logs/GUCs,
# not a JWT subject — this context never leaves the process.
SYSTEM_CTX = TenantContext(
    agent_id="disposition-enforcer",
    tenant_id="00000000-0000-0000-0000-000000000000",
    role=Role.PLATFORM_ADMIN,
)

MARKETING_PROMPT = """You are an elite real-estate disposition manager. An off-market wholesale
contract expires in {days_remaining} days and must be liquidated now.

Property:
- Address: {address}
- After Repair Value (ARV): {arv}
- Maximum Allowable Offer (MAO): {mao}
- Estimated rehab: {rehab}
- Distress signals: {distress}

Write an aggressive, metric-driven cash-buyer email blast and an Instagram
caption. Lead with the spread/equity. Create real urgency without fabricating
numbers — use only the figures above; if one is unknown, sell the opportunity
without it.

Respond with STRICT JSON only — no prose, no markdown:
{{"email_subject": "", "email_body": "", "ig_caption": ""}}"""


def _fmt_money(v) -> str:
    try:
        return f"${float(v):,.0f}"
    except (TypeError, ValueError):
        return "not on file"


def _extract_json(text: str) -> Optional[dict]:
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None


def _loads(value) -> dict:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return {}
    return value or {}


async def _generate_marketing(lead: dict, days_remaining: int) -> Optional[dict]:
    under, prop = lead["underwriting"], lead["payload"]
    prompt = MARKETING_PROMPT.format(
        days_remaining=days_remaining,
        address=prop.get("address") or lead["parcel_id"],
        arv=_fmt_money(under.get("arv") or under.get("estimated_value") or prop.get("estimated_value")),
        mao=_fmt_money(under.get("mao")),
        rehab=_fmt_money(under.get("rehab") or under.get("rehab_estimate")),
        distress=", ".join(prop.get("distress_flags") or []) or "none recorded",
    )
    raw = await asyncio.to_thread(invoke_bedrock_model, PRIMARY_MODEL, prompt, 1500)
    if not raw:
        raw = await asyncio.to_thread(invoke_bedrock_model, SECONDARY_MODEL, prompt, 1500)
    assets = _extract_json(raw) if raw else None
    if assets and {"email_subject", "email_body", "ig_caption"} <= assets.keys():
        return assets
    return None


async def sweep_once() -> None:
    """One full enforcement pass. Each phase isolates its own failures."""

    # ── Pass 1: expire overdue contracts ─────────────────────────────────────
    try:
        async with tenant_tx(SYSTEM_CTX) as conn:
            expired = await conn.fetch("SELECT id, tenant_id, parcel_id, payload FROM expire_stale_contracts()")
        for row in expired:
            prop = _loads(row["payload"])
            await ws_hub.broadcast(str(row["tenant_id"]), {
                "type": "CONTRACT_EXPIRED",
                "lead_id": str(row["id"]),
                "parcel_id": row["parcel_id"],
                "address": prop.get("address", ""),
            })
        if expired:
            logger.warning("Disposition sweep: %d contract(s) expired.", len(expired))
    except Exception:  # noqa: BLE001
        logger.exception("Disposition sweep: expire pass failed.")

    # ── Pass 2: danger zone — generate assets, transition, alert ─────────────
    try:
        async with tenant_tx(SYSTEM_CTX) as conn:
            dying = await conn.fetch(
                """
                SELECT s.id, s.tenant_id, s.days_remaining,
                       l.parcel_id, l.underwriting, l.payload
                  FROM lead_contract_status s
                  JOIN leads l ON l.id = s.id
                 WHERE s.days_remaining <= $1
                   AND s.dossier_status = 'under_contract'
                """,
                DANGER_ZONE_DAYS,
            )
    except Exception:  # noqa: BLE001
        logger.exception("Disposition sweep: danger-zone query failed.")
        return

    for row in dying:
        lead = {
            "id": str(row["id"]),
            "tenant_id": str(row["tenant_id"]),
            "parcel_id": row["parcel_id"],
            "underwriting": _loads(row["underwriting"]),
            "payload": _loads(row["payload"]),
        }
        days = row["days_remaining"]
        address = lead["payload"].get("address") or lead["parcel_id"]
        logger.warning(
            "Danger zone: lead %s (%s) — %d day(s) remaining.",
            lead["id"], address, days,
        )

        assets = None
        try:
            assets = await _generate_marketing(lead, days)
        except Exception:  # noqa: BLE001
            logger.exception("Marketing generation crashed for lead %s.", lead["id"])

        if assets:
            try:
                async with tenant_tx(SYSTEM_CTX) as conn:
                    await conn.execute(
                        """
                        UPDATE leads
                           SET dossier_status = 'marketing',
                               marketing_payload = $1::jsonb,
                               marketing_generated_at = now()
                         WHERE id = $2
                        """,
                        json.dumps(assets),
                        row["id"],
                    )
            except Exception:  # noqa: BLE001
                logger.exception("Failed to store marketing assets for lead %s.", lead["id"])
                assets = None  # don't claim assets the DB doesn't hold

        # Alert fires either way — a bleeding clock the agent doesn't know
        # about is the exact failure mode this worker exists to prevent.
        await ws_hub.broadcast(lead["tenant_id"], {
            "type": "CONTRACT_DANGER",
            "lead_id": lead["id"],
            "parcel_id": lead["parcel_id"],
            "address": address,
            "days_remaining": days,
            "assets_ready": bool(assets),
        })


async def _enforcer_loop() -> None:
    logger.info(
        "Disposition Enforcer online — danger zone ≤%d days, sweep every %ds.",
        DANGER_ZONE_DAYS, SWEEP_INTERVAL_S,
    )
    await asyncio.sleep(FIRST_SWEEP_DELAY_S)
    while True:
        try:
            await sweep_once()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — the clock must keep ticking
            logger.exception("Disposition sweep crashed; retrying next interval.")
        await asyncio.sleep(SWEEP_INTERVAL_S)


_task: Optional[asyncio.Task] = None


async def start_disposition_enforcer() -> None:
    """Called from server lifespan startup."""
    global _task
    _task = asyncio.create_task(_enforcer_loop())


async def stop_disposition_enforcer() -> None:
    """Called from server lifespan shutdown."""
    global _task
    if _task:
        _task.cancel()
        await asyncio.gather(_task, return_exceptions=True)
        _task = None
