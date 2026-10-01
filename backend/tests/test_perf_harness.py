"""The load-test harness itself (performance/), tested before any load runs.

A harness that silently points at production, leaks a real credential into a
test environment, or reports PASS on a run that did nothing is worse than no
harness. These run in the ordinary suite (the PR tier, docs/performance-
capacity-runbook.md) so a broken harness is caught in seconds, not two hours
into a soak.
"""

from __future__ import annotations

import csv
import gzip
import importlib.util
import json
import pathlib
import re
import subprocess
import sys

import pytest

PERF = pathlib.Path(__file__).resolve().parents[2] / "performance"


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, PERF / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


guard = _load("perf_guard", "guard.py")
result = _load("perf_result", "result.py")
analyze = _load("perf_analyze", "analyze.py")
fanout = _load("perf_fanout", "fanout_analyze.py")
secrets_mod = _load("perf_secrets", "fixtures/perf_secrets.py")
seed = _load("perf_seed", "fixtures/seed.py")
real_ai = _load("perf_real_ai", "ai_real_sample.py")

OPT_IN = {"NEOH_LOAD_TEST_ALLOWED": "1"}


# ── production-target refusal ───────────────────────────────────────────────

def test_refuses_without_explicit_opt_in():
    with pytest.raises(guard.Refused):
        guard.check("http://staging.example.test", env={}, version={"environment": "staging"})


@pytest.mark.parametrize("url", ["https://neohrs.com", "https://api.neohrs.com/x", "https://www.neoh.app"])
def test_refuses_known_production_hosts_even_if_they_claim_staging(url):
    with pytest.raises(guard.Refused):
        guard.check(url, env=OPT_IN, version={"environment": "loadtest"})


def test_refuses_operator_named_production_hosts(monkeypatch):
    monkeypatch.setenv("NEOH_PRODUCTION_HOSTS", "neoh-prod-abc.ondigitalocean.app")
    with pytest.raises(guard.Refused):
        guard.check("https://neoh-prod-abc.ondigitalocean.app", env=OPT_IN, version={"environment": "staging"})


@pytest.mark.parametrize("env", ["prod", "production", "unset", "", None, "dev"])
def test_refuses_any_target_that_does_not_say_it_is_staging_or_loadtest(env):
    with pytest.raises(guard.Refused):
        guard.check("http://oracle-perf-lb:8080", env=OPT_IN, version={"environment": env})


def test_an_unreachable_target_is_a_refusal_not_a_pass(monkeypatch):
    def boom(*a, **k):
        raise guard.urllib.error.URLError("no route")
    monkeypatch.setattr(guard.urllib.request, "urlopen", boom)
    with pytest.raises(guard.Refused):
        guard.check("http://nowhere.invalid", env=OPT_IN)


def test_accepts_a_loadtest_target_and_records_what_it_is():
    meta = guard.check("http://oracle-perf-lb:8080", env=OPT_IN,
                       version={"environment": "loadtest", "git_sha": "abc", "migration_head": "0115_x.sql"})
    assert meta == {"target_host": "oracle-perf-lb", "target_environment": "loadtest",
                    "target_git_sha": "abc", "target_migration_head": "0115_x.sql"}


# ── safe provider selection: no real credential reaches the perf env ────────

def _filter_env(lines: list[str]) -> list[str]:
    up = (PERF / "topology" / "up.sh").read_text()
    keep = re.search(r"^KEEP='([^']+)'", up, re.M).group(1)
    drop = re.search(r"^DROP='([^']+)'", up, re.M).group(1)
    awk = re.search(r"awk -v keep=\"\$KEEP\" -v drop=\"\$DROP\" '([^']+)'", up).group(1)
    out = subprocess.run(["awk", "-v", f"keep={keep}", "-v", f"drop={drop}", awk],
                         input="\n".join(lines) + "\n", capture_output=True, text=True, check=True)
    return [line.split("=", 1)[0] for line in out.stdout.splitlines()]


