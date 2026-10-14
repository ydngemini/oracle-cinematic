"""Password storage meets OWASP's scrypt cost; old hashes upgrade on login (L17-15).

Before: scrypt N=2^14, r=8, p=1 — a quarter of OWASP's minimum — computed
inline on the event loop, and any 10-character password accepted, including
"1234567890" and "qwertyuiop".

After: N=2^15, r=8, p=3 (one of the five settings the OWASP Password Storage
Cheat Sheet lists as equivalent to its N=2^17 minimum, at 32 MiB instead of
128 MiB), parameters stored in the hash, hashing on a two-thread pool, the old
form still verifying and rewritten on the next successful login, and a bundled
list of common passwords refused when a password is set.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import time
from contextlib import asynccontextmanager

import pytest
from fastapi import HTTPException, Response

import auth

PASSWORD = "a-perfectly-fine-passphrase"
USER_ID = "22222222-2222-2222-2222-222222222222"
TENANT_ID = "11111111-1111-1111-1111-111111111111"


def _legacy_hash(password: str) -> str:
    """Exactly what auth._hash_pw wrote before L17-15."""
    salt = b"0123456789abcdef"
    dk = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return "scrypt$%s$%s" % (base64.b64encode(salt).decode(), base64.b64encode(dk).decode())


def test_new_hashes_carry_owasp_equivalent_parameters():
    stored = auth._hash_pw(PASSWORD)

    assert stored.startswith("scrypt$ln=15,r=8,p=3$")
    assert auth._verify_pw(PASSWORD, stored)
    assert not auth._verify_pw(PASSWORD + "x", stored)
    assert auth._pw_hash_is_current(stored)
    # OWASP equivalence: N*p at least 2^15*3, and the memory per hash bounded.
    assert auth._SCRYPT_N * auth._SCRYPT_P >= 2 ** 15 * 3
    assert 128 * auth._SCRYPT_N * auth._SCRYPT_R <= 32 * 2 ** 20
    assert auth._PW_HASH_WORKERS == 2


def test_two_hashes_of_one_password_differ():
    assert auth._hash_pw(PASSWORD) != auth._hash_pw(PASSWORD)


def test_a_pre_l17_15_hash_still_verifies_and_is_flagged_for_rehash():
    legacy = _legacy_hash(PASSWORD)

    assert auth._verify_pw(PASSWORD, legacy)
    assert not auth._verify_pw("wrong-password-123", legacy)
    assert not auth._pw_hash_is_current(legacy)


@pytest.mark.parametrize("stored", [
    "scrypt$ln=30,r=8,p=1$AAAA$AAAA",       # 128 GiB: refused, never allocated
    "scrypt$ln=17,r=16,p=1$AAAA$AAAA",      # 256 MiB: over the memory cap
    "scrypt$ln=15,r=8,p=99$AAAA$AAAA",
    "scrypt$ln=15,r=8$AAAA$AAAA",
    "scrypt$ln=15,r=8,p=3,x=1$AAAA$AAAA",
    "bcrypt$whatever",
    "",
    None,
])
def test_hostile_or_malformed_stored_hashes_fail_closed(stored):
    started = time.perf_counter()
    assert auth._verify_pw(PASSWORD, stored) is False
    assert time.perf_counter() - started < 0.5


def test_hashing_runs_off_the_event_loop():
    """A ~0.4 s hash inline froze every request and WebSocket on the worker."""

    async def scenario():
        ticks = 0
        stop = asyncio.Event()

        async def ticker():
            nonlocal ticks
            while not stop.is_set():
                ticks += 1
                await asyncio.sleep(0.005)

        task = asyncio.create_task(ticker())
        stored = await auth._hash_pw_async(PASSWORD)
        ok = await auth._verify_pw_async(PASSWORD, stored)
        stop.set()
        await task
        return ok, ticks

    ok, ticks = asyncio.run(scenario())
    assert ok
    assert ticks >= 5


# --- Login rehash ------------------------------------------------------------


class _Conn:
    def __init__(self):
        self.executed: list[tuple[str, tuple]] = []

    async def execute(self, query, *args):
        self.executed.append((query, args))
        return "UPDATE 1"


def _login_with(monkeypatch, stored_hash, passphrase):
    conn = _Conn()

    @asynccontextmanager
    async def tx(_ctx):
        yield conn

    async def _ok(_agent_id, _client=None):
        return (10, 9, 0)

    async def _user(_agent_id):
        return {
            "id": USER_ID, "agent_id": "agent@tenant.test", "tenant_id": TENANT_ID,
            "role": "agent", "password_hash": stored_hash, "session_epoch": 0,
            "policy_acceptance_required": False, "has_current_policy_acceptance": True,
        }

    monkeypatch.setattr("db.connection.tenant_tx", tx, raising=False)
    monkeypatch.setattr(auth, "_check_rate_limit", _ok)
    monkeypatch.setattr(auth, "_lookup_user", _user)
    monkeypatch.setattr(auth, "_register_session", lambda _a: None)
    body = auth.LoginRequest(agent_id="agent@tenant.test", passphrase=passphrase)
    try:
        result = asyncio.run(auth.login(body, Response(), None))
    except HTTPException as exc:
        result = exc
    return result, conn


def test_a_successful_login_rewrites_a_legacy_hash_at_todays_cost(monkeypatch):
    legacy = _legacy_hash(PASSWORD)

    result, conn = _login_with(monkeypatch, legacy, PASSWORD)

    assert not isinstance(result, HTTPException)
    updates = [args for query, args in conn.executed if query.startswith("UPDATE users SET password_hash")]
    assert len(updates) == 1
    new_hash, user_id, expected_old = updates[0]
    assert user_id == USER_ID
    # Compare-and-swap: a concurrent password change is never overwritten.
    assert expected_old == legacy
    assert auth._pw_hash_is_current(new_hash)
    assert auth._verify_pw(PASSWORD, new_hash)


def test_a_current_hash_is_not_rewritten(monkeypatch):
    result, conn = _login_with(monkeypatch, auth._hash_pw(PASSWORD), PASSWORD)

    assert not isinstance(result, HTTPException)
    assert conn.executed == []


def test_a_wrong_password_never_rewrites(monkeypatch):
    result, conn = _login_with(monkeypatch, _legacy_hash(PASSWORD), "not-the-password-1")

    assert isinstance(result, HTTPException) and result.status_code == 401
    assert conn.executed == []


def test_a_failed_upgrade_does_not_fail_the_login(monkeypatch):
    async def boom(*_args):
        raise RuntimeError("db down")

    monkeypatch.setattr(auth, "_hash_pw_async", boom)
    result, _conn = _login_with(monkeypatch, _legacy_hash(PASSWORD), PASSWORD)

    assert not isinstance(result, HTTPException)


# --- Common passwords ----------------------------------------------------------


def test_the_bundled_list_is_small_local_and_long_enough_to_matter():
    assert 900 <= len(auth._COMMON_PASSWORDS) <= 1000
    # Every entry would otherwise pass the length rule; shorter ones are dead weight.
    assert all(len(entry) >= auth.MIN_PASSWORD_LEN for entry in auth._COMMON_PASSWORDS)


@pytest.mark.parametrize("password", ["1234567890", "qwertyuiop", "Password123", "QWERTY123456"])
def test_common_passwords_are_refused_when_set(password):
    with pytest.raises(HTTPException) as caught:
        auth._check_new_password(password)

    assert caught.value.status_code == 422
    assert "commonly used" in caught.value.detail


def test_short_and_ordinary_passwords_keep_their_existing_answers():
    with pytest.raises(HTTPException) as caught:
        auth._check_new_password("short")
    assert "at least" in caught.value.detail

    auth._check_new_password(PASSWORD)  # no exception


def test_every_password_setting_route_applies_the_check():
    """register, reset, change-password and accept-invite all set a password."""
    import inspect

    for route in (auth.register, auth.reset_password, auth.change_password, auth.accept_invite):
        assert "_check_new_password(" in inspect.getsource(route), route.__name__


def test_register_refuses_a_common_password_before_touching_the_database(monkeypatch):
    def _no_db(_ctx):
        raise AssertionError("the database must not be reached")

    monkeypatch.setattr("db.connection.tenant_tx", _no_db, raising=False)
    body = auth.RegisterRequest(email="new@broker.test", password="1q2w3e4r5t")

    with pytest.raises(HTTPException) as caught:
        asyncio.run(auth.register(body, Response()))

    assert caught.value.status_code == 422
