"""Two-step sign-in for tenant users (L17-19) and the public contacts (L17-20).

The database behaviour (sealed secret, replay, single-use recovery codes,
lockout, resets, audit rows) is proven against real PostgreSQL in
test_tenant_user_mfa_live.py. These are the parts that need no database:
the QR encoder, the enrolment-only gate and its carry-forward through every
token renewal, who must enrol, recovery-code handling, security.txt.
"""

from __future__ import annotations

import asyncio
import inspect
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException, Response

import auth
import tenancy
import totp
import user_mfa
from tenancy import Role, TenantContext

TENANT = "11111111-2222-3333-4444-555555555555"
UID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
REPO = Path(__file__).resolve().parents[2]


# ── QR code ──────────────────────────────────────────────────────────────────

def _decode(matrix) -> str:
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    img = np.where(np.array(matrix, dtype=bool), 0, 255).astype(np.uint8)
    img = np.pad(img, 4, constant_values=255)
    img = cv2.resize(img, None, fx=8, fy=8, interpolation=cv2.INTER_NEAREST)
    detector = cv2.QRCodeDetectorAruco() if hasattr(cv2, "QRCodeDetectorAruco") else cv2.QRCodeDetector()
    return detector.detectAndDecode(img)[0]


@pytest.mark.parametrize("account", [
    "a@b.co",
    "jordan.miles@northstar.example.test",
    "a.very.long.address.for.someone@an-unusually-long-brokerage-domain-name.example.test",
])
def test_the_setup_qr_code_decodes_to_the_otpauth_uri(account):
    import qr_code

    uri = totp.provisioning_uri(totp.new_secret(), account, user_mfa.ISSUER)
    matrix = qr_code.encode(uri)
    assert len(matrix) == len(matrix[0]) and (len(matrix) - 17) % 4 == 0
    assert _decode(matrix) == uri


@pytest.mark.parametrize("length", [1, 17, 60, 140, 213, 214, 400, 600])
def test_qr_versions_across_the_supported_range_decode(length):
    import qr_code

    text = ("otpauth-" * 100)[:length]
    assert _decode(qr_code.encode(text)) == text


def test_qr_rows_are_compact_strings_and_oversize_text_is_refused():
    import qr_code

    rows = qr_code.rows("hello")
    assert len(rows) == 21 and set("".join(rows)) <= {"0", "1"}
    with pytest.raises(qr_code.QrError):
        qr_code.encode("x" * 700)


# ── who must enrol ───────────────────────────────────────────────────────────

def test_broker_owners_and_platform_accounts_must_enrol_agents_only_when_the_brokerage_says():
    assert user_mfa.required_reason("broker_owner", False) == "role"
    assert user_mfa.required_reason("platform_admin", False) == "role"
    assert user_mfa.required_reason("agent", False) is None
    assert user_mfa.required_reason("agent", True) == "brokerage"


# ── recovery codes ───────────────────────────────────────────────────────────

def test_recovery_codes_are_80_bit_unambiguous_and_hash_the_same_however_typed():
    codes = user_mfa.new_recovery_codes()
    assert len(codes) == user_mfa.RECOVERY_CODE_COUNT == len(set(codes))
    for code in codes:
        assert len(code) == 19 and code.count("-") == 3
        assert not set(code.replace("-", "")) & set("ILOU")
        assert user_mfa.looks_like_recovery_code(code) and not user_mfa.looks_like_totp(code)
    code = codes[0]
    typed = code.lower().replace("-", " ").replace("0", "o").replace("1", "l")
    assert user_mfa.hash_recovery_code(typed) == user_mfa.hash_recovery_code(code)
    assert len(user_mfa.hash_recovery_code(code)) == 64


def test_a_totp_code_is_six_digits():
    assert user_mfa.looks_like_totp(" 123 456 ")
    assert not user_mfa.looks_like_totp("12345")
    assert not user_mfa.looks_like_totp("abcdef")


