"""Shared APIRouter, validation constants, and DB/validation helpers.

Depends on nothing else in the package, so it is always import-safe.
"""
from __future__ import annotations

import logging
import re
import uuid
from datetime import date, datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator

from db.connection import tenant_tx
from tenancy import TenantContext, Role, require_context, require_role

# Authoritative attorney-at-closing list — single source of truth shared with
# the compliance engine so the public state-profile API and ComplianceEngine
# never disagree about whether a state requires an attorney at closing.
from compliance_engine.closing import ATTORNEY_CLOSE_STATES

logger = logging.getLogger("oracle.state_compliance")

router = APIRouter(tags=["State Compliance"])

# ---------------------------------------------------------------------------
# Input validation constants
# ---------------------------------------------------------------------------

_STATE_RE = re.compile(r"^[A-Z]{2}$")
_FIPS_RE = re.compile(r"^\d{5}$")
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

ALL_STATE_CODES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
    "DC",
}

# Disclosure attorney-review states require an attorney at closing.
_ATTORNEY_REVIEW_STATES = {"CT", "DE", "GA", "MA", "NY", "SC", "WV"}

# States with mandatory seller disclosure forms (≠ "as-is" caveat emptor).
_MANDATORY_DISCLOSURE_STATES = ALL_STATE_CODES - {"AR", "NH", "NM", "ND", "WY"}

# States that operate a Transfer Disclosure Statement (TDS) or equivalent.
_TDS_STATES = {"CA", "CO", "FL", "IL", "MN", "OH", "OR", "TX", "WA"}

# Lead-paint disclosure required nationwide on pre-1978 housing (federal).
_FEDERAL_LEAD_PAINT_THRESHOLD_YEAR = 1978

# CE hours required per renewal cycle — a representative sample; full data is
# stored in the DB table state_licensing_requirements.
_CE_HOURS_BY_STATE: dict[str, int] = {
    "CA": 45, "TX": 18, "FL": 14, "NY": 22, "IL": 12,
    "GA": 36, "CO": 24, "WA": 30, "OR": 30, "AZ": 24,
}

# Reciprocity adjacency — value is the reciprocity class:
#   "full"    — no additional exam required
#   "partial" — additional state law exam required
#   "none"    — must re-apply from scratch
# Only a representative sample is stored here; the full matrix is in
# state_licensing_requirements rows with type = 'reciprocity'.
_RECIPROCITY_MATRIX: dict[tuple[str, str], str] = {
    ("GA", "AL"): "full",   ("GA", "FL"): "partial",
    ("TX", "OK"): "full",   ("TX", "NM"): "partial",
    ("CA", "NV"): "partial",("OR", "WA"): "full",
    ("CO", "UT"): "partial",("FL", "GA"): "partial",
    ("NY", "NJ"): "partial",("VA", "MD"): "full",
    ("VA", "DC"): "partial",("MD", "DC"): "partial",
}


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _iso(dt) -> Optional[str]:
    return dt.isoformat() if dt else None


def _num(v) -> Optional[float]:
    return float(v) if v is not None else None


def _require_state(state_code: str) -> str:
    code = state_code.upper()
    if code not in ALL_STATE_CODES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unknown state code: {state_code!r}.",
        )
    return code


def _require_uuid(value: str, name: str) -> str:
    if not value or not _UUID_RE.match(value):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{name} must be a valid UUID.",
        )
    return value.lower()


def _require_agent_id(value: str, name: str = "agent_id") -> str:
    """Validate an agent identifier. Agent IDs are TEXT (an email or handle such
    as 'ydnop@ydnhft.com' or 'demo-operator'), matching the text ``agent_id``
    columns on ``agent_licenses`` / ``agent_ce_log`` — they are NOT UUIDs, so the
    UUID validator wrongly 422'd every real identity. Only emptiness and a sane
    length bound are enforced here; the value is used solely as a parameterized
    query argument and is compared against the authenticated ``ctx.agent_id``."""
    v = (value or "").strip()
    if not v or len(v) > 320:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{name} is required.",
        )
    return v


