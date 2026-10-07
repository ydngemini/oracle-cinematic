"""The killer-demo operator tools: seed, reset, preflight (scripts/*demo*).

The reset deletes a tenant's CRM rows, so its refusals are the point of these
tests: production by environment and by hostname, any tenant that is not ALL of
the named id + is_demo + the demo slug, and a tenant with a provider action in
flight. None of these tests touches a network or a real database.
"""

from __future__ import annotations

import asyncio
import importlib.util
import pathlib
import sys
from datetime import datetime, timezone

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

import demo_tenant_common as common  # noqa: E402


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), REPO / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve their module by name
    spec.loader.exec_module(module)
    return module


reset = _load("reset-demo-tenant")
preflight = _load("demo-preflight")
seed = _load("seed-demo-tenant")

DEMO_ID = "6be7b644-54cd-45ae-b9a0-b9e0b385ce03"
DEMO_ROW = {"id": DEMO_ID, "slug": common.TENANT_SLUG, "is_demo": True}


# ── refusals ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("env", ["prod", "production", "PRODUCTION"])
def test_production_environment_is_refused(monkeypatch, env):
    monkeypatch.setenv("ORACLE_ENV", env)
    with pytest.raises(common.DemoSafetyError):
        common.refuse_production("https://neoh-staging.example.test")


@pytest.mark.parametrize("url", ["https://neohrs.com", "https://app.neohrs.com/x", "https://neoh.app"])
def test_production_hosts_are_refused(monkeypatch, url):
    monkeypatch.setenv("ORACLE_ENV", "staging")
    with pytest.raises(common.DemoSafetyError):
        common.refuse_production(url)


def test_staging_host_is_allowed(monkeypatch):
    monkeypatch.setenv("ORACLE_ENV", "staging")
    common.refuse_production("https://neoh-staging-ksfpn.ondigitalocean.app")


@pytest.mark.parametrize("row, expected", [
    (None, DEMO_ID),
    (DEMO_ROW, None),
    (DEMO_ROW, "11111111-1111-4111-8111-111111111111"),
    ({**DEMO_ROW, "is_demo": False}, DEMO_ID),
    ({**DEMO_ROW, "slug": "acme-realty"}, DEMO_ID),
])
def test_anything_but_the_explicit_demo_tenant_is_refused(row, expected):
    with pytest.raises(common.DemoSafetyError):
        common.assert_demo_tenant(row, expected)


def test_the_demo_tenant_passes():
    common.assert_demo_tenant(DEMO_ROW, DEMO_ID)


class FakeConn:
    """Records every statement; answers the reset's reads."""

    def __init__(self, tenant_row, busy=0, tables=None, fail=None):
        self.tenant_row, self.busy = tenant_row, busy
        self.tables = tables or {"clients": "uuid", "command_executions": "uuid"}
        self.fail = dict(fail or {})
        self.executed: list[str] = []
        self.closed = False

    async def fetchrow(self, sql, *args):
        if "FROM tenants" in sql:
            return self.tenant_row
        return None

    async def fetchval(self, sql, *args):
        if "state = ANY" in sql:
            return self.busy
        return 1

    async def fetch(self, sql, *args):
        if "information_schema.columns" in sql:
            return [{"table_name": t, "data_type": d} for t, d in self.tables.items()]
        return []

    async def execute(self, sql, *args):
        if sql.startswith("DELETE"):
            table = sql.split('"')[1]
            if self.fail.get(table, 0) > 0:
                self.fail[table] -= 1
                raise RuntimeError("violates foreign key constraint")
        self.executed.append(sql)
        return "DELETE 1"

    def transaction(self):
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _tx():
            yield self
        return _tx()

    async def close(self):
        self.closed = True


def _patch_conn(monkeypatch, conn):
    async def connect():
        return conn
    monkeypatch.setattr(common, "connect_admin", connect)


