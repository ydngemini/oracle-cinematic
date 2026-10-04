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
    migrate = next(i for i, n in enumerate(names) if n.startswith("Run database migrations"))
    update = next(i for i, s in enumerate(_steps(job)) if "doctl apps update" in (s.get("run") or ""))
    assert carry < migrate < update
    run = _steps(job)[carry]["run"]
    assert "list-deployments" in run and "apps spec get" not in run


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
