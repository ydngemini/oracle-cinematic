#!/usr/bin/env python3
"""Quarterly access review export (ISO 27001 A.5.18 / A.8.2; ledger L17-13). Read-only.

Lists who holds privileged access to Neoh, so the reviewer can confirm each
grant is still needed and record the decision:

  1. PostgreSQL roles: rolsuper, rolbypassrls, rolcreaterole, rolcreatedb,
     rolcanlogin, rolreplication, the roles each is a member of, and its
     members. Built-in pg_* roles are left out.
  2. Neoh platform admins: the environment operator account (ORACLE_ADMIN_ID)
     and every users row with role platform_admin.
  3. Broker owners per brokerage: count and agent ids (active and suspended).
  4. Operator second factor: which factor is configured (totp | email | none)
     and when the boot check last recorded it (security_alerts.py). Never the
     secret.

    python scripts/access_review.py                 # markdown to stdout
    python scripts/access_review.py --format csv    # one CSV, a `section` column per row

The output names people (agent ids are email addresses). It is personal data:
keep it with the review record, not in a ticket or chat, and delete it with
that record.

Everything runs in one READ ONLY transaction on the platform login (the same
connection settings as the app: ORACLE_DB_HOST, ORACLE_DB_PLATFORM_USER, ...).
It makes no external API calls.

Manual checklist (outside this script; record each result in the review):
  - GitHub: organisation owners and repository admins; outside collaborators;
    deploy keys; Actions secrets and who can edit them; branch protection on
    main (a push to main deploys staging, CONTRACT C4.4).
  - DigitalOcean: team members and their roles; API tokens (name, scope,
    expiry; the CI token is scoped); database trusted sources
    (scripts/db-access-window.sh closes them again); App Platform env secrets.
  - Stripe: team members and roles; restricted and secret API keys; webhook
    endpoints.
  - Email / DNS registrar: who can sign in; MFA on each account.
  - Telephony and AI providers (Plivo, Twilio, ElevenLabs, Fireworks): account
    users and API keys in use (docs/subprocessor-inventory.md).
  - Laptops and password manager: who holds ORACLE_ADMIN_PASSPHRASE and the
    operator TOTP seed; rotate both if anyone who held them left.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import os
import sys
from typing import Any

BACKEND = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend")
sys.path.insert(0, os.path.abspath(BACKEND))

DB_ROLES_SQL = """
SELECT r.rolname, r.rolsuper, r.rolbypassrls, r.rolcreaterole, r.rolcreatedb,
       r.rolcanlogin, r.rolreplication,
       COALESCE(ARRAY(SELECT g.rolname FROM pg_auth_members m JOIN pg_roles g ON g.oid = m.roleid
                       WHERE m.member = r.oid ORDER BY 1), '{}') AS member_of,
       COALESCE(ARRAY(SELECT u.rolname FROM pg_auth_members m JOIN pg_roles u ON u.oid = m.member
                       WHERE m.roleid = r.oid ORDER BY 1), '{}') AS members
  FROM pg_roles r
 WHERE r.rolname NOT LIKE 'pg\\_%'
 ORDER BY r.rolsuper DESC, r.rolbypassrls DESC, r.rolname
"""

PLATFORM_ADMINS_SQL = """
SELECT agent_id, tenant_id::text AS tenant_id, is_active, created_at
  FROM users WHERE role = 'platform_admin' ORDER BY agent_id
"""

BROKER_OWNERS_SQL = """
SELECT t.id::text AS tenant_id, t.name AS tenant_name, t.lifecycle_state,
       count(*) FILTER (WHERE u.is_active)     AS active_owners,
       count(*) FILTER (WHERE NOT u.is_active) AS suspended_owners,
       ARRAY_AGG(u.agent_id ORDER BY u.agent_id) FILTER (WHERE u.is_active)     AS active_ids,
       ARRAY_AGG(u.agent_id ORDER BY u.agent_id) FILTER (WHERE NOT u.is_active) AS suspended_ids
  FROM users u JOIN tenants t ON t.id = u.tenant_id
 WHERE u.role = 'broker_owner'
 GROUP BY t.id, t.name, t.lifecycle_state
 ORDER BY t.name, t.id
"""

FACTOR_CHECK_SQL = """
SELECT created_at, metadata FROM audit_ledger
 WHERE category = 'ADMIN_ACTION' AND action = 'operator_second_factor_configured'
 ORDER BY created_at DESC LIMIT 1
