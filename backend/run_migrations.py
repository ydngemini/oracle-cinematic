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
import hashlib
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

# Provenance, added later: a row that says only "applied" cannot distinguish a
# migration this runner executed from one a reconcile inferred was already
# there, and it cannot detect a file edited after the fact. `recorded_via` is
# nullable because rows written before these columns existed were all genuine
# runner applications and are backfilled as such.
_EXTEND_LEDGER_SQL = """
    ALTER TABLE schema_migrations
        ADD COLUMN IF NOT EXISTS sha256       char(64),
        ADD COLUMN IF NOT EXISTS recorded_via text,
        ADD COLUMN IF NOT EXISTS evidence     text;
    UPDATE schema_migrations SET recorded_via='applied' WHERE recorded_via IS NULL;
    ALTER TABLE schema_migrations DROP CONSTRAINT IF EXISTS chk_schema_migrations_via;
    ALTER TABLE schema_migrations
        ADD CONSTRAINT chk_schema_migrations_via
            CHECK (recorded_via IN ('applied', 'reconciled'));
    ALTER TABLE schema_migrations DROP CONSTRAINT IF EXISTS chk_schema_migrations_evidence;
    ALTER TABLE schema_migrations
        ADD CONSTRAINT chk_schema_migrations_evidence
            CHECK ((recorded_via = 'reconciled') = (evidence IS NOT NULL));
"""
_RECORD_MIGRATION_SQL = (
    "INSERT INTO schema_migrations (filename, sha256, recorded_via) "
    "VALUES ($1, $2, 'applied')"
)
_RECONCILE_MIGRATION_SQL = (
    "INSERT INTO schema_migrations (filename, sha256, recorded_via, evidence) "
    "VALUES ($1, $2, 'reconciled', $3) ON CONFLICT (filename) DO NOTHING"
)
_BEGIN_LINE_RE = re.compile(
    r"BEGIN(?:\s+(?:TRANSACTION|WORK))?\s*;\s*(?:--.*)?", re.IGNORECASE
)
_COMMIT_LINE_RE = re.compile(
    r"COMMIT(?:\s+(?:TRANSACTION|WORK))?\s*;\s*(?:--.*)?", re.IGNORECASE
)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sql_literal(value: str) -> str:
    """Quote a PostgreSQL string literal without logging the secret value."""
    return "'" + value.replace("'", "''") + "'"


def _split_credential(raw: str) -> tuple[str, str]:
    """Accept either a JSON {username, password} document or a bare password.

    Azure-managed Postgres secrets are commonly stored as just the password, with
    the administrator login held separately; RDS-managed secrets are always JSON."""
    default_user = os.environ.get("ORACLE_DB_ADMIN_USER", "postgres")
    raw = raw.strip()
    if raw.startswith("{"):
        parsed = json.loads(raw)
        return parsed.get("username", default_user), parsed["password"]
    return default_user, raw


def _keyvault_credential() -> tuple[str, str]:
    """Read the administrator credential from Azure Key Vault.

    Uses the same managed identity the app uses for its versionless Key Vault
    references, so migrations need no static secret of their own."""
    from azure.identity import DefaultAzureCredential
    from azure.keyvault.secrets import SecretClient

    vault_uri = os.environ["ORACLE_KEY_VAULT_URI"]
    secret_name = os.environ.get("ORACLE_DB_ADMIN_SECRET", "oracle-db-admin-password")
    client = SecretClient(vault_url=vault_uri, credential=DefaultAzureCredential())
    return _split_credential(client.get_secret(secret_name).value or "")


def _secretsmanager_credential() -> tuple[str, str]:
    """Legacy AWS path, kept so an RDS deployment can still be migrated."""
    import boto3

    arn = os.environ["DB_MASTER_SECRET_ARN"]
    region = os.environ.get("AWS_REGION", "us-east-1")
    return _split_credential(
        boto3.client("secretsmanager", region_name=region)
        .get_secret_value(SecretId=arn)["SecretString"]
    )


