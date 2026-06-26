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
# In-flight write tracking
#
# Audit writes run off the request hot path (no added latency), but a bare
# create_task() is unsafe twice over: CPython may garbage-collect a task that
# nothing holds a reference to, and a task still running when the process stops
# loses its ledger row. The audit ledger is compliance evidence, so we keep a
# strong reference to every in-flight write and drain the set on shutdown.
# ---------------------------------------------------------------------------

_pending: set[asyncio.Task] = set()


def _spawn_record(**kwargs) -> None:
    """Fire _record_safe as a tracked background task (strong-referenced until done)."""
    task = asyncio.create_task(_record_safe(**kwargs))
    _pending.add(task)
    task.add_done_callback(_pending.discard)


async def drain_pending(timeout: float = 5.0) -> None:
    """Await all in-flight audit writes. Call from the app lifespan shutdown so
    audit rows are not lost when the process stops."""
    if not _pending:
        return
    in_flight = list(_pending)
    log.info("Draining %d in-flight audit write(s) before shutdown.", len(in_flight))
    try:
        await asyncio.wait_for(
            asyncio.gather(*in_flight, return_exceptions=True), timeout=timeout
        )
    except asyncio.TimeoutError:
        log.warning(
            "Audit drain timed out after %.1fs — %d write(s) may be incomplete.",
            timeout, len(_pending),
        )


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

            # Tracked background task — off the hot path, but strong-referenced
            # and drained on shutdown so the write is not lost.
            _spawn_record(
                category=category,
                action=action,
                tenant_id=tenant_id,
                user_id=user_id,
                metadata=meta,
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

        # Attribute the mutation from the Bearer JWT when one is present.
        # decode_token is the same validation path the endpoints use, so a
        # forged token attributes nothing (entry still records, unattributed).
        tenant_id = user_id = None
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            try:
                from auth import decode_token

                claims = decode_token(auth_header.removeprefix("Bearer ").strip())
                tenant_id = claims.get("tenant_id")
                user_id = claims.get("sub")
            except Exception:  # noqa: BLE001 — invalid/expired token
                pass

        meta = {
            "method": request.method,
            "path": path,
            "status_code": response.status_code,
            "response_time_ms": elapsed_ms,
            "client_ip": client_ip,
        }

        # Tracked background task — off the hot path, strong-referenced, drained
        # on shutdown (see _spawn_record / drain_pending).
        _spawn_record(
            category=category,
            action=f"{request.method} {path}",
            tenant_id=tenant_id,
            user_id=user_id,
            metadata=meta,
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
