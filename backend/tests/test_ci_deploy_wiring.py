"""The deploy jobs in ci.yml are wired so that a release can actually land.

Two defects found before CI ever deployed anything:

* DIGITALOCEAN_APP_ID, NEOH_PUBLIC_API_BASE and the database variables were set
  only on the "Required secrets are present" step. Every later step expanded
  them EMPTY: `doctl apps get ""`, migrations with no host, a smoke test with no
  URL.
* `doctl apps update` applied the RENDERED spec, whose SECRET values are blank
  by design, and that wipes every secret on the app (proven on staging). It
  must apply the spec carry-secrets.py produced, and the carry must run before
  migrations, so a missing secret stops the release before the database changes.
"""

from __future__ import annotations

import pathlib
import re

import pytest
import yaml

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
CI = yaml.safe_load((REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
DEPLOY_JOBS = ("release", "promote")


def _steps(job):
    return CI["jobs"][job]["steps"]


def _available(job, step) -> set[str]:
    names = set((CI["jobs"][job].get("env") or {}).keys()) | set((step.get("env") or {}).keys())
    run = step.get("run") or ""
    names |= set(re.findall(r"^\s*(?:export\s+)?([A-Z_][A-Z0-9_]*)=", run, re.M))
    names |= set(re.findall(r"^\s*export\s+([A-Z_][A-Z0-9_]*)\s*$", run, re.M))
    return names


SECRET_VARS = {"DIGITALOCEAN_APP_ID", "DIGITALOCEAN_REGISTRY", "NEOH_PUBLIC_API_BASE", "ORACLE_DB_HOST", "ORACLE_DB_PORT",
               "ORACLE_DB_NAME", "ORACLE_DB_ADMIN_USER", "ORACLE_DB_ADMIN_PASSWORD",
               "ORACLE_DB_PLATFORM_PASSWORD", "ORACLE_DB_CA_CERT"}


@pytest.mark.parametrize("job", DEPLOY_JOBS)
def test_every_variable_a_step_reads_is_given_to_that_step(job):
    for step in _steps(job):
        run = step.get("run") or ""
        used = set(re.findall(r"\$\{?([A-Z_][A-Z0-9_]*)\}?", run)) | set(re.findall(r"-e ([A-Z_][A-Z0-9_]*)\b(?!=)", run))
        missing = (used & SECRET_VARS) - _available(job, step)
        assert not missing, f"{job} / {step.get('name')!r} reads {sorted(missing)} but is never given them"


@pytest.mark.parametrize("job", DEPLOY_JOBS)
def test_the_app_is_updated_only_with_the_carried_spec(job):
    updates = [s for s in _steps(job) if "doctl apps update" in (s.get("run") or "")]
    assert updates
    for s in updates:
        spec = re.search(r"--spec\s+(\S+)", s["run"]).group(1)
        assert spec.endswith(".deploy.yaml"), f"{job}: {s['name']!r} applies {spec}, which wipes secrets"


@pytest.mark.parametrize("job", DEPLOY_JOBS)
def test_secrets_are_carried_before_migrations_and_from_the_active_deployment(job):
    names = [s.get("name", "") for s in _steps(job)]
    carry = next(i for i, n in enumerate(names) if n.startswith("Carry the app's secrets"))
    update = next(i for i, s in enumerate(_steps(job)) if "doctl apps update" in (s.get("run") or ""))
    assert carry < update
    # Migrations run INSIDE DigitalOcean (the spec's PRE_DEPLOY job): the
    # databases accept the app only, so no runner step may reach them.
    for step in _steps(job):
        text = (step.get("name") or "") + (step.get("run") or "")
        assert "run_migrations.py" not in text and "migration-precheck.sh" not in text, step.get("name")
    run = _steps(job)[carry]["run"]
    assert "list-deployments" in run and "apps spec get" not in run


@pytest.mark.parametrize("job", DEPLOY_JOBS)
def test_injected_secrets_are_given_to_the_carry_step_and_allowed_for_its_environment(job):
    """Every key the carry step injects must be mapped from a GitHub secret
    onto THAT step, and be injectable for the job's environment — the demo
    recipient allowlist only ever reaches staging."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("carry_secrets_w", REPO / "scripts" / "carry-secrets.py")
    cs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cs)
    env_name = "production" if job == "promote" else "staging"
    step = next(s for s in _steps(job) if s.get("name", "").startswith("Carry the app's secrets"))
    m = re.search(r"--inject-from-env\s+(\S+)", step["run"])
    assert m, f"{job}: the carry step injects nothing"
    keys = m.group(1).split(",")
    cs.check_injectable(keys, env_name)  # raises on a key not allowed there
    for key in keys:
        assert (step.get("env") or {}).get(key) == "${{ secrets.%s }}" % key, (job, key)
    if job == "promote":
        assert "ORACLE_DEMO_RECIPIENT_ALLOWLIST" not in step["run"] + str(step.get("env"))


def test_no_job_has_a_duplicate_key():
    """YAML keeps the LAST duplicate key silently; a second `env:` on a job
    dropped the first one's variables."""
    class Strict(yaml.SafeLoader):
        pass

    def no_dupes(loader, node, deep=False):
        keys = [loader.construct_object(k, deep=deep) for k, _ in node.value]
        dupes = {k for k in keys if keys.count(k) > 1}
        assert not dupes, f"duplicate key(s) {dupes} at line {node.start_mark.line + 1}"
        return yaml.SafeLoader.construct_mapping(loader, node, deep)

    Strict.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, no_dupes)
    yaml.load((REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"), Loader=Strict)


def test_the_carried_spec_is_never_uploaded():
    for job in DEPLOY_JOBS:
        for s in _steps(job):
            if "upload-artifact" in str(s.get("uses", "")):
                assert ".deploy.yaml" not in str((s.get("with") or {}).get("path", "")), s.get("name")


@pytest.mark.parametrize("job", DEPLOY_JOBS)
def test_validation_runs_on_the_rendered_spec_not_the_carried_one(job):
    """App Platform's validate endpoint refuses encrypted EV[...] values
    ("secret env value must not be encrypted"), which failed the first real
    staging release. Validate the rendered spec; only `apps update` gets the
    carried one."""
    validates = [s for s in _steps(job) if "doctl apps spec validate" in (s.get("run") or "")]
    assert validates
    for s in validates:
        target = re.search(r"spec validate\s+(\S+)", s["run"]).group(1)
        assert not target.endswith(".deploy.yaml"), f"{job}: validates the carried spec {target}"


@pytest.mark.parametrize("job", DEPLOY_JOBS)
def test_the_migration_job_gets_its_credentials_from_the_carry_step(job):
    step = next(s for s in _steps(job) if s.get("name", "").startswith("Carry the app's secrets"))
    for key in ("ORACLE_DB_ADMIN_PASSWORD", "ORACLE_DB_PLATFORM_PASSWORD"):
        assert key in (step.get("env") or {}), key
        assert key in re.search(r"--inject-from-env\s+(\S+)", step["run"]).group(1).split(",")


def test_the_spec_migrates_in_a_pre_deploy_job_on_the_backend_image():
    spec = yaml.safe_load((REPO / "infra" / "digitalocean" / "app.yaml").read_text())
    jobs = [j for j in spec.get("jobs") or [] if j.get("kind") == "PRE_DEPLOY"]
    assert len(jobs) == 1
    job = jobs[0]
    assert job["image"]["repository"] == "neoh-backend"
    assert job["image"]["digest"] == "__BACKEND_DIGEST__"
    assert "--precheck-then-migrate" in job["run_command"]
