"""scripts/neoh-launch-readiness.py — one command, one honest report.

Every DigitalOcean/GitHub/HTTP interaction is faked: these tests prove the
classification (PASS / WARN / BLOCKED), that nothing it runs can change
anything, and that no secret value it is handed ever reaches its output.
"""

from __future__ import annotations

import copy
import datetime as dt
import importlib.util
import json
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
_s = importlib.util.spec_from_file_location("neoh_launch_readiness", REPO / "scripts" / "neoh-launch-readiness.py")
lr = importlib.util.module_from_spec(_s)
sys.modules["neoh_launch_readiness"] = lr  # dataclasses resolve annotations through sys.modules
_s.loader.exec_module(lr)
_r = importlib.util.spec_from_file_location("render_app_spec_lr", REPO / "scripts" / "render-app-spec.py")
render = importlib.util.module_from_spec(_r)
_r.loader.exec_module(render)

NOW = dt.datetime(2026, 10, 3, 20, 0, tzinfo=dt.timezone.utc)
SHA = "c" * 40
BD = "sha256:" + "a" * 64
FD = "sha256:" + "b" * 64
SENTINEL = "SENTINEL-do-not-print-7f3a9c"


def _spec(env="production"):
    spec = render.render(env, BD, FD)
    for comp in spec["services"] + spec["workers"]:
        for e in comp.get("envs") or []:
            if e.get("type") == "SECRET" and not e.get("value"):
                e["value"] = f"EV[1:{SENTINEL}-{e['key']}]"
    return spec


def _db(name, engine, version, status="online"):
    return {"id": f"id-{name}", "name": name, "engine": engine, "version": version, "status": status,
            "size": "db-s-2vcpu-4gb", "region": "nyc3", "created_at": "2026-09-01T00:00:00Z",
            "connection": {"uri": f"postgresql://doadmin:{SENTINEL}-pw@h:25060/x", "password": f"{SENTINEL}-pw",
                           "user": "doadmin", "host": "h"}}


class World:
    """A fake account. Mutate the attributes, then run the audit."""

    def __init__(self, env="production"):
        self.env = env
        name = "neoh" if env == "production" else "neoh-staging"
        suffix = "" if env == "production" else "-staging"
        self.apps = [{"id": "app-0123456789", "spec": {"name": name}, "live_url": "https://neoh.example.com",
                      "region": {"slug": "nyc"}, "active_deployment": {"phase": "ACTIVE"}}]
        self.spec = _spec(env)
        self.dbs = [_db(f"neoh-postgres{suffix}", "pg", "16"), _db(f"neoh-redis{suffix}", "valkey", "8")]
        self.backups = [{"created_at": "2026-10-03T08:00:00Z", "size_gigabytes": 0.1}]
        self.firewall = [{"type": "app", "value": "app-0123456789"}]
        self.gh_env = {"name": env, "protection_rules": [{"type": "required_reviewers"}],
                       "deployment_branch_policy": {"protected_branches": True}}
        self.gh_secrets = [{"name": n} for n in lr.CI_ENV_SECRETS]
        self.gh_env_vars = [{"name": "NEOH_DOMAIN", "value": "neoh.example.com"}]
        self.gh_repo_vars = [{"name": "STAGING_ENABLED", "value": "true"}]
        self.http = {
            "/live": (200, {}, b'{"status":"ok"}'),
            "/health": (200, {}, b'{"status":"ok"}'),
            "/version": (200, {}, json.dumps({"git_sha": SHA, "migration_head": "0125_x.sql"}).encode()),
            "/health/workers": (200, {}, json.dumps({"healthy": True, "live_workers": 1,
                                                     "live_git_shas": [SHA]}).encode()),
            "/": (200, {"content-security-policy": "default-src 'self'; frame-ancestors 'none'",
                        "strict-transport-security": "max-age=63072000"}, b"<html>"),
        }
        self.calls = []

    def runner(self, argv):
        self.calls.append(list(argv))
        a = tuple(argv)
        j = lambda x: (0, json.dumps(x), "")  # noqa: E731
        if a[:3] == ("doctl", "account", "get"):
            return j({"email": "owner@example.test", "status": "active"})
        if a[:3] == ("doctl", "apps", "list"):
            return j(self.apps)
        if a[:3] == ("doctl", "apps", "spec"):
            return j(self.spec)
        if a[:3] == ("doctl", "databases", "list"):
            return j(self.dbs)
        if a[:3] == ("doctl", "databases", "firewalls"):
            return j(self.firewall)
        if a[:3] == ("doctl", "databases", "backups"):
            return j(self.backups)
        if a[:3] == ("doctl", "registry", "get"):
            return j([{"name": "neoh-registry"}])
        if a[:3] == ("doctl", "registry", "repository"):
            return j([{"name": "neoh-backend"}, {"name": "neoh-frontend"}])
        if a[:2] == ("gh", "api"):
            return j(self.gh_env) if self.gh_env else (1, '{"message":"Not Found"}', "")
        if a[:3] == ("gh", "secret", "list"):
            return j(self.gh_secrets)
        if a[:3] == ("gh", "variable", "list"):
            return j(self.gh_env_vars if "--env" in a else self.gh_repo_vars)
        return 1, "", "unexpected command"

    def http_get(self, url, headers):
        path = url.split("neoh.example.com", 1)[-1] or "/"
        st, h, b = self.http.get(path, (404, {}, b"{}"))
        if "Origin" in headers:
            h = {**h}
        return st, h, b


