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

Env: ORACLE_DB_HOST (writer endpoint), ORACLE_DB_NAME (default oracle),
DB_MASTER_SECRET_ARN (RDS-managed master secret), AWS_REGION,
ORACLE_RDS_CA_BUNDLE (path to the RDS global CA bundle in the image).
"""
import asyncio
import glob
import json
import os
import ssl
import sys

MIGRATIONS_DIR = os.environ.get("ORACLE_MIGRATIONS_DIR", "/app/db/migrations")


async def main() -> int:
    import asyncpg
    import boto3

    host = os.environ["ORACLE_DB_HOST"]
    db = os.environ.get("ORACLE_DB_NAME", "oracle")
    arn = os.environ["DB_MASTER_SECRET_ARN"]
    region = os.environ.get("AWS_REGION", "us-east-1")
    ca = os.environ.get("ORACLE_RDS_CA_BUNDLE", "/etc/ssl/certs/rds-global-bundle.pem")

    sec = json.loads(
        boto3.client("secretsmanager", region_name=region)
        .get_secret_value(SecretId=arn)["SecretString"]
    )
    user, pw = sec["username"], sec["password"]

    ctx = ssl.create_default_context(cafile=ca)
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED

    conn = await asyncpg.connect(host=host, port=5432, user=user, password=pw, database=db, ssl=ctx)
    try:
        files = sorted(glob.glob(os.path.join(MIGRATIONS_DIR, "*.sql")))
        if not files:
            print(f"!! no migrations found in {MIGRATIONS_DIR}", flush=True)
            return 2

        # Ledger — apply each file at most once. Without this, a re-run blindly
        # re-applied non-idempotent 0001 (`CREATE TABLE tenants`) and died with
        # "relation tenants already exists". On a DB that already has the schema
        # but no ledger yet (applied pre-ledger), we run a one-time BACK-FILL:
        # an "already exists" error means it was applied before → record + move on.
        # Once the ledger is populated, runs are strict again (real errors abort).
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "filename text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        applied = {r["filename"] for r in await conn.fetch("SELECT filename FROM schema_migrations")}
        schema_exists = await conn.fetchval("SELECT to_regclass('public.tenants') IS NOT NULL")
        backfill = (not applied) and bool(schema_exists)
        # SQLSTATEs meaning "already exists / already applied".
        DUP = {"42P07", "42710", "42701", "42P06", "42723", "42P16", "42P04", "42P05", "23505"}

        print(f"{len(files)} files; {len(applied)} recorded; backfill={backfill}", flush=True)
        processed = 0
        for f in files:
            name = os.path.basename(f)
            if name in applied:
                print(f"-- {name} (recorded, skip)", flush=True)
                continue
            sql = open(f).read()
            try:
                await conn.execute(sql)
                print(f">> applied {name}", flush=True)
            except asyncpg.PostgresError as e:
                code = getattr(e, "sqlstate", None)
                if backfill and (code in DUP or "already exists" in str(e).lower()):
                    print(f"~~ {name} already applied ({code}) — recording", flush=True)
                else:
                    print(f"!! {name} FAILED ({code}): {e}", flush=True)
                    raise
            await conn.execute(
                "INSERT INTO schema_migrations (filename) VALUES ($1) ON CONFLICT DO NOTHING", name
            )
            processed += 1
        print(f"migrations complete ({processed} processed)", flush=True)
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
