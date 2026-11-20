"""The cross_version CI job is wired the way the contract needs (audit 35-06).

Static checks on .github/workflows/ci.yml, infra/digitalocean/app.yaml and the
harness. The job proves something only if:

* it runs on every pull request and every push to main, and gates nothing
  yet: it is in neither release.needs nor promote.needs (owner decision F7);
* the control is a separate PostgreSQL cluster (roles are cluster-wide);
* its candidate migrate step is the staging PRE_DEPLOY job: the same command
  and the same environment keys, read from app.yaml so the two cannot drift,
  with ORACLE_DB_APP_PASSWORD absent as it is there;
* the previous app boots fail-closed (ORACLE_ENV=staging) with outbound side
  effects blocked (ORACLE_RECOVERY_MODE=1) and no provider credential;
* its record is uploaded even when the job fails;
* the single live-test line tests/test_ci_lists_every_live_test.py guards is
  still the only one.
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
CI_TEXT = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
CI = yaml.safe_load(CI_TEXT)
APP = yaml.safe_load((REPO / "infra" / "digitalocean" / "app.yaml").read_text(encoding="utf-8"))
SCRIPT_TEXT = (REPO / "scripts" / "cross-version-contract.sh").read_text(encoding="utf-8")
SERVER_TEXT = (REPO / "backend" / "server.py").read_text(encoding="utf-8")
JOB = CI["jobs"]["cross_version"]
PINNED = re.compile(r"^[\w.-]+/[\w.-]+@[0-9a-f]{40}$")


def _boot_probe():
    path = REPO / "scripts" / "cross_version" / "boot_probe.py"
    spec = importlib.util.spec_from_file_location("xv_wiring_boot_probe", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _step(name_part: str) -> dict:
    found = [s for s in JOB["steps"] if name_part in (s.get("name") or "") or name_part in (s.get("uses") or "")]
    assert len(found) == 1, (name_part, found)
    return found[0]


def _uses(job: str, action: str) -> str:
    found = {s["uses"] for s in CI["jobs"][job]["steps"] if (s.get("uses") or "").startswith(action + "@")}
    assert len(found) == 1, (job, action, found)
    return found.pop()


def test_the_job_runs_on_every_pull_request_and_every_push_to_main():
    assert JOB["if"] == "github.event_name == 'pull_request' || github.ref == 'refs/heads/main'"
    assert JOB["runs-on"] == "ubuntu-latest"
    assert JOB["timeout-minutes"] == 25
    assert JOB["permissions"] == {"contents": "read"}


def test_the_job_gates_nothing_yet():
    for job in ("release", "promote"):
        assert CI["jobs"][job]["needs"] == ["backend", "frontend", "security", "rls"], job
    for name, job in CI["jobs"].items():
        needs = job.get("needs") or []
        assert "cross_version" not in ([needs] if isinstance(needs, str) else needs), name


def test_the_job_uses_no_secret_and_only_the_clusters_own_password():
    job_text = yaml.safe_dump(JOB)
    assert "secrets." not in job_text
    assert set(JOB["env"]) == {"XV_PG_SUPERUSER_PASSWORD"}
    assert all("env" not in step for step in JOB["steps"])


def test_candidate_and_control_are_two_postgres_16_clusters_with_the_rls_jobs_health_check():
    services = JOB["services"]
    assert set(services) == {"postgres", "pgcontrol"}
    rls = CI["jobs"]["rls"]["services"]["postgres"]
    for service in services.values():
        assert service["image"] == "postgres:16-alpine"
        assert service["options"] == rls["options"]
        assert service["env"]["POSTGRES_PASSWORD"] == JOB["env"]["XV_PG_SUPERUSER_PASSWORD"]
    assert services["postgres"]["ports"] == ["5432:5432"]
    assert services["pgcontrol"]["ports"] == ["5433:5432"]
    assert 'CANDIDATE_PORT="${XV_CANDIDATE_PORT:-5432}"' in SCRIPT_TEXT
    assert 'CONTROL_PORT="${XV_CONTROL_PORT:-5433}"' in SCRIPT_TEXT


def test_every_action_is_pinned_to_the_same_commit_as_the_rest_of_ci():
    for step in JOB["steps"]:
        if "uses" in step:
            assert PINNED.match(step["uses"]), step["uses"]
    assert _step("actions/checkout")["uses"] == _uses("rls", "actions/checkout")
    assert _step("actions/setup-python")["uses"] == _uses("rls", "actions/setup-python")
    assert _step("actions/upload-artifact")["uses"] == _uses("release", "actions/upload-artifact")


def test_checkout_has_the_previous_commit_and_python_matches_the_image():
    assert _step("actions/checkout")["with"]["fetch-depth"] == 2
    setup = _step("actions/setup-python")["with"]
    assert setup["python-version"] == "3.12"
    assert setup["cache"] == "pip"
    assert set(setup["cache-dependency-path"].split()) == {"backend/requirements.txt", "backend/requirements-test.txt"}


def test_identify_then_run_only_for_a_gap_or_the_canary():
    identify = _step("Identify")
    assert identify["id"] == "identify"
    assert identify["run"].strip() == "bash scripts/cross-version-contract.sh identify"
    run = _step("live suite on the new schema")
    assert run["if"] == "steps.identify.outputs.mode == 'gap' || steps.identify.outputs.mode == 'canary'"
    assert run["run"].strip() == "bash scripts/cross-version-contract.sh run"


def test_the_record_is_uploaded_even_when_the_job_fails():
    upload = _step("actions/upload-artifact")
    assert upload["if"] == "always()"
    assert upload["with"]["retention-days"] == 90
    assert upload["with"]["path"] == "${{ runner.temp }}/xv/out/"
    assert "${{ github.run_attempt }}" in upload["with"]["name"]
    assert upload["with"]["name"].startswith("cross-version-")


def _pre_deploy_block() -> str:
    found = re.search(r"# BEGIN PRE_DEPLOY ENV(.*?)# END PRE_DEPLOY ENV", SCRIPT_TEXT, re.S)
    assert found, "the PRE_DEPLOY block markers are gone from cross-version-contract.sh"
    return found.group(1)


def test_the_candidate_migrate_step_is_the_staging_pre_deploy_job():
    migrate = [j for j in APP["jobs"] if j["name"] == "migrate"]
    assert len(migrate) == 1 and migrate[0]["kind"] == "PRE_DEPLOY"
    migrate = migrate[0]
    app_keys = {e["key"] for e in migrate["envs"]}
    block = _pre_deploy_block()
    code = "\n".join(line for line in block.splitlines() if not line.lstrip().startswith("#"))
    script_keys = set(re.findall(r"(?:^|\s)([A-Z_][A-Z0-9_]*)=", code))

    assert migrate["run_command"] == "python run_migrations.py --precheck-then-migrate"
    assert re.search(r'"\$XV_NEW_PY" run_migrations\.py --precheck-then-migrate\)', code)
    # Nothing inherited: what is not listed is not set.
    assert "env -i" in code
    for key in ("ORACLE_EXPECTED_ENVIRONMENT", "ORACLE_DB_PLATFORM_PASSWORD", "ORACLE_DB_ADMIN_USER",
                "ORACLE_DB_ADMIN_PASSWORD"):
        assert key in app_keys and key in script_keys, key
    assert "ORACLE_DB_APP_PASSWORD" not in app_keys
    assert "ORACLE_DB_APP_PASSWORD" not in script_keys
    # Every key the real job sets, except its managed-cluster CA (TLS is off
    # against the local service), and nothing else beyond the process basics
    # and the migrations directory the image bakes in.
    assert app_keys - {"ORACLE_DB_CA_CERT"} <= script_keys, (app_keys - {"ORACLE_DB_CA_CERT"}) - script_keys
    assert script_keys - app_keys <= {"PATH", "HOME", "LANG", "ORACLE_MIGRATIONS_DIR"}, script_keys - app_keys
    # The stand-in for doadmin, and the environment stamp the precheck reads.
    assert "ORACLE_DB_ADMIN_USER=xv_doadmin" in code
    assert "ORACLE_EXPECTED_ENVIRONMENT=ci" in code
    assert "COMMENT ON DATABASE :\"db\" IS 'neoh-environment=ci';" in SCRIPT_TEXT
    assert "ALTER ROLE xv_doadmin LOGIN NOSUPERUSER BYPASSRLS CREATEROLE CREATEDB" in SCRIPT_TEXT


def test_the_previous_app_boots_fail_closed_with_side_effects_blocked(monkeypatch):
    for name in ("STRIPE_SECRET_KEY", "TWILIO_AUTH_TOKEN", "REDIS_URL", "ORACLE_FIREWORKS_API_KEY"):
        monkeypatch.setenv(name, "from-the-runner")
    env = _boot_probe().child_env(role="web", port=5432, db="xv_candidate",
                                  app_password="a" * 32, platform_password="p" * 32)
    assert env["ORACLE_ENV"] == "staging"
    assert env["ORACLE_RECOVERY_MODE"] == "1"
    assert env["ORACLE_SKIP_DOTENV"] == "1"
    assert env["ORACLE_ALERTS_ENABLED"] == "0"
    assert env["ORACLE_PROCESS_ROLE"] == "web"
    for key in ("ORACLE_SECRET_KEY", "ORACLE_ENCRYPTION_MASTER_KEY"):
        assert len(env[key]) >= 32
    leaked = [k for k in env if re.search(r"REDIS|STRIPE|TWILIO|PLIVO|TELNYX|FIREWORKS|ELEVENLABS|OPENAI|ANTHROPIC|"
                                          r"GEMINI|SMTP|AWS|AZURE|DIGITALOCEAN|ORACLE_REQUIRE_REDIS", k)]
    assert not leaked, leaked
    # The premise: outside dev a database failure aborts startup.
    assert 'raise RuntimeError("Production database initialization failed") from e' in SERVER_TEXT
    assert "if not config.IS_DEV:" in SERVER_TEXT
    assert "Production database initialization failed" in _boot_probe().CONTRACT_MARKERS


def test_the_ci_file_still_has_exactly_one_live_pytest_line():
    runs = [line for line in CI_TEXT.splitlines() if "python -m pytest" in line and "_live.py" in line]
    assert len(runs) == 1, runs
    assert "_live.py" not in yaml.safe_dump(JOB)
    # The previous tree's suite runs from the script, over its own glob.
    assert "targets=(tests/test_*_live.py)" in SCRIPT_TEXT


def _canary_sql() -> str:
    found = re.search(r'^CANARY_SQL="([^"]*)"', SCRIPT_TEXT, re.M)
    assert found, "CANARY_SQL is gone from cross-version-contract.sh"
    return found.group(1)


def test_the_canary_breaks_a_write_the_previous_worker_makes_at_startup():
    """The canary must reach the previous APP, not only the previous tests'
    own SQL (which is DRIFT). Every worker upserts process_heartbeats as its
    last startup step and logs the refusal; the probe's worker phase catches
    it. leads stays: the previous tests write it, which shows the suite side."""
    sql = _canary_sql()
    assert "ALTER TABLE process_heartbeats ADD CONSTRAINT xv_canary_hb CHECK (false) NOT VALID;" in sql
    assert "ALTER TABLE leads ADD CONSTRAINT xv_canary CHECK (false) NOT VALID;" in sql
    migrations = REPO / "backend" / "db" / "migrations"
    assert any("CREATE TABLE IF NOT EXISTS process_heartbeats" in p.read_text() for p in migrations.glob("*.sql"))
    # The classifier calls it ADDITIVE, which is the point (CLASSIFIER MISS).
    import migration_safety

    assert migration_safety.classify_sql("9999_xv_canary.sql", sql).classification == "additive"

    heartbeat = (REPO / "backend" / "process_heartbeat.py").read_text(encoding="utf-8")
    assert "INSERT INTO process_heartbeats" in heartbeat
    assert 'log.warning("heartbeat not written (%s); will retry in %ss", exc, INTERVAL)' in heartbeat
    assert "recovery" not in heartbeat.lower()              # ORACLE_RECOVERY_MODE does not stop it
    lifespan = SERVER_TEXT[SERVER_TEXT.index("async def lifespan"):SERVER_TEXT.index("yield", SERVER_TEXT.index("async def lifespan"))]
    block = lifespan[lifespan.rindex("if config.RUNS_BACKGROUND_WORK:"):]
    assert "await start_heartbeat()" in block
    config_text = (REPO / "backend" / "config.py").read_text(encoding="utf-8")
    assert 'RUNS_BACKGROUND_WORK: bool = PROCESS_ROLE != "web"' in config_text
    env = _boot_probe().child_env(role="worker", port=5432, db="x", app_password="a" * 32, platform_password="p" * 32)
    assert env["ORACLE_PROCESS_ROLE"] == "worker"


def test_application_warnings_reach_the_boot_log():
    """uvicorn configures only its own loggers; server.py configures the root
    at import (INFO by default), and the probe sends stderr into the log."""
    assert "\n_configure_logging()\n" in SERVER_TEXT
    assert 'level = os.getenv("ORACLE_LOG_LEVEL", "INFO").upper()' in SERVER_TEXT
    assert "logging.basicConfig(level=level," in SERVER_TEXT
    probe = _boot_probe()
    env = probe.child_env(role="worker", port=5432, db="x", app_password="a" * 32, platform_password="p" * 32)
    assert "ORACLE_LOG_LEVEL" not in env
    source = (REPO / "scripts" / "cross_version" / "boot_probe.py").read_text(encoding="utf-8")
    assert "stdout=log, stderr=subprocess.STDOUT" in source
