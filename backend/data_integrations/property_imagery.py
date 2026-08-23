"""Licensed exterior imagery for an address.

Why this exists in this shape: the obvious way to get a photo of a house is to
take it from a listing portal, and that is the one way this codebase must not.
`docs/data-access-tiers.md` records the decision — Zillow and Redfin both forbid
scraping in their terms, *CoStar v. Zillow* is live litigation over reuse of
listing photos, and the photos themselves are copyrighted works owned by the
photographer or brokerage. Republishing them inside a paid CRM is infringement
independent of any terms of service. `spatial_agent.py`, which did exactly that
behind SPATIAL_ALLOW_WEB_SCRAPE, has been deleted.

So this module takes the licensed routes instead:

  * **Google Street View Static API** — imagery Google licenses for display,
    addressable by street address, national coverage. Needs a server-side API
    key (a browser key with an HTTP-referrer restriction will 403 here).
  * **Mapillary** — community street-level imagery under CC-BY-SA. Useful where
    Street View has no coverage, but the share-alike terms mean attribution is
    mandatory, so the caller is handed the attribution string rather than left
    to invent one.

Both are EXTERIOR ONLY. Neither can produce interior photographs, and this
module never claims otherwise: `interior=False` is on every result, because the
one thing a marketing surface must not do is present a streetside frame as
though it showed the inside of the home.

For interior photos the honest sources are the MLS feed (data_integrations/
listings_feed.py already ingests `Media[].MediaURL`, licensed for display with
the board's attribution rules) or the property owner uploading their own — both
of which carry the right to show them.
"""

from __future__ import annotations

import os
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Optional

from .base import DataIntegrationError, DataSource, RateLimiter, RetryConfig

#: Street View Static caps a single request at 640x640 without premium billing.
_MAX_EDGE = 640
_STREETVIEW_META = "https://maps.googleapis.com/maps/api/streetview/metadata"
_STREETVIEW_IMAGE = "https://maps.googleapis.com/maps/api/streetview"
_MAPILLARY_SEARCH = "https://graph.mapillary.com/images"
_TTL_IMAGERY = 30 * 86_400  # imagery changes on the order of years, not days


class ImageryConfigurationError(DataIntegrationError):
    """No imagery provider is configured. Distinct from 'no coverage here'."""


class ImageryAuthError(DataIntegrationError):
    """The imagery credential was rejected — wrong key type, or revoked.

    Separated from a generic failure because the most common cause is specific
    and fixable: a Google *browser* key carries an HTTP-referrer restriction and
    is refused for server-side Static API calls. Reporting that as an outage
    would send someone hunting for a network problem.
    """


@dataclass
class PropertyImage:
    """One licensed exterior frame.

    `attribution` is not optional metadata. Mapillary is CC-BY-SA and Google
    requires its own attribution, so a caller that renders the image without
    rendering this string is out of licence.
    """

    url: str
    source: str                 # 'google_streetview' | 'mapillary'
    attribution: str
    #: Always False here. Street-level imagery cannot show a home's interior,
    #: and labelling it truthfully is what stops it being used as one.
    interior: bool = False
    #: Where the camera was, not where the property is — they differ by the
    #: width of the street, and conflating them misplaces a pin.
    captured_lat: Optional[float] = None
    captured_lng: Optional[float] = None
    captured_at: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "source": self.source,
            "attribution": self.attribution,
            "interior": self.interior,
            "captured_lat": self.captured_lat,
            "captured_lng": self.captured_lng,
            "captured_at": self.captured_at,
            **({"metadata": self.metadata} if self.metadata else {}),
        }