def test_every_provider_credential_is_stripped_from_the_perf_env():
    live = ["ORACLE_FIREWORKS_API_KEY", "AZURE_AI_API_KEY", "AWS_SECRET_ACCESS_KEY", "AWS_ACCESS_KEY_ID",
            "BEDROCK_AWS_SECRET_ACCESS_KEY", "TWILIO_AUTH_TOKEN", "TWILIO_ACCOUNT_SID", "PLIVO_AUTH_ID",
            "PLIVO_AUTH_TOKEN", "TELNYX_API_KEY", "ACS_CONNECTION_STRING", "STRIPE_SECRET_KEY",
            "STRIPE_WEBHOOK_SECRET", "ORACLE_SMTP_PASSWORD", "RUNPOD_API_KEY", "ELEVENLABS_API_KEY",
            "DASHSCOPE_API_KEY", "FAL_KEY", "REGRID_API_TOKEN", "ORACLE_LOCAL_LLM_URL",
            "ORACLE_ALLOW_LIVE_STRIPE", "ORACLE_ENV", "REDIS_URL", "ORACLE_S3_SECRET_ACCESS_KEY"]
    kept = ["ORACLE_DB_PASSWORD", "ORACLE_DB_APP_PASSWORD", "ORACLE_SECRET_KEY",
            "ORACLE_ENCRYPTION_MASTER_KEY", "ORACLE_DB_HOST", "ORACLE_FEATURE_AI_CHAT"]
    names = _filter_env([f"{n}=x" for n in live + kept])
    assert sorted(names) == sorted(kept)


def test_the_perf_env_gets_mock_endpoints_and_no_private_key():
    s = {"stripe_webhook_secret": "whsec_perf1", "telnyx_private_key": "PRIV", "telnyx_public_key": "PUB",
         "plivo_auth_id": "MAPERF1", "plivo_auth_token": "perftok"}
    env = secrets_mod.server_env(s)
    assert "PRIV" not in env
    assert "ORACLE_LOCAL_LLM_URL=http://oracle-perf-mock:9000" in env
    assert "DASHSCOPE_REALTIME_URL=ws://oracle-perf-mock:9000/realtime" in env
    assert "RECONSTRUCTION_PROVIDER=stub" in env


# ── real-provider cost guard ────────────────────────────────────────────────

def test_real_ai_is_refused_without_its_own_opt_in():
    with pytest.raises(real_ai.CostGuardRefused):
        real_ai.plan({"NEOH_LOAD_TEST_ALLOWED": "1"}, requests=5, concurrency=1)


def test_real_ai_is_hard_capped_whatever_is_asked_for():
    env = {"NEOH_LOAD_TEST_ALLOWED": "1", "NEOH_REAL_AI_ALLOWED": "1"}
    p = real_ai.plan(env, requests=10_000, concurrency=500)
    assert p["requests"] <= real_ai.HARD_MAX_REQUESTS
    assert p["concurrency"] <= real_ai.HARD_MAX_CONCURRENCY
    assert p["estimated_cost_usd"] <= real_ai.HARD_MAX_USD


# ── fixture tenant isolation ────────────────────────────────────────────────

def test_every_synthetic_tenant_has_a_distinct_sentinel_no_other_contains():
    tenants = seed.plan("pilot")
    sentinels = [t["sentinel"] for t in tenants]
    assert len(set(sentinels)) == len(sentinels)
    for a in sentinels:
        assert not any(a != b and a in b for b in sentinels)


def test_fixture_ids_are_deterministic_and_tenant_scoped():
    assert seed.uid("lead", "perf-solo-01", 0) == seed.uid("lead", "perf-solo-01", 0)
    assert seed.uid("lead", "perf-solo-01", 0) != seed.uid("lead", "perf-solo-02", 0)


def test_every_load_scenario_goes_through_the_isolation_check():
    lib = (PERF / "lib" / "neoh.js").read_text()
    assert "exec.test.abort(`TENANT LEAK" in lib
    for js in (PERF / "scenarios").glob("*.js"):
        text = js.read_text()
        # Either through call() (HTTP, checked centrally) or an explicit
        # sentinel check on socket frames.
        assert "tenant_leaks" in text, f"{js.name} declares no tenant_leaks threshold"


def test_expired_sessions_and_empty_runs_cannot_pass():
    # Expired JWTs close every socket at auth; with only percentile and
    # count==0 thresholds the run PASSED with zero chat turns.
    lib = (PERF / "lib" / "neoh.js").read_text()
    assert "SESSION_MARGIN_S" in lib and "re-mint" in lib
    chat = (PERF / "scenarios" / "ai_chat.js").read_text()
    assert "ai_turns_completed: ['count>0']" in chat
    # Admission alone passed a 13 s first-token run; the person waits on this.
    assert "ai_first_token_ms: ['p(95)<10000']" in chat


# ── threshold / result artifacts ────────────────────────────────────────────

