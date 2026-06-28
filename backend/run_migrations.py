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
        print(f"applying {len(files)} migrations to {host}/{db} as {user}", flush=True)
        for f in files:
            print(f">> {os.path.basename(f)}", flush=True)
            await conn.execute(open(f).read())
        print("migrations complete", flush=True)
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
