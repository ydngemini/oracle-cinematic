"""Exact-origin CORS configuration shared by HTTP and WebSocket entrypoints."""

from __future__ import annotations

import os
from urllib.parse import urlsplit


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


def _origin_of(url: str) -> str | None:
    parts = urlsplit((url or "").strip())
    if parts.scheme in ("http", "https") and parts.hostname:
        return f"{parts.scheme}://{parts.netloc.rsplit('@', 1)[-1]}"
    return None


def get_allowed_origins(configured_origins: str | None = None) -> list[str]:
    """Return a de-duplicated exact-origin allowlist.

    ``*`` is deliberately rejected because Neoh allows credentialed browser
    requests. ``ORACLE_CORS_ORIGINS`` is a comma-separated list of exact origins.

    The localhost defaults exist for development ONLY. They used to apply in
    every environment whenever the variable was unset — and the App Platform
    spec never set it — so production granted credentialed CORS and WebSocket
    access to any page served from a developer's localhost port, while
    refusing its own origin (security review WEB-1). Outside development the
    fallback is the public base URL's origin; production with neither refuses
    to start, and production never accepts a plain-http or loopback origin.
    """
    import config

    raw_origins = (
        os.getenv("ORACLE_CORS_ORIGINS")
        if configured_origins is None
        else configured_origins
    )
    if raw_origins is not None and raw_origins.strip():
        candidates: tuple[str, ...] = tuple(raw_origins.split(","))
    elif config.IS_DEV:
        candidates = DEFAULT_CORS_ORIGINS
    else:
        derived = _origin_of(os.getenv("ORACLE_PUBLIC_BASE_URL", ""))
        candidates = (derived,) if derived else ()
    origins = list(dict.fromkeys(origin.strip() for origin in candidates if origin and origin.strip()))
    if "*" in origins:
        raise RuntimeError(
            "ORACLE_CORS_ORIGINS must contain exact origins; wildcard CORS is not allowed"
        )
    if config.IS_PROD:
        if not origins:
            raise RuntimeError(
                "Production needs ORACLE_CORS_ORIGINS (or ORACLE_PUBLIC_BASE_URL) — "
                "refusing to start with no trusted browser origin."
            )
        unsafe = [
            o for o in origins
            if not o.startswith("https://")
            or (urlsplit(o).hostname or "") in ("localhost", "127.0.0.1", "::1")
        ]
        if unsafe:
            raise RuntimeError(
                f"Production CORS origins must be https and non-loopback: {unsafe}"
            )
    return origins
