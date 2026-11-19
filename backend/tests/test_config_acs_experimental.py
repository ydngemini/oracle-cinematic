"""Audit 34.7: the ACS callback webhook acknowledges an event before its
handler runs, so a crash after the 200 loses it. Production may enable the
webhook only as an explicit, acknowledged experiment."""

from tests.test_config_demo_logins import _validate

_SECRET = "acs-webhook-secret-with-plenty-of-entropy"


def test_production_refuses_the_acs_webhook_unless_acknowledged():
    refused = _validate(ORACLE_ENV="production", ORACLE_ACS_WEBHOOK_SECRET=_SECRET)
    assert refused.returncode != 0 and "ORACLE_ACS_EXPERIMENTAL" in refused.stderr
    assert _SECRET not in refused.stderr, "the refusal names the setting, never its value"
    acknowledged = _validate(ORACLE_ENV="production", ORACLE_ACS_WEBHOOK_SECRET=_SECRET, ORACLE_ACS_EXPERIMENTAL="1")
    assert "ORACLE_ACS_EXPERIMENTAL" not in acknowledged.stderr


def test_production_without_acs_and_staging_with_it_are_unaffected():
    assert "ORACLE_ACS_EXPERIMENTAL" not in _validate(ORACLE_ENV="production").stderr
    assert "ORACLE_ACS_EXPERIMENTAL" not in _validate(
        ORACLE_ENV="staging", ORACLE_ACS_WEBHOOK_SECRET=_SECRET,
        ORACLE_JWT_ISSUER="neoh-staging", ORACLE_JWT_AUDIENCE="neoh-staging").stderr
