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
"""

from __future__ import annotations

import os
import ssl
from contextlib import asynccontextmanager
from typing import Optional

from tenancy import TenantContext, apply_rls_context

DB_HOST = os.getenv("ORACLE_DB_HOST", "")
DB_PORT = int(os.getenv("ORACLE_DB_PORT", "5432"))
DB_NAME = os.getenv("ORACLE_DB_NAME", "oracle")
DB_USER = os.getenv("ORACLE_DB_USER", "oracle_app_login")
AWS_REGION = os.getenv("AWS_REGION", "us-east-2")
RDS_CA_BUNDLE = os.getenv("ORACLE_RDS_CA_BUNDLE", "/etc/ssl/certs/rds-global-bundle.pem")

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


async def init_pool(min_size: int = 2, max_size: int = 10):
    """Create the shared pool. Call once on app startup."""
    global _pool
    if _pool is not None:
        return _pool

    import asyncpg  # lazy

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
    )
    return _pool


async def close_pool():
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


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
