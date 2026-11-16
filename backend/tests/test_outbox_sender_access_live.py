"""A queued CRM email leaves only on the sender's CURRENT authority (external
audit 26.2), against real PostgreSQL with FORCE RLS as the application role
(skipped without ORACLE_LIVE_DB_ADMIN_DSN).

email_outbox.deliver() rechecked billing, suppression and the footer, then
sent as the stored created_by without asking whether that person could still
contact the client. This drives commands_api.requester_may_contact_record (the
check deliver() now makes) through each way access is lost after queueing:
reassignment, an edit share revoked, a view-only share, and the sender's
deactivation.
"""
from __future__ import annotations

import asyncio
import os
import uuid

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")


def test_the_outbox_sender_must_still_reach_the_client():
    assert asyncio.run(_scenario()) == {
        "own client": True,
        "colleague's private client": False,
        "after reassignment to a colleague": False,
        "edit share": True,
        "edit share revoked": False,
        "view-only share": False,
        "deactivated, own client (require_member)": False,
        "deactivated, own client (command-worker semantics)": True,
        "unknown sender (require_member)": False,
        "broker owner": True,
    }


async def _scenario() -> dict[str, bool]:
    import asyncpg

    from commands_api import requester_may_contact_record
    from db import connection

    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=2)
    tenant, tag = str(uuid.uuid4()), uuid.uuid4().hex[:8]
    users = {k: uuid.uuid4() for k in ("owner", "a", "b", "c")}
    agent = {k: f"{k}-{tag}@outbox.test" for k in users}
    client_a, client_b = uuid.uuid4(), uuid.uuid4()
    out: dict[str, bool] = {}

    async def may(who: str, client, **kw) -> bool:
        return await requester_may_contact_record(tenant, agent.get(who, who), str(client), None, **kw)

    try:
        await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,'Outbox access')",
                            tenant, f"outbox-{tag}")
        for who, uid in users.items():
            await admin.execute(
                "INSERT INTO users (id, tenant_id, agent_id, role, email, password_hash, full_name, is_active) "
                "VALUES ($1,$2,$3,$4,$3,'scrypt$x$y',$5,true)",
                uid, tenant, agent[who], "broker_owner" if who == "owner" else "agent", f"Agent {who}")
        for cid, who in ((client_a, "a"), (client_b, "b")):
            await admin.execute(
                "INSERT INTO clients (id, tenant_id, full_name, assignee_id, email) VALUES ($1,$2,$3,$4,$5)",
                cid, tenant, f"Client {who}", agent[who], f"{who}-{tag}@client.test")

        out["own client"] = await may("a", client_a, require_member=True)
        out["colleague's private client"] = await may("a", client_b, require_member=True)

        await admin.execute("UPDATE clients SET assignee_id=$2 WHERE id=$1", client_a, agent["b"])
        out["after reassignment to a colleague"] = await may("a", client_a, require_member=True)
        await admin.execute("UPDATE clients SET assignee_id=$2 WHERE id=$1", client_a, agent["a"])

        await admin.execute(
            "INSERT INTO record_shares (tenant_id, resource_kind, resource_id, grantee_user_id, permission, "
            "granted_by) VALUES ($1,'client',$2,$3,'edit',$4)", tenant, client_b, users["c"], agent["b"])
        out["edit share"] = await may("c", client_b, require_member=True)
        await admin.execute("DELETE FROM record_shares WHERE resource_id=$1 AND grantee_user_id=$2",
                            client_b, users["c"])
        out["edit share revoked"] = await may("c", client_b, require_member=True)
        await admin.execute(
            "INSERT INTO record_shares (tenant_id, resource_kind, resource_id, grantee_user_id, permission, "
            "granted_by) VALUES ($1,'client',$2,$3,'view',$4)", tenant, client_b, users["c"], agent["b"])
        out["view-only share"] = await may("c", client_b, require_member=True)

        await admin.execute("UPDATE users SET is_active=false WHERE id=$1", users["a"])
        out["deactivated, own client (require_member)"] = await may("a", client_a, require_member=True)
        out["deactivated, own client (command-worker semantics)"] = await may("a", client_a)
        out["unknown sender (require_member)"] = await may(f"ghost-{tag}@outbox.test", client_a,
                                                           require_member=True)
        out["broker owner"] = await may("owner", client_b, require_member=True)
        return out
    finally:
        await admin.execute("DELETE FROM record_shares WHERE tenant_id=$1", tenant)
        await admin.execute("DELETE FROM clients WHERE tenant_id=$1", tenant)
        await admin.execute("DELETE FROM users WHERE tenant_id=$1", tenant)
        await admin.execute("DELETE FROM tenants WHERE id=$1", tenant)
        await admin.close()
        await connection.close_pool()