def test_the_lockout_answer_tells_the_person_what_to_do():
    locked = user_mfa.VerifyResult(ok=False, just_locked=True).error()
    assert locked.status_code == 429 and locked.code == "MFA_LOCKED"
    wrong = user_mfa.VerifyResult(ok=False).error()
    assert wrong.status_code == 401 and wrong.code == "OTP_INVALID"


def test_no_secret_is_ever_stored_without_the_master_key(monkeypatch):
    monkeypatch.delenv("ORACLE_ENCRYPTION_MASTER_KEY", raising=False)
    with pytest.raises(user_mfa.MfaUnavailable):
        asyncio.run(user_mfa.begin_enrolment(object(), user_id=UID, tenant_id=TENANT, account="a@b.co"))


def test_every_factor_query_is_a_literal_sql_constant():
    """bandit B608 and the RLS rule: no SQL is built from strings at runtime."""
    src = inspect.getsource(user_mfa)
    assert "f\"SELECT" not in src and "f\"UPDATE" not in src and ".format(" not in src


# ── the enrolment-only gate ──────────────────────────────────────────────────

def _token(**kw) -> str:
    return auth._issue_jwt("owner@x.test", TENANT, "broker_owner", user_id=UID, **kw)


def test_a_session_owed_setup_reaches_only_the_policy_and_setup_routes():
    pending = f"Bearer {_token(mfa_enrolment=True)}"
    with pytest.raises(HTTPException) as exc:
        tenancy.require_context(pending)
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "mfa_enrolment_required"
    assert tenancy.require_policy_context(pending).mfa_enrolment_pending is True
    assert tenancy.require_mfa_context(pending).mfa_enrolment_pending is True
    ordinary = f"Bearer {_token()}"
    assert tenancy.require_context(ordinary).mfa_enrolment_pending is False


def test_the_setup_routes_wait_for_the_policy_gate():
    both = f"Bearer {_token(mfa_enrolment=True, extra={'policy_pending': True})}"
    with pytest.raises(HTTPException) as exc:
        tenancy.require_mfa_context(both)
    assert exc.value.status_code == 403
    assert tenancy.require_policy_context(both).mfa_enrolment_pending is True


@pytest.mark.parametrize("value", ["true", 1, "1", None])
def test_a_malformed_enrolment_claim_is_refused(value):
    token = auth._issue_jwt("owner@x.test", TENANT, "broker_owner", extra={"mfa_enrol": value})
    for gate in (tenancy.require_context, tenancy.require_mfa_context):
        with pytest.raises(HTTPException) as exc:
            gate(f"Bearer {token}")
        assert exc.value.status_code == 401


def test_no_bypass_switch_exists():
    """Only a confirmed factor lifts the requirement — no environment flag, in
    any module that decides it."""
    import mfa_api

    for module in (auth, tenancy, user_mfa, mfa_api):
        src = inspect.getsource(module)
        assert "MFA_BYPASS" not in src and "SKIP_MFA" not in src and "MFA_DISABLED" not in src


def test_every_session_issuer_decides_the_enrolment_claim():
    """Login, password reset, signup and invitation acceptance each set it;
    each renewal (policy acceptance, password change, the setup routes)
    carries it forward — a renewal that dropped it would lift the gate."""
    import mfa_api
    import policy_acceptance

    assert "mfa_enrolment=mfa_enrolment" in inspect.getsource(auth.login)
    assert "mfa_enrolment=mfa_enrolment" in inspect.getsource(auth.reset_password)
    assert "mfa_enrolment=True" in inspect.getsource(auth.register)
    assert "mfa_enrolment=mfa_enrolment" in inspect.getsource(auth.accept_invite)
    assert 'claims.get("mfa_enrol") is True' in inspect.getsource(auth.change_password)
    src = inspect.getsource(policy_acceptance)
    assert src.count("mfa_enrolment=ctx.mfa_enrolment_pending") == 2
    assert "mfa_enrolment=enrolment_owed" in inspect.getsource(mfa_api._renew)


