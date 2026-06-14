"""
Oracle Data Integration Layer — 50-state market data, compliance, and enrichment.

Core modules:
  base.py            — Abstract DataSource with rate limiting + retry
  cache.py           — Redis L1 / PostgreSQL L2 hybrid cache
  geocoder.py        — Cascading geocoder (Census → Nominatim)
  fema_flood.py      — FEMA NFHL flood zone lookup
  census.py          — Census ACS 5-yr + TIGER/Line boundaries
  usps.py            — USPS Web Tools address validation
  school_districts.py — NCES EDGE + GreatSchools ratings
  scheduler.py       — Async worker pool with circuit breakers
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
