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
REDIS_DRILL = (REPO / "scripts" / "dr-drill-redis.sh").read_text(encoding="utf-8")


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


def _published_ports(script: str) -> list[str]:
    """Every publish option on every `docker run` in a shell script, as written.

    Reads the command's tokens rather than matching one spelling, so
    `-p "$PORT:5432"`, `--publish 55432:5432`, `--publish=...` and `-P` are all
    seen. Line continuations are joined first; `-p` belonging to some other
    command on the line (mkdir -p) is ignored because only tokens after
    `docker run` count, up to the next shell separator."""
    import shlex

    found: list[str] = []
    for line in script.replace("\\\n", " ").splitlines():
        if "docker run" not in line:
            continue
        lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        lexer.commenters = "#"
        tokens = list(lexer)
        in_run = False
        for i, tok in enumerate(tokens):
            if tok == "run" and i and tokens[i - 1] == "docker":
                in_run = True
            elif tok in ("||", "&&", ";", "|", "{", "}", "(", ")"):
                in_run = False
            elif in_run and tok in ("-p", "--publish") and i + 1 < len(tokens):
                found.append(tokens[i + 1])
            elif in_run and tok.startswith(("-p=", "--publish=")):
                found.append(tok.split("=", 1)[1])
            elif in_run and tok in ("-P", "--publish-all"):
                found.append(tok)
    return found


def test_the_publish_reader_sees_every_spelling():
    """The gate is only as good as its reader: each of these would bring back a
    fixed or all-interface host port, and each must be visible to it."""
    for line, value in [
        ('docker run -d -p "$PORT:5432" postgres:16-alpine', "$PORT:5432"),
        ("docker run -d --publish 55432:5432 postgres:16-alpine", "55432:5432"),
        ("docker run -d --publish=0.0.0.0:6379:6379 valkey/valkey:8-alpine", "0.0.0.0:6379:6379"),
        ("docker run -d -P valkey/valkey:8-alpine", "-P"),
        ('docker run -d --name x \\\n  -p "56379:6379" redis:7-alpine', "56379:6379"),
    ]:
        assert _published_ports(line) == [value], line
    assert _published_ports('mkdir -p "$DIR" && docker run -d -p "127.0.0.1::5432" pg') == ["127.0.0.1::5432"]
    assert _published_ports("docker run --rm img sh -c 'x' || mkdir -p /tmp/x") == []


def test_the_drill_publishes_no_fixed_host_port():
    """A fixed port (55432) sits in Linux's ephemeral range, where an outbound
    connection on the runner can already hold it: CI run 44803825496 failed
    "address already in use". The drill reaches its clusters by docker exec,
    so Docker picks a free loopback port, and a cluster that cannot start
    ends the drill instead of producing follow-on FAILs."""
    assert _published_ports(DRILL) == ["127.0.0.1::5432", "127.0.0.1::5432"]
    assert DRILL.count('|| { bad "could not start the') == 2
    assert DRILL.count('never accepted connections"; exit 1; }') == 2


def test_the_redis_drill_publishes_on_loopback_and_asks_docker_for_the_port():
    """The Redis drill talks to Valkey from the host, so it does need a host
    port: a loopback one Docker picks (not 56379, not every interface), read
    back with `docker port`, and a container with no port ends the drill."""
    assert _published_ports(REDIS_DRILL) == ["127.0.0.1::6379", "127.0.0.1::6379"]
    assert 'PORT="$(docker port "$CONTAINER" 6379/tcp' in REDIS_DRILL
    assert "PORT=56379" not in REDIS_DRILL
    assert 'published no port for it"; exit 1; }' in REDIS_DRILL
    assert REDIS_DRILL.index("docker port") < REDIS_DRILL.index('export REDIS_URL="redis://127.0.0.1:$PORT/0"')
