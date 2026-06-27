"""
AVM client — real Automated Valuation Model with a fact-check layer.

Primary source: RentCast (RENTCAST_API_KEY) — one call returns a point value, a
value range, and comparable sales. ATTOM (ATTOM_API_KEY) is wired as a second
source for cross-validation when configured; absent that, RentCast self-checks
against its own comps plus the parcel's county assessed value.

The reconciliation layer never presents a single number as truth. For each
estimate it runs:
  - comps coherence:  is the AVM's implied $/sqft inside the comps' $/sqft spread?
  - assessed sanity:  is the value grossly inconsistent with the county assessment?
  - range tightness:  a wide provider range lowers confidence.
  - cross-source:     if two providers disagree by >20% the verdict is CONFLICT.
It emits: estimated_value, a (possibly widened) confidence band, a confidence
score in [0,1], a verdict (VERIFIED | UNVERIFIED | CONFLICT | UNAVAILABLE), the
contributing sources, and the individual check results — so a low-confidence or
conflicting valuation is labeled, never laundered into a confident number.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field, asdict, fields
from typing import Optional

import aiohttp

logger = logging.getLogger("oracle.avm")

RENTCAST_BASE = "https://api.rentcast.io/v1"
ATTOM_BASE = "https://api.gateway.attomdata.com/propertyapi/v1.0.0"
REQUEST_TIMEOUT = 20

# Plausible band for value / county-assessed-value. Many states assess
# fractionally, so a value well above assessed is normal; we only flag GROSS
# inconsistency (value far below assessed, or absurdly above it).
ASSESSED_MIN_RATIO = 0.5    # value should not be < 50% of assessed
ASSESSED_MAX_RATIO = 12.0   # value should not be > 12x assessed (fractional-assessment slack)

VERIFY_CONFIDENCE = 0.65    # >= this -> VERIFIED
CROSS_SOURCE_CONFLICT = 0.20  # two sources >20% apart -> CONFLICT


@dataclass
class AVMEstimate:
    """A single provider's raw estimate."""
    source: str
    value: int
    low: int
    high: int
    comps: list = field(default_factory=list)  # [{price, sqft, ...}]


@dataclass
class AVMResult:
    """Reconciled, fact-checked valuation."""
    estimated_value: int
    confidence_low: int
    confidence_high: int
    price_per_sqft: float
    verdict: str                 # VERIFIED | UNVERIFIED | CONFLICT | UNAVAILABLE
    confidence: float            # 0..1
    sources: list = field(default_factory=list)
    checks: dict = field(default_factory=dict)
    comps_used: int = 0


# ── Providers ────────────────────────────────────────────────────────────────

async def _rentcast_avm(session: aiohttp.ClientSession, subject: dict) -> Optional[AVMEstimate]:
    key = os.environ.get("RENTCAST_API_KEY", "")
    if not key:
        return None
    address = subject.get("address") or ""
    if not address:
        return None
    params = {"address": address}
    # Optional hints improve the estimate; only send what we actually have.
    if subject.get("bedrooms"):
        params["bedrooms"] = subject["bedrooms"]
    if subject.get("bathrooms"):
        params["bathrooms"] = subject["bathrooms"]
    if subject.get("sqft"):
        params["squareFootage"] = subject["sqft"]
    try:
        async with session.get(
            f"{RENTCAST_BASE}/avm/value",
            params=params,
            headers={"X-Api-Key": key, "accept": "application/json"},
        ) as resp:
            if resp.status == 401:
                logger.warning("RentCast AVM: API key rejected (401) — set a valid RENTCAST_API_KEY.")
                return None
            if resp.status != 200:
                logger.warning("RentCast AVM: HTTP %s for %s", resp.status, address)
                return None
            data = await resp.json()
    except Exception as e:  # noqa: BLE001 — provider is best-effort
        logger.warning("RentCast AVM request failed: %s", e)
        return None

    price = int(data.get("price") or 0)
    if price <= 0:
        return None
    comps = []
    for c in (data.get("comparables") or []):
        comps.append({
            "address": c.get("formattedAddress", ""),
            "price": int(c.get("price") or 0),
            "sqft": int(c.get("squareFootage") or 0),
            "distance": c.get("distance"),
            "correlation": c.get("correlation"),
        })
    return AVMEstimate(
        source="rentcast",
        value=price,
        low=int(data.get("priceRangeLow") or price),
        high=int(data.get("priceRangeHigh") or price),
        comps=comps,
    )


