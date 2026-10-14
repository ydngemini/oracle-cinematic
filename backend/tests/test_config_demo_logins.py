"""Demo logins never boot on production (ISO 27001 A.8.9, L17-14).

ORACLE_ENABLE_DEMO_LOGINS turns a plaintext password from the environment into
a working login with whatever role and tenant ORACLE_DEMO_ROLE / _TENANT name.
Before this guard, nothing stopped a production instance from booting with it:
validate_or_die never looked at the flag (grep at e3ea107 — the only other
mention was the ENV_VARS catalogue).

The rule is an allowlist: dev and staging may use it; production, and an unset
or unrecognised ORACLE_ENV (which validate_or_die already treats as
production), refuse. Staging's demo brokerage does not depend on the flag — its
logins are ordinary `users` rows (scripts/demo_tenant_common.py) — but the
operator may still want it there, so staging stays allowed.

Subprocesses, like test_config_weak_secrets.py: reloading `config` in-process
would leak its os.environ.setdefault calls into later tests.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parents[1]
_CLEARED = (
    "ORACLE_ADMIN_ID", "ORACLE_ADMIN_PASSPHRASE", "ORACLE_ENABLE_WEBHOOKS",
    "ORACLE_JWT_ISSUER", "ORACLE_JWT_AUDIENCE", "ORACLE_QWEN_REALTIME_ENABLED",
    "ORACLE_TWILIO_QWEN_REALTIME_ENABLED", "ORACLE_DB_PASSWORD", "STRIPE_SECRET_KEY",
    "ORACLE_DEMO_RECIPIENT_ALLOWLIST", "ORACLE_ENABLE_DEMO_LOGINS",
    "ORACLE_DEMO_USER", "ORACLE_DEMO_PASS",
)
_USER = "demo@example.test"
_PASS = "demo-pass-123"


def _run(code: str, **settings) -> subprocess.CompletedProcess:
    environment = dict(os.environ)
    for name in _CLEARED:
        environment.pop(name, None)
    environment.update({
        "PYTHONPATH": str(_BACKEND_DIR),
        "ORACLE_SKIP_DOTENV": "1",
        "ORACLE_BASE_URL": "https://neohrs.com",
        "ORACLE_SECRET_KEY": "s3cur3-signing-key-with-plenty-of-entropy-01",
        "ORACLE_ENCRYPTION_MASTER_KEY": "m4st3r-encryption-key-with-entropy-02xyz",
    })
    for key, value in settings.items():
        if value is None:
            environment.pop(key, None)
        else:
            environment[key] = value
    return subprocess.run([sys.executable, "-c", code], cwd=_BACKEND_DIR, env=environment,
                          capture_output=True, text=True, check=False)


def _validate(**settings) -> subprocess.CompletedProcess:
    return _run("import config; config.validate_or_die()", **settings)


@pytest.mark.parametrize("env", ["prod", "production", "", "test", "ci"])
@pytest.mark.parametrize("flag", ["1", "true", "YES", "on"])
def test_demo_logins_refuse_the_boot_outside_dev_and_staging(env, flag):
    result = _validate(ORACLE_ENV=env, ORACLE_ENABLE_DEMO_LOGINS=flag,
                       ORACLE_DEMO_USER=_USER, ORACLE_DEMO_PASS=_PASS)

    assert result.returncode != 0
    assert "ORACLE_ENABLE_DEMO_LOGINS" in result.stderr
    # The refusal names the setting, never the credential.
    assert _PASS not in result.stderr
    assert _USER not in result.stderr


@pytest.mark.parametrize("env", ["dev", "local", "staging"])
def test_dev_and_staging_may_use_demo_logins(env):
    result = _validate(ORACLE_ENV=env, ORACLE_ENABLE_DEMO_LOGINS="1",
                       ORACLE_JWT_ISSUER="neoh-staging", ORACLE_JWT_AUDIENCE="neoh-staging")

    assert "ORACLE_ENABLE_DEMO_LOGINS" not in result.stderr
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("flag", ["0", "false", "no", "off", "", None])
def test_production_with_the_flag_off_boots(flag):
    """.env.prod.example ships ORACLE_ENABLE_DEMO_LOGINS=0; that must boot."""
    result = _validate(ORACLE_ENV="prod", ORACLE_ENABLE_DEMO_LOGINS=flag)

    assert result.returncode == 0, result.stderr


def test_auth_never_loads_a_demo_password_on_production():
    """Defence in depth for a process that imports auth without the lifespan."""
    probe = "import auth; print(sorted(auth.DEMO_CREDENTIALS))"
    common = {"ORACLE_ENABLE_DEMO_LOGINS": "1", "ORACLE_DEMO_USER": _USER,
              "ORACLE_DEMO_PASS": _PASS, "ORACLE_JWT_ISSUER": "neoh",
              "ORACLE_JWT_AUDIENCE": "neoh"}
    prod = _run(probe, ORACLE_ENV="prod", **common)
    dev = _run(probe, ORACLE_ENV="dev", **common)

    assert prod.returncode == 0, prod.stderr
    assert _USER not in prod.stdout
    assert dev.returncode == 0, dev.stderr
    assert _USER in dev.stdout
