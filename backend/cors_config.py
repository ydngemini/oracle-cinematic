"""Exact-origin CORS configuration shared by HTTP and WebSocket entrypoints."""

from __future__ import annotations

import os


# Vite uses 5173 for development and 4173 for ``vite preview``. Keep both
# hostname forms because browsers compare the serialized Origin exactly.
DEFAULT_CORS_ORIGINS: tuple[str, ...] = (
    "http://localhost:3000",
    "http://localhost:4173",
    "http://localhost:5173",
    "http://localhost:5174",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:4173",
    "http://127.0.0.1:5173",
    "http://127.0.0.1:5174",
)


def get_allowed_origins(configured_origins: str | None = None) -> list[str]:
    """Return a de-duplicated exact-origin allowlist.

    ``*`` is deliberately rejected because Neoh allows credentialed browser
    requests. Production can replace the local defaults through
    ``ORACLE_CORS_ORIGINS`` with a comma-separated list of exact origins.
    """

    raw_origins = (
        os.getenv("ORACLE_CORS_ORIGINS")
        if configured_origins is None
        else configured_origins
    )
    candidates = (
        DEFAULT_CORS_ORIGINS
        if raw_origins is None or not raw_origins.strip()
        else tuple(raw_origins.split(","))
    )
    origins = list(dict.fromkeys(origin.strip() for origin in candidates if origin.strip()))
    if "*" in origins:
        raise RuntimeError(
            "ORACLE_CORS_ORIGINS must contain exact origins; wildcard CORS is not allowed"
        )
    return origins
