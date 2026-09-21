"""The mission digest sends an operator email; it must never touch a lead.

Three separate claims tested here: (1) it is off unless explicitly turned on,
independently of missions itself being on; (2) turning it on with nothing
else configured still sends nothing rather than raising; (3) the report it
builds never carries a name, phone, email, or address — only counts.
"""

from __future__ import annotations

import asyncio
import inspect
import pathlib
from datetime import datetime, timezone

from missions import digest


class TestDormancy:
    def test_off_by_default(self, monkeypatch):
        monkeypatch.delenv("ORACLE_MISSION_DIGEST_ENABLED", raising=False)
        assert digest.enabled() is False

    def test_send_digest_is_a_noop_when_disabled(self, monkeypatch):
        monkeypatch.delenv("ORACLE_MISSION_DIGEST_ENABLED", raising=False)

        async def explode(_ctx):
            raise AssertionError("the database was touched while disabled")

        monkeypatch.setattr("db.connection.tenant_tx", explode)
        out = asyncio.run(digest.send_digest())
        assert "not enabled" in out["skipped"]

    def test_send_digest_requires_missions_enabled_too(self, monkeypatch):
        """Two independent switches — turning the digest on must not, by
        itself, start reading mission data if missions itself is off."""
        monkeypatch.setenv("ORACLE_MISSION_DIGEST_ENABLED", "1")
        monkeypatch.delenv("ORACLE_FEATURE_MISSIONS", raising=False)

        async def explode(_ctx):
            raise AssertionError("the database was touched while missions are disabled")

        monkeypatch.setattr("db.connection.tenant_tx", explode)
        out = asyncio.run(digest.send_digest())
        assert "not enabled" in out["skipped"]

    def test_send_digest_requires_a_recipient(self, monkeypatch):
        monkeypatch.setenv("ORACLE_MISSION_DIGEST_ENABLED", "1")
        monkeypatch.setenv("ORACLE_FEATURE_MISSIONS", "1")
        monkeypatch.delenv("ORACLE_MISSION_DIGEST_EMAIL", raising=False)

        async def explode(_ctx):
            raise AssertionError("the database was touched with no recipient configured")

        monkeypatch.setattr("db.connection.tenant_tx", explode)
        out = asyncio.run(digest.send_digest())
        assert "ORACLE_MISSION_DIGEST_EMAIL" in out["skipped"]

    def test_the_scheduled_task_is_off_unless_explicitly_enabled(self):
        source = (
            pathlib.Path(__file__).resolve().parents[1]
            / "data_integrations" / "periodic.py"
        ).read_text(encoding="utf-8")
        block = source.split('name="mission_digest"')[1].split("    ))")[0]
        assert 'os.getenv("ORACLE_MISSION_DIGEST_ENABLED", "0") == "1"' in block, (
            "the mission digest must default off"
        )


class TestNoLeadDataLeaves:
    def test_the_tenant_query_selects_no_contact_columns(self):
        source = inspect.getsource(digest._tenant_report)
        for forbidden in ("full_name", "email", "phone", "address", "clients.", "leads.", "contacts."):
            assert forbidden not in source, (
                f"the digest query must not touch contact data; found {forbidden!r}"
            )
        assert "FROM missions" in source
        assert "FROM mission_actions" in source
        assert "FROM mission_events" in source

    def test_it_sends_through_smtp_mailer_only(self):
        source = inspect.getsource(digest)
        assert "smtp_mailer.send" in source
        for forbidden in ("twilio", "requests.post", "httpx.post"):
            assert forbidden not in source.lower()


class TestReportFormatting:
    def _report(self, **overrides):
        base = {
            "missions": [{
                "id": "aaaaaaaa-0000-0000-0000-000000000001",
                "objective_kind": "sphere_touched",
                "objective_text": "Touch every past client before the holidays.",
                "status": "shadow",
                "mode": "shadow",
                "allowed_channels": ["email", "task"],
                "auto_channels": [],
                "target_count": 50,
                "launched_at": None,
            }],
            "actions": {"aaaaaaaa-0000-0000-0000-000000000001": {"would_have_done": 12, "blocked": 2}},
            "blocked": {"aaaaaaaa-0000-0000-0000-000000000001": [
                {"reason": "suppressed: opted out", "count": 2},
            ]},
            "events": [],
        }
        base.update(overrides)
        return base

    def test_summarizes_counts_and_blocked_reasons(self):
        since = datetime(2026, 1, 1, tzinfo=timezone.utc)
        until = datetime(2026, 1, 2, tzinfo=timezone.utc)
        text, html = digest._format_report(
            [("11111111-1111-1111-1111-111111111111", self._report())],
            since=since, until=until,
        )
        assert "sphere_touched" in text
        assert "would_have_done: 12" in text
        assert "blocked: 2" in text
        assert "suppressed: opted out" in text
        assert "autopilot channels: none" in text
        assert "sphere_touched" in html
        assert "suppressed: opted out" in html

    def test_no_missions_reads_as_no_missions_not_silence(self):
        text, html = digest._format_report(
            [], since=datetime.now(timezone.utc), until=datetime.now(timezone.utc),
        )
        assert "No missions are running." in text
        assert "No missions are running." in html