async def _fetch(ctx: TenantContext, query: str, *args) -> list[dict]:
    """Single-read helper; maps connection failures to 503."""
    try:
        async with tenant_tx(ctx) as conn:
            rows = await conn.fetch(query, *args)
            return [dict(r) for r in rows]
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("State compliance DB query failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Memory Core offline.",
        )


async def _fetchrow(ctx: TenantContext, query: str, *args) -> Optional[dict]:
    rows = await _fetch(ctx, query, *args)
    return rows[0] if rows else None


# ---------------------------------------------------------------------------
# Dataset availability
# ---------------------------------------------------------------------------
#
# Several tables in this schema are read by a route and written by nothing —
# no INSERT, no COPY, no migration seed, anywhere in the codebase. A caller
# cannot tell that apart from a genuine answer, because both arrive as an empty
# result: `[]` reads as "Texas requires no disclosure forms" and a 404 reads as
# "we checked, that county has no data". Both are false, and both are the kind
# of unsupported positive claim this codebase does not make.
#
# So: an empty result for *this* subject stays an empty result, and only a
# table with no rows at all reports DATASET_NOT_LOADED. The distinction is the
# whole point — it costs one cheap EXISTS, and only on the path that was
# already returning nothing.

DATASET_NOT_LOADED = "DATASET_NOT_LOADED"

# How an operator would actually populate each one, so the error is actionable
# rather than merely honest.
_DATASET_LOAD_HINTS: dict[str, str] = {
    "county_market_stats": (
        "No loader exists. County market aggregates would come from a Census/BLS "
        "or MLS-derived pipeline that has not been built."
    ),
    "parcel_zoning": (
        "No loader exists. Zoning geometry would come from a municipal GIS "
        "harvest; see backend/harvesters/ for the parcel equivalents."
    ),
    "mls_boards": (
        "No loader exists. The MLS board registry is unpopulated; live listing "
        "access goes through ORACLE_RESO_* feeds instead."
    ),
    "state_disclosure_forms": (
        "Seeded by migration 0035 for a subset of states only, and never refreshed. "
        "If this table is empty the seed did not run."
    ),
    "state_contract_templates": (
        "Seeded by migration 0035 for a subset of states only, and never refreshed. "
        "If this table is empty the seed did not run."
    ),
    "state_advertising_rules": (
        "Seeded by migration 0035 for a subset of states only, and never refreshed. "
        "If this table is empty the seed did not run."
    ),
    "state_reciprocity_matrix": (
        "No loader exists. The route falls back to a small in-code matrix; rows "
        "here would override it."
    ),
}


def dataset_load_hint(dataset: str) -> str:
    """How an operator would populate a table, for callers outside this package.

    The hints are the same ones the 404 body carries; this exists so the agent
    tool surface can say the same thing in a tool result without reaching into
    a private dict that a rename would silently break.
    """
    return _DATASET_LOAD_HINTS.get(
        dataset, "No loader for this table exists in the codebase."
    )


def dataset_not_loaded(dataset: str) -> HTTPException:
    """404 stating the dataset was never loaded — not that the subject has none.

    Deliberately keeps the 404 status so existing callers' not-found handling
    still fires; the body is what changes. `code` is machine-readable so a UI
    can render "this data isn't loaded on this deployment" rather than the
    much more damaging "no results found".
    """
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={
            "code": DATASET_NOT_LOADED,
            "dataset": dataset,
            "reason": (
                f"The {dataset!r} table is empty on this deployment, so this "
                f"endpoint cannot answer. This is not a finding that no data exists."
            ),
            "how_to_populate": dataset_load_hint(dataset),
        },
    )


async def _require_dataset_loaded(ctx: TenantContext, table: str) -> None:
    """Raise DATASET_NOT_LOADED when `table` holds no rows at all.

    Call this only after a query already came back empty — on the populated
    path it never runs, so the extra round trip costs nothing in the normal case.
    """
    probe = await _fetch(ctx, f"SELECT 1 AS present FROM {table} LIMIT 1")  # noqa: S608 — fixed identifiers, never user input
    if not probe:
        raise dataset_not_loaded(table)


# ---------------------------------------------------------------------------
# Pydantic schemas — States
# ---------------------------------------------------------------------------

