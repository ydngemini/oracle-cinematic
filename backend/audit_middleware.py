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
import hashlib
import json
import logging
import time
from collections import defaultdict, deque
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
    if path.startswith(("/admin", "/api/admin")):
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

# Per-process detection is deliberately bounded; durable alerts are written to
# Postgres and can be correlated across ECS replicas by fingerprint/time.
_denial_windows: dict[tuple[str, str], deque[float]] = defaultdict(deque)
_denial_last_alert: dict[tuple[str, str], float] = {}
_DENIAL_WINDOW_SECONDS = 300.0
_DENIAL_THRESHOLD = 5
_MAX_ANOMALY_KEYS = 5_000


async def _record_anomaly_safe(
    *,
    tenant_id: Optional[str],
    user_id: Optional[str],
    source_ip: str,
    path: str,
    count: int,
) -> None:
    fingerprint = hashlib.sha256(
        f"repeated_access_denial:{source_ip}:{path}".encode("utf-8")
    ).hexdigest()
    evidence = {
        "denials": count,
        "window_seconds": int(_DENIAL_WINDOW_SECONDS),
        "status_codes": [401, 403],
    }
    try:
        from uuid import UUID

        from db.connection import tenant_tx
        from tenancy import Role, TenantContext

        platform_tenant = "00000000-0000-0000-0000-000000000000"
        try:
            persisted_tenant = str(UUID(str(tenant_id))) if tenant_id else platform_tenant
        except ValueError:
            persisted_tenant = platform_tenant
        ctx = TenantContext(
            agent_id="audit-anomaly-detector",
            tenant_id=platform_tenant,
            role=Role.PLATFORM_ADMIN,
        )
        async with tenant_tx(ctx) as conn:
            await conn.execute(
                """
                INSERT INTO audit_anomaly_alerts (
                    tenant_id,anomaly_type,severity,fingerprint,actor_id,
                    source_ip,route,evidence
                ) VALUES ($1::uuid,'repeated_access_denial','high',$2,$3,$4::inet,$5,$6::jsonb)
                """,
                persisted_tenant,
                fingerprint,
                user_id,
                source_ip if source_ip != "unknown" else None,
                path[:500],
                json.dumps(evidence, separators=(",", ":")),
            )
        await ledger.record(
            category=AuditCategory.ADMIN_ACTION,
            action="anomaly_repeated_access_denial",
            tenant_id=persisted_tenant,
            user_id=user_id,
            metadata={"fingerprint": fingerprint, "route": path, **evidence},
        )
    except Exception as exc:  # noqa: BLE001 — anomaly recording never breaks auth
        log.warning("Anomaly alert write failed (degraded mode): %s", exc)


def _track_access_denial(
    *,
    tenant_id: Optional[str],
    user_id: Optional[str],
    source_ip: str,
    path: str,
) -> None:
    now = time.monotonic()
    key = (source_ip, path)
    if key not in _denial_windows and len(_denial_windows) >= _MAX_ANOMALY_KEYS:
        oldest = next(iter(_denial_windows))
        _denial_windows.pop(oldest, None)
        _denial_last_alert.pop(oldest, None)
    window = _denial_windows[key]
    while window and now - window[0] > _DENIAL_WINDOW_SECONDS:
        window.popleft()
    window.append(now)
    last_alert = _denial_last_alert.get(key, 0.0)
    if len(window) >= _DENIAL_THRESHOLD and now - last_alert >= _DENIAL_WINDOW_SECONDS:
        _denial_last_alert[key] = now
        task = asyncio.create_task(
            _record_anomaly_safe(
                tenant_id=tenant_id,
                user_id=user_id,
                source_ip=source_ip,
                path=path,
                count=len(window),
            )
        )
        _pending.add(task)
        task.add_done_callback(_pending.discard)


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
        path = request.url.path
        if any(path.startswith(prefix) for prefix in _SKIP_PREFIXES):
            return await call_next(request)

        is_mutation = request.method in _MUTATION_METHODS

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
        if is_mutation:
            _spawn_record(
                category=category,
                action=f"{request.method} {path}",
                tenant_id=tenant_id,
                user_id=user_id,
                metadata=meta,
            )

        if response.status_code in {401, 403}:
            _track_access_denial(
                tenant_id=tenant_id,
                user_id=user_id,
                source_ip=client_ip,
                path=path,
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