"""

PLATFORM_CONTEXT_SQL = (
    "SELECT set_config('app.current_role', 'platform_admin', true), "
    "set_config('app.current_tenant', '00000000-0000-0000-0000-000000000000', true)"
)


def operator_factor_from_env(env: dict[str, str]) -> str:
    if "".join(env.get("ORACLE_ADMIN_TOTP_SECRET", "").split()):
        return "totp"
    if env.get("ORACLE_ADMIN_OTP_EMAIL", "").strip():
        return "email"
    return "none"


async def collect(conn, env: dict[str, str] | None = None) -> dict[str, Any]:
    """Run every listing in one read-only transaction on `conn`."""
    env = dict(os.environ if env is None else env)
    async with conn.transaction(readonly=True):
        await conn.execute(PLATFORM_CONTEXT_SQL)
        roles = [dict(r) for r in await conn.fetch(DB_ROLES_SQL)]
        admins = [dict(r) for r in await conn.fetch(PLATFORM_ADMINS_SQL)]
        owners = [dict(r) for r in await conn.fetch(BROKER_OWNERS_SQL)]
        factor_row = await conn.fetchrow(FACTOR_CHECK_SQL)
    recorded = None
    if factor_row:
        meta = factor_row["metadata"]
        meta = json.loads(meta) if isinstance(meta, str) else (meta or {})
        recorded = {"factor": meta.get("factor"), "recorded_at": factor_row["created_at"].isoformat()}
    operator_id = env.get("ORACLE_ADMIN_ID", "").strip()
    return {
        "db_roles": roles,
        "platform_admins": (
            ([{"agent_id": operator_id, "source": "environment (ORACLE_ADMIN_ID)", "is_active": True,
               "tenant_id": "00000000-0000-0000-0000-000000000000"}] if operator_id else [])
            + [{**a, "source": "users table"} for a in admins]),
        "broker_owners": owners,
        "operator_mfa": {
            "operator_configured": bool(operator_id),
            "factor_in_this_environment": operator_factor_from_env(env) if operator_id else "n/a",
            "last_recorded_by_boot_check": recorded,
        },
    }


def _cell(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return "; ".join(str(v) for v in value) if value else "-"
    if value is None:
        return "-"
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value).replace("|", "\\|")


_ROLE_COLS = ["rolname", "rolsuper", "rolbypassrls", "rolcreaterole", "rolcreatedb", "rolcanlogin",
              "rolreplication", "member_of", "members"]
_ADMIN_COLS = ["agent_id", "source", "is_active", "tenant_id"]
_OWNER_COLS = ["tenant_id", "tenant_name", "lifecycle_state", "active_owners", "suspended_owners",
               "active_ids", "suspended_ids"]


def _md_table(rows: list[dict], cols: list[str]) -> list[str]:
    if not rows:
        return ["(none)"]
    out = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    out += ["| " + " | ".join(_cell(r.get(c)) for c in cols) + " |" for r in rows]
    return out


def render_markdown(report: dict[str, Any]) -> str:
    mfa = report["operator_mfa"]
    rec = mfa["last_recorded_by_boot_check"]
    flagged = [r["rolname"] for r in report["db_roles"] if r["rolsuper"] or r["rolbypassrls"]]
    lines = ["# Neoh access review", "",
             "Read-only export (scripts/access_review.py). Confirm or revoke each grant and record the decision.",
             "", "## 1. Database roles", "",
             f"Superuser or BYPASSRLS: {', '.join(flagged) if flagged else 'none'}.", ""]
    lines += _md_table(report["db_roles"], _ROLE_COLS)
    lines += ["", f"## 2. Platform admins ({len(report['platform_admins'])})", ""]
    lines += _md_table(report["platform_admins"], _ADMIN_COLS)
    total = sum(r["active_owners"] for r in report["broker_owners"])
    lines += ["", f"## 3. Broker owners: {total} active across {len(report['broker_owners'])} brokerages", ""]
    lines += _md_table(report["broker_owners"], _OWNER_COLS)
    lines += ["", "## 4. Operator second factor", "",
              f"- Operator account configured: {'yes' if mfa['operator_configured'] else 'no'}",
              f"- Factor in this environment: {mfa['factor_in_this_environment']}",
              "- Last recorded by the boot check: "
              + (f"{rec['factor']} at {rec['recorded_at']}" if rec else "never"),
              "", "## 5. Outside this script", "",
              "Work through the manual checklist in the script's docstring (GitHub, DigitalOcean, "
              "Stripe, registrar, providers, secret holders).", ""]
    return "\n".join(lines)


def render_csv(report: dict[str, Any]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["section", "name", "attributes"])
    for r in report["db_roles"]:
        writer.writerow(["db_role", r["rolname"],
                         "; ".join(f"{c}={_cell(r[c])}" for c in _ROLE_COLS[1:])])
    for a in report["platform_admins"]:
        writer.writerow(["platform_admin", a["agent_id"], f"source={a['source']}; active={a['is_active']}"])
    for o in report["broker_owners"]:
        writer.writerow(["broker_owners", o["tenant_id"],
                         f"name={o['tenant_name']}; state={o['lifecycle_state']}; active={o['active_owners']}; "
                         f"suspended={o['suspended_owners']}; active_ids={_cell(o['active_ids'])}; "
                         f"suspended_ids={_cell(o['suspended_ids'])}"])
    mfa = report["operator_mfa"]
    rec = mfa["last_recorded_by_boot_check"]
    writer.writerow(["operator_mfa", "operator",
                     f"configured={mfa['operator_configured']}; factor={mfa['factor_in_this_environment']}; "
                     f"last_recorded={(rec['factor'] + '@' + rec['recorded_at']) if rec else 'never'}"])
    return buf.getvalue()


async def main(fmt: str) -> int:
    from db import connection

    await connection.init_pool(min_size=1, max_size=2)
    try:
        pool = connection.get_platform_pool()
        async with pool.acquire() as conn:
            report = await collect(conn)
    finally:
        await connection.close_pool()
    sys.stdout.write(render_csv(report) if fmt == "csv" else render_markdown(report))
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--format", choices=("markdown", "csv"), default="markdown")
    sys.exit(asyncio.run(main(parser.parse_args().format)))