def _admin_credentials() -> tuple[str, str]:
    direct_password = os.environ.get("ORACLE_DB_ADMIN_PASSWORD", "")
    if direct_password:
        return os.environ.get("ORACLE_DB_ADMIN_USER", "postgres"), direct_password

    if os.environ.get("ORACLE_KEY_VAULT_URI"):
        return _keyvault_credential()

    if os.environ.get("DB_MASTER_SECRET_ARN"):
        return _secretsmanager_credential()

    raise RuntimeError(
        "One of ORACLE_DB_ADMIN_PASSWORD, ORACLE_KEY_VAULT_URI or "
        "DB_MASTER_SECRET_ARN is required"
    )


# Hosts a plaintext connection is permitted to reach. A local container has no
# certificate, which is why dev-start.sh grew its own psql loop instead of using
# this runner — and that loop silences every error, so a migration that genuinely
# failed printed "(already applied / no-op)". Letting the real runner reach a
# local database is what stops the two paths diverging.
_LOCAL_DB_HOSTS = frozenset({"db", "localhost", "127.0.0.1", "::1", "oracle-db-1"})


def _ssl_context(host: str = "") -> "ssl.SSLContext | bool":
    # cafile=None uses the system trust store, which verifies Azure Flexible
    # Server's publicly-rooted certificate. Only RDS needs a pinned bundle, so
    # defaulting to the RDS .pem made this raise FileNotFoundError on Azure.
    if os.environ.get("ORACLE_DB_SSL", "").strip().lower() == "disable":
        if host not in _LOCAL_DB_HOSTS:
            raise RuntimeError(
                f"ORACLE_DB_SSL=disable is only honoured for a local database; "
                f"{host!r} is not one of {sorted(_LOCAL_DB_HOSTS)}. Migrating a "
                f"managed instance over plaintext would put the master "
                f"credential on the wire."
            )
        print(f"** TLS disabled for local host {host!r}", flush=True)
        return False
    ca = os.environ.get("ORACLE_DB_CA_BUNDLE") or os.environ.get("ORACLE_RDS_CA_BUNDLE")
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


_TABLE_RE = re.compile(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][\w.]*)", re.I)
_INDEX_RE = re.compile(r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:CONCURRENTLY\s+)?(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][\w]*)", re.I)
# Columns are parsed one statement at a time. A sliding window over the file
# pairs an ADD COLUMN with whichever ALTER TABLE happened to be within N
# characters, which in this repo attributed leads.seller_client_id to `clients`
# and inbound_voice_calls.forwarded_at to `telephony_routes` — six false
# "partial" verdicts on the first run.
_ALTER_TABLE_RE = re.compile(r"^\s*ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?([A-Za-z_][\w.]*)", re.I)
_ADD_COLUMN_RE = re.compile(r"ADD\s+COLUMN\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][\w]*)", re.I)
_FUNCTION_RE = re.compile(r"CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\s+([A-Za-z_][\w]*)", re.I)
_CONSTRAINT_RE = re.compile(r"ADD\s+CONSTRAINT\s+([A-Za-z_][\w]*)", re.I)
_POLICY_RE = re.compile(r"CREATE\s+POLICY\s+([A-Za-z_][\w]*)", re.I)
_TRIGGER_RE = re.compile(r"CREATE\s+TRIGGER\s+([A-Za-z_][\w]*)", re.I)
_DO_BLOCK_RE = re.compile(r"DO\s+\$\$[\s\S]*?\$\$\s*;?", re.I)
_ENV_CONDITIONAL_RE = re.compile(r"pg_extension|current_setting|pg_available_extensions", re.I)
_CONDITIONAL_BLOCKS: dict = {}


