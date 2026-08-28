"""
Oracle — hardened PostgreSQL connection layer (decision 007).

The "pour the concrete" piece: a single asyncpg pool that enforces the three
non-negotiables on every connection —

  1. Passwordless auth — each new connection mints a fresh, short-lived cloud
     token (no static DB password exists to steal from source/env). Azure
     Database for PostgreSQL Flexible Server (Entra) is the default target;
     RDS/Aurora IAM remains available via ORACLE_DB_AUTH=aws-iam.
  2. Encrypted transit — verified TLS, server cert checked against the system
     trust store (Azure) or a pinned RDS CA bundle. No plaintext on the wire.
  3. Tenant isolation — every transaction SETs the app.current_tenant /
     app.current_role GUCs (via tenancy.apply_rls_context) so the RLS policies
     in db/migrations/* evaluate against the request's identity.

Connects as `oracle_app_login` (token-auth, inherits the non-owner oracle_app
group) so FORCE RLS strictly applies — a missing WHERE clause physically cannot
spill another brokerage's data.

Env: ORACLE_DB_HOST, ORACLE_DB_PORT(=5432), ORACLE_DB_NAME, ORACLE_DB_USER
(=oracle_app_login), ORACLE_DB_AUTH(=azure-entra), ORACLE_DB_TLS_MIN,
ORACLE_DB_CA_BUNDLE. Azure additionally reads AZURE_CLIENT_ID to select the
user-assigned managed identity; the aws-iam path reads AWS_REGION and
ORACLE_RDS_CA_BUNDLE. azure-identity, boto3 and asyncpg are all imported lazily
so importing this module never breaks a backend that hasn't wired the DB yet.

Pool sizing (env overrides, lowest priority; keyword args to init_pool() win):
  ORACLE_DB_POOL_MIN  — minimum idle connections (default 2)
  ORACLE_DB_POOL_MAX  — maximum open connections (default 10)
"""

from __future__ import annotations

import asyncio
import logging
import os
import ssl
import time
from contextlib import asynccontextmanager
from typing import Optional

from tenancy import TenantContext, apply_rls_context

logger = logging.getLogger("oracle.db")

DB_HOST = os.getenv("ORACLE_DB_HOST", "")
DB_PORT = int(os.getenv("ORACLE_DB_PORT", "5432"))
DB_NAME = os.getenv("ORACLE_DB_NAME", "oracle")
DB_USER = os.getenv("ORACLE_DB_USER", "oracle_app_login")

# Which passwordless mechanism to use when no static password is configured.
# 'azure-entra' mints an Entra access token for Azure Database for PostgreSQL
# Flexible Server; 'aws-iam' keeps the legacy RDS/Aurora behaviour.
_DB_AUTH_ENV = os.getenv("ORACLE_DB_AUTH", "").strip().lower()


def _default_db_auth(host: str) -> str:
    """Pick the passwordless mechanism from the host when nothing is configured.

    Azure is the target, so it stays the default. But a still-running Aurora
    deployment redeployed without adding ORACLE_DB_AUTH=aws-iam would otherwise
    hand RDS an Entra JWT as its password — init_pool() fails and every request
    returns 503 at boot, with an opaque auth error and the wrong CA bundle and
    TLS floor to boot. The hostname says unambiguously which cloud answers, so
    an unset variable reads it rather than guessing wrong.
    """
    return "aws-iam" if host.endswith(".rds.amazonaws.com") else "azure-entra"


DB_AUTH = _DB_AUTH_ENV or _default_db_auth(DB_HOST)
if not _DB_AUTH_ENV and DB_AUTH != "azure-entra":
    logger.warning(
        "ORACLE_DB_AUTH is unset; inferred %r from host %r. Set it explicitly.",
        DB_AUTH,
        DB_HOST,
    )

# --- Azure (default target) ---------------------------------------------------
# Entra tokens for Flexible Server are issued against this fixed OSS-RDBMS scope.
AZURE_PG_SCOPE = "https://ossrdbms-aad.database.windows.net/.default"

# --- AWS (legacy, opt-in via ORACLE_DB_AUTH=aws-iam) ---------------------------
AWS_REGION = os.getenv("AWS_REGION", "us-east-2")
RDS_CA_BUNDLE = os.getenv("ORACLE_RDS_CA_BUNDLE", "/etc/ssl/certs/rds-global-bundle.pem")

# Azure Flexible Server presents a publicly-rooted cert (DigiCert/Microsoft RSA
# Root), so the system trust store verifies it — pinning a bundle is only needed
# for RDS. Defaulting DB_CA_BUNDLE to the RDS .pem unconditionally used to make
# _build_ssl_context() raise FileNotFoundError on any host without that file.
DB_CA_BUNDLE = os.getenv("ORACLE_DB_CA_BUNDLE") or (
    RDS_CA_BUNDLE if DB_AUTH == "aws-iam" else None
)