async def _attom_avm(session: aiohttp.ClientSession, subject: dict) -> Optional[AVMEstimate]:
    """Second source for cross-validation. Inert unless ATTOM_API_KEY is set."""
    key = os.environ.get("ATTOM_API_KEY", "")
    if not key:
        return None
    address = subject.get("address") or ""
    if not address:
        return None
    # ATTOM splits the address: "<street>, <city, ST zip>".
    parts = address.split(",", 1)
    address1 = parts[0].strip()
    address2 = parts[1].strip() if len(parts) > 1 else ""
    try:
        async with session.get(
            f"{ATTOM_BASE}/avm/detail",
            params={"address1": address1, "address2": address2},
            headers={"apikey": key, "accept": "application/json"},
        ) as resp:
            if resp.status != 200:
                logger.warning("ATTOM AVM: HTTP %s for %s", resp.status, address)
                return None
            data = await resp.json()
    except Exception as e:  # noqa: BLE001
        logger.warning("ATTOM AVM request failed: %s", e)
        return None

    try:
        amount = data["property"][0]["avm"]["amount"]
        value = int(amount.get("value") or 0)
    except (KeyError, IndexError, TypeError):
        return None
    if value <= 0:
        return None
    return AVMEstimate(
        source="attom",
        value=value,
        low=int(amount.get("low") or value),
        high=int(amount.get("high") or value),
        comps=[],
    )


# ── Fact-check / reconciliation ──────────────────────────────────────────────

def _median(xs: list[float]) -> float:
    s = sorted(xs)
    n = len(s)
    if not n:
        return 0.0
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2


def reconcile(estimates: list[AVMEstimate], assessed_value: int, sqft: int) -> AVMResult:
    """Combine + fact-check provider estimates into a labeled AVMResult."""
    estimates = [e for e in estimates if e and e.value > 0]
    if not estimates:
        return AVMResult(0, 0, 0, 0.0, "UNAVAILABLE", 0.0, [], {"reason": "no provider returned a value"})

    points = [e.value for e in estimates]
    value = int(_median(points))
    low = min(e.low for e in estimates)
    high = max(e.high for e in estimates)
    sources = [e.source for e in estimates]
    checks: dict = {}
    confidence = 0.5

    # ── comps coherence (use the richest comp set available) ──
    comps = max((e.comps for e in estimates), key=len, default=[])
    if comps and sqft:
        psf = [c["price"] / c["sqft"] for c in comps if c.get("sqft") and c.get("price")]
        if psf:
            lo_psf, hi_psf = min(psf), max(psf)
            implied = value / sqft
            inside = lo_psf * 0.8 <= implied <= hi_psf * 1.2
            checks["comps"] = {
                "pass": inside,
                "implied_psf": round(implied, 2),
                "comp_psf_range": [round(lo_psf, 2), round(hi_psf, 2)],
                "n": len(psf),
            }
            confidence += 0.2 if inside else -0.2
    else:
        checks["comps"] = {"pass": None, "reason": "no comps or sqft"}

    # ── assessed-value sanity ──
    if assessed_value and assessed_value > 0:
        ratio = value / assessed_value
        ok = ASSESSED_MIN_RATIO <= ratio <= ASSESSED_MAX_RATIO
        checks["assessed"] = {"pass": ok, "value_to_assessed": round(ratio, 2)}
        confidence += 0.15 if ok else -0.25
    else:
        checks["assessed"] = {"pass": None, "reason": "no assessed value on record"}

    # ── range tightness ──
    rel_width = (high - low) / value if value else 1.0
    if rel_width <= 0.15:
        checks["range"] = {"pass": True, "rel_width": round(rel_width, 3)}
        confidence += 0.15
    elif rel_width <= 0.30:
        checks["range"] = {"pass": True, "rel_width": round(rel_width, 3)}
    else:
        checks["range"] = {"pass": False, "rel_width": round(rel_width, 3)}
        confidence -= 0.10

    # ── cross-source agreement ──
    verdict = None
    if len(points) >= 2:
        spread = (max(points) - min(points)) / value if value else 1.0
        agree = spread <= CROSS_SOURCE_CONFLICT
        checks["cross_source"] = {"pass": agree, "spread": round(spread, 3), "points": points}
        if not agree:
            verdict = "CONFLICT"
            confidence = min(confidence, 0.35)
        elif spread <= 0.10:
            confidence += 0.25
        else:
            confidence += 0.10

    confidence = max(0.0, min(confidence, 1.0))
    if verdict is None:
        verdict = "VERIFIED" if confidence >= VERIFY_CONFIDENCE else "UNVERIFIED"

    return AVMResult(
        estimated_value=value,
        confidence_low=low,
        confidence_high=high,
        price_per_sqft=round(value / sqft, 2) if sqft else 0.0,
        verdict=verdict,
        confidence=round(confidence, 2),
        sources=sources,
        checks=checks,
        comps_used=len(comps),
    )


# ── Cache layer ───────────────────────────────────────────────────────────────
# RentCast's free tier is 50 calls/MONTH, so re-valuing the same property is
# expensive. We cache reconciled results in di_cache (the global, source-namespaced
# integration cache backed by migration 0020) for AVM_TTL, plus a tiny in-process
# memo for intra-run dedup. Everything here DEGRADES GRACEFULLY: if the app DB
# pool is absent (e.g. called outside app context) or any cache op fails, we skip
# the cache and value live — caching never blocks or breaks a valuation.

AVM_TTL = 7 * 86_400  # mirrors TTL["avm"] in data_integrations/cache.py

