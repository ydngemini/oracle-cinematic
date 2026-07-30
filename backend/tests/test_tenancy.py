"""
App-layer runtime tests for the Oracle multi-tenant IAM enforcement layer
(backend/tenancy.py + the JWT tenancy claims stamped by backend/auth.py).

These exercise the Python *mirror* of the RLS policies — the visibility
predicates and the require_context() Auth Gatekeeper — with NO database. The
matching DB-side proof that the actual Postgres RLS policies enforce the same
truth table lives in tests/rls_test.sql.

Runs under pytest if installed, else standalone:
    python3 backend/tests/test_tenancy.py
Requires: fastapi, pyjwt (present in system python3).
"""

import os
import sys
import time

# Put backend/ on the path so `import tenancy` / `import auth` resolve whether
# this is run from the repo root, backend/, or under pytest.
BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import jwt
from fastapi import HTTPException

import auth
from policy_contract import PLATFORM_POLICY_VERSION
from tenancy import (
    Role,
    TenantContext,
    can_read_private,
    can_read_listing,
    can_write,
    require_role,
    require_context,
)

# --- fixtures ---------------------------------------------------------------

TENANT_A = "11111111-1111-1111-1111-111111111111"  # Apex (matches auth.APEX_TENANT_ID)
TENANT_B = "22222222-2222-2222-2222-222222222222"  # Beta
PLATFORM = "00000000-0000-0000-0000-000000000000"

CTX_A = TenantContext(agent_id="apex_owner", tenant_id=TENANT_A, role=Role.BROKER_OWNER)
CTX_B = TenantContext(agent_id="beta_owner", tenant_id=TENANT_B, role=Role.BROKER_OWNER)
CTX_ADMIN = TenantContext(agent_id="ydn", tenant_id=PLATFORM, role=Role.PLATFORM_ADMIN)


def _token(sub="apex_owner", tenant_id=TENANT_A, role="broker_owner", ttl=60, **extra):
    payload = {
        "sub": sub,
        "policy_version": PLATFORM_POLICY_VERSION,
        "iat": time.time(),
        "exp": time.time() + ttl,
    }
    if tenant_id is not None:
        payload["tenant_id"] = tenant_id
    if role is not None:
        payload["role"] = role
    payload.update(extra)
    return jwt.encode(payload, auth.SECRET_KEY, algorithm=auth.ALGORITHM)


def _expect_401(callable_, *args):
    try:
        callable_(*args)
    except HTTPException as e:
        assert e.status_code == 401, f"expected 401, got {e.status_code}: {e.detail}"
        return
    raise AssertionError("expected HTTPException(401), none raised")


# --- enum / dataclass -------------------------------------------------------

def test_role_values():
    assert Role.PLATFORM_ADMIN.value == "platform_admin"
    assert Role.BROKER_OWNER.value == "broker_owner"
    assert Role.AGENT.value == "agent"
    assert Role("agent") is Role.AGENT


def test_context_flags():
    assert CTX_ADMIN.is_platform_admin and not CTX_ADMIN.is_broker_owner
    assert CTX_A.is_broker_owner and not CTX_A.is_platform_admin


# --- private read (clients/leads): hard wall --------------------------------

def test_private_read_own_tenant_only():
    row_a = {"tenant_id": TENANT_A}
    row_b = {"tenant_id": TENANT_B}
    assert can_read_private(CTX_A, row_a) is True
    assert can_read_private(CTX_A, row_b) is False        # cross-tenant blocked
    assert can_read_private(CTX_ADMIN, row_b) is True      # god-mode


# --- listing read: own / shared MLS / explicit grant ------------------------

def test_listing_read_matrix():
    own = {"id": "L1", "tenant_id": TENANT_A, "is_shared_mls": False}
    shared = {"id": "L2", "tenant_id": TENANT_A, "is_shared_mls": True}
    foreign_private = {"id": "L3", "tenant_id": TENANT_B, "is_shared_mls": False}
    foreign_granted = {"id": "L4", "tenant_id": TENANT_B, "is_shared_mls": False}
    grants_to_a = frozenset({"L4"})

    # Tenant A's view
    assert can_read_listing(CTX_A, own) is True
    assert can_read_listing(CTX_A, shared) is True
    assert can_read_listing(CTX_A, foreign_private) is False
    assert can_read_listing(CTX_A, foreign_private, grants_to_a) is False   # not granted
    assert can_read_listing(CTX_A, foreign_granted, grants_to_a) is True    # co-broke grant

    # Tenant B sees A's shared listing in the public pool
    assert can_read_listing(CTX_B, shared) is True
    assert can_read_listing(CTX_B, own) is False

    # Admin sees everything
    assert can_read_listing(CTX_ADMIN, foreign_private) is True


# --- write: owner-only, even when shared ------------------------------------

def test_write_owner_only_even_if_shared():
    shared = {"id": "L2", "tenant_id": TENANT_A, "is_shared_mls": True}
    assert can_write(CTX_A, shared) is True
    assert can_write(CTX_B, shared) is False       # shared for READ != writable
    assert can_write(CTX_ADMIN, shared) is True


# --- RBAC -------------------------------------------------------------------

def test_require_role():
    require_role(CTX_A, Role.BROKER_OWNER)              # allowed -> no raise
    require_role(CTX_ADMIN, Role.AGENT)                 # admin implicitly allowed
    try:
        require_role(CTX_A, Role.PLATFORM_ADMIN)        # broker asking for admin-only
    except HTTPException as e:
        assert e.status_code == 403
    else:
        raise AssertionError("expected 403 for disallowed role")


# --- require_context: the JWT Auth Gatekeeper -------------------------------

def test_require_context_happy_path():
    ctx = require_context(f"Bearer {_token()}")
    assert ctx.tenant_id == TENANT_A
    assert ctx.role is Role.BROKER_OWNER
    assert ctx.agent_id == "apex_owner"


def test_require_context_admin():
    tok = _token(sub="ydn", tenant_id=PLATFORM, role="platform_admin")
    assert require_context(f"Bearer {tok}").is_platform_admin


def test_require_context_rejections():
    _expect_401(require_context, None)                                  # missing header
    _expect_401(require_context, _token())                              # no "Bearer " scheme
    _expect_401(require_context, f"Bearer {_token(ttl=-10)}")           # expired
    _expect_401(require_context, f"Bearer {_token(tenant_id=None)}")    # no tenant claim
    _expect_401(require_context, f"Bearer {_token(role=None)}")         # no role claim
    _expect_401(require_context, f"Bearer {_token(role='wizard')}")     # unknown role
    _expect_401(require_context, "Bearer not.a.jwt")                    # garbage token
    # tampered signature
    forged = jwt.encode(
        {"sub": "x", "tenant_id": TENANT_A, "role": "platform_admin",
         "iat": time.time(), "exp": time.time() + 60},
        "wrong-test-secret-key-with-at-least-32-bytes", algorithm=auth.ALGORITHM,
    )
    _expect_401(require_context, f"Bearer {forged}")


def test_require_context_rejects_stale_policy_version():
    try:
        require_context(f"Bearer {_token(policy_version='prior-policy')}")
    except HTTPException as e:
        assert e.status_code == 403
        assert "current platform policy" in e.detail
    else:
        raise AssertionError("expected a stale-policy token to be rejected")


# --- standalone runner (no pytest needed) -----------------------------------

def _main():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for t in tests:
        try:
            t()
            passed += 1
            print(f"  PASS  {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  FAIL  {t.__name__}: {type(e).__name__}: {e}")
    print(f"\napp-layer: {passed} passed, {failed} failed ({len(tests)} total)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
