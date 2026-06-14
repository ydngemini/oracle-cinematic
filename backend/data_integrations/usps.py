"""
data_integrations/usps.py — USPS Web Tools address standardization.

API: https://production.shippingapis.com/ShippingAPI.dll
Requires USPS_USER_ID env var (free registration).
Cache TTL: 30 days.
"""

from __future__ import annotations

import asyncio
import os
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Optional

from .base import DataSource, RateLimiter, RetryConfig
from .cache import IntegrationCache, TTL

_USPS_API_URL = "https://production.shippingapis.com/ShippingAPI.dll"
_USER_ID = os.environ.get("USPS_USER_ID", "")


def _xml_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
         .replace('"', "&quot;")
    )


class USPSAddressSource(DataSource):
    source_name = "usps"

    def __init__(self, cache: Optional[IntegrationCache] = None):
        super().__init__(
            rate_limiter=RateLimiter(min_interval=0.2, jitter=0.1),
            retry_config=RetryConfig(max_attempts=3, base_backoff=3.0),
            cache=cache,
        )

    def _cache_ttl(self) -> int:
        return TTL["usps"]

    async def fetch(self, *, address1: str = "", address2: str, city: str, state: str, zip5: str = "") -> Optional[dict]:
        if not _USER_ID:
            self._log.debug("USPS_USER_ID not set — skipping validation")
            return None

        xml_body = (
            f'<AddressValidateRequest USERID="{_USER_ID}">'
            f'<Revision>1</Revision>'
            f'<Address ID="0">'
            f'<Address1>{_xml_escape(address1)}</Address1>'
            f'<Address2>{_xml_escape(address2)}</Address2>'
            f'<City>{_xml_escape(city)}</City>'
            f'<State>{_xml_escape(state.upper())}</State>'
            f'<Zip5>{_xml_escape(zip5)}</Zip5>'
            f'<Zip4></Zip4>'
            f'</Address>'
            f'</AddressValidateRequest>'
        )
        params = urllib.parse.urlencode({"API": "Verify", "XML": xml_body})
        url = f"{_USPS_API_URL}?{params}"

        try:
            await self._limiter.acquire()
            self._metrics["requests"] += 1
            req = urllib.request.Request(url)

            def _blocking():
                with urllib.request.urlopen(req, timeout=15) as resp:
                    return resp.read().decode("utf-8", errors="replace")

            body = await asyncio.to_thread(_blocking)
            return {"xml": body}
        except Exception as e:
            self._log.warning("USPS validation failed: %s", e)
            return None

    def normalize(self, raw: dict) -> Optional[dict]:
        try:
            root = ET.fromstring(raw["xml"])
            addr = root.find(".//Address")
            if addr is None:
                return None

            error = addr.find("Error")
            if error is not None:
                return None

            a1 = addr.findtext("Address1", "").strip()
            a2 = addr.findtext("Address2", "").strip()
            city = addr.findtext("City", "").strip()
            state = addr.findtext("State", "").strip()
            zip5 = addr.findtext("Zip5", "").strip()
            zip4 = addr.findtext("Zip4", "").strip()
            dpv = addr.findtext("DPVFootnotes", "").strip()

            parts = [p for p in [a1, a2] if p]
            street = ", ".join(parts)
            zip_full = f"{zip5}-{zip4}" if zip4 else zip5
            standardized = f"{street}, {city}, {state} {zip_full}".strip(", ")

            return {
                "address1": a1,
                "address2": a2,
                "city": city,
                "state": state,
                "zip5": zip5,
                "zip4": zip4,
                "standardized": standardized,
                "dpv_footnote": dpv,
                "source": "usps",
            }
        except ET.ParseError as e:
            self._log.warning("USPS XML parse error: %s", e)
            return None

    async def validate(self, address2: str, city: str, state: str, address1: str = "", zip5: str = "") -> Optional[dict]:
        raw_key = f"{address1}|{address2}|{city}|{state}|{zip5}".upper()
        key = f"usps:{urllib.parse.quote(raw_key)}"
        return await self.get(key, address1=address1, address2=address2, city=city, state=state, zip5=zip5)
