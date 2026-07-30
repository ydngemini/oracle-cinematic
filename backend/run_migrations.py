#!/usr/bin/env python3
"""One-off production migration runner.

Applies backend/db/migrations/*.sql in filename order against Aurora as the
MASTER user over TLS (verify-full). Aurora lives in private subnets, so this is
meant to run as a one-off ECS task inside the VPC:

  aws ecs run-task --cluster neoh-prod --task-definition <backend-td> \
    --launch-type FARGATE --network-configuration '...private subnets...' \
    --overrides '{"containerOverrides":[{"name":"backend",
      "command":["python","/app/run_migrations.py"],
      "environment":[{"name":"DB_MASTER_SECRET_ARN","value":"<arn>"}]}]}'

The migrations are idempotent (IF NOT EXISTS / guarded DO-blocks), so a re-run is
safe. 0001/0003 create the oracle_app_login role + RLS the app depends on, which
is why this must run as the master user before the service stabilizes.

Credential sources, in priority order:

* Direct provider-neutral credentials: ``ORACLE_DB_ADMIN_USER`` and
  ``ORACLE_DB_ADMIN_PASSWORD``. This is used by the Azure Container Apps
  migration job, whose password value comes from a Key Vault secret reference.
* AWS RDS: ``DB_MASTER_SECRET_ARN`` and ``AWS_REGION``.

The runner creates ``ORACLE_DB_NAME`` when it does not exist, applies the SQL
ledger, and optionally sets the fixed ``oracle_app_login`` role password from
``ORACLE_DB_APP_PASSWORD``. The application still connects as that non-owner
role, so FORCE RLS remains effective.
"""
import asyncio
import glob
import json
import os
import re
import ssl
import sys

MIGRATIONS_DIR = os.environ.get("ORACLE_MIGRATIONS_DIR", "/app/db/migrations")
_DB_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
_MIGRATION_LOCK_KEY = 0x4F5241434C454D47  # ASCII "ORACLEMG", as a signed bigint.
_LOCK_SQL = "SELECT pg_advisory_lock($1)"
_UNLOCK_SQL = "SELECT pg_advisory_unlock($1)"
_CREATE_LEDGER_SQL = (
    "CREATE TABLE IF NOT EXISTS schema_migrations ("
    "filename text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
)
_RECORD_MIGRATION_SQL = "INSERT INTO schema_migrations (filename) VALUES ($1)"
_BEGIN_LINE_RE = re.compile(
    r"BEGIN(?:\s+(?:TRANSACTION|WORK))?\s*;\s*(?:--.*)?", re.IGNORECASE
)
_COMMIT_LINE_RE = re.compile(
    r"COMMIT(?:\s+(?:TRANSACTION|WORK))?\s*;\s*(?:--.*)?", re.IGNORECASE
)


def _sql_literal(value: str) -> str:
    """Quote a PostgreSQL string literal without logging the secret value."""
    return "'" + value.replace("'", "''") + "'"


def _admin_credentials() -> tuple[str, str]:
    direct_password = os.environ.get("ORACLE_DB_ADMIN_PASSWORD", "")
    if direct_password:
        return os.environ.get("ORACLE_DB_ADMIN_USER", "postgres"), direct_password

    arn = os.environ.get("DB_MASTER_SECRET_ARN", "")
    if not arn:
        raise RuntimeError(
            "ORACLE_DB_ADMIN_PASSWORD or DB_MASTER_SECRET_ARN is required"
        )

    import boto3

    region = os.environ.get("AWS_REGION", "us-east-1")
    sec = json.loads(
        boto3.client("secretsmanager", region_name=region)
        .get_secret_value(SecretId=arn)["SecretString"]
    )
    return sec["username"], sec["password"]


def _ssl_context() -> ssl.SSLContext:
    ca = os.environ.get(
        "ORACLE_DB_CA_BUNDLE",
        os.environ.get(
            "ORACLE_RDS_CA_BUNDLE", "/etc/ssl/certs/rds-global-bundle.pem"
        ),
    )
    ctx = ssl.create_default_context(cafile=ca)
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