# Minimum TLS version. Azure Flexible Server negotiates 1.2 or 1.3 depending on
# server config, so requiring 1.3 there can hard-fail a healthy server; RDS keeps
# the stricter 1.3 floor it was hardened to. Override with ORACLE_DB_TLS_MIN.
DB_TLS_MIN = os.getenv("ORACLE_DB_TLS_MIN", "1.3" if DB_AUTH == "aws-iam" else "1.2").strip()

# Local-dev escape hatch: if a static password is supplied we connect with plain
# password auth (and SSL off unless ORACLE_DB_SSLMODE says otherwise) instead of
# minting a cloud auth token. This lets the full stack run against a throwaway
# `postgres:16` container. Prod leaves these unset and falls through to the
# passwordless + TLS path selected by ORACLE_DB_AUTH.
DB_PASSWORD = os.getenv("ORACLE_DB_PASSWORD", "")
DB_SSLMODE = os.getenv("ORACLE_DB_SSLMODE", "")  # 'disable' (default for local) | 'require'

# Pool sizing — configurable via env; init_pool() kwargs take precedence.
_ENV_POOL_MIN = int(os.getenv("ORACLE_DB_POOL_MIN", "2"))
_ENV_POOL_MAX = int(os.getenv("ORACLE_DB_POOL_MAX", "10"))

# ws_hub holds one connection open for the process lifetime to run LISTEN
# (ws_hub.py). It is never returned, so it is capacity no request can use.
# init_pool() adds this to the requested max so ORACLE_DB_POOL_MAX keeps meaning
# "connections available to requests".
LISTENER_RESERVED_CONNECTIONS = 1

# How long a connection may go unprobed before _health_check_connection pays for
# another round trip. 0 restores the previous probe-on-every-checkout behaviour.
HEALTH_CHECK_INTERVAL_SECONDS = float(
    os.getenv("ORACLE_DB_HEALTH_CHECK_INTERVAL", "30")
)

# Keyed by backend server PID, not by the connection object: asyncpg hands the
# setup callback a PoolConnectionProxy whose __slots__ has no __weakref__, so it
# can neither be weak-referenced nor tagged with an attribute. The server PID is
# public API on the proxy, unique per live physical connection, and a *replaced*
# connection gets a fresh PID — so a stale entry can never vouch for a new
# connection. (OS-level PID reuse after wraparound would at worst skip one probe
# within the interval.) Entries are pruned in _prune_verification_cache; a plain
# dict here would otherwise grow by one tiny entry per connection ever replaced.
_connection_last_verified: dict[int, float] = {}
_VERIFICATION_CACHE_MAX = 64

_pool = None  # asyncpg.Pool, lazily created


_TLS_FLOOR = {"1.2": ssl.TLSVersion.TLSv1_2, "1.3": ssl.TLSVersion.TLSv1_3}


def _build_ssl_context() -> ssl.SSLContext:
    """Verified TLS, with a provider-specific or system CA bundle.

    cafile=None falls back to the system trust store, which is what verifies
    Azure Flexible Server's publicly-rooted certificate."""
    ctx = ssl.create_default_context(cafile=DB_CA_BUNDLE)
    floor = _TLS_FLOOR.get(DB_TLS_MIN)
    if floor is None:
        raise RuntimeError(
            f"ORACLE_DB_TLS_MIN={DB_TLS_MIN!r} is not supported; use '1.2' or '1.3'."
        )
    ctx.minimum_version = floor
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


_azure_cred = None  # azure.identity.DefaultAzureCredential, lazily created


def _azure_credential():
    """One process-wide credential. DefaultAzureCredential caches tokens and
    refreshes them before expiry, so re-using it avoids re-doing IMDS discovery
    on every new pool connection. Honours AZURE_CLIENT_ID, which is how the
    user-assigned `neoh-app-id` identity is selected inside Container Apps."""
    global _azure_cred
    if _azure_cred is None:
        from azure.identity import DefaultAzureCredential  # lazy

        _azure_cred = DefaultAzureCredential()
    return _azure_cred


async def _entra_auth_token() -> str:
    """Mint an Entra access token for Flexible Server. Run off the event loop:
    unlike RDS token minting (local signing), acquiring an Entra token can make
    a network call to IMDS on a cache miss."""
    cred = _azure_credential()
    token = await asyncio.to_thread(cred.get_token, AZURE_PG_SCOPE)
    return token.token