@pytest.mark.parametrize("row", [None, {**DEMO_ROW, "is_demo": False}, {**DEMO_ROW, "slug": "x"}])
def test_reset_refuses_a_non_demo_tenant_before_writing(monkeypatch, row):
    monkeypatch.setenv("ORACLE_ENV", "staging")
    conn = FakeConn(row)
    _patch_conn(monkeypatch, conn)
    with pytest.raises(common.DemoSafetyError):
        asyncio.run(reset.reset(DEMO_ID, "https://staging.example.test", True))
    assert conn.executed == [] and conn.closed


def test_reset_refuses_production_before_connecting(monkeypatch):
    monkeypatch.setenv("ORACLE_ENV", "prod")

    async def boom():  # pragma: no cover - must not be reached
        raise AssertionError("connected to a database in production")
    monkeypatch.setattr(common, "connect_admin", boom)
    with pytest.raises(common.DemoSafetyError):
        asyncio.run(reset.reset(DEMO_ID, "https://staging.example.test", True))


def test_reset_refuses_while_a_provider_action_is_executing(monkeypatch):
    monkeypatch.setenv("ORACLE_ENV", "staging")
    conn = FakeConn(DEMO_ROW, busy=1)
    _patch_conn(monkeypatch, conn)
    with pytest.raises(common.DemoSafetyError, match="executing"):
        asyncio.run(reset.reset(DEMO_ID, "https://staging.example.test", True))
    assert conn.executed == []


def test_dry_run_is_the_default_and_writes_nothing(monkeypatch):
    monkeypatch.setenv("ORACLE_ENV", "staging")
    conn = FakeConn(DEMO_ROW)
    _patch_conn(monkeypatch, conn)
    out = asyncio.run(reset.reset(DEMO_ID, "https://staging.example.test", False))
    assert out["dry_run"] is True and conn.executed == []


def test_every_delete_is_scoped_to_the_tenant_and_retries_fk_order():
    conn = FakeConn(DEMO_ROW, tables={"clients": "uuid", "showings": "uuid", "legacy": "text"},
                    fail={"clients": 1})
    deleted, stuck = asyncio.run(reset.purge(conn, conn.tables, DEMO_ID))
    assert stuck == []
    assert set(deleted) == {"clients", "showings", "legacy"}
    assert all("WHERE tenant_id" in sql for sql in conn.executed)
    assert any("tenant_id::text = $1" in sql for sql in conn.executed)


def test_identity_evidence_and_configuration_are_kept():
    for table in ("users", "team_memberships", "audit_ledger", "erasure_ledger", "legal_holds",
                  "outreach_attempt_log", "subscriptions", "telephony_routes", "provider_credentials"):
        assert table in reset.KEEP, table
    assert "clients" not in reset.KEEP and "command_executions" not in reset.KEEP


def test_cli_dry_run_by_default_and_refusal_exit_code(monkeypatch, capsys):
    monkeypatch.setenv("ORACLE_ENV", "staging")
    conn = FakeConn({**DEMO_ROW, "is_demo": False})
    _patch_conn(monkeypatch, conn)
    assert reset.main(["--tenant-id", DEMO_ID, "--base-url", "https://s.example.test"]) == 2
    assert "REFUSED" in capsys.readouterr().err


# ── preflight logic ─────────────────────────────────────────────────────────

def test_verdicts():
    assert preflight.verdict(["pass", "pass"]) == preflight.READY
    assert preflight.verdict(["pass", "limitation"]) == preflight.LIMITED
    assert preflight.verdict(["limitation", "blocked", "pass"]) == preflight.BLOCKED


def test_report_blocks_on_a_blocking_failure_and_limits_on_an_optional_one():
    rep = preflight.Report()
    rep.add("api", True, "ok")
    rep.add("texting", False, "not set up", blocking=False)
    assert rep.verdict == preflight.LIMITED
    rep.add("demo tenant", False, "missing")
    assert rep.verdict == preflight.BLOCKED


