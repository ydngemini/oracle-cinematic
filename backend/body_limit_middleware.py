"""Bound every request body before anything reads it.

FastAPI parses a JSON or multipart body BEFORE it resolves a route's
dependencies — authentication included — and Starlette's multipart parser caps
only non-file fields. With uvicorn serving the API directly (no proxy with a
body limit in front of it on App Platform), an anonymous client could make any
replica buffer or spool an arbitrarily large body and exhaust its memory or
ephemeral disk (security review UPL-1, 2026-10-01; confirmed: a 50 MB part was
fully parsed before the 401).

This is a pure ASGI middleware, so it sees the body as it streams:
  * a declared Content-Length over the route's cap is refused with 413 before
    a byte is read;
  * a chunked or lying body is counted as it arrives and cut off at the cap.

Caps are per route family and sized from the limits the handlers themselves
enforce, so no legitimate upload is refused here that the handler would accept.
"""
from __future__ import annotations

import json
import os
import re

_MiB = 1024 * 1024

# Everything not listed: JSON APIs. The largest legitimate JSON body found is
# the contract workspace input (160 KB); 2 MiB leaves an order of magnitude.
DEFAULT_LIMIT = int(os.getenv("ORACLE_MAX_BODY_BYTES", str(2 * _MiB)))

# (path pattern, cap) — first match wins. Each cap = the handler's own file
# limit x the files it accepts, plus multipart overhead.
_ROUTE_LIMITS: tuple[tuple[re.Pattern, int], ...] = tuple(
    (re.compile(pattern), limit)
    for pattern, limit in (
        # property-view media: one video up to MAX_VIDEO_BYTES (512 MB), or a batch of photos.
        (r"^/api/crm/property-view/media$", 520 * _MiB),
        (r"^/api/public/property-upload/[^/]+$", 520 * _MiB),
        (r"^/api/crm/property-view/scan$", 70 * _MiB),          # MAX_SCAN_BYTES 64 MB
        (r"^/api/ai/chat/attachments$", 64 * _MiB),             # 5 x 12 MB
        (r"^/api/crm/(leads|listings)/[^/]+/media$", 16 * _MiB),  # media_api MAX_BYTES 12 MB
        (r"^/api/crm/floorplan/extract-image$", 26 * _MiB),     # MAX_PLAN_IMAGE_BYTES 25 MB
        (r"^/api/voice/log-walkthrough/[^/]+$", 26 * _MiB),     # MAX_AUDIO_BYTES 25 MB
        (r"^/api/messaging/business-number/documents/[^/]+$", 6 * _MiB),  # Telnyx 5 MB
    )
)


def limit_for(path: str) -> int:
    for pattern, limit in _ROUTE_LIMITS:
        if pattern.match(path):
            return limit
    return DEFAULT_LIMIT


class _BodyTooLarge(Exception):
    pass


async def _send_413(send, limit: int) -> None:
    body = json.dumps({
        "detail": f"Request body exceeds the {max(1, limit // _MiB)} MB limit for this endpoint.",
    }).encode()
    await send({
        "type": "http.response.start",
        "status": 413,
        "headers": [(b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"connection", b"close")],
    })
    await send({"type": "http.response.body", "body": body})


class BodyLimitMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") in ("GET", "HEAD", "OPTIONS"):
            return await self.app(scope, receive, send)

        limit = limit_for(scope.get("path", ""))
        for name, value in scope.get("headers", ()):
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = -1
                if declared < 0 or declared > limit:
                    return await _send_413(send, limit)
                break

        received = 0
        response_started = False

        async def counted_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise _BodyTooLarge()
            return message

        async def tracking_send(message):
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, counted_receive, tracking_send)
        except _BodyTooLarge:
            if not response_started:
                await _send_413(send, limit)