def audit(world, gates_root=None, **kw):
    a = lr.Audit(world.env, runner=world.runner, http=world.http_get, now=NOW,
                 repo_root=gates_root or REPO, **kw)
    a.run_all()
    return a, {c.id: c for c in a.checks}


@pytest.fixture
def passing_gates(tmp_path):
    (tmp_path / "docs" / "launch-readiness").mkdir(parents=True)
    (tmp_path / "docs" / "security-launch-gate.json").write_text(json.dumps({
        "verdict": "PASS", "date": "2026-10-03",
        "conditions": [{"id": "rls_attack_suite", "pass": True, "evidence": "ok"}]}))
    return tmp_path


# ── The honest default: nothing deployed ───────────────────────────────────

def test_no_app_is_blocked_with_a_plain_reason():
    w = World()
    w.apps = []
    a, c = audit(w)
    assert c["app_exists"].status == lr.BLOCKED
    assert "never been deployed" in c["app_exists"].reason
    # Requirements that depend on the app are not quietly passed.
    for dep in ("components", "worker_count", "core_secrets", "recovery_mode", "health", "worker_alive"):
        assert c[dep].status == lr.BLOCKED, dep
        assert "not verifiable" in c[dep].reason, dep
    assert lr.exit_code(a.checks) == 2


def test_missing_doctl_blocks_instead_of_guessing():
    w = World()
    a = lr.Audit("production", runner=lambda argv: (127, "", "doctl not installed"), http=w.http_get,
                 now=NOW, repo_root=REPO)
    c = {x.id: x for x in a.run_all()}
    assert c["doctl_access"].status == lr.BLOCKED
    assert "not verifiable" in c["postgres_attached"].reason


# ── A correct environment passes its hard requirements ─────────────────────

def test_a_correct_production_has_no_blockers(passing_gates):
    a, c = audit(World(), gates_root=passing_gates)
    blocked = {i: x.reason for i, x in c.items() if x.status == lr.BLOCKED}
    assert blocked == {}
    # What it cannot see is a WARN, not a PASS.
    assert c["stripe_live_mode"].status == lr.WARN
    assert c["operator_console"].status == lr.WARN
    assert lr.exit_code(a.checks) == 1


def test_a_correct_staging_has_no_blockers(passing_gates):
    a, c = audit(World("staging"), gates_root=passing_gates)
    assert {i for i, x in c.items() if x.status == lr.BLOCKED} == set()
    assert c["recovery_mode"].status == lr.PASS
    assert c["sizing"].status == lr.PASS  # the reduced staging render


# ── §6 blockers ────────────────────────────────────────────────────────────

def _set_env(spec, comp, key, value):
    for c in spec["services"] + spec["workers"]:
        if c["name"] == comp:
            c["envs"] = [e for e in c["envs"] if e["key"] != key]
            if value is not None:
                c["envs"].append({"key": key, "value": value})


def test_recovery_mode_in_production_blocks():
    w = World()
    _set_env(w.spec, "worker", "ORACLE_RECOVERY_MODE", "1")
    _, c = audit(w)
    assert c["recovery_mode"].status == lr.BLOCKED
    assert "worker" in c["recovery_mode"].reason


def test_staging_without_recovery_mode_blocks():
    w = World("staging")
    _set_env(w.spec, "api", "ORACLE_RECOVERY_MODE", None)
    _, c = audit(w)
    assert c["recovery_mode"].status == lr.BLOCKED
    assert "real people" in c["recovery_mode"].reason