class PropertyImagerySource(DataSource):
    """Exterior imagery by address, from licensed providers only."""

    source_name = "property_imagery"

    def __init__(self, cache=None) -> None:
        super().__init__(
            rate_limiter=RateLimiter(min_interval=0.2, jitter=0.05),
            retry_config=RetryConfig(max_attempts=3, base_backoff=1.5),
            cache=cache,
        )
        self._google_key = (os.environ.get("GOOGLE_STREETVIEW_API_KEY") or "").strip()
        self._mapillary_token = (os.environ.get("MAPILLARY_TOKEN") or "").strip()

    def _cache_ttl(self) -> int:
        return _TTL_IMAGERY

    # -- readiness ---------------------------------------------------------
    @property
    def configured(self) -> bool:
        return bool(self._google_key or self._mapillary_token)

    def available(self) -> tuple[bool, str]:
        """(ready, reason-if-not), matching the convention in
        reconstruction_providers.py so callers can 503 with the reason."""
        if self.configured:
            return (True, "")
        return (
            False,
            "set GOOGLE_STREETVIEW_API_KEY (server-side key, no HTTP-referrer "
            "restriction) or MAPILLARY_TOKEN",
        )

    def providers(self) -> list[str]:
        out = []
        if self._google_key:
            out.append("google_streetview")
        if self._mapillary_token:
            out.append("mapillary")
        return out

    # -- Google Street View -------------------------------------------------
    async def _streetview(self, address: str, size: int) -> Optional[PropertyImage]:
        """Street View frame for an address, or None when Google has no coverage.

        The metadata endpoint is queried first and is free: it answers whether a
        panorama exists at all. Skipping it means billing for an image request
        that returns Google's grey "no imagery" placeholder — which is not a
        photo of the property, but is indistinguishable from one downstream.
        """
        params = {"location": address, "key": self._google_key}
        meta = await self._get_json(
            f"{_STREETVIEW_META}?{urllib.parse.urlencode(params)}", timeout=15
        )
        if not isinstance(meta, dict):
            return None

        status = str(meta.get("status", "")).upper()
        if status in ("REQUEST_DENIED", "OVER_QUERY_LIMIT"):
            raise ImageryAuthError(
                f"Google Street View rejected the key ({status}). A browser key "
                "with an HTTP-referrer restriction cannot be used server-side — "
                "GOOGLE_STREETVIEW_API_KEY needs an unrestricted or IP-restricted key."
            )
        if status != "OK":
            # ZERO_RESULTS / NOT_FOUND: genuinely no coverage. Not an error.
            return None

        edge = max(64, min(int(size), _MAX_EDGE))
        image_params = {
            "location": address,
            "size": f"{edge}x{edge}",
            # Prefer the exact panorama the metadata call resolved, so the image
            # is the frame we just confirmed exists rather than a fresh lookup
            # that may resolve differently.
            **({"pano": meta["pano_id"]} if meta.get("pano_id") else {}),
            "key": self._google_key,
        }
        return PropertyImage(
            url=f"{_STREETVIEW_IMAGE}?{urllib.parse.urlencode(image_params)}",
            source="google_streetview",
            attribution="Imagery © Google",
            interior=False,
            captured_lat=(meta.get("location") or {}).get("lat"),
            captured_lng=(meta.get("location") or {}).get("lng"),
            captured_at=meta.get("date"),
            metadata={"pano_id": meta.get("pano_id")} if meta.get("pano_id") else {},
        )

    # -- Mapillary ----------------------------------------------------------
    async def _mapillary(
        self, lat: float, lng: float, radius_m: int = 40
    ) -> Optional[PropertyImage]:
        """Nearest community street-level frame. CC-BY-SA, so attribution is
        returned with it and is not optional for the caller."""
        # Mapillary takes a bbox rather than a radius; ~1e-5 deg ≈ 1.1 m of
        # latitude, close enough at the scale of one parcel.
        d = radius_m * 1e-5
        params = {
            "fields": "id,thumb_1024_url,captured_at,geometry,creator",
            "bbox": f"{lng - d},{lat - d},{lng + d},{lat + d}",
            "limit": "1",
            "access_token": self._mapillary_token,
        }
        payload = await self._get_json(
            f"{_MAPILLARY_SEARCH}?{urllib.parse.urlencode(params)}", timeout=15
        )
        if not isinstance(payload, dict):
            return None
        items = payload.get("data") or []
        if not items:
            return None
        item = items[0]
        url = item.get("thumb_1024_url")
        if not url:
            return None
        coords = ((item.get("geometry") or {}).get("coordinates") or [None, None])
        creator = (item.get("creator") or {}).get("username") or "Mapillary contributors"
        return PropertyImage(
            url=str(url),
            source="mapillary",
            attribution=f"© {creator} / Mapillary (CC BY-SA 4.0)",
            interior=False,
            captured_lng=coords[0] if len(coords) > 0 else None,
            captured_lat=coords[1] if len(coords) > 1 else None,
            captured_at=str(item.get("captured_at") or "") or None,
            metadata={"image_id": item.get("id")},
        )

    # -- public -------------------------------------------------------------
    async def fetch(
        self,
        *,
        address: str,
        lat: Optional[float] = None,
        lng: Optional[float] = None,
        size: int = _MAX_EDGE,
    ) -> dict:
        """Exterior imagery for one address.

        Returns `matched: False` with a reason rather than an empty list, so a
        caller can tell "no imagery exists for this address" from "no provider
        is configured" — the same distinction the flood-zone and dataset-loaded
        surfaces already make. Never raises for absent coverage.
        """
        if not self.configured:
            raise ImageryConfigurationError(self.available()[1])
        if not address or not address.strip():
            raise DataIntegrationError("address is required")

        images: list[PropertyImage] = []

        if self._google_key:
            found = await self._streetview(address.strip(), size)
            if found:
                images.append(found)

        # Mapillary needs coordinates; it is a fallback for where Street View
        # has no car, so it only runs when Street View found nothing.
        if not images and self._mapillary_token and lat is not None and lng is not None:
            found = await self._mapillary(float(lat), float(lng))
            if found:
                images.append(found)

        return {
            "matched": bool(images),
            "address": address.strip(),
            "images": [i.as_dict() for i in images],
            "providers_configured": self.providers(),
            # Said explicitly so no caller has to infer it: nothing here shows
            # the inside of the home.
            "exterior_only": True,
            "reason": "" if images else (
                "No licensed street-level imagery covers this address. "
                "Interior photos require an MLS feed or an owner upload."
            ),
        }

    def normalize(self, raw: dict) -> dict:
        return raw
