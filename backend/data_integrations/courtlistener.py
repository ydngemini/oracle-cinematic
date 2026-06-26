"""
data_integrations/courtlistener.py — CourtListener / RECAP bankruptcy dockets (FREE-KEY).

CourtListener (Free Law Project) mirrors PACER federal dockets via RECAP. We use
it for one thing: federal BANKRUPTCY dockets — an owner-distress signal that no
free source covers cleanly otherwise.

SCOPE (important): federal bankruptcy ONLY. Foreclosure and probate are STATE
court matters and are NOT in PACER/RECAP — do not look for them here; those come
from county recorder/court open-data harvesters.

Auth: the v4 dockets endpoint requires a token (verified: unauthenticated GETs
return 401). Reads `COURTLISTENER_TOKEN` and sends `Authorization: Token ...`.
DORMANT-SAFE: with no token, this attempts the call once and, when the upstream
blocks it (401/403), returns {"skipped": "set COURTLISTENER_TOKEN"} instead of
raising — the platform keeps running with this source simply dark.

Bankruptcy court ids (CourtListener `court` filter) are the PACER court codes,
e.g. `deb` (D. Delaware Bankr.), `nysb` (S.D.N.Y. Bankr.), `cacb` (C.D. Cal.
Bankr.) — they end in `b`. List them via /courts/?jurisdiction=FB.
"""

from __future__ import annotations

import os
import urllib.error
import urllib.parse
from typing import Optional

from .base import DataSource, RateLimiter, RetryConfig
from .cache import IntegrationCache

_BASE = "https://www.courtlistener.com/api/rest/v4"

# Dockets are appended continuously; keep the cache short so new filings surface.
_TTL = 6 * 3_600


class CourtListenerSource(DataSource):
    source_name = "courtlistener"

    def __init__(self, cache: Optional[IntegrationCache] = None):
        super().__init__(
            rate_limiter=RateLimiter(min_interval=1.0, jitter=0.3),
            retry_config=RetryConfig(max_attempts=3, base_backoff=2.0),
            cache=cache,
        )
        self._token = (os.environ.get("COURTLISTENER_TOKEN") or "").strip()

    def _cache_ttl(self) -> int:
        return _TTL

    async def fetch(
        self, *, court: str, date_filed_after: Optional[str], page_size: int
    ) -> dict:
        params = {"court": court, "order_by": "-date_filed", "page_size": page_size}
        if date_filed_after:
            params["date_filed__gte"] = date_filed_after
        url = f"{_BASE}/dockets/?{urllib.parse.urlencode(params)}"
        headers = {"Authorization": f"Token {self._token}"} if self._token else {}
        try:
            data = await self._get_json(url, headers=headers, timeout=30)
            return {"data": data}
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                return {"auth_error": e.code}
            self._log.warning("CourtListener HTTP %s for court=%s", e.code, court)
            return {"error": f"HTTP {e.code}"}
        except Exception as e:  # noqa: BLE001 — never crash the request path
            self._log.warning("CourtListener fetch failed (court=%s): %s", court, e)
            return {"error": str(e)}

    def normalize(self, raw: dict) -> dict:
        data = raw.get("data") or {}
        results = data.get("results", []) or []
        dockets = []
        for d in results:
            url = d.get("absolute_url")
            dockets.append({
                "id": d.get("id"),
                "case_name": d.get("case_name") or d.get("case_name_full"),
                "docket_number": d.get("docket_number"),
                "court": d.get("court_id") or d.get("court"),
                "date_filed": d.get("date_filed"),
                "chapter": d.get("chapter"),
                "nature_of_suit": d.get("nature_of_suit"),
                "url": f"https://www.courtlistener.com{url}" if url else None,
            })
        return {
            "count": data.get("count"),
            "returned": len(dockets),
            "authenticated": bool(self._token),
            "scope": "federal bankruptcy dockets only (PACER/RECAP); foreclosure "
                     "and probate are state courts and are NOT covered here",
            "dockets": dockets,
            "source": "courtlistener_recap",
        }

    async def bankruptcy_dockets(
        self,
        court: str,
        *,
        date_filed_after: Optional[str] = None,
        page_size: int = 20,
    ) -> dict:
        court = (court or "").strip().lower()
        if not court:
            return {"error": "court is required (e.g. 'deb')", "source": "courtlistener"}
        page_size = max(1, min(int(page_size), 100))
        cache_key = f"courtlistener:dockets:{court}:{date_filed_after or ''}:{page_size}"

        if self._cache:
            cached = await self._cache.get(cache_key)
            if cached is not None:
                self._metrics["cache_hits"] += 1
                return cached

        raw = await self.fetch(
            court=court, date_filed_after=date_filed_after, page_size=page_size
        )

        if raw.get("auth_error"):
            if not self._token:
                return {
                    "skipped": "set COURTLISTENER_TOKEN",
                    "detail": "CourtListener v4 dockets require authentication; "
                              "this source is dormant until a token is provided.",
                    "court": court,
                    "source": "courtlistener",
                }
            return {
                "error": "authentication failed — check COURTLISTENER_TOKEN",
                "status": raw["auth_error"],
                "court": court,
                "source": "courtlistener",
            }
        if raw.get("error"):
            return {"error": raw["error"], "court": court, "source": "courtlistener"}

        result = self.normalize(raw)
        result["court"] = court
        self._metrics["normalized"] += 1
        if self._cache:
            await self._cache.set(cache_key, result, ttl=self._cache_ttl())
        return result