def test_missing_core_secret_blocks_and_names_only_the_key():
    w = World()
    _set_env(w.spec, "api", "ORACLE_ENCRYPTION_MASTER_KEY", None)
    w.spec["services"][0]["envs"].append({"key": "ORACLE_ENCRYPTION_MASTER_KEY", "type": "SECRET"})
    _, c = audit(w)
    assert c["core_secrets"].status == lr.BLOCKED
    assert "api:ORACLE_ENCRYPTION_MASTER_KEY" in c["core_secrets"].reason


def test_operator_needs_a_second_factor():
    w = World()
    for k in lr.OPERATOR_2FA:
        _set_env(w.spec, "api", k, None)
    _, c = audit(w)
    assert c["core_secrets"].status == lr.BLOCKED
    assert "TOTP" in c["core_secrets"].reason


def test_two_workers_block():
    w = World()
    w.spec["workers"][0]["instance_count"] = 2
    _, c = audit(w)
    assert c["worker_count"].status == lr.BLOCKED


def test_worker_on_another_release_blocks():
    w = World()
    w.http["/health/workers"] = (200, {}, json.dumps({"healthy": True, "live_workers": 1,
                                                      "live_git_shas": ["d" * 40]}).encode())
    _, c = audit(w)
    assert c["worker_alive"].status == lr.BLOCKED


def test_dead_worker_blocks():
    w = World()
    w.http["/health/workers"] = (503, {}, b'{"healthy": false}')
    _, c = audit(w)
    assert c["worker_alive"].status == lr.BLOCKED


def test_wrong_release_against_the_manifest_blocks():
    w = World()
    _, c = audit(w, manifest={"git_sha": "e" * 40, "backend_digest": BD, "frontend_digest": FD})
    assert c["version"].status == lr.BLOCKED
    assert c["release_manifest"].status == lr.PASS


def test_tag_instead_of_digest_blocks():
    w = World()
    w.spec["services"][0]["image"] = {"registry_type": "DOCR", "repository": "neoh-backend", "tag": "latest"}
    _, c = audit(w)
    assert c["images_pinned"].status == lr.BLOCKED


def test_database_not_online_blocks():
    w = World()
    w.dbs[0]["status"] = "creating"
    _, c = audit(w)
    assert c["postgres_attached"].status == lr.BLOCKED
    assert "creating" in c["postgres_attached"].reason


def test_billing_missing_blocks_only_when_charging():
    w = World()
    _set_env(w.spec, "api", "STRIPE_PRICE_ID", None)
    _, c = audit(w)
    assert c["billing"].status == lr.BLOCKED
    _, c = audit(w, charging=False)
    assert c["billing"].status == lr.WARN


def test_security_gate_fail_blocks():
    """The real gate in the repo is FAIL (no staging DAST) and must say so."""
    _, c = audit(World())
    assert c["security_gate"].status == lr.BLOCKED
    assert "staging_dast_no_blocker" in c["security_gate"].reason


def test_open_owner_blocker_blocks_and_warning_warns():
    _, c = audit(World())
    assert c["gate:privacy_counsel_signoff"].status == lr.BLOCKED
    assert c["gate:privacy_elevenlabs_training"].status == lr.WARN


def test_stale_backup_blocks_production():
    w = World()
    w.backups = [{"created_at": "2026-10-01T00:00:00Z"}]
    _, c = audit(w)
    assert c["backups"].status == lr.BLOCKED


def test_a_brand_new_cluster_without_a_backup_only_warns():
    w = World()
    w.backups = []
    w.dbs[0]["created_at"] = "2026-10-03T19:00:00Z"
    _, c = audit(w)
    assert c["backups"].status == lr.WARN
    assert "pending" in c["backups"].reason


def test_localhost_cors_blocks_production():
    w = World()
    _set_env(w.spec, "api", "ORACLE_CORS_ORIGINS", "http://localhost:5173")
    _, c = audit(w)
    assert c["cors"].status == lr.BLOCKED


def test_demo_login_blocks_production():
    w = World()
    _set_env(w.spec, "api", "ORACLE_ENABLE_DEMO_LOGINS", "1")
    _, c = audit(w)
    assert c["demo_login_off"].status == lr.BLOCKED


def test_missing_github_environment_blocks():
    w = World()
    w.gh_env = None
    _, c = audit(w)
    assert c["github_environment"].status == lr.BLOCKED


# ── §6 warnings: optional things never block ───────────────────────────────