def _declared_objects(sql: str) -> dict[str, list]:
    """Objects a migration file claims to create, for existence probing.

    Only DDL is discoverable this way. A migration whose whole body is INSERTs —
    the state reference seeds, for instance — declares nothing here, and the
    reconcile treats that as unverifiable rather than as absent.
    """
    body = "\n".join(
        line for line in sql.splitlines() if not line.lstrip().startswith("--")
    )
    # Two kinds of DO block, and only one is unprobeable.
    #
    # Environment-conditional (0013 adds fema_flood_zones.geom and its GiST
    # index only `IF EXISTS (SELECT 1 FROM pg_extension WHERE extname='postgis')`):
    # absence proves nothing, so probing reports drift on a database that is
    # exactly what the migration intended. Stripped.
    #
    # Idempotency-guarded (0018 wraps ADD CONSTRAINT leads_tenant_parcel_key in
    # BEGIN/EXCEPTION): the object is meant to exist unconditionally, so it is
    # probeable — and it has to be, because that guard catches duplicate_object
    # while Postgres raises duplicate_table for a constraint index name clash.
    # Treating it as unverifiable made the runner try to apply an already-applied
    # migration, which is where "relation already exists" came from.
    conditional = 0
    kept: list[str] = []
    position = 0
    for block in _DO_BLOCK_RE.finditer(body):
        kept.append(body[position:block.start()])
        if _ENV_CONDITIONAL_RE.search(block.group(0)):
            conditional += 1
        else:
            kept.append(block.group(0))
        position = block.end()
    kept.append(body[position:])
    body = "".join(kept)
    _CONDITIONAL_BLOCKS[id(sql)] = conditional
    columns: set[tuple[str, str]] = set()
    for statement in body.split(";"):
        match = _ALTER_TABLE_RE.match(statement)
        if not match:
            continue
        table = match.group(1).split(".")[-1].lower()
        for column in _ADD_COLUMN_RE.findall(statement):
            columns.add((table, column.lower()))
    return {
        "tables": sorted({name.split(".")[-1].lower() for name in _TABLE_RE.findall(body)}),
        "indexes": sorted({name.lower() for name in _INDEX_RE.findall(body)}),
        "columns": sorted(columns),
        "functions": sorted({name.lower() for name in _FUNCTION_RE.findall(body)}),
        # A UNIQUE or PRIMARY KEY constraint creates an index, but under
        # ADD CONSTRAINT rather than CREATE INDEX — 0018's leads_tenant_parcel_key
        # was invisible to the probe, so the file read as unverifiable and the
        # apply then failed on "relation already exists".
        "constraints": sorted({name.lower() for name in _CONSTRAINT_RE.findall(body)}),
        "policies": sorted({name.lower() for name in _POLICY_RE.findall(body)}),
        "triggers": sorted({name.lower() for name in _TRIGGER_RE.findall(body)}),
    }


async def _objects_present(conn, declared: dict) -> tuple[int, int, list[str]]:
    """(present, total, missing) for the objects a migration declares."""
    missing: list[str] = []
    total = 0
    for table in declared["tables"]:
        total += 1
        if not await conn.fetchval("SELECT to_regclass($1)", f"public.{table}"):
            missing.append(f"table {table}")
    for index in declared["indexes"]:
        total += 1
        if not await conn.fetchval(
            "SELECT 1 FROM pg_indexes WHERE schemaname='public' AND indexname=$1", index
        ):
            missing.append(f"index {index}")
    for table, column in declared["columns"]:
        total += 1
        if not await conn.fetchval(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name=$1 AND column_name=$2",
            table, column,
        ):
            missing.append(f"column {table}.{column}")
    for constraint in declared["constraints"]:
        total += 1
        if not await conn.fetchval("SELECT 1 FROM pg_constraint WHERE conname=$1", constraint):
            missing.append(f"constraint {constraint}")
    for policy in declared["policies"]:
        total += 1
        if not await conn.fetchval("SELECT 1 FROM pg_policies WHERE policyname=$1", policy):
            missing.append(f"policy {policy}")
    for trigger in declared["triggers"]:
        total += 1
        if not await conn.fetchval("SELECT 1 FROM pg_trigger WHERE tgname=$1", trigger):
            missing.append(f"trigger {trigger}")
    for function in declared["functions"]:
        total += 1
        if not await conn.fetchval(
            "SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
            "WHERE n.nspname='public' AND p.proname=$1", function
        ):
            missing.append(f"function {function}")
    return total - len(missing), total, missing


