"""Audit 34.7: the ACS callback webhook acknowledges an event before its
handler runs, so a crash after the 200 loses it. Production may enable the
webhook only as an explicit, acknowledged experiment."""

import pytest

from tests.test_config_demo_logins import _validate

_SECRET = "acs-webhook-secret-with-plenty-of-entropy"
_JWT = {"ORACLE_JWT_ISSUER": "neoh", "ORACLE_JWT_AUDIENCE": "neoh"}


@pytest.mark.parametrize("env", ["production", "prod", "", "ci"])
def test_production_rules_refuse_the_acs_webhook_unless_acknowledged(env):
    refused = _validate(ORACLE_ENV=env, ORACLE_ACS_WEBHOOK_SECRET=_SECRET, **_JWT)
    assert refused.returncode != 0 and "ORACLE_ACS_EXPERIMENTAL" in refused.stderr
    assert _SECRET not in refused.stderr, "the refusal names the setting, never its value"


def test_an_acknowledged_experiment_boots():
    booted = _validate(ORACLE_ENV="production", ORACLE_ACS_WEBHOOK_SECRET=_SECRET, ORACLE_ACS_EXPERIMENTAL="1", **_JWT)
    assert booted.returncode == 0, booted.stderr


def test_webhooks_on_without_acs_need_no_acs_secret():
    """34.7 review: turning webhooks on must not force the ACS experiment."""
    booted = _validate(ORACLE_ENV="production", ORACLE_ENABLE_WEBHOOKS="1",
                       ORACLE_CUSTOM_CALL_WEBHOOK_SECRET="a-genuine-custom-call-secret-0123", **_JWT)
    assert booted.returncode == 0, booted.stderr


def test_production_without_acs_and_staging_with_it_are_unaffected():
    assert _validate(ORACLE_ENV="production", **_JWT).returncode == 0
    assert "ORACLE_ACS_EXPERIMENTAL" not in _validate(
        ORACLE_ENV="staging", ORACLE_ACS_WEBHOOK_SECRET=_SECRET,
        ORACLE_JWT_ISSUER="neoh-staging", ORACLE_JWT_AUDIENCE="neoh-staging").stderr
