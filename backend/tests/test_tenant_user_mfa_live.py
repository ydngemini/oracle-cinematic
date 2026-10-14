"""Two-step sign-in for tenant users, against real PostgreSQL with FORCE RLS
(L17-19; skipped without ORACLE_LIVE_DB_ADMIN_DSN).

Proves, through the real routes and the application roles:
- a broker owner without two-step signs in to an enrolment-only session:
  every ordinary route answers 403 mfa_enrolment_required; the policy routes
  work and their renewal keeps the session held;
- setup seals the secret with the brokerage key (no plaintext in the row), a
  brokerage's own context cannot read the factor tables, confirmation needs
  a real code, turns it on, returns 10 recovery codes, ends other sessions
  and is audited;
- sign-in then needs a code: none → OTP_REQUIRED, the code already used →
  refused (replay inside its validity window), a fresh one → in;
- a recovery code works once;
- five wrong codes lock the factor (429) even for the right code; audited;
- a password reset link alone does not get past an enabled factor;
- a broker owner resets an agent's factor (sessions end, audited), cannot
  reach another owner's; a platform admin can (audited as an admin action);
- the brokerage-wide switch holds agents on the setup screen; audited;
- turning it off as an owner sends the session straight back to setup.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")
MASTER = "live-test-mfa-master-key-0123456789abcdef"
PASSWORD = "correct horse battery staple"


def test_tenant_user_two_step_sign_in(monkeypatch):
    monkeypatch.setenv("ORACLE_ENCRYPTION_MASTER_KEY", MASTER)
    asyncio.run(_scenario(monkeypatch))


def _cookie(response) -> str:
    header = response.headers.get("set-cookie", "")
    return header.split("oracle_session=", 1)[1].split(";", 1)[0]


async def _scenario(monkeypatch):
    import asyncpg
    from fastapi import HTTPException, Response

    import auth
    import brokerage_onboarding
    import mfa_api
    import operator_api
    import policy_acceptance
    import tenancy
    import totp
    import user_mfa
    from crypto import derive_tenant_key
    from db import connection
    from tenancy import Role, TenantContext

    async def no_limit(*_a, **_k):
        return 10, 10, int(time.time()) + 60

    monkeypatch.setattr(auth, "_check_rate_limit", no_limit)

    admin = await asyncpg.connect(DSN)
    await connection.init_pool(min_size=1, max_size=4)
    tenant, other_tenant = str(uuid.uuid4()), str(uuid.uuid4())
    stamp = tenant[:8]
    owner_email, agent_email = f"owner-{stamp}@mfa.test", f"agent-{stamp}@mfa.test"
    owner2_email = f"owner2-{stamp}@mfa.test"
    owner_id, agent_id, owner2_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    pw_hash = auth._hash_pw(PASSWORD)

    async def login(email, otp=None):
        response = Response()
        result = await auth.login(auth.LoginRequest(agent_id=email, passphrase=PASSWORD, otp=otp), response)
        return result, _cookie(response)

    async def refused(coro, status, code=None):
        with pytest.raises(HTTPException) as exc:
            await coro
        assert exc.value.status_code == status, (exc.value.status_code, exc.value.detail)
        if code:
            assert isinstance(exc.value.detail, dict) and exc.value.detail.get("code") == code, exc.value.detail
        return exc.value

    async def audit_actions():
        rows = await admin.fetch(
            "SELECT action, category FROM audit_ledger WHERE tenant_id = $1 ORDER BY created_at", tenant)
        return [(r["action"], r["category"]) for r in rows]

    async def secret_of(user_id):
        row = await admin.fetchrow("SELECT totp_secret_ciphertext AS c FROM user_mfa_factors WHERE user_id=$1", user_id)
        return await admin.fetchval("SELECT pgp_sym_decrypt($1, $2)", row["c"], derive_tenant_key(tenant, MASTER))

    def code_at_offset(secret, offset):
        return totp.code_at(secret, int(time.time() // totp.STEP_SECONDS) + offset)

    try:
        for tid, name in ((tenant, "MFA live"), (other_tenant, "MFA other")):
            await admin.execute("INSERT INTO tenants (id, slug, name) VALUES ($1,$2,$3)", tid, f"mfa-{tid[:8]}", name)
        for uid, email, role, tid in ((owner_id, owner_email, "broker_owner", tenant),
                                      (agent_id, agent_email, "agent", tenant),
                                      (owner2_id, owner2_email, "broker_owner", tenant)):
            await admin.execute(
                "INSERT INTO users (id, tenant_id, agent_id, role, email, password_hash, policy_acceptance_required) "
                "VALUES ($1,$2,$3,$4,$3,$5,true)", uid, tid, email, role, pw_hash)

        # ── a broker owner without two-step: enrolment-only session ─────────
        result, token = await login(owner_email)
        assert result.mfa_enrolment_required is True
        await refused(asyncio.to_thread(tenancy.require_context, f"Bearer {token}"), 403)  # policy first
        pctx = tenancy.require_policy_context(f"Bearer {token}")
        response = Response()
        status_now = await policy_acceptance.policy_acceptance_status(pctx, response)
        assert status_now.required is True  # the policy gate still comes first
        response = Response()
        accepted = await policy_acceptance.accept_policy(
            policy_acceptance.PolicyAcceptanceRequest(
                policy_version=status_now.policy_version,
                account_security_version=status_now.account_security_version),
            pctx, response)
        assert accepted.accepted is True
        token = _cookie(response)
        assert auth.decode_token(token)["mfa_enrol"] is True, "policy acceptance lifted the gate"
        await refused(asyncio.to_thread(tenancy.require_context, f"Bearer {token}"), 403, "mfa_enrolment_required")
        mctx = tenancy.require_mfa_context(f"Bearer {token}")
        state = await mfa_api.mfa_status(mctx)
        assert state["enrolment_required"] is True and state["required_reason"] == "role"

        # ── setup: sealed, platform-only, needs a real code ────────────────
        setup = await mfa_api.start_setup(mfa_api.SetupRequest(), mctx)
        secret = setup["secret"].replace(" ", "")
        assert setup["otpauth_uri"].startswith("otpauth://totp/Neoh:") and secret in setup["otpauth_uri"]
        stored = await admin.fetchrow("SELECT * FROM user_mfa_factors WHERE user_id=$1", owner_id)
        assert stored["confirmed_at"] is None and secret.encode() not in bytes(stored["totp_secret_ciphertext"])
        assert await secret_of(owner_id) == secret
        owner_ctx = TenantContext(owner_email, tenant, Role.BROKER_OWNER)
        async with connection.tenant_tx(owner_ctx) as conn:
            assert await conn.fetchval("SELECT count(*) FROM user_mfa_factors") == 0, "RLS let a brokerage read factors"
            assert await conn.fetchval("SELECT count(*) FROM user_mfa_recovery_codes") == 0
        wrong = "000000" if code_at_offset(secret, 0) != "000000" else "111111"
        await refused(mfa_api.confirm_setup(mfa_api.CodeRequest(code=wrong), Response(), mctx), 400, "OTP_INVALID")
        epoch_before = await admin.fetchval("SELECT session_epoch FROM users WHERE id=$1", owner_id)
        first_code = code_at_offset(secret, 0)
        response = Response()
        confirmed = await mfa_api.confirm_setup(mfa_api.CodeRequest(code=first_code), response, mctx)
        recovery = confirmed["recovery_codes"]
        assert confirmed["enabled"] is True and len(recovery) == 10
        assert await admin.fetchval("SELECT session_epoch FROM users WHERE id=$1", owner_id) == epoch_before + 1
        renewed = auth.decode_token(_cookie(response))
        assert "mfa_enrol" not in renewed and renewed["sep"] == epoch_before + 1
        tenancy.require_context(f"Bearer {_cookie(response)}")  # an ordinary session now
        hashes = {r["code_hash"] for r in await admin.fetch(
            "SELECT code_hash FROM user_mfa_recovery_codes WHERE user_id=$1", owner_id)}
        assert hashes == {user_mfa.hash_recovery_code(c) for c in recovery}
        assert ("auth.mfa.enabled", "USER_STATE_CHANGE") in await audit_actions()

        # ── sign-in now needs a code; a used code is refused ──────────────
        await refused(login(owner_email), 401, "OTP_REQUIRED")
        await refused(login(owner_email, first_code), 401, "OTP_INVALID")   # replay of the setup code
        fresh_code = code_at_offset(secret, 1)
        result, token = await login(owner_email, fresh_code)
        assert result.mfa_enrolment_required is False and "mfa_enrol" not in auth.decode_token(token)
        await refused(login(owner_email, fresh_code), 401, "OTP_INVALID")   # replay within its window

        # ── a recovery code works once ─────────────────────────────────────
        result, _ = await login(owner_email, recovery[0].lower())
        assert result.mfa_enrolment_required is False
        await refused(login(owner_email, recovery[0]), 401, "OTP_INVALID")
        assert ("auth.mfa.recovery_code_used", "USER_STATE_CHANGE") in await audit_actions()
        assert (await mfa_api.mfa_status(tenancy.require_mfa_context(f"Bearer {token}")))[
            "recovery_codes_remaining"] == 9

        # ── five wrong codes lock it, even against the right code ─────────
        # (A success clears the counter; the refused replays above left one.)
        assert await admin.fetchval("SELECT failed_attempts FROM user_mfa_factors WHERE user_id=$1", owner_id) == 1
        await admin.execute("UPDATE user_mfa_factors SET failed_attempts = 0 WHERE user_id=$1", owner_id)
        for attempt in range(user_mfa.MAX_FAILED_CODES):
            error = await refused(login(owner_email, "999999" if fresh_code != "999999" else "888888"),
                                  429 if attempt == user_mfa.MAX_FAILED_CODES - 1 else 401)
        assert error.detail["code"] == "MFA_LOCKED"
        await refused(login(owner_email, recovery[1]), 429, "MFA_LOCKED")
        assert ("auth.mfa.locked", "USER_STATE_CHANGE") in await audit_actions()
        await admin.execute("UPDATE user_mfa_factors SET locked_until = now() - interval '1 second' WHERE user_id=$1",
                            owner_id)
        result, token = await login(owner_email, recovery[1])
        owner_session = token

        # ── a reset link alone does not pass an enabled factor ────────────
        links = []
        monkeypatch.setattr(auth, "_send_reset_email", lambda _to, link: links.append(link))
        await auth._issue_password_reset(owner_email)
        reset_token = links[-1].split("reset=", 1)[1]
        await refused(auth.reset_password(auth.ResetRequest(token=reset_token, new_password=PASSWORD), Response()),
                      401, "OTP_REQUIRED")
        response = Response()
        reset = await auth.reset_password(
            auth.ResetRequest(token=reset_token, new_password=PASSWORD, otp=recovery[2]), response)
        assert reset.mfa_enrolment_required is False
        await refused(auth.reset_password(  # the link is spent now
            auth.ResetRequest(token=reset_token, new_password=PASSWORD, otp=recovery[3]), Response()), 400)
        result, owner_session = await login(owner_email, recovery[3])

        # ── an agent: optional, owner can reset it ─────────────────────────
        result, agent_token = await login(agent_email)
        assert result.mfa_enrolment_required is False
        actx = tenancy.require_policy_context(f"Bearer {agent_token}")
        st = await policy_acceptance.policy_acceptance_status(actx, Response())
        response = Response()
        await policy_acceptance.accept_policy(policy_acceptance.PolicyAcceptanceRequest(
            policy_version=st.policy_version, account_security_version=st.account_security_version), actx, response)
        actx = tenancy.require_mfa_context(f"Bearer {_cookie(response)}")
        agent_setup = await mfa_api.start_setup(mfa_api.SetupRequest(), actx)
        agent_secret = agent_setup["secret"].replace(" ", "")
        await mfa_api.confirm_setup(mfa_api.CodeRequest(code=code_at_offset(agent_secret, 0)), Response(), actx)
        octx = tenancy.require_context(f"Bearer {owner_session}")
        team = await brokerage_onboarding.get_team(octx)
        flags = {m["agent_id"]: m.get("mfa_enabled") for m in team["members"]}
        assert flags[owner_email] is True and flags[agent_email] is True and flags[owner2_email] is False
        actx_now = tenancy.require_context(f"Bearer {await _fresh_agent_session(login, agent_email, agent_secret)}")
        agent_team = await brokerage_onboarding.get_team(actx_now)
        assert all("mfa_enabled" not in m for m in agent_team["members"]), "an agent sees who lacks two-step"
        # An agent cannot reset anyone; an owner cannot reach another owner.
        reason = brokerage_onboarding.MemberStatusChange(reason="lost phone")
        await refused(brokerage_onboarding.reset_member_two_step(owner_id, reason, actx_now), 403)
        await refused(brokerage_onboarding.reset_member_two_step(owner2_id, reason, octx), 404)
        agent_epoch = await admin.fetchval("SELECT session_epoch FROM users WHERE id=$1", agent_id)
        done = await brokerage_onboarding.reset_member_two_step(agent_id, reason, octx)
        assert done["mfa_enabled"] is False
        assert await admin.fetchval("SELECT count(*) FROM user_mfa_factors WHERE user_id=$1", agent_id) == 0
        assert await admin.fetchval("SELECT count(*) FROM user_mfa_recovery_codes WHERE user_id=$1", agent_id) == 0
        assert await admin.fetchval("SELECT session_epoch FROM users WHERE id=$1", agent_id) == agent_epoch + 1
        assert ("team.member.mfa_reset", "USER_STATE_CHANGE") in await audit_actions()
        await refused(brokerage_onboarding.reset_member_two_step(agent_id, reason, octx), 409)

        # ── the brokerage-wide switch holds agents on setup ───────────────
        await brokerage_onboarding.put_security_settings(
            brokerage_onboarding.SecuritySettings(require_mfa=True), octx)
        assert (await brokerage_onboarding.get_security_settings(octx))["require_mfa"] is True
        await refused(brokerage_onboarding.put_security_settings(
            brokerage_onboarding.SecuritySettings(require_mfa=False), actx_now), 403)
        result, agent_token = await login(agent_email)
        assert result.mfa_enrolment_required is True and auth.decode_token(agent_token)["mfa_enrol"] is True
        assert ("brokerage.mfa_policy.changed", "USER_STATE_CHANGE") in await audit_actions()

        # ── the operator resets an owner (admin action) ───────────────────
        platform = TenantContext("ops@neoh.test", "00000000-0000-0000-0000-000000000000", Role.PLATFORM_ADMIN)
        result_reset = await operator_api.reset_user_two_step(owner_id, operator_api.MfaResetBody(reason="ticket 42"),
                                                              platform)
        assert result_reset["mfa_enabled"] is False
        assert ("admin.user.mfa_reset", "ADMIN_ACTION") in await audit_actions()
        result, token = await login(owner_email)
        assert result.mfa_enrolment_required is True  # back to setup at the next sign-in

        # ── an owner turning it off goes straight back to setup ───────────
        mctx = tenancy.require_mfa_context(f"Bearer {token}")
        setup = await mfa_api.start_setup(mfa_api.SetupRequest(), mctx)
        secret = setup["secret"].replace(" ", "")
        response = Response()
        await mfa_api.confirm_setup(mfa_api.CodeRequest(code=code_at_offset(secret, 0)), response, mctx)
        mctx = tenancy.require_mfa_context(f"Bearer {_cookie(response)}")
        await refused(mfa_api.disable(mfa_api.ProtectedRequest(password="not my password", code=code_at_offset(secret, 1)),
                                      Response(), mctx), 403)
        response = Response()
        off = await mfa_api.disable(mfa_api.ProtectedRequest(password=PASSWORD, code=code_at_offset(secret, 1)),
                                    response, mctx)
        assert off == {"enabled": False, "enrolment_required": True, "token": off["token"]}
        assert auth.decode_token(_cookie(response))["mfa_enrol"] is True
        assert ("auth.mfa.disabled", "USER_STATE_CHANGE") in await audit_actions()

        # No audit row carries a code or a secret.
        metadata = " ".join(str(r["metadata"]) for r in await admin.fetch(
            "SELECT metadata FROM audit_ledger WHERE tenant_id = $1", tenant))
        assert secret not in metadata and recovery[4] not in metadata
    finally:
        for table in ("user_mfa_recovery_codes", "user_mfa_factors", "user_policy_acceptances",
                      "account_security_acceptances", "password_reset_tokens", "team_memberships"):
            for tid in (tenant, other_tenant):
                await admin.execute(_DELETE_BY_TENANT[table], tid)
        for tid in (tenant, other_tenant):
            await admin.execute("DELETE FROM users WHERE tenant_id = $1::uuid", tid)
            await admin.execute("DELETE FROM tenants WHERE id = $1::uuid", tid)
        await admin.close()
        await connection.close_pool()


async def _fresh_agent_session(login, email, secret):
    import totp

    step = int(time.time() // totp.STEP_SECONDS) + 1
    _, token = await login(email, totp.code_at(secret, step))
    return token


_DELETE_BY_TENANT = {
    "user_mfa_recovery_codes": "DELETE FROM user_mfa_recovery_codes WHERE tenant_id = $1::uuid",
    "user_mfa_factors": "DELETE FROM user_mfa_factors WHERE tenant_id = $1::uuid",
    "user_policy_acceptances": "DELETE FROM user_policy_acceptances WHERE tenant_id = $1::uuid",
    "account_security_acceptances": "DELETE FROM account_security_acceptances WHERE tenant_id = $1::uuid",
    "password_reset_tokens": "DELETE FROM password_reset_tokens WHERE tenant_id = $1::uuid",
    "team_memberships": "DELETE FROM team_memberships WHERE tenant_id = $1::uuid",
}