async def _ensure_database(*, host: str, port: int, db: str, user: str, pw: str, ctx) -> None:
    import asyncpg

    if not _DB_NAME_RE.fullmatch(db):
        raise ValueError("ORACLE_DB_NAME must be a safe PostgreSQL identifier")
    maintenance_db = os.environ.get("ORACLE_DB_MAINTENANCE_NAME", "postgres")
    conn = await asyncpg.connect(
        host=host,
        port=port,
        user=user,
        password=pw,
        database=maintenance_db,
        ssl=ctx,
    )
    try:
        exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname=$1", db)
        if not exists:
            await conn.execute(f'CREATE DATABASE "{db}"')
            print(f">> created database {db}", flush=True)
    finally:
        await conn.close()


def _strip_outer_transaction(sql: str) -> str:
    """Remove a migration's standalone BEGIN/COMMIT wrapper, if present.

    Several existing migrations are directly executable with psql and therefore
    include their own transaction wrapper. The runner owns the transaction so it
    can commit the migration and ledger row together; leaving an inner COMMIT in
    place would break that guarantee. PL/pgSQL BEGIN/END blocks are untouched.
    """
    lines = sql.splitlines(keepends=True)
    code_lines = [
        index
        for index, line in enumerate(lines)
        if line.strip() and not line.lstrip().startswith("--")
    ]
    if not code_lines:
        return sql

    first = code_lines[0]
    last = code_lines[-1]
    has_begin = bool(_BEGIN_LINE_RE.fullmatch(lines[first].strip()))
    has_commit = bool(_COMMIT_LINE_RE.fullmatch(lines[last].strip()))
    if has_begin != has_commit:
        raise RuntimeError("migration has an incomplete outer transaction wrapper")
    if not has_begin:
        return sql

    del lines[last]
    del lines[first]
    return "".join(lines)


async def _run_migrations(conn, files: list[str]) -> int:
    """Apply migration files while holding the database-wide advisory lock."""
    lock_acquired = False
    try:
        await conn.execute(_LOCK_SQL, _MIGRATION_LOCK_KEY)
        lock_acquired = True

        if not files:
            print(f"!! no migrations found in {MIGRATIONS_DIR}", flush=True)
            return 2

        await conn.execute(_CREATE_LEDGER_SQL)
        applied = {
            row["filename"]
            for row in await conn.fetch("SELECT filename FROM schema_migrations")
        }

        print(f"{len(files)} files; {len(applied)} recorded", flush=True)
        processed = 0
        for migration_path in files:
            name = os.path.basename(migration_path)
            if name in applied:
                print(f"-- {name} (recorded, skip)", flush=True)
                continue

            with open(migration_path, encoding="utf-8") as migration_file:
                sql = _strip_outer_transaction(migration_file.read())

            try:
                async with conn.transaction():
                    await conn.execute(sql)
                    await conn.execute(_RECORD_MIGRATION_SQL, name)
            except Exception as exc:
                # Never infer that an unledgered legacy file is complete from a
                # duplicate-object error: later statements may still be missing.
                code = getattr(exc, "sqlstate", None)
                print(f"!! {name} FAILED ({code}): {exc}", flush=True)
                raise

            print(f">> applied {name}", flush=True)
            processed += 1

        app_password = os.environ.get("ORACLE_DB_APP_PASSWORD", "")
        if app_password:
            await conn.execute(
                "ALTER ROLE oracle_app_login PASSWORD " + _sql_literal(app_password)
            )
            print(">> configured oracle_app_login credential", flush=True)
        print(f"migrations complete ({processed} processed)", flush=True)
        return 0
    finally:
        if lock_acquired:
            unlocked = await conn.fetchval(_UNLOCK_SQL, _MIGRATION_LOCK_KEY)
            if unlocked is not True:
                raise RuntimeError("failed to release migration advisory lock")


async def main() -> int:
    import asyncpg

    host = os.environ["ORACLE_DB_HOST"]
    port = int(os.environ.get("ORACLE_DB_PORT", "5432"))
    db = os.environ.get("ORACLE_DB_NAME", "oracle")
    user, pw = _admin_credentials()
    ctx = _ssl_context()

    await _ensure_database(host=host, port=port, db=db, user=user, pw=pw, ctx=ctx)
    conn = await asyncpg.connect(
        host=host, port=port, user=user, password=pw, database=db, ssl=ctx
    )
    try:
        files = sorted(glob.glob(os.path.join(MIGRATIONS_DIR, "*.sql")))
        return await _run_migrations(conn, files)
    finally:
        await conn.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
