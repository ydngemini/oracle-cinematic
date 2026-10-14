"""Public security and privacy contacts (L17-20, ISO A.6.8 / A.5.5).

Owner decision 2026-10-14: security@ and privacy@ mailboxes on Neoh's domain,
created by the owner. Nothing is published until they are configured:

- ORACLE_SECURITY_CONTACT  → GET /.well-known/security.txt (RFC 9116). Unset
  (or no valid entry) → 404, so a scanner never sees a contact that does not
  exist.
- ORACLE_PRIVACY_CONTACT   → `privacy_contact` in the privacy-request answer
  (POST /api/privacy/requests), the one place the app tells a brokerage how
  to reach Neoh about a data-subject request. Unset → omitted.

Each value is a comma-separated list of `mailto:` / `https:` / `tel:` URIs or
bare email addresses (made into `mailto:`). Anything else is ignored, never
echoed.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Response, status

router = APIRouter(tags=["Security contact"])

# RFC 9116 §2.5.5: Expires should be less than a year away. Computed per
# request, so the file never goes stale however long a release runs.
EXPIRES_IN = timedelta(days=365)
_EMAIL = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
_LINE_BREAKS = re.compile(r"[\r\n]")


def _contacts(raw: str) -> list[str]:
    out: list[str] = []
    for item in (raw or "").split(","):
        value = item.strip()
        if not value or _LINE_BREAKS.search(value):
            continue
        if _EMAIL.match(value):
            out.append(f"mailto:{value}")
            continue
        parsed = urlparse(value)
        if parsed.scheme == "mailto" and _EMAIL.match(parsed.path):
            out.append(value)
        elif parsed.scheme == "https" and parsed.netloc:
            out.append(value)
        elif parsed.scheme == "tel" and re.fullmatch(r"\+?[0-9-]{4,20}", parsed.path or ""):
            out.append(value)
    return out


def security_contacts() -> list[str]:
    return _contacts(os.getenv("ORACLE_SECURITY_CONTACT", ""))


def privacy_contact() -> Optional[str]:
    """The first configured privacy contact (a mailto:/https: URI), or None."""
    found = _contacts(os.getenv("ORACLE_PRIVACY_CONTACT", ""))
    return found[0] if found else None


def render_security_txt(contacts: list[str], *, now: Optional[datetime] = None,
                        canonical: Optional[str] = None) -> str:
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(microsecond=0)
    lines = [f"Contact: {c}" for c in contacts]
    lines.append(f"Expires: {(moment + EXPIRES_IN).strftime('%Y-%m-%dT%H:%M:%SZ')}")
    lines.append("Preferred-Languages: en")
    if canonical:
        lines.append(f"Canonical: {canonical}")
    return "\n".join(lines) + "\n"


def _canonical() -> Optional[str]:
    try:
        import config

        base = config.public_base_url().rstrip("/")
    except Exception:  # noqa: BLE001 — Canonical is optional; never fail the file over it
        return None
    return f"{base}/.well-known/security.txt" if base.startswith("https://") else None


@router.get("/.well-known/security.txt", include_in_schema=False)
async def security_txt() -> Response:
    contacts = security_contacts()
    if not contacts:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not Found")
    return Response(
        render_security_txt(contacts, canonical=_canonical()),
        media_type="text/plain; charset=utf-8",
        headers={"Cache-Control": "public, max-age=3600"},
    )