# In-process memo + per-key locks: dedup identical lookups within one run and
# collapse concurrent same-key calls onto a single provider hit.
_INPROC: dict[str, "AVMResult"] = {}
_INPROC_LOCKS: dict[str, asyncio.Lock] = {}


def _avm_cache_key(subject: dict) -> str:
    """Stable key from the normalized address plus the hints that actually change
    the provider call (beds/baths/sqft). Empty string ⇒ uncacheable (no address)."""
    address = (subject.get("address") or "").strip().lower()
    if not address:
        return ""
    # Collapse whitespace and repeated commas so cosmetic variants share a key.
    norm = re.sub(r"\s+", " ", re.sub(r",\s*", ",", address)).strip(" ,")
    parts = [norm]
    for f in ("bedrooms", "bathrooms", "sqft"):
        v = subject.get(f)
        if v:
            parts.append(f"{f}={v}")
    digest = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:24]
    return f"avm:{digest}"


def _lock_for(key: str) -> asyncio.Lock:
    lock = _INPROC_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _INPROC_LOCKS[key] = lock
    return lock


def _serialize_result(result: AVMResult) -> dict:
    return asdict(result)


def _deserialize_result(payload) -> Optional[AVMResult]:
    """payload may be a dict (jsonb codec) or a str (asyncpg's default jsonb)."""
    try:
        data = json.loads(payload) if isinstance(payload, str) else dict(payload)
        names = {f.name for f in fields(AVMResult)}
        return AVMResult(**{k: v for k, v in data.items() if k in names})
    except Exception as e:  # noqa: BLE001 — bad cache row must never break a valuation
        logger.debug("AVM cache deserialize failed: %s", e)
        return None


def _get_pool():
    """The app-wide asyncpg pool, or None if we're outside app context."""
    try:
        from db.connection import get_pool
        return get_pool()
    except Exception:
        return None


async def _cache_get(key: str) -> Optional[AVMResult]:
    if not key:
        return None
    pool = _get_pool()
    if pool is None:
        return None
    try:
        from data_integrations.cache import IntegrationCache
        payload = await IntegrationCache(pool).get(key)  # PG-only (no Redis client)
        if payload is None:
            return None
        return _deserialize_result(payload)
    except Exception as e:  # noqa: BLE001
        logger.debug("AVM cache GET skipped for %s: %s", key, e)
        return None


async def _cache_set(key: str, result: AVMResult) -> None:
    if not key:
        return
    pool = _get_pool()
    if pool is None:
        return
    try:
        from data_integrations.cache import IntegrationCache, TTL
        ttl = TTL.get("avm", AVM_TTL)
        await IntegrationCache(pool).set(key, _serialize_result(result), ttl)
    except Exception as e:  # noqa: BLE001
        logger.debug("AVM cache SET skipped for %s: %s", key, e)


async def _value_property_uncached(subject: dict) -> AVMResult:
    """Hit all configured providers concurrently and reconcile — no caching."""
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)) as session:
        rc, at = await asyncio.gather(
            _rentcast_avm(session, subject),
            _attom_avm(session, subject),
        )
    return reconcile([rc, at], int(subject.get("assessed_value") or 0), int(subject.get("sqft") or 0))


async def value_property(subject: dict) -> AVMResult:
    """Reconciled, fact-checked valuation — cache-first.

    Order of operations: in-process memo → di_cache (7-day TTL) → live providers.
    Always returns an AVMResult (UNAVAILABLE if nothing is configured or nothing
    answers) — callers should branch on `.verdict`. Caching is best-effort and
    degrades to a live call on any cache miss/error/absent-pool.

    Only successful valuations (estimated_value > 0) are persisted: caching an
    UNAVAILABLE would let one transient provider failure/429 poison a property
    for a week, and we never launder a stale failure into a confident answer."""
    if not (os.environ.get("RENTCAST_API_KEY") or os.environ.get("ATTOM_API_KEY")):
        return AVMResult(0, 0, 0, 0.0, "UNAVAILABLE", 0.0, [], {"reason": "no AVM provider configured"})

    key = _avm_cache_key(subject)
    if not key:
        # No address to key on — can't cache, value live.
        return await _value_property_uncached(subject)

    memo = _INPROC.get(key)
    if memo is not None:
        return memo

    # Serialize same-key work so concurrent callers share one provider hit.
    async with _lock_for(key):
        memo = _INPROC.get(key)
        if memo is not None:
            return memo

        cached = await _cache_get(key)
        if cached is not None:
            _INPROC[key] = cached
            # Capital visibility: a hit means we served an already-underwritten
            # address from di_cache and spent $0 instead of a billable AVM call.
            logger.info("AVM cache HIT %s — served from di_cache, no provider call ($0).", key)
            return cached

        logger.info("AVM cache MISS %s — calling live AVM provider(s) (billable).", key)
        result = await _value_property_uncached(subject)

        if result and result.estimated_value > 0:
            _INPROC[key] = result
            await _cache_set(key, result)
        return result
