"""
Oracle — Audit middleware and decorator layer.

Three primitives for mandatory mutation logging:

  1. @audit_action(category, action_template)
       Decorator for FastAPI endpoints.  Records one ledger entry after a
       successful response; template placeholders are filled from the Request.

  2. AuditMiddleware
       Starlette BaseHTTPMiddleware that auto-logs every POST/PUT/DELETE/PATCH
       request.  Skips /health and /docs.  Categorises /admin/* paths as
       ADMIN_ACTION; everything else as USER_STATE_CHANGE.

  3. audit_now(ctx, category, action, target_id, metadata)
       Thin async helper for explicit audit calls inside endpoint bodies.

All three write through the module-level `ledger` from audit_ledger.py via its
async `record()` method.

Usage
-----
# Decorator
@app.post("/api/export-leads")
@audit_action(AuditCategory.EXPORT_LEAD, "Export leads CSV")
async def export_leads(ctx: TenantContext = Depends(require_context)):
    ...

# Middleware (register once in server.py)
app.add_middleware(AuditMiddleware)

# Explicit inline audit
async def some_endpoint(ctx: TenantContext = Depends(require_context)):
    ...
    await audit_now(ctx, AuditCategory.DATA_DELETE, "delete_lead", target_id=lead_id)
    ...
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time
from typing import Callable, Optional

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response
from starlette.types import ASGIApp

from audit_ledger import AuditCategory, ledger
from tenancy import TenantContext

log = logging.getLogger("oracle.audit_middleware")

# ---------------------------------------------------------------------------
# Paths that produce high-frequency, non-security-relevant traffic.
# The middleware skips these to keep the ledger uncluttered.
# ---------------------------------------------------------------------------
_SKIP_PREFIXES: tuple[str, ...] = ("/health", "/docs", "/openapi", "/redoc")

# HTTP methods that constitute mutations — only these are auto-audited.
_MUTATION_METHODS: frozenset[str] = frozenset({"POST", "PUT", "DELETE", "PATCH"})


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _tenant_and_user(ctx: Optional[TenantContext]) -> tuple[Optional[str], Optional[str]]:
    """Return (tenant_id, user_id) from a TenantContext, or (None, None) for anonymous."""
    if ctx is None:
        return None, None
    return ctx.tenant_id, ctx.agent_id


def _fill_template(template: str, request: Request) -> str:
    """Substitute {method}, {path}, {client} placeholders in action_template.

    Any unrecognised placeholder is left verbatim rather than raising KeyError.
    """
    client_ip = request.client.host if request.client else "unknown"
    try:
        return template.format(
            method=request.method,
            path=request.url.path,
            client=client_ip,
        )
    except KeyError:
        return template


def _category_for_path(path: str) -> AuditCategory:
    """Pick a category based on whether the path lives under /admin."""
    if path.startswith("/admin"):
        return AuditCategory.ADMIN_ACTION
    return AuditCategory.USER_STATE_CHANGE


async def _record_safe(
    category: AuditCategory,
    action: str,
    tenant_id: Optional[str] = None,
    user_id: Optional[str] = None,
    target_id: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> None:
    """Await ledger.record() and swallow any exception so audit never breaks the app."""
    try:
        await ledger.record(
            category=category,
            action=action,
            tenant_id=tenant_id,
            user_id=user_id,
            target_id=target_id,
            metadata=metadata,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("Audit ledger write failed (degraded mode): %s", exc)


# ---------------------------------------------------------------------------
# 1. @audit_action decorator
# ---------------------------------------------------------------------------

def audit_action(
    category: AuditCategory,
    action_template: str,
) -> Callable:
    """Decorator that records a ledger entry after a successful endpoint call.

    Parameters
    ----------
    category:
        The AuditCategory that classifies this action.
    action_template:
        Human-readable description of the action.  Supports {method}, {path},
        and {client} placeholders filled from the incoming Request.

    The decorator:
    - Extracts TenantContext from the endpoint's resolved kwargs if present.
    - Falls back to tenant_id=None / user_id=None for unauthenticated endpoints.
    - Records on success only (no exception raised by the endpoint).
    - Fires the ledger write as a background task — does not delay the response.
    - Is compatible with both async and sync endpoint functions.
    """

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            # Locate Request and TenantContext from FastAPI-resolved kwargs.
            request: Optional[Request] = None
            ctx: Optional[TenantContext] = None

            for v in kwargs.values():
                if isinstance(v, Request):
                    request = v
                elif isinstance(v, TenantContext):
                    ctx = v

            # Fallback: scan positional args as well.
            for a in args:
                if isinstance(a, Request) and request is None:
                    request = a
                elif isinstance(a, TenantContext) and ctx is None:
                    ctx = a

            # Call the actual endpoint.
            if asyncio.iscoroutinefunction(func):
                result = await func(*args, **kwargs)
            else:
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(None, functools.partial(func, *args, **kwargs))

            # Build audit entry fields.
            tenant_id, user_id = _tenant_and_user(ctx)
            action = _fill_template(action_template, request) if request is not None else action_template

            meta: dict = {}
            if request is not None:
                meta["method"] = request.method
                meta["path"] = request.url.path
            if isinstance(result, Response):
                meta["status_code"] = result.status_code

            # Fire and forget — do not block the response.
            asyncio.get_event_loop().create_task(
                _record_safe(
                    category=category,
                    action=action,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    metadata=meta,
                )
            )

            return result

        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# 2. AuditMiddleware
# ---------------------------------------------------------------------------

class AuditMiddleware(BaseHTTPMiddleware):
    """Starlette middleware that auto-audits every mutation request.

    Logs POST/PUT/DELETE/PATCH requests — skips /health, /docs, /openapi,
    /redoc.  The ledger entry is written after the response is sent so that
    the status_code is included.

    The middleware has no access to the resolved TenantContext (that lives
    inside the endpoint), so tenant_id/user_id are not populated here.
    For per-user attribution on sensitive endpoints use @audit_action or
    audit_now() instead.

    Registration (server.py):
        app.add_middleware(AuditMiddleware)
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # Pass through non-mutation methods and skipped paths immediately.
        if request.method not in _MUTATION_METHODS:
            return await call_next(request)

        path = request.url.path
        if any(path.startswith(prefix) for prefix in _SKIP_PREFIXES):
            return await call_next(request)

        t0 = time.monotonic()
        response = await call_next(request)
        elapsed_ms = round((time.monotonic() - t0) * 1000, 1)

        client_ip = request.client.host if request.client else "unknown"
        category = _category_for_path(path)

        meta = {
            "method": request.method,
            "path": path,
            "status_code": response.status_code,
            "response_time_ms": elapsed_ms,
            "client_ip": client_ip,
        }

        # Dispatch off the hot path — do not await.
        asyncio.get_event_loop().create_task(
            _record_safe(
                category=category,
                action=f"{request.method} {path}",
                metadata=meta,
            )
        )

        return response


# ---------------------------------------------------------------------------
# 3. audit_now — explicit inline helper
# ---------------------------------------------------------------------------

async def audit_now(
    ctx: TenantContext,
    category: AuditCategory,
    action: str,
    target_id: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> None:
    """Convenience wrapper for explicit audit calls inside endpoint bodies.

    Parameters
    ----------
    ctx:
        The TenantContext from require_context() — provides tenant_id/agent_id.
    category:
        The AuditCategory that classifies this action.
    action:
        Free-form human-readable description, e.g. "delete_lead".
    target_id:
        Optional ID of the entity being acted upon (lead_id, property_id, …).
    metadata:
        Any additional key/value context to store alongside the entry.

    Example
    -------
    await audit_now(
        ctx,
        AuditCategory.DATA_DELETE,
        "delete_lead",
        target_id=str(lead_id),
        metadata={"reason": "user_request"},
    )
    """
    tenant_id, user_id = _tenant_and_user(ctx)
    await _record_safe(
        category=category,
        action=action,
        tenant_id=tenant_id,
        user_id=user_id,
        target_id=target_id,
        metadata=metadata,
    )