async def _reconcile(conn, files: list[str]) -> int:
    """Record migrations whose objects are already present, without re-running them.

    A database built the way dev is — every .sql piped through psql with errors
    silenced — has the schema but no ledger. Switching it to this runner without
    reconciling would re-execute all of them, and the ones that are not
    idempotent would fail.

    Three outcomes per file, and the middle one is the point:

      all objects present   record as 'reconciled' with the evidence
      no objects present    leave unrecorded; it genuinely needs applying
      SOME present          refuse. A half-applied migration is the dangerous
                            case, and guessing either way is how a schema
                            silently diverges.
    """
    await conn.execute(_CREATE_LEDGER_SQL)
    await conn.execute(_EXTEND_LEDGER_SQL)
    recorded = {
        row["filename"] for row in await conn.fetch("SELECT filename FROM schema_migrations")
    }
    reconciled = pending = unverifiable = 0
    partial: list[str] = []
    for path in files:
        name = os.path.basename(path)
        if name in recorded:
            continue
        with open(path, encoding="utf-8") as handle:
            sql = handle.read()
        declared = _declared_objects(sql)
        present, total, missing = await _objects_present(conn, declared)
        if total == 0:
            unverifiable += 1
            print(f"?? {name} declares no probeable object; left unrecorded", flush=True)
            continue
        if present == total:
            conditional = _CONDITIONAL_BLOCKS.get(id(sql), 0)
            evidence = f"all {total} unconditional objects present"
            if conditional:
                evidence += f"; {conditional} conditional block(s) not probed"
            await conn.execute(_RECONCILE_MIGRATION_SQL, name, _sha256(sql), evidence)
            reconciled += 1
            print(f"== {name} reconciled ({total} objects present)", flush=True)
        elif present == 0:
            pending += 1
            print(f"-- {name} not applied ({total} objects absent)", flush=True)
        else:
            partial.append(f"{name}: {present}/{total} present, missing {missing[:4]}")
    for line in partial:
        print(f"!! PARTIAL {line}", flush=True)
    print(
        f"reconcile: {reconciled} recorded, {pending} pending, "
        f"{unverifiable} unverifiable, {len(partial)} partial",
        flush=True,
    )
    return 3 if partial else 0


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
        await conn.execute(_EXTEND_LEDGER_SQL)
        recorded = {
            row["filename"]: row["sha256"]
            for row in await conn.fetch("SELECT filename, sha256 FROM schema_migrations")
        }
        applied = set(recorded)

        print(f"{len(files)} files; {len(applied)} recorded", flush=True)
        processed = 0
        drifted: list[str] = []
        for migration_path in files:
            name = os.path.basename(migration_path)
            with open(migration_path, encoding="utf-8") as migration_file:
                raw = migration_file.read()
            digest = _sha256(raw)

            if name in applied:
                known = recorded.get(name)
                # A recorded file whose contents have changed means the schema
                # in this database is not what the repository now describes.
                # Silence here is how two environments drift apart unnoticed.
                if known and known != digest:
                    print(
                        f"!! {name} CHANGED since it was recorded "
                        f"({known[:12]}… -> {digest[:12]}…); it will NOT be re-applied",
                        flush=True,
                    )
                    drifted.append(name)
                elif known is None:
                    await conn.execute(
                        "UPDATE schema_migrations SET sha256=$2 WHERE filename=$1",
                        name, digest,
                    )
                print(f"-- {name} (recorded, skip)", flush=True)
                continue

            sql = _strip_outer_transaction(raw)

            try:
                async with conn.transaction():
                    await conn.execute(sql)
                    await conn.execute(_RECORD_MIGRATION_SQL, name, digest)
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
        if drifted:
            print(
                f"!! {len(drifted)} recorded migration(s) differ from the files on "
                f"disk: {', '.join(drifted)}. The database does not match the "
                f"repository; resolve with a new migration, never by editing one.",
                flush=True,
            )
        print(f"migrations complete ({processed} processed)", flush=True)
        return 4 if drifted else 0
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
    ctx = _ssl_context(host)

    await _ensure_database(host=host, port=port, db=db, user=user, pw=pw, ctx=ctx)
    conn = await asyncpg.connect(
        host=host, port=port, user=user, password=pw, database=db, ssl=ctx
    )
    try:
        files = sorted(glob.glob(os.path.join(MIGRATIONS_DIR, "*.sql")))
        if "--reconcile" in sys.argv:
            return await _reconcile(conn, files)
        return await _run_migrations(conn, files)
    finally:
        await conn.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
