"""CSRF protection middleware for Oracle API.

Provides double-submit cookie CSRF protection for state-changing operations.
In production, all POST/PUT/DELETE/PATCH requests require a valid CSRF token.
"""

from __future__ import annotations

import os
import secrets
from typing import Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

import config

# CSRF token header name
CSRF_HEADER = "X-CSRF-Token"
CSRF_COOKIE = "csrf_token"

# Token length in bytes
TOKEN_LENGTH = 32

# Methods that require CSRF protection
PROTECTED_METHODS = {"POST", "PUT", "DELETE", "PATCH"}

# Paths exempt from CSRF (API uses Bearer token auth)
CSRF_EXEMPT_PATHS = {
    "/auth/login",
    "/auth/register",
    "/auth/forgot",
    "/auth/reset",
    "/auth/verify",
    "/api/commands/webhooks/",
    # Twilio posts these; they authenticate by X-Twilio-Signature over the full
    # URL + params, not by session cookie. Without the exemption every inbound
    # call, agent-dial status callback and live-agent transfer was rejected 403
    # by this middleware before its signature check ever ran.
    "/api/telephony/webhooks/",
    "/api/public/lead-intake/",
    # Unauthenticated client capture: the single-use link token IS the
    # capability. There is no ambient session credential for an attacker to
    # ride, which is the only thing CSRF defends against.
    "/api/public/property-upload/",
    # Portal telemetry, same reasoning as property-upload above. The homeowner's
    # session is a bearer JWT handed back in a response body and replayed in the
    # Authorization header; no cookie is set, so there is no ambient credential
    # for a third-party page to ride — which is the only thing CSRF defends
    # against. Listed as the exact path, NOT "/portal/", because /portal/links
    # is an agent-authenticated route that mints capability tokens and must keep
    # its protection.
    "/portal/activity",
    "/billing/webhook",
}


def _generate_csrf_token() -> str:
    return secrets.token_urlsafe(TOKEN_LENGTH)


def issue_csrf_cookie(response: Response) -> str:
    """Issue a host-only double-submit token and return it to the SPA bootstrap."""
    token = _generate_csrf_token()
    response.set_cookie(
        key=CSRF_COOKIE,
        value=token,
        httponly=False,
        secure=not config.IS_DEV,
        samesite="lax" if config.IS_DEV else "none",
        path="/",
    )
    return token


def get_or_issue_csrf_cookie(request: Request, response: Response) -> str:
    """Return the browser's current token, issuing one only when it is absent.

    Reusing the cookie is important for multi-tab browsers: a bootstrap in one
    tab must not rotate the token out from under an in-flight mutation in
    another tab.
    """
    token = request.cookies.get(CSRF_COOKIE, "")
    if 32 <= len(token) <= 128 and all(
        character.isalnum() or character in "-_" for character in token
    ):
        return token
    return issue_csrf_cookie(response)


def _validate_csrf_token(cookie_token: str, header_token: str) -> bool:
    if not cookie_token or not header_token:
        return False
    return secrets.compare_digest(cookie_token, header_token)


def _response_sets_csrf_cookie(response: Response) -> bool:
    """Return True when the endpoint already issued the bootstrap cookie."""
    cookie_prefix = f"{CSRF_COOKIE}="
    return any(
        header.lstrip().startswith(cookie_prefix)
        for header in response.headers.getlist("set-cookie")
    )


class CSRFMiddleware(BaseHTTPMiddleware):
    """CSRF protection middleware using double-submit cookie pattern."""

    def __init__(self, app, enabled: bool = True):
        super().__init__(app)
        self.enabled = enabled

    def _is_exempt(self, path: str) -> bool:
        for exempt in CSRF_EXEMPT_PATHS:
            if path.startswith(exempt):
                return True
        return False

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        if not self.enabled:
            return await call_next(request)

        path = request.url.path

        # Skip WebSocket
        if request.scope.get("type") == "websocket":
            return await call_next(request)

        # Skip exempt paths
        if self._is_exempt(path):
            return await call_next(request)

        # Skip GET/HEAD/OPTIONS (safe methods)
        if request.method not in PROTECTED_METHODS:
            response = await call_next(request)
            # Set CSRF cookie on safe responses for next request
            if (
                CSRF_COOKIE not in request.cookies
                and not _response_sets_csrf_cookie(response)
            ):
                issue_csrf_cookie(response)
            return response

        # For protected methods, validate CSRF token
        cookie_token = request.cookies.get(CSRF_COOKIE, "")
        header_token = request.headers.get(CSRF_HEADER, "")

        if not _validate_csrf_token(cookie_token, header_token):
            return JSONResponse(
                status_code=403,
                content={
                    "detail": "CSRF validation failed. Refresh the page and try again.",
                    "code": "CSRF_TOKEN_INVALID",
                },
            )

        return await call_next(request)
