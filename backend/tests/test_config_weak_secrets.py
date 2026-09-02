"""A secret that is present but worthless must fail the boot.

`validate_or_die` asked only `if not os.environ.get(name)`, and
infra/terraform/secrets.tf seeds the app secret with the literal string
"REPLACE_ME" so the ECS task definition can reference each JSON key before an
operator fills it. "REPLACE_ME" is not empty.

So a fresh production deploy would have booted signing JWTs with "REPLACE_ME",
encrypting PII with it, and — because auth.py registers
ORACLE_ADMIN_ID/ORACLE_ADMIN_PASSPHRASE as a platform_admin login whenever both
are non-empty, and terraform hardcodes a real ORACLE_ADMIN_ID — serving a public
admin account whose password was "REPLACE_ME". validate_or_die logged
"Config validated for production — all required settings present."

Each case runs in a subprocess, matching test_recovery_security.py. Reloading
`config` in-process would re-run its module body, whose
`os.environ.setdefault("ORACLE_JWT_ISSUER", ...)` monkeypatch cannot undo — the
issuer/audience binding would leak into every later test in the session.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parents[1]

# Cleared so an operator's own .env cannot make a case pass or fail by accident.
_CLEARED = (
    "ORACLE_ADMIN_PASSPHRASE",
    "ORACLE_ACS_WEBHOOK_SECRET",
    "ORACLE_CUSTOM_CALL_WEBHOOK_SECRET",
    "ORACLE_ENABLE_WEBHOOKS",
    "ORACLE_DOMAIN",
    "ORACLE_PUBLIC_BASE_URL",
    "ORACLE_JWT_ISSUER",
    "ORACLE_JWT_AUDIENCE",
    "ORACLE_QWEN_REALTIME_ENABLED",
    "ORACLE_TWILIO_QWEN_REALTIME_ENABLED",
)

_STRONG_KEY = "s3cur3-signing-key-with-plenty-of-entropy-01"
_STRONG_MASTER = "m4st3r-encryption-key-with-entropy-02xyz"


def _validate(**settings) -> subprocess.CompletedProcess:
    environment = dict(os.environ)
    for name in _CLEARED:
        environment.pop(name, None)
    environment.update(
        {
            "PYTHONPATH": str(_BACKEND_DIR),
            "ORACLE_ENV": "prod",
            "ORACLE_BASE_URL": "https://neohrs.com",
            "ORACLE_SECRET_KEY": _STRONG_KEY,
            "ORACLE_ENCRYPTION_MASTER_KEY": _STRONG_MASTER,
        }
    )
    for key, value in settings.items():
        if value is None:
            environment.pop(key, None)
        else:
            environment[key] = value
    return subprocess.run(
        [sys.executable, "-c", "import config; config.validate_or_die()"],
        cwd=_BACKEND_DIR,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_terraform_placeholder_no_longer_boots():
    result = _validate(
        ORACLE_SECRET_KEY="REPLACE_ME",
        ORACLE_ENCRYPTION_MASTER_KEY="REPLACE_ME",
        ORACLE_ADMIN_PASSPHRASE="REPLACE_ME",
    )

    assert result.returncode != 0
    assert "placeholder" in result.stderr
    # All three are named, not just the first — an operator fixing them one boot
    # at a time is three deploys they did not need.
    assert "ORACLE_SECRET_KEY" in result.stderr
    assert "ORACLE_ENCRYPTION_MASTER_KEY" in result.stderr
    assert "ORACLE_ADMIN_PASSPHRASE" in result.stderr


def test_the_rejection_never_echoes_the_secret():
    """A secret that leaks through its own rejection message is no better off."""
    result = _validate(ORACLE_SECRET_KEY="hunter2")

    assert result.returncode != 0
    assert "hunter2" not in result.stderr
    assert "shorter than" in result.stderr


@pytest.mark.parametrize(
    "value", ["replace_me", "REPLACE-ME", "  ChangeMe  ", "placeholder", "password", "TODO"]
)
def test_placeholders_are_caught_regardless_of_case_or_padding(value):
    result = _validate(ORACLE_SECRET_KEY=value)

    assert result.returncode != 0
    assert "placeholder or weak" in result.stderr


def test_a_long_but_degenerate_key_is_refused():
    """48 characters of "a" satisfies a length check and nothing else."""
    result = _validate(ORACLE_SECRET_KEY="a" * 48)

    assert result.returncode != 0
    assert "too few distinct characters" in result.stderr


def test_real_secrets_boot():
    result = _validate(ORACLE_ADMIN_PASSPHRASE="c0rrect-horse-battery-staple")

    assert result.returncode == 0, result.stderr


def test_an_unset_optional_secret_is_not_this_checks_complaint():
    """The operator account and webhook secrets are optional.

    Absence is the `missing` check's business; conflating the two would refuse a
    deployment that simply has no operator login configured.
    """
    result = _validate(ORACLE_ADMIN_PASSPHRASE=None)

    assert result.returncode == 0, result.stderr


def test_webhook_secrets_are_checked_once_webhooks_are_on():
    result = _validate(
        ORACLE_ENABLE_WEBHOOKS="1",
        ORACLE_ACS_WEBHOOK_SECRET="REPLACE_ME",
        ORACLE_CUSTOM_CALL_WEBHOOK_SECRET="a-genuine-webhook-secret",
    )

    assert result.returncode != 0
    assert "ORACLE_ACS_WEBHOOK_SECRET" in result.stderr


def test_development_stays_relaxed():
    """Dev returns before the secret checks — a local box must still start."""
    result = _validate(
        ORACLE_ENV="dev",
        ORACLE_SECRET_KEY="REPLACE_ME",
        ORACLE_ENCRYPTION_MASTER_KEY="REPLACE_ME",
    )

    assert result.returncode == 0, result.stderr


# ── "not production" is not the same question as "is development" ───────────

def test_is_prod_is_not_the_negation_of_is_dev():
    """The gap between the two is every unrecognised value, and it is not empty.

    `IS_DEV` answers "is this a developer's machine" and is deliberately narrow —
    it mounts the chaos router and relaxes auth elsewhere, so a value has to opt
    in. Using `not IS_DEV` to mean "production" therefore hands production's
    privileges to "", "test", "staging" and anything else unrecognised.
    """
    import importlib
    import os

    import config as config_module

    original = os.environ.get("ORACLE_ENV")
    try:
        for env in ("", "test", "staging", "ci", "qa"):
            os.environ["ORACLE_ENV"] = env
            cfg = importlib.reload(config_module)
            assert not cfg.IS_DEV, f"{env!r} must not read as development"
            assert not cfg.IS_PROD, f"{env!r} must not read as production"

        for env in ("dev", "development", "local"):
            os.environ["ORACLE_ENV"] = env
            cfg = importlib.reload(config_module)
            assert cfg.IS_DEV and not cfg.IS_PROD

        for env in ("prod", "production"):
            os.environ["ORACLE_ENV"] = env
            cfg = importlib.reload(config_module)
            assert cfg.IS_PROD and not cfg.IS_DEV
    finally:
        if original is None:
            os.environ.pop("ORACLE_ENV", None)
        else:
            os.environ["ORACLE_ENV"] = original
        importlib.reload(config_module)


def test_the_live_stripe_interlock_gates_on_not_prod():
    """A live key must be refused everywhere except explicit production.

    Before this, the guard read `config.IS_DEV`, so it fired only for
    dev/development/local. Measured: ORACLE_ENV unset, "test" and "staging" all
    SILENTLY ACCEPTED an sk_live_* key that charges real cards — with unset being
    the default, and the guard's own message claiming it covered "dev/unset".

    Asserted against the source: the failure is silent by construction (the app
    boots fine and simply bills real cards), so nothing else would catch a
    revert.
    """
    import ast
    from pathlib import Path

    src = (Path(__file__).parent.parent / "billing.py").read_text()
    guard = next(
        node for node in ast.parse(src).body
        if isinstance(node, ast.If) and "_IS_LIVE_STRIPE" in ast.dump(node.test)
    )
    dumped = ast.dump(guard.test)
    assert "IS_PROD" in dumped, "the interlock must gate on IS_PROD"
    assert "IS_DEV" not in dumped, (
        "gating on IS_DEV lets every non-dev value — including the unset default "
        "— through with a live key"
    )
