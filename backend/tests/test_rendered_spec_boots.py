"""Every rendered App Platform component must pass the backend's own boot checks.

The first real staging bring-up (2026-10-03) found that infra/digitalocean/
app.yaml set neither ORACLE_JWT_ISSUER nor ORACLE_JWT_AUDIENCE. Outside dev,
config.validate_or_die and auth.py's import-time check refuse to boot without
them, so the first deploy would have crash-looped — and no test noticed,
because nothing ran the backend's validation against the spec it ships with.

This does exactly that, offline: render each environment, give each backend
component precisely the env App Platform would (bindable variables resolved to
plausible values, every SECRET filled with a strong dummy), and run the boot
checks in a clean interpreter. A required env var missing from the spec now
fails here instead of in production.
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import re
import subprocess
import sys

import pytest

BACKEND = pathlib.Path(__file__).resolve().parent.parent
REPO = BACKEND.parent
_s = importlib.util.spec_from_file_location("render_app_spec_boot", REPO / "scripts" / "render-app-spec.py")
r = importlib.util.module_from_spec(_s)
_s.loader.exec_module(r)

D = "sha256:" + "a" * 64

# What App Platform substitutes for each bindable variable.
BINDINGS = {
    "APP_URL": "https://neoh.example.test",
    "neoh-postgres.HOSTNAME": "db.example.test",
    "neoh-postgres.PORT": "25060",
    "neoh-postgres.DATABASE": "oracle",
    "neoh-postgres.USERNAME": "oracle_app_login",
    "neoh-postgres.PASSWORD": "Pg-dummy-9f8e7d6c5b4a3210fedcba98",
    "neoh-postgres.CA_CERT": "-----BEGIN CERTIFICATE-----\nMIIBdummy\n-----END CERTIFICATE-----\n",
    "neoh-redis.DATABASE_URL": "rediss://default:Vk-dummy-0a1b2c3d4e5f@cache.example.test:25061",
}


def _secret_dummy(key: str, env: str) -> str:
    """A strong, well-formed stand-in for one SECRET value."""
    special = {
        "ORACLE_ADMIN_TOTP_SECRET": "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP",
        "ORACLE_ADMIN_OTP_EMAIL": "ops@example.test",
        "ORACLE_ALERT_EMAIL": "ops@example.test",
        "ORACLE_ADMIN_ID": "operator",
        "STRIPE_SECRET_KEY": ("sk_live_" if env == "production" else "sk_test_") + "Dummy0123456789abcdefXYZ",
        "STRIPE_WEBHOOK_SECRET": "whsec_Dummy0123456789abcdefXYZ",
        "STRIPE_PRICE_ID": "price_Dummy0123456789",
        "ORACLE_SMTP_HOST": "smtp.example.test",
        "ORACLE_SMTP_USERNAME": "mailer@example.test",
        "TWILIO_ACCOUNT_SID": "AC" + "0123456789abcdef" * 2,
    }
    if key in special:
        return special[key]
    return f"{key.lower()}-Dummy-7Qx2Lm9Rv4Tz8Wn3Kp6Ys1"


def _resolve(value: str) -> str:
    return re.sub(r"\$\{([^}]+)\}", lambda m: BINDINGS[m.group(1)], value)


def _component_env(env: str, comp: dict) -> dict:
    out = {}
    for e in comp.get("envs") or []:
        v = e.get("value")
        if v in (None, "") and e.get("type") == "SECRET":
            v = _secret_dummy(e["key"], env)
        out[e["key"]] = _resolve(str(v))
    return out


BOOT = """
import config
config.validate_or_die()
import auth      # issuer/audience pair is checked at import time
import billing   # live-key interlock is checked at import time
print("BOOT-CHECKS-OK")
"""

CASES = [(env, comp["name"]) for env in ("production", "staging")
         for comp in r._backend_components(r.render(env, D, D))]


@pytest.mark.parametrize("env,name", CASES)
def test_rendered_component_passes_the_backend_boot_checks(env, name):
    spec = r.render(env, D, D)
    comp = next(c for c in r._backend_components(spec) if c["name"] == name)
    child_env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", "/tmp"),
        "ORACLE_SKIP_DOTENV": "1",          # never read the developer's real .env
        "PYTHONDONTWRITEBYTECODE": "1",
        **_component_env(env, comp),
    }
    proc = subprocess.run([sys.executable, "-c", BOOT], cwd=BACKEND, env=child_env,
                          capture_output=True, text=True, timeout=120)
    assert "BOOT-CHECKS-OK" in proc.stdout, (
        f"{env}/{name} would not boot with the env its spec gives it:\n{proc.stderr[-2500:]}"
    )


def test_the_check_would_have_caught_the_missing_jwt_pair():
    """Prove the test bites: the worker WITHOUT the pair must fail it."""
    spec = r.render("staging", D, D)
    worker = next(c for c in r._backend_components(spec) if c["name"] == "worker")
    env = _component_env("staging", worker)
    env.pop("ORACLE_JWT_ISSUER")
    env.pop("ORACLE_JWT_AUDIENCE")
    child_env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "/tmp"),
                 "ORACLE_SKIP_DOTENV": "1", **env}
    proc = subprocess.run([sys.executable, "-c", BOOT], cwd=BACKEND, env=child_env,
                          capture_output=True, text=True, timeout=120)
    assert "BOOT-CHECKS-OK" not in proc.stdout
    assert "ORACLE_JWT_ISSUER" in proc.stderr
