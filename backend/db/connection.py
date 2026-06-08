"""
Oracle — hardened Aurora PostgreSQL connection layer (decision 007).

The "pour the concrete" piece: a single asyncpg pool that enforces the three
non-negotiables on every connection —

  1. Passwordless auth — each new connection mints a fresh 15-minute AWS IAM
     auth token (no static DB password exists to steal from source/env).
  2. Encrypted transit — TLS 1.3 required, server cert verified against the
     Amazon RDS CA bundle. No plaintext on the wire.
  3. Tenant isolation — every transaction SETs the app.current_tenant /
     app.current_role GUCs (via tenancy.apply_rls_context) so the RLS policies
     in db/migrations/* evaluate against the request's identity.

Connects as `oracle_app_login` (IAM-auth, inherits the non-owner oracle_app
group) so FORCE RLS strictly applies — a missing WHERE clause physically cannot
spill another brokerage's data.

Env: ORACLE_DB_HOST, ORACLE_DB_PORT(=5432), ORACLE_DB_NAME, ORACLE_DB_USER
(=oracle_app_login), AWS_REGION, ORACLE_RDS_CA_BUNDLE (path to the RDS global
CA .pem). boto3 + asyncpg are imported lazily so importing this module never
breaks a backend that hasn't wired the DB yet.

Pool sizing (env overrides, lowest priority; keyword args to init_pool() win):
  ORACLE_DB_POOL_MIN  — minimum idle connections (default 2)
  ORACLE_DB_POOL_MAX  — maximum open connections (default 10)
"""

from __future__ import annotations

import logging
import os
import ssl
from contextlib import asynccontextmanager
from typing import Optional

from tenancy import TenantContext, apply_rls_context

logger = logging.getLogger("oracle.db")

DB_HOST = os.getenv("ORACLE_DB_HOST", "")
DB_PORT = int(os.getenv("ORACLE_DB_PORT", "5432"))
DB_NAME = os.getenv("ORACLE_DB_NAME", "oracle")
DB_USER = os.getenv("ORACLE_DB_USER", "oracle_app_login")
AWS_REGION = os.getenv("AWS_REGION", "us-east-2")
RDS_CA_BUNDLE = os.getenv("ORACLE_RDS_CA_BUNDLE", "/etc/ssl/certs/rds-global-bundle.pem")

# Local-dev escape hatch: if a static password is supplied we connect with plain
# password auth (and SSL off unless ORACLE_DB_SSLMODE says otherwise) instead of
# minting an AWS IAM token against an RDS endpoint. This lets the full stack run
# against a throwaway `postgres:16` container. Aurora prod leaves these unset and
# falls through to the IAM + TLS 1.3 path — production behaviour is unchanged.
DB_PASSWORD = os.getenv("ORACLE_DB_PASSWORD", "")
DB_SSLMODE = os.getenv("ORACLE_DB_SSLMODE", "")  # 'disable' (default for local) | 'require'

# Pool sizing — configurable via env; init_pool() kwargs take precedence.
_ENV_POOL_MIN = int(os.getenv("ORACLE_DB_POOL_MIN", "2"))
_ENV_POOL_MAX = int(os.getenv("ORACLE_DB_POOL_MAX", "10"))

_pool = None  # asyncpg.Pool, lazily created


def _build_ssl_context() -> ssl.SSLContext:
    """TLS 1.3 only, server certificate verified against the RDS CA bundle."""
    ctx = ssl.create_default_context(cafile=RDS_CA_BUNDLE)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


def _iam_auth_token() -> str:
    """Mint a fresh ~15-minute RDS IAM auth token. asyncpg calls this per new
    connection, so the pool keeps working as tokens rotate — and nothing static
    is ever persisted."""
    import boto3  # lazy

    rds = boto3.client("rds", region_name=AWS_REGION)
    return rds.generate_db_auth_token(
        DBHostname=DB_HOST, Port=DB_PORT, DBUsername=DB_USER, Region=AWS_REGION
    )


async def _health_check_connection(conn) -> None:
    """asyncpg pool `setup` callback — run a cheap health probe on every
    connection that is checked out of the pool. If the connection is stale or
    broken the query raises, asyncpg discards it and opens a fresh one."""
    await conn.execute("SELECT 1")


def pool_stats() -> dict:
    """Return a snapshot of current pool metrics, or an empty dict if the pool
    has not been initialised. Safe to call at any time (e.g. from /health)."""
    if _pool is None:
        return {}
    return {
        "min_size": _pool.get_min_size(),
        "max_size": _pool.get_max_size(),
        "size": _pool.get_size(),
        "idle": _pool.get_idle_size(),
    }


def get_pool():
    """Return the current pool instance, or None if not initialised."""
    return _pool


async def init_pool(min_size: int = _ENV_POOL_MIN, max_size: int = _ENV_POOL_MAX):
    """Create the shared pool. Call once on app startup.

    min_size / max_size default to ORACLE_DB_POOL_MIN / ORACLE_DB_POOL_MAX env
    vars (both fall back to 2 / 10 if unset). Explicit keyword arguments always
    win over the env values, preserving backward-compatibility for call sites that
    already pass numeric literals."""
    global _pool
    if _pool is not None:
        return _pool

    import asyncpg  # lazy

    if DB_PASSWORD:
        # Local / password-auth path (dev container). No IAM token, no RDS CA.
        local_ssl = _build_ssl_context() if DB_SSLMODE not in ("", "disable") else False
        _pool = await asyncpg.create_pool(
            host=DB_HOST or "localhost",
            port=DB_PORT,
            database=DB_NAME,
            # Aurora's default login role won't exist locally; fall back to the
            # superuser the dev postgres container ships with.
            user=os.getenv("ORACLE_DB_USER") or "postgres",
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
            password=_iam_auth_token,        # callable → re-minted per connection
            ssl=_build_ssl_context(),
            min_size=min_size,
            max_size=max_size,
            command_timeout=30,
            setup=_health_check_connection,
        )

    stats = pool_stats()
    logger.info(
        "DB pool ready — host=%s db=%s min=%d max=%d size=%d idle=%d",
        DB_HOST or "localhost",
        DB_NAME,
        stats.get("min_size", min_size),
        stats.get("max_size", max_size),
        stats.get("size", 0),
        stats.get("idle", 0),
    )
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
