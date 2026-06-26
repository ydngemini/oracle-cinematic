"""
Auth token tests — decode_token is the single validation path every protected
endpoint and the WebSocket handlers funnel through, so a forged/expired/garbled
token must be rejected here or the whole tenancy gate is moot. Auth previously
had zero coverage.

No database required.

Runs under pytest, or standalone:
    ORACLE_ENV=dev python3 backend/tests/test_auth.py
"""

import os
import sys
import time

os.environ.setdefault("ORACLE_ENV", "dev")  # auth.py fails fast in prod without a key
_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import jwt
from fastapi import HTTPException

import auth


def _mint(claims: dict, key: str | None = None, algorithm: str | None = None) -> str:
    return jwt.encode(claims, key or auth.SECRET_KEY, algorithm=algorithm or auth.ALGORITHM)


def _valid_claims(**over) -> dict:
    now = int(time.time())
    base = {
        "sub": "agent-1",
        "tenant_id": "11111111-1111-1111-1111-111111111111",
        "role": "agent",
        "iat": now,
        "exp": now + 3600,
    }
    base.update(over)
    return base


def test_valid_token_roundtrips():
    claims = _mint(_valid_claims())
    decoded = auth.decode_token(claims)
    assert decoded["sub"] == "agent-1"
    assert decoded["role"] == "agent"


def test_expired_token_rejected():
    expired = _mint(_valid_claims(exp=int(time.time()) - 10))
    try:
        auth.decode_token(expired)
        assert False, "expired token was accepted"
    except HTTPException as exc:
        assert exc.status_code == 401


def test_forged_signature_rejected():
    # Signed with a key that is NOT the server's — a token an attacker mints.
    forged = _mint(_valid_claims(role="platform_admin"), key="ATTACKER_KEY_NOT_THE_SERVERS")
    try:
        auth.decode_token(forged)
        assert False, "forged token was accepted — privilege escalation"
    except HTTPException as exc:
        assert exc.status_code == 401


def test_empty_and_garbage_tokens_rejected():
    for bad in ("", "not.a.jwt", "Bearer x", "a" * 10):
        try:
            auth.decode_token(bad)
            assert False, f"garbage token accepted: {bad!r}"
        except HTTPException as exc:
            assert exc.status_code == 401


def test_oversized_token_rejected():
    # decode_token guards on length before doing any crypto work.
    huge = _mint(_valid_claims(blob="x" * 9000))
    try:
        auth.decode_token(huge)
        assert False, "oversized token accepted"
    except HTTPException as exc:
        assert exc.status_code == 401


def test_none_algorithm_attack_rejected():
    # Classic JWT "alg: none" downgrade — must not be honored.
    unsigned = jwt.encode(_valid_claims(role="platform_admin"), key="", algorithm="none")
    try:
        auth.decode_token(unsigned)
        assert False, "alg=none token accepted — signature bypass"
    except HTTPException as exc:
        assert exc.status_code == 401


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  ok   {fn.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL {fn.__name__}: {exc}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