class _PolicyConn:
    async def fetchrow(self, query, *_args):
        if "FROM users" in query:
            return {"id": UID, "policy_acceptance_required": False}
        return {"agreement_version": "v", "accepted_at": datetime.now(timezone.utc),
                "policy_version": "p"}


def test_policy_renewal_keeps_the_session_on_the_setup_screen(monkeypatch):
    import db.connection
    import policy_acceptance

    @asynccontextmanager
    async def tx(_ctx):
        yield _PolicyConn()

    monkeypatch.setattr(db.connection, "tenant_tx", tx)
    ctx = tenancy.require_policy_context(f"Bearer {_token(mfa_enrolment=True)}")
    response = Response()
    asyncio.run(policy_acceptance.policy_acceptance_status(ctx, response))
    cookie = response.headers["set-cookie"].split("oracle_session=", 1)[1].split(";", 1)[0]
    assert auth.decode_token(cookie)["mfa_enrol"] is True


def _owner_row(**overrides):
    row = {
        "id": uuid.UUID(UID), "agent_id": "owner@x.test", "tenant_id": uuid.UUID(TENANT),
        "role": "broker_owner", "password_hash": auth._hash_pw("correct horse battery"),
        "policy_acceptance_required": False, "session_epoch": 3,
        "has_current_policy_acceptance": True, "mfa_enabled": False, "tenant_requires_mfa": False,
    }
    row.update(overrides)
    return row


def _login(monkeypatch, row, otp=None):
    async def lookup(_agent_id):
        return row

    async def no_limit(*_a, **_k):
        return 10, 10, int(time.time()) + 60

    monkeypatch.setattr(auth, "_lookup_user", lookup)
    monkeypatch.setattr(auth, "_check_rate_limit", no_limit)
    response = Response()
    body = auth.LoginRequest(agent_id=row["agent_id"], passphrase="correct horse battery", otp=otp)
    result = asyncio.run(auth.login(body, response))
    cookie = response.headers["set-cookie"].split("oracle_session=", 1)[1].split(";", 1)[0]
    return result, auth.decode_token(cookie)


def test_a_broker_owner_without_two_step_signs_in_to_the_setup_screen(monkeypatch):
    result, claims = _login(monkeypatch, _owner_row())
    assert result.mfa_enrolment_required is True and claims["mfa_enrol"] is True


def test_an_agent_signs_in_normally_unless_the_brokerage_requires_it(monkeypatch):
    result, claims = _login(monkeypatch, _owner_row(role="agent"))
    assert result.mfa_enrolment_required is False and "mfa_enrol" not in claims
    result, claims = _login(monkeypatch, _owner_row(role="agent", tenant_requires_mfa=True))
    assert result.mfa_enrolment_required is True and claims["mfa_enrol"] is True


def test_an_account_with_two_step_on_is_asked_for_the_code_after_the_password(monkeypatch):
    with pytest.raises(HTTPException) as exc:
        _login(monkeypatch, _owner_row(mfa_enabled=True))
    assert exc.value.status_code == 401
    assert exc.value.detail["code"] == "OTP_REQUIRED" and exc.value.detail["factor"] == "totp"


def test_a_wrong_password_never_reaches_the_code_prompt(monkeypatch):
    row = _owner_row(mfa_enabled=True)

    async def lookup(_agent_id):
        return row

    async def no_limit(*_a, **_k):
        return 10, 10, 0

    monkeypatch.setattr(auth, "_lookup_user", lookup)
    monkeypatch.setattr(auth, "_check_rate_limit", no_limit)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(auth.login(auth.LoginRequest(agent_id="owner@x.test", passphrase="wrong password!"), Response()))
    assert exc.value.status_code == 401 and exc.value.detail == "Invalid agent credentials."


def test_a_signed_in_session_status_says_when_setup_is_owed():
    request = type("R", (), {"cookies": {"oracle_session": _token(mfa_enrolment=True)}})()
    status = asyncio.run(auth.session_status(request, Response()))
    assert status.authenticated is True and status.mfa_enrolment_required is True


# ── security.txt and the privacy contact (L17-20) ────────────────────────────