def test_result_artifact_carries_identity_and_verdict_but_never_secrets(tmp_path):
    users = tmp_path / "users.json"
    users.write_text(json.dumps({"profile": "tiny", "users": [
        {"tenant_slug": "perf-solo-01", "shape": "solo", "password": "hunter2"}]}))
    art = result.build("read_load-75vu-X", {"target_git_sha": "abc", "target_environment": "loadtest"},
                       {"endpoints": {"GET /x": {"p95_ms": 10}}, "total_requests": 1},
                       {"db": {"conns_active": {"peak": 3}}}, 99, ["-e", "VUS=75"], users)
    assert art["verdict"] == "FAIL" and art["k6_exit_code"] == 99
    assert art["environment"]["target_git_sha"] == "abc"
    assert art["data_scale"] == {"profile": "tiny", "tenants": 1, "users": 1, "tenants_by_shape": {"solo": 1}}
    assert "hunter2" not in json.dumps(art)
    assert result.build("x", {}, {}, {}, 0, [], users)["verdict"] == "PASS"


def test_scrub_drops_credential_keys_at_any_depth():
    assert result.scrub({"a": {"token": "t", "Password": "p", "ok": 1}, "l": [{"api_key": 1, "n": 2}]}) == \
        {"a": {"ok": 1}, "l": [{"n": 2}]}


def _csv(path, rows):
    cols = ["metric_name", "timestamp", "metric_value", "check", "error", "error_code", "expected_response",
            "group", "method", "name", "proto", "scenario", "service", "status", "subproto", "tls_version",
            "url", "extra_tags", "metadata"]
    with gzip.open(path, "wt", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in cols})


def test_analyzer_reports_per_endpoint_latency_and_replica_split(tmp_path):
    p = tmp_path / "s.csv.gz"
    rows = [{"metric_name": "http_req_duration", "metric_value": str(v), "name": "GET /a", "status": "200"}
            for v in range(1, 101)]
    rows += [{"metric_name": "http_req_failed", "metric_value": "1", "name": "GET /a"}]
    rows += [{"metric_name": "replica_hits", "metric_value": "1", "extra_tags": f"replica=r{i % 2}"}
             for i in range(10)]
    _csv(p, rows)
    out = analyze.analyze(str(p))
    assert out["endpoints"]["GET /a"]["p95_ms"] == 95.0
    assert out["endpoints"]["GET /a"]["error_rate"] == 0.01
    assert out["replica_distribution"] == {"r0": 0.5, "r1": 0.5}


def test_fanout_join_counts_missed_and_duplicate_deliveries(tmp_path):
    p = tmp_path / "f.csv.gz"
    rows = [
        {"metric_name": "fanout_listener", "metric_value": "1", "extra_tags": "tenant=A&vu=1&replica=r1"},
        {"metric_name": "fanout_listener", "metric_value": "1", "extra_tags": "tenant=A&vu=2&replica=r2"},
        {"metric_name": "fanout_listener", "metric_value": "1", "extra_tags": "tenant=A&vu=3&replica=r2"},
        {"metric_name": "fanout_sent", "metric_value": "1000", "extra_tags": "lead=L&tenant=A&replica=r1&ok=true"},
        {"metric_name": "fanout_recv", "metric_value": "1010", "extra_tags": "lead=L&tenant=A&vu=1"},
        {"metric_name": "fanout_recv", "metric_value": "1030", "extra_tags": "lead=L&tenant=A&vu=2"},
        {"metric_name": "fanout_recv", "metric_value": "1031", "extra_tags": "lead=L&tenant=A&vu=2"},
    ]
    _csv(p, rows)
    out = fanout.analyze(str(p))
    assert (out["expected_deliveries"], out["delivered"], out["missed"], out["duplicates"]) == (3, 2, 1, 1)
    assert out["latency_local"]["p50_ms"] == 10.0
    assert out["latency_cross_replica"]["p50_ms"] == 30.0


def test_scenarios_that_can_do_nothing_cannot_pass_vacuously():
    recon = (PERF / "scenarios" / "recon_queue.js").read_text()
    assert "checks: ['rate==1']" in recon and "recon_claimed: [`count>=${JOBS}`]" in recon
    baseline = (PERF / "scenarios" / "baseline.js").read_text()
    assert "http_req_failed" in baseline


def test_the_noop_load_job_is_registered_only_in_a_loadtest_environment():
    import importlib
    import automation_jobs
    import config
    import loadtest_jobs
    assert config.ORACLE_ENV != "loadtest"
    importlib.reload(loadtest_jobs)
    assert loadtest_jobs.JOB_TYPE not in automation_jobs.registered_handlers()