@pytest.mark.parametrize("raw, ok", [
    ("+13024078981", True),
    (" +13024078981 ", True),
    ("+13024078981,+13025550100", False),
    ("+13025550100", False),
    ("", False),
])
def test_allowlist_must_be_exactly_the_operators_phone(raw, ok):
    assert preflight.allowlist_is_exactly_the_demo_phone(raw) is ok


def test_calling_hours_are_recipient_local():
    assert preflight.within_calling_hours(datetime(2026, 10, 5, 16, 0, tzinfo=timezone.utc))
    assert not preflight.within_calling_hours(datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc))
    assert not preflight.within_calling_hours(datetime(2026, 10, 6, 0, 30, tzinfo=timezone.utc))


# ── seed data honesty ───────────────────────────────────────────────────────

def test_the_only_real_number_in_the_seed_is_the_operators_own():
    phones = [c["phone"] for c in common.CLIENTS]
    real = [p for p in phones if not p.startswith("+130255501")]
    assert real == [common.DEMO_RECIPIENT]
    assert next(c for c in common.CLIENTS if c["key"] == "sarah")["phone"] == common.DEMO_RECIPIENT


def test_seed_people_are_synthetic():
    for c in common.CLIENTS:
        assert c["email"].endswith("@example.test")
    for user in (common.OWNER, common.AGENT):
        assert user["email"].endswith(".example.test")


def test_sarah_has_the_story_criteria_as_structured_preferences():
    prefs = next(c for c in common.CLIENTS if c["key"] == "sarah")["preferences"]
    assert prefs["budget_max"] == 525000 and prefs["beds_min"] == 3
    assert prefs["target_cities"] == ["Wilmington"]
    assert {"updated kitchen", "home office"} <= set(prefs["must_haves"])
    assert len([s for s in common.SHOWINGS if s["client"] == "sarah"]) == 2


def test_the_subject_listing_says_what_the_story_claims():
    main = next(item for item in common.LISTINGS if item["key"] == "main")
    assert main["price"] <= 525000 and main["beds"] >= 3
    assert any("office" in f.lower() for f in main["features"])
    assert any("kitchen" in f.lower() for f in main["features"])


def test_seed_is_dry_run_by_default(monkeypatch):
    called = {}

    async def fake_seed(base_url, execute, **kw):
        called["execute"] = execute
        return {}
    monkeypatch.setattr(seed, "seed", fake_seed)
    seed.main(["--base-url", "https://s.example.test"])
    assert called["execute"] is False


def _spec(**envs):
    return {"services": [{"name": "api", "envs": [
        {"key": k, "value": v, **({"type": "SECRET"} if "KEY" in k or "AGENT" in k else {})}
        for k, v in envs.items()]}]}


@pytest.mark.parametrize("spec, live", [
    (_spec(ORACLE_PLIVO_REALTIME_PROVIDER="elevenlabs", ELEVENLABS_API_KEY="EV[1:x]",
           ELEVENLABS_AGENT_ID="EV[1:y]"), True),
    (_spec(ORACLE_PLIVO_REALTIME_PROVIDER="elevenlabs", ELEVENLABS_API_KEY="EV[1:x]"), False),
    (_spec(ORACLE_PLIVO_QWEN_REALTIME_ENABLED="0", DASHSCOPE_API_KEY="EV[1:z]"), False),
    (_spec(ORACLE_PLIVO_QWEN_REALTIME_ENABLED="1", DASHSCOPE_API_KEY="EV[1:z]"), True),
    (None, False),
])
def test_live_voice_is_read_from_the_deployed_spec_not_this_machine(monkeypatch, spec, live):
    monkeypatch.setenv("ORACLE_PLIVO_QWEN_REALTIME_ENABLED", "1")   # local env must not matter
    assert preflight.live_voice_from_spec(spec)[0] is live
