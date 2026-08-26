"""
Oracle Data Integration Layer — 50-state market data, compliance, and enrichment.

Core modules:
  base.py            — Abstract DataSource with rate limiting + retry
  cache.py           — Redis L1 / PostgreSQL L2 hybrid cache
  geocoder.py        — Cascading geocoder (Census → Nominatim)
  census.py          — Census ACS 5-yr + TIGER/Line boundaries
  scheduler.py       — Async worker pool with circuit breakers

STAGED, NOT WIRED — written but imported by nothing, so none of it executes:
  school_districts.py — NCES EDGE + GreatSchools ratings. The `school_districts`
                       table it would populate has no writer, so
                       /api/market/schools returns an empty list on every
                       deployment.

Treat the list above as a to-do, not an inventory of live capability. A
flood-zone client used to sit here too; it duplicated the wired lookup in
apis/property_data.py and the two drifted — only one of them had been fixed to
stop reporting "zone X, minimal hazard" for coordinates outside NFHL coverage.
It was deleted rather than kept in parallel. Wire a staged module here or delete
it; a second copy of a live integration is the failure mode to avoid.

usps.py was deleted on the same principle. It offered ZIP+4 and DPV
standardization, which nothing else here provides — but it had no importer, no
configured secret, and no test, and address→coordinates is already covered by
census_geocoder.py. Reviving it needs the licensing review noted in
docs/data-access-tiers.md before it needs code: USPS terms restrict address
validation to shipping-related use.
"""

from .base import DataSource, DataIntegrationError, RetryableError, RateLimiter, RetryConfig
from .cache import IntegrationCache, TTL

__all__ = [
    "DataSource",
    "DataIntegrationError",
    "RetryableError",
    "RateLimiter",
    "RetryConfig",
    "IntegrationCache",
    "TTL",
]
