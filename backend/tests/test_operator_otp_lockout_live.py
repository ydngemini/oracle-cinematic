"""The operator email-OTP lock engages on real PostgreSQL (external audit 28.1).

auth._require_operator_email_otp raised its 401 INSIDE tenant_tx, so the
attempt counter it had just written was rolled back: the five-attempt lock
never engaged and the 6-digit code could be guessed without limit for its
10-minute life. The unit test passed only because its fake transaction never
rolled back. This runs the real function against real PostgreSQL (skipped
without ORACLE_LIVE_DB_ADMIN_DSN); no mail is sent (the sender is replaced).
"""
from __future__ import annotations

import asyncio
import os
import uuid

import pytest

DSN = os.getenv("ORACLE_LIVE_DB_ADMIN_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="needs ORACLE_LIVE_DB_ADMIN_DSN (real PostgreSQL)")


def test_five_wrong_codes_lock_the_challenge_in_postgres(monkeypatch):
    from fastapi import HTTPException

    import auth

    account = f"ops-{uuid.uuid4().hex[:8]}@otp.test"
    sent: list[str] = []
    monkeypatch.setattr(auth, "_ADMIN_OTP_EMAIL", "owner@example.test")
    monkeypatch.setattr(auth, "_ADMIN_TOTP_SECRET", "")
    monkeypatch.setattr(auth, "_send_operator_code", lambda _to, code: sent.append(code))

    async def go():
        import asyncpg

        from db import connection

        admin = await asyncpg.connect(DSN)
        await connection.init_pool(min_size=1, max_size=2)
        try:
            with pytest.raises(HTTPException) as required:
                await auth._require_operator_email_otp(account, None)
            assert required.value.detail["code"] == "OTP_REQUIRED"
            code = sent[0]
            wrong = "000000" if code != "000000" else "111111"
            statuses = []
            for _ in range(5):
                with pytest.raises(HTTPException) as refused:
                    await auth._require_operator_email_otp(account, wrong)
                statuses.append(refused.value.status_code)
            row = await admin.fetchrow(
                "SELECT attempts, consumed_at FROM operator_otp_challenges WHERE account = $1 "
                "ORDER BY created_at DESC LIMIT 1", account)
            with pytest.raises(HTTPException) as after_lock:          # even the right code
                await auth._require_operator_email_otp(account, code)
            return statuses, row, after_lock.value.status_code
        finally:
            await admin.execute("DELETE FROM operator_otp_challenges WHERE account = $1", account)
            await admin.close()
            await connection.close_pool()

    statuses, row, after = asyncio.run(go())
    assert statuses == [401] * 5
    assert row["attempts"] == 5, f"the attempt counter did not persist: {row['attempts']}"
    assert row["consumed_at"] is not None, "five wrong codes must retire the challenge"
    assert after == 401


def test_an_expired_code_is_refused_in_postgres(monkeypatch):
    """Review of #86: nothing pinned `expires_at > now()`; the right code after
    expiry must be refused."""
    from fastapi import HTTPException

    import auth

    account = f"ops-{uuid.uuid4().hex[:8]}@otp.test"
    sent: list[str] = []
    monkeypatch.setattr(auth, "_ADMIN_OTP_EMAIL", "owner@example.test")
    monkeypatch.setattr(auth, "_ADMIN_TOTP_SECRET", "")
    monkeypatch.setattr(auth, "_send_operator_code", lambda _to, code: sent.append(code))

    async def go():
        import asyncpg

        from db import connection

        admin = await asyncpg.connect(DSN)
        await connection.init_pool(min_size=1, max_size=2)
        try:
            with pytest.raises(HTTPException):
                await auth._require_operator_email_otp(account, None)
            await admin.execute("UPDATE operator_otp_challenges SET expires_at = now() - interval '1 second' "
                                "WHERE account = $1", account)
            with pytest.raises(HTTPException) as refused:
                await auth._require_operator_email_otp(account, sent[0])
            return refused.value.status_code
        finally:
            await admin.execute("DELETE FROM operator_otp_challenges WHERE account = $1", account)
            await admin.close()
            await connection.close_pool()

    assert asyncio.run(go()) == 401
