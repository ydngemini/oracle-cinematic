"""Google OAuth provider client for the command router's calendar connect flow.

Moved verbatim out of commands_api.py (external audit 35-02, slice 1): the
client configuration, PKCE, the token exchange/refresh/revoke calls against
Google, the post-callback return URL and the browser-binding HMAC. It holds no
tenant state and reads no database rows; the routes that use it
(/api/commands/providers/google/oauth/start and /callback), the stored
credential rows and the token loader stay in commands_api.

commands_api re-exports every name its routes, privacy_lifecycle and the tests
use, and its routes call them through commands_api's own globals, so
`monkeypatch.setattr(commands_api, "<name>", ...)` keeps taking effect for
those callers. A caller added here that calls another function of this module
resolves it in this module, not through commands_api.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import os
import urllib.parse
from datetime import timedelta
from typing import Any

import aiohttp
from fastapi import HTTPException


_GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"
# Calendar only. Mail never leaves through a Google account, so requesting
# gmail.send would be an unused grant over an agent's whole mailbox.
_GOOGLE_SCOPES = (
    "openid",
    "email",
    "https://www.googleapis.com/auth/calendar.events",
)
_OAUTH_STATE_TTL = timedelta(minutes=10)


class GoogleOAuthError(RuntimeError):
    """Safe provider-linking error that never contains client credentials."""


def _google_oauth_settings() -> tuple[str, str, str]:
    client_id = os.getenv("GOOGLE_CLIENT_ID", "").strip()
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()
    redirect_uri = os.getenv("GOOGLE_OAUTH_REDIRECT_URI", "").strip()
    if not redirect_uri:
        public_base = os.getenv("ORACLE_PUBLIC_BASE_URL", "").rstrip("/")
        if public_base:
            redirect_uri = f"{public_base}/api/commands/providers/google/oauth/callback"
    if not all((client_id, client_secret, redirect_uri)):
        raise HTTPException(
            status_code=503,
            detail="Google OAuth client configuration is incomplete.",
        )
    if not redirect_uri.startswith("https://") and not (
        os.getenv("ORACLE_ENV", "").lower() in {"dev", "development", "local", "test"}
        and redirect_uri.startswith("http://")
    ):
        raise HTTPException(status_code=503, detail="Google OAuth redirect URI must use HTTPS.")
    return client_id, client_secret, redirect_uri


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


async def _exchange_google_oauth_code(
    *,
    code: str,
    code_verifier: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
) -> dict[str, Any]:
    timeout = aiohttp.ClientTimeout(total=15, connect=5, sock_read=10)
    payload = {
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
        "code_verifier": code_verifier,
    }
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(_GOOGLE_TOKEN_URL, data=payload) as response:
                data = await response.json(content_type=None)
                if response.status < 200 or response.status >= 300:
                    reason = str(data.get("error") or "token_exchange_failed")[:120]
                    raise GoogleOAuthError(f"Google rejected the authorization code: {reason}")
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise GoogleOAuthError("Google OAuth token service was unavailable.") from exc
    if not isinstance(data, dict) or not str(data.get("access_token") or ""):
        raise GoogleOAuthError("Google OAuth response did not include an access token.")
    return data


async def _refresh_google_oauth_token(
    *, refresh_token: str, client_id: str, client_secret: str
) -> dict[str, Any]:
    timeout = aiohttp.ClientTimeout(total=15, connect=5, sock_read=10)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                _GOOGLE_TOKEN_URL,
                data={
                    "refresh_token": refresh_token,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "grant_type": "refresh_token",
                },
            ) as response:
                data = await response.json(content_type=None)
                if response.status < 200 or response.status >= 300:
                    reason = str(data.get("error") or "refresh_failed")[:120]
                    raise GoogleOAuthError(f"Google rejected the refresh token: {reason}")
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise GoogleOAuthError("Google OAuth token service was unavailable.") from exc
    if not isinstance(data, dict) or not str(data.get("access_token") or ""):
        raise GoogleOAuthError("Google OAuth refresh did not include an access token.")
    return data


async def revoke_google_token(token: str) -> str:
    """Revoke a Google OAuth grant at Google (offboarding, erasure).

    Revoking the refresh token ends the whole grant, so a copy of an access
    token taken earlier stops refreshing too. Google answers 400 invalid_token
    for a grant that is already gone — that is success for an idempotent
    revoke. Returns "revoked" or "already_invalid".
    """
    import recovery_mode

    recovery_mode.guard("revoke a Google account connection")
    timeout = aiohttp.ClientTimeout(total=15, connect=5, sock_read=10)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(_GOOGLE_REVOKE_URL, data={"token": token}) as response:
                if 200 <= response.status < 300:
                    return "revoked"
                data = await response.json(content_type=None)
                if response.status == 400 and isinstance(data, dict) and data.get("error") == "invalid_token":
                    return "already_invalid"
                reason = str((data or {}).get("error") if isinstance(data, dict) else "revoke_failed")[:120]
                raise GoogleOAuthError(f"Google refused the revoke: {reason}")
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise GoogleOAuthError("Google OAuth revoke service was unavailable.") from exc


def _oauth_return_url(return_path: str, outcome: str) -> str:
    import config as _config

    base = _config.public_base_url()
    parsed = urllib.parse.urlsplit(return_path)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query.append(("google", outcome))
    return base + urllib.parse.urlunsplit(
        ("", "", parsed.path or "/", urllib.parse.urlencode(query), parsed.fragment)
    )


_OAUTH_BINDING_COOKIE = "neoh_oauth_binding"
_OAUTH_BINDING_PATH = "/api/commands/providers/google/oauth"


def _oauth_browser_binding(state_hash: str) -> str:
    """HMAC of the state under the app secret: proof that the browser finishing
    the flow is the one that started it (review AUTH-9)."""
    from auth import SECRET_KEY

    return hmac.new(SECRET_KEY.encode(), f"google-oauth:{state_hash}".encode(), hashlib.sha256).hexdigest()
