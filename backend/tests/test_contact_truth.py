from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from contact_truth import (
    BUYER_INTAKE_QUESTIONS,
    INTAKE_TOOL_ACCESS,
    SELLER_INTAKE_QUESTIONS,
    ContactTruthConfigError,
    evaluate_nurture,
    lookup_hash,
    name_query_tokens,
    name_search_tokens,
    normalize_email,
    normalize_intake_answers,
    normalize_phone,
    nurture_idempotency_key,
    questions_for,
)


TENANT_ID = "11111111-1111-1111-1111-111111111111"
CONTACT_ID = "22222222-2222-2222-2222-222222222222"
MIGRATION = (
    Path(__file__).parents[1]
    / "db"
    / "migrations"
    / "0054_agent_contacts_and_intake.sql"
)


def test_question_sets_are_exactly_three_and_have_no_property_tools():
    assert questions_for("buyer") == BUYER_INTAKE_QUESTIONS == (
        "What is your target budget?",
        "How many bedrooms and bathrooms do you need?",
        "What area or ZIP code are you targeting?",
    )
    assert questions_for("seller") == SELLER_INTAKE_QUESTIONS == (
        "What is the property address?",
        "What is your desired timeline?",
        "What outcome are you hoping for?",
    )
    assert INTAKE_TOOL_ACCESS == ()


def test_normalization_and_tenant_keyed_lookup_tokens(monkeypatch):
    monkeypatch.setenv("ORACLE_ENCRYPTION_MASTER_KEY", "unit-test-master-key")

    email = normalize_email(" Agent@Exämple.com ")
    phone = normalize_phone("(302) 407-8981")
    assert email == "agent@xn--exmple-cua.com"
    assert phone == "+13024078981"

    email_hash = lookup_hash(TENANT_ID, "email", email)
    assert email_hash and len(email_hash) == 64
    assert email_hash != lookup_hash(
        "33333333-3333-3333-3333-333333333333", "email", email
    )
    assert email_hash != lookup_hash(TENANT_ID, "phone", email)
    assert email not in email_hash


def test_lookup_refuses_to_fall_back_to_a_hardcoded_key(monkeypatch):
    monkeypatch.delenv("ORACLE_ENCRYPTION_MASTER_KEY", raising=False)
    with pytest.raises(ContactTruthConfigError):
        lookup_hash(TENANT_ID, "phone", "+13024078981")


def test_name_blind_index_supports_word_prefixes_and_is_tenant_separated(monkeypatch):
    monkeypatch.setenv("ORACLE_ENCRYPTION_MASTER_KEY", "unit-test-master-key")
    indexed = name_search_tokens(TENANT_ID, "José Smith")
    query = name_query_tokens(TENANT_ID, "jos sm")

    assert query
    assert set(query).issubset(indexed)
    assert all(len(token) == 64 for token in indexed)
    assert "jose" not in "".join(indexed)
    assert indexed != name_search_tokens(
        "33333333-3333-3333-3333-333333333333", "José Smith"
    )


def test_buyer_intake_normalizes_only_the_three_recorded_answers():
    normalized = normalize_intake_answers(
        "buyer",
        ["$200K", "3 bedrooms and 2.5 bathrooms", "Milford, DE 19963"],
    )
    assert normalized == {
        "target_budget": 200_000,
        "target_budget_raw": "$200K",
        "bedrooms": 3,
        "bathrooms": 2.5,
        "area_or_zip": "Milford, DE 19963",
        "zip_code": "19963",
    }
    assert not any(
        forbidden in normalized
        for forbidden in ("listings", "valuation", "public_records", "estimate")
    )


def test_seller_intake_is_exact_and_does_not_invent_property_facts():
    normalized = normalize_intake_answers(
        "seller",
        ["5639 S Homan Ave, Chicago, IL", "Within 60 days", "A clean sale"],
    )
    assert normalized == {
        "property_address": "5639 S Homan Ave, Chicago, IL",
        "desired_timeline": "Within 60 days",
        "desired_outcome": "A clean sale",
    }
    with pytest.raises(ValueError, match="exactly three"):
        normalize_intake_answers("seller", ["address", "timeline"])


def test_nurture_policy_requires_due_date_consent_and_non_quiet_local_time():
    now = datetime(2026, 8, 3, 14, 0, tzinfo=timezone.utc)  # 10am New York
    allowed = evaluate_nurture(
        event_type="birthday",
        channel="sms",
        event_month=8,
        event_day=3,
        timezone_name="America/New_York",
        consent={"sms": {"granted": True}},
        suppression={"global": False, "sms": False, "dnc": False},
        nurture_enabled=True,
        now=now,
    )
    assert allowed.eligible is True
    assert allowed.calendar_year == 2026

    missing_consent = evaluate_nurture(
        event_type="birthday",
        channel="sms",
        event_month=8,
        event_day=3,
        timezone_name="America/New_York",
        consent={"sms": {"granted": False}},
        suppression={},
        nurture_enabled=True,
        now=now,
    )
    assert (missing_consent.eligible, missing_consent.reason) == (
        False,
        "consent_missing",
    )

    quiet = evaluate_nurture(
        event_type="birthday",
        channel="email",
        event_month=8,
        event_day=3,
        timezone_name="America/New_York",
        consent={"email": {"granted": True}},
        suppression={},
        nurture_enabled=True,
        now=datetime(2026, 8, 3, 5, 0, tzinfo=timezone.utc),  # 1am local
    )
    assert (quiet.eligible, quiet.reason) == (False, "quiet_hours")


def test_nurture_key_is_exactly_scoped_to_one_contact_channel_and_year():
    key = nurture_idempotency_key(
        TENANT_ID, CONTACT_ID, "home_anniversary", "email", 2026
    )
    assert key == (
        "nurture:v1:11111111-1111-1111-1111-111111111111:"
        "22222222-2222-2222-2222-222222222222:home_anniversary:email:2026"
    )


def test_migration_is_tenant_safe_encrypted_and_dual_compatible():
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS agent_contacts" in sql
    assert "pii_ciphertext      bytea" in sql
    assert "phone_lookup_hash   char(64)" in sql
    assert "FOREIGN KEY (tenant_id, contact_id)" in sql
    assert "agent_contacts_tenant_legacy_client_fk" in sql
    assert "FOREIGN KEY (tenant_id, legacy_client_id)" in sql
    assert "ALTER TABLE clients ADD COLUMN IF NOT EXISTS contact_id uuid" in sql
    assert "'pending_encryption'" in sql
    assert "FORCE ROW LEVEL SECURITY" in sql
    assert "cardinality(tool_access) = 0" in sql
    assert "tenant_id, contact_id, event_type, channel, calendar_year" in sql
    assert "REVOKE UPDATE, DELETE, TRUNCATE ON contact_intake_sessions" in sql
    assert "ORACLE_ENCRYPTION_MASTER_KEY" not in sql
