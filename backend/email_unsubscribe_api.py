"""Public email unsubscribe: the link in every commercial email footer and the
RFC 8058 one-click target named by its List-Unsubscribe header.

    GET  /api/public/email/unsubscribe/{token}  confirm page (or JSON); changes
         nothing, because mail scanners and link previewers fetch every URL in
         a message and must not unsubscribe anyone by doing so.
    POST /api/public/email/unsubscribe/{token}  records the opt-out, no login.
         Mailbox providers send `List-Unsubscribe=One-Click` as the form body
         (RFC 8058 §3.1); the confirm page's button posts the same. The body is
         not required — the token is the whole capability — and is never read.

No session, no cookie, no Neoh account: the person unsubscribing is a client
or lead of a brokerage, not a user. The token (commercial_email.make_token) is
an authenticated ciphertext of brokerage + address, so a forged or altered one
reads as "not valid" and the URL carries no personal data. Repeating a POST is
harmless (ConsentLedger.suppress is ON CONFLICT DO NOTHING) and answers the
same, so a provider's retry never errors.

CSRF: exempt (csrf_middleware.CSRF_EXEMPT_PATHS). CSRF defends an ambient
browser credential; this route reads none — it ignores cookies and sessions —
so a cross-site POST can do nothing a direct POST cannot, and RFC 8058 POSTs
come from the mailbox provider's servers, which carry no CSRF token by design.
Rate-limited per source IP (rate_limit_middleware, ORACLE_UNSUBSCRIBE_RATE_LIMIT)
and the token is masked in access/audit logs (log_redaction).
"""

from __future__ import annotations

import html
import logging

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

import commercial_email

log = logging.getLogger("oracle.email_unsubscribe")

router = APIRouter(prefix="/api/public/email/unsubscribe", tags=["Public email unsubscribe"])

# The API-wide CSP is `default-src 'none'; form-action 'none'` (server.py). This
# page needs its own inline style and a form that posts back to itself, and
# nothing else: no script, no images, no frames.
_PAGE_HEADERS = {
    "Content-Security-Policy": ("default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
                                "frame-ancestors 'none'; base-uri 'none'"),
    "Cache-Control": "no-store",
    "X-Robots-Tag": "noindex, nofollow",
}

_STYLE = ("body{font:16px/1.5 system-ui,sans-serif;margin:0;padding:24px 16px;background:#f6f7f9;color:#14171a}"
          "main{max-width:32rem;margin:0 auto;background:#fff;border-radius:12px;padding:24px}"
          "h1{font-size:1.25rem;margin:0 0 12px}button{font:inherit;padding:10px 18px;border-radius:8px;"
          "border:0;background:#14171a;color:#fff;cursor:pointer}"
          "@media (prefers-color-scheme:dark){body{background:#0f1113;color:#e8eaed}"
          "main{background:#1a1d21}button{background:#e8eaed;color:#14171a}}")

INVALID_MESSAGE = ("This unsubscribe link is not valid. To stop these emails, reply to the email "
                   "and ask to be removed.")


def _wants_json(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    return "application/json" in accept and "text/html" not in accept


def _page(title: str, body_html: str, status_code: int) -> HTMLResponse:
    doc = ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
           "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
           f"<title>{html.escape(title)}</title><style>{_STYLE}</style></head>"
           f"<body><main>{body_html}</main></body></html>")
    return HTMLResponse(doc, status_code=status_code, headers=_PAGE_HEADERS)


def _invalid(request: Request) -> Response:
    if _wants_json(request):
        return JSONResponse({"valid": False, "detail": INVALID_MESSAGE}, status_code=404,
                            headers={"Cache-Control": "no-store"})
    return _page("Link not valid", f"<h1>Link not valid</h1><p>{html.escape(INVALID_MESSAGE)}</p>", 404)


def _sender_label(name: str) -> str:
    return " ".join(str(name or "").split()) or "this brokerage"


@router.get("/{token}")
async def confirm_unsubscribe(token: str, request: Request) -> Response:
    parsed = commercial_email.read_token(token)
    if parsed is None:
        return _invalid(request)
    tenant_id, _email = parsed
    try:
        sender = _sender_label(await commercial_email.tenant_name(tenant_id))
    except Exception as exc:  # noqa: BLE001 - the page still works without the name
        log.warning("unsubscribe page: brokerage name unavailable (%s)", type(exc).__name__)
        sender = "this brokerage"
    if _wants_json(request):
        return JSONResponse({"valid": True, "sender": sender, "method": "POST"},
                            headers={"Cache-Control": "no-store"})
    name = html.escape(sender)
    action = html.escape(commercial_email.UNSUBSCRIBE_PATH + token, quote=True)
    return _page(
        "Unsubscribe",
        f"<h1>Unsubscribe from {name} emails?</h1>"
        f"<p>You will stop receiving email from every agent at {name} sent through Neoh.</p>"
        f"<form method=\"post\" action=\"{action}\">"
        f"<input type=\"hidden\" name=\"List-Unsubscribe\" value=\"One-Click\">"
        f"<button type=\"submit\">Unsubscribe</button></form>",
        200,
    )


@router.post("/{token}")
async def one_click_unsubscribe(token: str, request: Request) -> Response:
    parsed = commercial_email.read_token(token)
    if parsed is None:
        return _invalid(request)
    tenant_id, email = parsed
    try:
        await commercial_email.record_unsubscribe(tenant_id, email)
    except Exception as exc:  # noqa: BLE001 - say so; a 5xx makes the provider retry
        log.error("unsubscribe NOT recorded for tenant %s (%s)", tenant_id, type(exc).__name__)
        if _wants_json(request):
            return JSONResponse({"unsubscribed": False, "detail": "Try again in a few minutes."},
                                status_code=503, headers={"Cache-Control": "no-store"})
        return _page("Try again", "<h1>We could not record that</h1>"
                     "<p>Please try the link again in a few minutes, or reply to the email and ask "
                     "to be removed.</p>", 503)
    if _wants_json(request):
        return JSONResponse({"unsubscribed": True, "channel": "email"}, headers={"Cache-Control": "no-store"})
    return _page("Unsubscribed", "<h1>You are unsubscribed</h1>"
                 "<p>You will not receive more email from this brokerage through Neoh. "
                 "It can take a moment for messages already on their way to stop.</p>", 200)
