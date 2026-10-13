"""The one switch for legacy MLS (owner decision D-2026-10-12-01: MLS is out
of scope; prompt 11 T11.01).

Off unless ``ORACLE_MLS_ENABLED`` is truthy. While off, every MLS route answers
404, the RESO/Bridge sync jobs skip, the AI's listing tools search only the
brokerage's own listings and non-MLS market data, and the health snapshot
reports MLS as not configured. Nothing is deleted: the code, the cached MLS
listing rows and the migrations stay, so turning it back on is
the owner's decision and one variable away.
"""

from __future__ import annotations

import os

from fastapi import HTTPException, status

NOT_OFFERED = "MLS is not offered on this deployment (owner decision D-2026-10-12-01)."


def mls_enabled() -> bool:
    return (os.getenv("ORACLE_MLS_ENABLED") or "").strip().lower() in {"1", "true", "yes", "on"}


async def require_mls_enabled() -> None:
    if not mls_enabled():
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_OFFERED)