def test_optional_providers_and_3d_only_warn():
    _, c = audit(World())
    for i in ("optional_providers", "gpu_3d"):
        assert c[i].status == lr.WARN, i


def test_missing_mls_credential_only_warns():
    w = World()
    _set_env(w.spec, "api", "ORACLE_BRIDGE_ACCESS_TOKEN", None)
    _, c = audit(w)
    assert c["mls_credentials"].status == lr.WARN


def test_mls_endpoint_absent_degrades_to_not_checked(monkeypatch):
    w = World()
    _, c = audit(w, token="tok-" + SENTINEL)
    assert c["mls_state"].status == lr.WARN
    assert "not checked" in c["mls_state"].reason


# ── Never prints a secret; never changes anything ──────────────────────────

def test_no_secret_value_reaches_stdout_or_json(tmp_path, monkeypatch, capsys):
    w = World()
    # A SECRET given as a plain value (as an operator might paste it) and the
    # operator token must be scrubbed too, not just EV[...] blobs.
    _set_env(w.spec, "api", "ORACLE_FIREWORKS_API_KEY", None)
    w.spec["services"][0]["envs"].append({"key": "ORACLE_FIREWORKS_API_KEY", "type": "SECRET",
                                          "value": f"fw_{SENTINEL}"})
    monkeypatch.setenv("NEOH_OPERATOR_TOKEN", f"op-{SENTINEL}-token")
    out = tmp_path / "r.json"
    code = lr.main(["--env", "production", "--json", str(out)], runner=w.runner, http=w.http_get, now=NOW)
    printed = capsys.readouterr().out
    assert code == 2
    assert SENTINEL not in printed
    assert SENTINEL not in out.read_text()
    assert json.loads(out.read_text())["summary"]["verdict"] == "BLOCKED"


def test_redactor_scrubs_everything_doctl_hands_it_but_keeps_names():
    """Belt and braces: even if a reason ever interpolated a raw value, every
    password/URI/EV value doctl returned is scrubbed; env NAMES survive."""
    w = World()
    red = lr.Redactor()
    red.harvest(w.dbs)
    red.harvest(w.spec)
    text = (f"uri postgresql://doadmin:{SENTINEL}-pw@h:25060/x pw {SENTINEL}-pw "
            f"key EV[1:{SENTINEL}-ORACLE_SECRET_KEY] name ORACLE_SECRET_KEY")
    out = red.scrub(text)
    assert SENTINEL not in out
    assert "ORACLE_SECRET_KEY" in out


READ_ONLY = (("doctl", "account", "get"), ("doctl", "apps", "list"), ("doctl", "apps", "spec", "get"),
             ("doctl", "databases", "list"), ("doctl", "databases", "backups"),
             ("doctl", "databases", "firewalls", "list"), ("doctl", "registry", "get"),
             ("doctl", "registry", "repository", "list-v2"), ("gh", "api"), ("gh", "secret", "list"),
             ("gh", "variable", "list"))


def test_every_command_it_runs_is_read_only():
    w = World()
    audit(w, token="t")
    assert w.calls
    for argv in w.calls:
        assert any(tuple(argv[:len(p)]) == p for p in READ_ONLY), argv
        if argv[:2] == ["gh", "api"]:
            assert not any(x in argv for x in ("-X", "--method", "-f", "-F", "--input")), argv


def test_exit_codes():
    mk = lambda s: lr.Check("x", "a", "t", s, "r", True)  # noqa: E731
    assert lr.exit_code([mk(lr.PASS)]) == 0
    assert lr.exit_code([mk(lr.PASS), mk(lr.WARN)]) == 1
    assert lr.exit_code([mk(lr.WARN), mk(lr.BLOCKED)]) == 2


def test_base_url_must_be_https():
    with pytest.raises(SystemExit):
        lr.main(["--env", "staging", "--base-url", "http://x.example.com"],
                runner=World().runner, http=World().http_get, now=NOW)


def test_expectations_come_from_the_renderer():
    """Staging sizing is checked against render-app-spec.py, not a copy."""
    exp = lr.load_expected("staging")
    assert exp["sizes"]["api"] == (2, "apps-s-1vcpu-1gb")
    assert exp["clusters"] == {"neoh-postgres": "neoh-postgres-staging", "neoh-redis": "neoh-redis-staging"}
    assert copy.deepcopy(lr._FALLBACK_EXPECTED["staging"]["sizes"]["api"]) == exp["sizes"]["api"]