def _client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import security_contact

    app = FastAPI()
    app.include_router(security_contact.router)
    return TestClient(app)


def test_security_txt_is_not_published_until_configured(monkeypatch):
    monkeypatch.delenv("ORACLE_SECURITY_CONTACT", raising=False)
    assert _client().get("/.well-known/security.txt").status_code == 404
    monkeypatch.setenv("ORACLE_SECURITY_CONTACT", "not a contact, javascript:alert(1)")
    assert _client().get("/.well-known/security.txt").status_code == 404


def test_security_txt_follows_rfc_9116(monkeypatch):
    monkeypatch.setenv("ORACLE_SECURITY_CONTACT", "security@neoh.example, https://neoh.example/security")
    monkeypatch.setenv("ORACLE_PUBLIC_BASE_URL", "https://app.neoh.example")
    monkeypatch.delenv("ORACLE_DOMAIN", raising=False)
    monkeypatch.delenv("ORACLE_BASE_URL", raising=False)
    r = _client().get("/.well-known/security.txt")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    fields = [line.split(": ", 1) for line in r.text.strip().split("\n")]
    assert ["Contact", "mailto:security@neoh.example"] in fields
    assert ["Contact", "https://neoh.example/security"] in fields
    assert ["Preferred-Languages", "en"] in fields
    assert ["Canonical", "https://app.neoh.example/.well-known/security.txt"] in fields
    expires = datetime.strptime(dict(fields)["Expires"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    ahead = expires - datetime.now(timezone.utc)
    assert timedelta(days=364) < ahead <= timedelta(days=365)


def test_security_txt_expires_is_computed_per_request():
    import security_contact

    then = datetime(2030, 1, 1, tzinfo=timezone.utc)
    text = security_contact.render_security_txt(["mailto:s@x.example"], now=then)
    assert "Expires: 2031-01-01T00:00:00Z" in text
    assert "Canonical" not in text


def test_a_contact_cannot_inject_extra_fields(monkeypatch):
    import security_contact

    monkeypatch.setenv("ORACLE_SECURITY_CONTACT", "a@x.example\nPolicy: https://evil.example")
    assert security_contact.security_contacts() == []


def test_the_privacy_contact_rides_the_privacy_request_answer_only_when_set(monkeypatch):
    import privacy_api
    import privacy_requests

    async def handled(*_a, **_k):
        return {"kind": "dsr_access", "found": 0}

    monkeypatch.setattr(privacy_requests, "handle_subject_request", handled)
    owner = TenantContext("owner@x.test", TENANT, Role.BROKER_OWNER)
    body = privacy_api.SubjectRequest(kind="dsr_access", email="p@x.example", reason="asked by phone")
    monkeypatch.delenv("ORACLE_PRIVACY_CONTACT", raising=False)
    assert "privacy_contact" not in asyncio.run(privacy_api.subject_request(body, owner))
    monkeypatch.setenv("ORACLE_PRIVACY_CONTACT", "privacy@neoh.example")
    assert asyncio.run(privacy_api.subject_request(body, owner))["privacy_contact"] == "mailto:privacy@neoh.example"


def test_the_edge_sends_security_txt_to_the_api_and_the_static_site_never_fakes_it():
    import importlib.util

    spec = importlib.util.spec_from_file_location("render_app_spec", REPO / "scripts" / "render-app-spec.py")
    render = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(render)
    rules = render.render("staging", "sha256:" + "b" * 64, "sha256:" + "f" * 64)["ingress"]["rules"]
    api = [r for r in rules if r["component"]["name"] == "api"]
    assert any(r["match"]["path"]["prefix"] == "/.well-known/security.txt" for r in api)
    web_index = next(i for i, r in enumerate(rules) if r["component"]["name"] == "web")
    sec_index = next(i for i, r in enumerate(rules) if r["match"]["path"]["prefix"] == "/.well-known/security.txt")
    assert sec_index < web_index
    nginx = (REPO / "oracle-app" / "nginx.conf").read_text()
    assert "location = /.well-known/security.txt" in nginx
