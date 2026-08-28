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
"""

from __future__ import annotations

import importlib

import pytest

import config as config_module


def _prod(monkeypatch, **overrides):
    """Reload config under a production environment with the given secrets."""
    env = {
        "ORACLE_ENV": "prod",
        "ORACLE_BASE_URL": "https://neohrs.com",
        "ORACLE_SECRET_KEY": "s" * 40 + "trongEnough1",
        "ORACLE_ENCRYPTION_MASTER_KEY": "m4st3r" + "K" * 30 + "xyz",
    }
    env.update(overrides)
    for key in (
        "ORACLE_ADMIN_PASSPHRASE",
        "ORACLE_ACS_WEBHOOK_SECRET",
        "ORACLE_CUSTOM_CALL_WEBHOOK_SECRET",
        "ORACLE_ENABLE_WEBHOOKS",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    return importlib.reload(config_module)


def test_the_terraform_placeholder_no_longer_boots(monkeypatch):
    config = _prod(
        monkeypatch,
        ORACLE_SECRET_KEY="REPLACE_ME",
        ORACLE_ENCRYPTION_MASTER_KEY="REPLACE_ME",
        ORACLE_ADMIN_PASSPHRASE="REPLACE_ME",
    )
    with pytest.raises(RuntimeError) as excinfo:
        config.validate_or_die()

    message = str(excinfo.value)
    assert "placeholder" in message
    # All three are named, not just the first one found — an operator fixing
    # them one boot at a time is three deploys they did not need.
    assert "ORACLE_SECRET_KEY" in message
    assert "ORACLE_ENCRYPTION_MASTER_KEY" in message
    assert "ORACLE_ADMIN_PASSPHRASE" in message


def test_the_rejection_never_echoes_the_secret(monkeypatch):
    """A secret that leaks through its own rejection message is no better off."""
    config = _prod(monkeypatch, ORACLE_SECRET_KEY="hunter2")
    with pytest.raises(RuntimeError) as excinfo:
        config.validate_or_die()

    assert "hunter2" not in str(excinfo.value)
    assert "shorter than" in str(excinfo.value)


@pytest.mark.parametrize(
    "value",
    ["replace_me", "REPLACE-ME", "  ChangeMe  ", "placeholder", "password", "TODO"],
)
def test_placeholders_are_caught_regardless_of_case_or_padding(monkeypatch, value):
    config = _prod(monkeypatch, ORACLE_SECRET_KEY=value)
    with pytest.raises(RuntimeError, match="placeholder or weak"):
        config.validate_or_die()


def test_a_long_but_degenerate_key_is_refused(monkeypatch):
    """32 characters of "aaaa..." satisfies a length check and nothing else."""
    config = _prod(monkeypatch, ORACLE_SECRET_KEY="a" * 48)
    with pytest.raises(RuntimeError, match="too few distinct characters"):
        config.validate_or_die()


def test_real_secrets_boot(monkeypatch):
    config = _prod(monkeypatch, ORACLE_ADMIN_PASSPHRASE="c0rrect-horse-battery-staple")
    config.validate_or_die()  # must not raise


def test_an_unset_optional_secret_is_not_this_checks_complaint(monkeypatch):
    """The operator account and webhook secrets are optional.

    Absence is the `missing` check's business; conflating the two would refuse
    a deployment that simply has no operator login configured.
    """
    config = _prod(monkeypatch, ORACLE_ADMIN_PASSPHRASE=None)
    config.validate_or_die()  # must not raise


def test_development_stays_relaxed(monkeypatch):
    """Dev returns before the secret checks — a local box must still start."""
    config = _prod(
        monkeypatch,
        ORACLE_ENV="dev",
        ORACLE_SECRET_KEY="REPLACE_ME",
        ORACLE_ENCRYPTION_MASTER_KEY="REPLACE_ME",
    )
    config.validate_or_die()  # must not raise