def _iam_auth_token() -> str:
    """Mint a fresh ~15-minute RDS IAM auth token. asyncpg calls this per new
    connection, so the pool keeps working as tokens rotate — and nothing static
    is ever persisted."""
    import boto3  # lazy

    rds = boto3.client("rds", region_name=AWS_REGION)
    return rds.generate_db_auth_token(
        DBHostname=DB_HOST, Port=DB_PORT, DBUsername=DB_USER, Region=AWS_REGION
    )


def _passwordless_credential():
    """Return the per-connection token callable for the configured provider.
    asyncpg re-invokes it for every new connection, so tokens rotate with the
    pool instead of being pinned at startup."""
    if DB_AUTH == "azure-entra":
        return _entra_auth_token
    if DB_AUTH == "aws-iam":
        return _iam_auth_token
    raise RuntimeError(
        f"ORACLE_DB_AUTH={DB_AUTH!r} is not supported; use 'azure-entra' or 'aws-iam' "
        "(or set ORACLE_DB_PASSWORD for local password auth)."
    )


async def _health_check_connection(conn) -> None:
    """asyncpg pool `setup` callback — probe a connection before handing it out.

    The probe is skipped for connections used recently. `SELECT 1` is cheap in
    CPU but costs a full round trip, and this deployment runs the app in North
    Central US against a database in Central US — so an unconditional probe
    added that latency to *every* checkout, including the fast path where the
    connection was verified moments earlier.

    A connection idle longer than the interval is the one that might have been
    dropped by a firewall or failover, and that is the case still probed. A
    connection broken *since* the probe raises on first real use and asyncpg
    discards it either way, so the probe was never the only line of defence.
    """
    try:
        key = conn.get_server_pid()
    except Exception:  # noqa: BLE001 — no PID, no cache; just probe
        key = None
    now = time.monotonic()
    if key is not None:
        last_ok = _connection_last_verified.get(key)
        if last_ok is not None and (now - last_ok) < HEALTH_CHECK_INTERVAL_SECONDS:
            return
    await conn.execute("SELECT 1")
    if key is not None:
        _connection_last_verified[key] = now
        _prune_verification_cache(now)


def _prune_verification_cache(now: float) -> None:
    """Drop entries too old to suppress a probe anyway.

    Any entry older than the interval no longer changes behaviour — its
    connection would be probed regardless — so discarding it is free. Only runs
    when the dict outgrows a bound several times any sane pool size, keeping the
    fast path a plain dict get/set.
    """
    if len(_connection_last_verified) <= _VERIFICATION_CACHE_MAX:
        return
    stale = [
        pid
        for pid, ts in _connection_last_verified.items()
        if now - ts >= HEALTH_CHECK_INTERVAL_SECONDS
    ]
    for pid in stale:
        _connection_last_verified.pop(pid, None)


def pool_stats() -> dict:
    """Return a snapshot of current pool metrics, or an empty dict if the pool
    has not been initialised. Safe to call at any time (e.g. from /health)."""
    if _pool is None:
        return {}
    max_size = _pool.get_max_size()
    return {
        "min_size": _pool.get_min_size(),
        "max_size": max_size,
        "size": _pool.get_size(),
        "idle": _pool.get_idle_size(),
        # What a request can actually obtain. Reported separately because
        # max_size includes the listener's permanent connection, and an operator
        # sizing replicas against Postgres max_connections off the raw number is
        # over by one per replica.
        "usable_for_requests": max(0, max_size - LISTENER_RESERVED_CONNECTIONS),
        "reserved_for_listener": LISTENER_RESERVED_CONNECTIONS,
    }


def get_pool():
    """Return the current pool instance, or None if not initialised."""
    return _pool


