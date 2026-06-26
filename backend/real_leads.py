"""Real-lead sourcing for the live dashboard pipeline.

The WorkflowEngine's flagship loop (harvest → novelty → CMA → contact) scores
whatever is in the in-memory PropertyGraph. The CountyAssessorHarvester targets
live county portals, but those are frequently unreachable in dev (DNS/404), so
without a second source the graph stays empty and the dashboard shows nothing.

This module seeds the graph from the operator's REAL `leads` rows (RLS-scoped),
mapping each onto the `graph.ingest_public_record()` record shape. It never
fabricates a field: missing values are conservatively derived or left at a
neutral zero/NULL rather than invented, and every record is tagged
`record_type="REAL_PARCEL"` / `_source="real"` so the UI and audit trail can
distinguish a real parcel from any synthetic placeholder.
"""

import json
import logging

logger = logging.getLogger("oracle.real_leads")


def _loads(value, default=None):
    """Tolerant JSON decode for jsonb columns that may arrive as str or dict."""
    if value is None:
        return default if default is not None else {}
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default if default is not None else {}


def record_from_lead_row(r) -> dict:
    """Map a `leads` row onto the firehose record dict that
    graph.ingest_public_record() expects.

    Missing fields get conservative derived defaults so a real row never
    silently produces a malformed record:
      - assessed_value derived from market_value (~0.72) when not present
      - equity_pct from payload.equity_percent (clamped 0..100)
      - mortgage_balance implied from market_value and equity
      - life_event mapped from the first distress flag, else NULL (unknown)
    """
    payload = _loads(r["payload"])
    under = _loads(r["underwriting"])

    market_value = (
        payload.get("estimated_value")
        or under.get("estimated_value")
        or (float(r["asking_price"]) if r["asking_price"] is not None else 0)
        or 0
    )
    try:
        market_value = int(market_value)
    except (TypeError, ValueError):
        market_value = 0

    equity_raw = payload.get("equity_percent")
    if equity_raw is None:
        equity_raw = under.get("equity_percent")
    try:
        equity_pct = int(max(0.0, min(float(equity_raw), 100.0))) if equity_raw is not None else 0
    except (TypeError, ValueError):
        equity_pct = 0

    distress = payload.get("distress_flags") or []
    # First distress flag stands in for the firehose "life_event"; NULL when the
    # row carries no distress signal (we never invent one for a real record).
    life_event = distress[0].upper() if distress else None

    last_sale = payload.get("last_sale_date")

    # Real harvester-computed motivated-seller signal (0-100), derived from
    # genuine distress flags (absentee owner, tax status, etc). This is the real
    # signal the graph surfaces on — we never fabricate equity to pass the gate.
    try:
        motivation_score = int(r["motivation_score"]) if r["motivation_score"] is not None else 0
    except (TypeError, ValueError):
        motivation_score = 0

    return {
        # Real provenance — not one of the synthetic TAX_ASSESSMENT placeholders.
        "record_type": "REAL_PARCEL",
        "motivation_score": motivation_score,
        "owner_name": payload.get("owner_name") or "Owner of Record",
        "address": r["address"] or payload.get("address") or r["parcel_id"],
        "assessed_value": int(market_value * 0.72) if market_value else 0,
        "market_value": market_value,
        "sqft": r["sqft"] or 0,
        "bedrooms": r["beds"] or 0,
        "bathrooms": float(r["baths"]) if r["baths"] is not None else 0,
        "county": payload.get("county") or payload.get("city") or "",
        "state": r["state"],
        "equity_pct": equity_pct,
        # years_owned / purchase_year are not in the schema; derive from the last
        # sale year when available, else leave 0 (unknown) rather than fabricate.
        "years_owned": 0,
        "purchase_year": int(last_sale[:4]) if isinstance(last_sale, str) and len(last_sale) >= 4 and last_sale[:4].isdigit() else 0,
        "mortgage_balance": int(market_value * (1 - equity_pct / 100)) if market_value else 0,
        "life_event": life_event,
        "event_date": last_sale,
        # No probate/lien case number on a parcel row — carry the parcel id so the
        # downstream audit trail can still tie the record to its source.
        "case_number": r["parcel_id"],
        "_source": "real",
    }


async def fetch_real_records(tenant_id: str, user_id: str, limit: int) -> list[dict]:
    """Pull real `leads` rows, RLS-scoped to this operator's tenant via
    tenant_tx, and map each to the firehose record dict.

    Returns [] on any failure (no DB, non-UUID dev tenant, import gap) so the
    caller can degrade gracefully. Randomized ordering keeps the firehose lively
    across batches when the real set is small.
    """
    try:
        from tenancy import TenantContext, Role
        from db.connection import tenant_tx

        ctx = TenantContext(agent_id=user_id or "demo-operator", tenant_id=tenant_id, role=Role.AGENT)
        async with tenant_tx(ctx) as conn:
            rows = await conn.fetch(
                "SELECT parcel_id, state, motivation_score, underwriting, payload, "
                "       address, asking_price, beds, baths, sqft "
                "FROM leads ORDER BY random() LIMIT $1",
                limit,
            )
        return [record_from_lead_row(r) for r in rows]
    except Exception as e:  # noqa: BLE001 — lead source is best-effort
        logger.warning("fetch_real_records failed (%s); graph will not be seeded from leads.", e)
        return []
