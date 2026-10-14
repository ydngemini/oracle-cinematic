"""CI gates for dependency vulnerabilities and disaster recovery (L17-16, L17-18).

ISO 27001 A.8.8: the backend already failed the build on a known CVE
(`pip_audit --strict`), but the frontend installed with `npm ci --no-audit`
and nothing raised update PRs. A.8.13: the DR drill had only ever been run by
hand, although its own report recommends "every merge to main".
"""

from __future__ import annotations

import pathlib

import yaml

REPO = pathlib.Path(__file__).resolve().parents[2]
CI = yaml.safe_load((REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
DEPENDABOT = yaml.safe_load((REPO / ".github" / "dependabot.yml").read_text(encoding="utf-8"))
DRILL = (REPO / "scripts" / "dr-drill.sh").read_text(encoding="utf-8")


def _runs(job: str) -> list[str]:
    return [step.get("run", "") for step in CI["jobs"][job]["steps"]]


def test_the_frontend_job_fails_on_a_high_runtime_advisory():
    runs = _runs("frontend")
    install = next(i for i, run in enumerate(runs) if "npm ci" in run)
    audit = next(i for i, run in enumerate(runs) if "npm audit" in run)

    assert audit > install
    assert "--audit-level=high" in runs[audit] and "--omit=dev" in runs[audit]
    # A failing audit must fail the job: no `|| true`, no continue-on-error.
    assert "||" not in runs[audit]
    assert not CI["jobs"]["frontend"]["steps"][audit].get("continue-on-error")


def test_the_backend_job_still_fails_on_a_known_cve():
    assert any("pip_audit --strict" in run and "requirements.txt" in run for run in _runs("backend"))


def test_dependabot_covers_everything_that_ships():
    covered = {(u["package-ecosystem"], u["directory"]) for u in DEPENDABOT["updates"]}

    assert {("pip", "/backend"), ("npm", "/oracle-app"), ("github-actions", "/"),
            ("docker", "/backend"), ("docker", "/oracle-app")} <= covered
    for update in DEPENDABOT["updates"]:
        assert update["schedule"]["interval"] == "weekly"
        assert 1 <= update["open-pull-requests-limit"] <= 3


def test_the_dr_drill_runs_in_ci_against_the_migrated_schema():
    job = CI["jobs"]["rls"]
    runs = _runs("rls")
    migrate = next(i for i, run in enumerate(runs) if "run_migrations.py" in run)
    drill = next(i for i, run in enumerate(runs) if "scripts/dr-drill.sh" in run)

    assert drill > migrate
    # The drill refuses to run without these three exports (dr-drill.sh step 2).
    for export in ("schema.sql", "ledger.sql", "roles.sql"):
        assert export in runs[drill]
    # Passwords never leave the cluster, even into a throwaway drill.
    assert "--no-role-passwords" in runs[drill]
    assert not job["steps"][drill].get("continue-on-error")


def test_the_drill_waits_for_the_real_server_not_the_init_server():
    """A socket-only pg_isready answers during initdb's temporary server, and
    the next query lands in the restart gap (step 6 read an empty target as
    "already has  tables")."""
    assert 'pg_isready -h 127.0.0.1' in DRILL
    assert DRILL.count("wait_ready \"$") == 2
    assert 'pg_isready -U postgres -q' not in DRILL