async def _assert_rls_is_enforced(pool) -> None:
    """Refuse a connection role that row-level security does not apply to.

    Tenant isolation on most read paths is RLS and nothing else: the queries
    select without a tenant predicate and rely on
    `USING (app_is_platform_admin() OR tenant_id = app_current_tenant())`.
    PostgreSQL exempts superusers and BYPASSRLS roles from every policy, so
    connecting as one does not weaken isolation — it removes it, silently, with
    no error and no log line.

    That is exactly what a local dev box did until 2026-08-28: it connected as
    `postgres`, and a freshly registered broker could list another tenant's
    clients. Production was fine because it connects as `oracle_app_login`,
    which is precisely what made the hole invisible — the one environment where
    it was broken was the one nobody could observe it in.

    In production this refuses to boot. Serving no requests is better than
    serving every tenant's data to every tenant.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT current_user AS role, "
            "       bool_or(rolsuper OR rolbypassrls) AS exempt "
            "  FROM pg_roles WHERE rolname = current_user"
        )
    if not row or not row["exempt"]:
        return

    message = (
        f"database role {row['role']!r} is a superuser or has BYPASSRLS, so "
        "row-level security does not apply to it and tenant isolation is not "
        "enforced. Connect as a non-superuser role such as oracle_app_login."
    )
    if os.getenv("ORACLE_ENV", "").lower() in ("prod", "production"):
        raise RuntimeError(f"Refusing to start: {message}")
    logger.error("TENANT ISOLATION IS OFF: %s", message)


async def init_pool(min_size: int = _ENV_POOL_MIN, max_size: int = _ENV_POOL_MAX):
    """Create the shared pool. Call once on app startup.

    min_size / max_size default to ORACLE_DB_POOL_MIN / ORACLE_DB_POOL_MAX env
    vars (both fall back to 2 / 10 if unset). Explicit keyword arguments always
    win over the env values, preserving backward-compatibility for call sites that
    already pass numeric literals.

    The pool is widened by `LISTENER_RESERVED_CONNECTIONS` beyond the requested
    size. `ws_hub` holds one connection open for the process lifetime to run
    LISTEN, so on a pool of 10 a request could only ever obtain 9 — the
    configured number silently meant one less than it said, which matters most
    on pools this small. Widening rather than subtracting keeps
    ORACLE_DB_POOL_MAX meaning "connections available to requests", which is the
    number an operator is actually sizing against Postgres `max_connections`."""
    global _pool
    if _pool is not None:
        return _pool

    requested_max = max_size
    max_size = max_size + LISTENER_RESERVED_CONNECTIONS

    import asyncpg  # lazy

    if DB_PASSWORD:
        # Local / password-auth path (dev container). No IAM token, no RDS CA.
        local_ssl = _build_ssl_context() if DB_SSLMODE not in ("", "disable") else False
        _pool = await asyncpg.create_pool(
            host=DB_HOST or "localhost",
            port=DB_PORT,
            database=DB_NAME,
            # NOT a superuser fallback. Superusers bypass row-level security
            # unconditionally, and RLS is the ONLY thing enforcing tenant
            # isolation on most read paths — the queries themselves carry no
            # tenant predicate. Defaulting to postgres meant a dev box silently
            # served one broker another broker's clients while every test
            # stayed green. _assert_rls_is_enforced below catches it either way.
            user=os.getenv("ORACLE_DB_USER") or "oracle_app_login",
            password=DB_PASSWORD,
            ssl=local_ssl,
            min_size=min_size,
            max_size=max_size,
            command_timeout=30,
            setup=_health_check_connection,
        )
    else:
        _pool = await asyncpg.create_pool(
            host=DB_HOST,
            port=DB_PORT,
            database=DB_NAME,
            user=DB_USER,
            password=_passwordless_credential(),  # callable → re-minted per connection
            ssl=_build_ssl_context(),
            min_size=min_size,
            max_size=max_size,
            command_timeout=30,
            setup=_health_check_connection,
        )

    stats = pool_stats()
    logger.info(
        "DB pool ready — host=%s db=%s min=%d max=%d (%d usable for requests, "
        "%d reserved for the ws_hub listener) size=%d idle=%d",
        DB_HOST or "localhost",
        DB_NAME,
        stats.get("min_size", min_size),
        stats.get("max_size", max_size),
        requested_max,
        LISTENER_RESERVED_CONNECTIONS,
        stats.get("size", 0),
        stats.get("idle", 0),
    )
    await _assert_rls_is_enforced(_pool)
    return _pool


async def close_pool():
    global _pool
    if _pool is not None:
        logger.info("Closing DB pool (size=%d idle=%d).", _pool.get_size(), _pool.get_idle_size())
        try:
            await _pool.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Error while closing DB pool: %s", exc)
        finally:
            _pool = None
            try:
                from data_integrations.cache import reset_shared_cache

                reset_shared_cache()
            except Exception:  # noqa: BLE001 - shutdown remains best effort
                pass
        logger.info("DB pool closed.")


@asynccontextmanager
async def tenant_tx(ctx: TenantContext):
    """Acquire a connection inside a transaction with this request's tenant
    context applied. RLS is live for everything done with the yielded conn:

        async with tenant_tx(ctx) as conn:
            rows = await conn.fetch("SELECT * FROM leads")   # auto-scoped

    The GUCs are SET LOCAL, so they reset when the transaction ends — no leakage
    of one request's identity into the next connection that reuses the socket.
    """
    if _pool is None:
        raise RuntimeError("DB pool not initialized — call init_pool() at startup.")

    async with _pool.acquire() as conn:
        async with conn.transaction():
            await apply_rls_context(conn, ctx)
            yield conn
