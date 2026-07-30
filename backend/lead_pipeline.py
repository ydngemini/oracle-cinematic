"""Shared, side-effect-free contracts for the agent lead pipeline."""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from typing import Any


DEFAULT_PAGE_SIZE = 60
MAX_PAGE_SIZE = 200
VALID_PRIORITIES = {"all", "hot", "contract", "distress"}
VALID_SCOPE_FILTERS = {"all", "statewide", "county", "city", "geometry_only"}
VALID_DETAIL_FILTERS = {"all", "comprehensive", "standard", "limited", "legacy"}
VALID_FRESHNESS_FILTERS = {"all", "fresh", "verify"}
VALID_MAP_FILTERS = {"all", "source_coordinate", "address_approximation", "unmapped"}


def _text(value: Any, *, maximum: int = 160) -> str:
    return str(value or "").strip()[:maximum]


def encode_cursor(score: int, updated_at: datetime, lead_id: str) -> str:
    """Encode ordering fields only; no tenant data or user input is trusted."""
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    payload = json.dumps(
        {"score": int(score), "updated_at": updated_at.astimezone(timezone.utc).isoformat(), "id": str(lead_id)},
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_cursor(value: Any) -> tuple[int, datetime, str] | None:
    text = _text(value, maximum=512)
    if not text:
        return None
    try:
        raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
        data = json.loads(raw.decode("utf-8"))
        score = int(data["score"])
        updated_at = datetime.fromisoformat(str(data["updated_at"]).replace("Z", "+00:00"))
        lead_id = _text(data["id"], maximum=64)
        if not -1_000_000 <= score <= 1_000_000 or not lead_id:
            return None
        return score, updated_at.astimezone(timezone.utc), lead_id
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def parse_request(message: Any) -> dict[str, Any]:
    """Whitelist the bounded WebSocket request contract for lead browsing."""
    body = message if isinstance(message, dict) else {}
    try:
        limit = int(body.get("limit", DEFAULT_PAGE_SIZE))
    except (TypeError, ValueError):
        limit = DEFAULT_PAGE_SIZE
    limit = max(1, min(MAX_PAGE_SIZE, limit))

    state = _text(body.get("state"), maximum=2).upper()
    if state and (len(state) != 2 or not state.isalpha()):
        state = ""

    def selected(name: str, allowed: set[str]) -> str:
        value = _text(body.get(name), maximum=32).lower() or "all"
        return value if value in allowed else "all"

    return {
        "cursor": decode_cursor(body.get("cursor")),
        "limit": limit,
        "state": state or None,
        "priority": selected("priority", VALID_PRIORITIES),
        "scope": selected("scope", VALID_SCOPE_FILTERS),
        "detail": selected("detail", VALID_DETAIL_FILTERS),
        "freshness": selected("freshness", VALID_FRESHNESS_FILTERS),
        "map_confidence": selected("map_confidence", VALID_MAP_FILTERS),
        "query": _text(body.get("query"), maximum=120).lower() or None,
    }


def scope_class(scope: Any, *, geometry_only: bool = False) -> str:
    if geometry_only:
        return "geometry_only"
    value = _text(scope).lower()
    if value == "statewide":
        return "statewide"
    if value.startswith("county:"):
        return "county"
    if value.startswith("city:"):
        return "city"
    return "unknown"


def priority_factors(payload: dict[str, Any], score: Any) -> list[str]:
    """Expose the actual public-record signals behind the routing heuristic."""
    factors: list[str] = []
    if payload.get("is_absentee_owner") is True:
        factors.append("reported absentee")
    flags = payload.get("distress_flags")
    if isinstance(flags, list) and flags:
        factors.append("public record signal")
    if payload.get("equity_percent") is not None:
        factors.append("reported equity")
    if payload.get("owner_type") in {"corporate", "trust"}:
        factors.append("entity ownership")
    if not factors and int(score or 0) > 0:
        factors.append("source record priority")
    return factors


def location_confidence(payload: dict[str, Any]) -> str:
    try:
        latitude = float(payload.get("latitude"))
        longitude = float(payload.get("longitude"))
    except (TypeError, ValueError):
        latitude = longitude = None
    if latitude is not None and longitude is not None and -90 <= latitude <= 90 and -180 <= longitude <= 180:
        return "source_coordinate"
    if _text(payload.get("address"), maximum=320):
        return "address_approximation"
    return "unmapped"


def freshness_status(updated_at: datetime | None, *, now: datetime | None = None) -> str:
    if updated_at is None:
        return "verify"
    reference = now or datetime.now(timezone.utc)
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    return "fresh" if reference - updated_at.astimezone(timezone.utc) <= timedelta(days=45) else "verify"


def source_record_refreshed_at(payload: dict[str, Any], fallback: datetime | None) -> datetime | None:
    """Use the public source timestamp; DB maintenance must not refresh a lead."""
    provenance = payload.get("provenance") if isinstance(payload, dict) else None
    value = provenance.get("record_refreshed_at") if isinstance(provenance, dict) else None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
        except ValueError:
            pass
    return fallback
