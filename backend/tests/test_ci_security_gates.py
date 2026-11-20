"""CI gates for dependency vulnerabilities and disaster recovery (L17-16, L17-18).

ISO 27001 A.8.8: the backend already failed the build on a known CVE
(`pip_audit --strict`), but the frontend installed with `npm ci --no-audit`
and nothing raised update PRs. A.8.13: the DR drill had only ever been run by
hand, although its own report recommends "every merge to main".
"""

from __future__ import annotations

import pathlib

import pytest
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


# --- what the drills ask Docker to publish ------------------------------------
#
# Two checks. A static reader goes over every `docker run|create` in both
# drills; it is a tripwire for the spellings in its probe, not a shell parser,
# so a flag held in a variable, an array or `eval` is invisible to it. For the
# Redis drill, which needs a host port, the script itself is also run against a
# stub `docker` and the argument lists Docker would actually receive are read.

_SEPARATORS = {"||", "&&", ";", ";;", "|", "|&", "&", "{", "}", "(", ")"}
_DOCKER_GLOBAL_VALUE_OPTS = {"-H", "--host", "-c", "--context", "--config",
                             "-l", "--log-level", "--tlscacert", "--tlscert", "--tlskey"}
# `docker run`'s long options, from Docker 28.2.2 `docker run --help`: the 14
# that take no value (--publish-all is read separately), and the 85 that take
# one plus the hidden aliases --net and --net-alias. A listed value option
# always takes the next word, as Docker's parser does (`--entrypoint -x`,
# `--pids-limit -1`). An option in neither list takes the next word unless that
# word is a flag (a dash and a letter), so a future boolean cannot hide the
# option after it.
_RUN_BOOLEAN_LONG = {"--detach", "--disable-content-trust", "--help", "--init",
                     "--interactive", "--no-healthcheck", "--oom-kill-disable",
                     "--privileged", "--quiet", "--read-only", "--rm", "--sig-proxy",
                     "--tty", "--use-api-socket"}
_RUN_VALUE_LONG = {
    "--add-host", "--annotation", "--attach", "--blkio-weight", "--blkio-weight-device",
    "--cap-add", "--cap-drop", "--cgroup-parent", "--cgroupns", "--cidfile",
    "--cpu-period", "--cpu-quota", "--cpu-rt-period", "--cpu-rt-runtime", "--cpu-shares",
    "--cpus", "--cpuset-cpus", "--cpuset-mems", "--detach-keys", "--device",
    "--device-cgroup-rule", "--device-read-bps", "--device-read-iops",
    "--device-write-bps", "--device-write-iops", "--dns", "--dns-option", "--dns-search",
    "--domainname", "--entrypoint", "--env", "--env-file", "--expose", "--gpus",
    "--group-add", "--health-cmd", "--health-interval", "--health-retries",
    "--health-start-interval", "--health-start-period", "--health-timeout", "--hostname",
    "--ip", "--ip6", "--ipc", "--isolation", "--kernel-memory", "--label", "--label-file",
    "--link", "--link-local-ip", "--log-driver", "--log-opt", "--mac-address", "--memory",
    "--memory-reservation", "--memory-swap", "--memory-swappiness", "--mount", "--name",
    "--network", "--network-alias", "--oom-score-adj", "--pid", "--pids-limit",
    "--platform", "--publish", "--pull", "--restart", "--runtime", "--security-opt",
    "--shm-size", "--stop-signal", "--stop-timeout", "--storage-opt", "--sysctl",
    "--tmpfs", "--ulimit", "--user", "--userns", "--uts", "--volume", "--volume-driver",
    "--volumes-from", "--workdir",
    "--net", "--net-alias",
}
# Short flags that take no value: -d, -i, -t, -q (and -P, read as a publish).
_RUN_BOOLEAN_SHORT = "ditq"
_FLAG_WORD = __import__("re").compile(r"--?[A-Za-z]")


def _publish_options(args: list[str]) -> list[str]:
    """Publish and host-network options in one `docker run` argument list (the
    words after `run`), read the way Docker's flag parser reads them, up to the
    image name — after it, words belong to the container's own command."""
    found: list[str] = []
    i = 0
    while i < len(args):
        tok = args[i]
        nxt = args[i + 1] if i + 1 < len(args) else ""
        if tok == "--":
            break
        if tok.startswith("--"):
            name, eq, value = tok.partition("=")
            takes_next = not eq and (
                name in _RUN_VALUE_LONG
                or (name not in _RUN_BOOLEAN_LONG and name != "--publish-all"
                    and not _FLAG_WORD.match(nxt)))
            if takes_next:
                value = nxt
            if name == "--publish":
                found.append(value)
            elif name == "--publish-all" and value.lower() != "false":
                found.append("--publish-all")
            elif name in ("--network", "--net") and value == "host":
                found.append("--network=host")
            i += 2 if takes_next else 1
            continue
        if tok.startswith("-") and len(tok) > 1:
            # A cluster of short flags: -d, -i, -t, -q and -P take no value;
            # the first other letter takes the rest of the word, or the next word.
            took_next = False
            for j, ch in enumerate(tok[1:], start=1):
                if ch == "P":
                    found.append("-P")
                    continue
                if ch in _RUN_BOOLEAN_SHORT:
                    continue
                rest = tok[j + 1:]
                took_next = not rest
                if ch == "p":
                    found.append(rest.removeprefix("=") if rest else nxt)
                break
            i += 2 if took_next else 1
            continue
        if tok.startswith(("{", "<(", ">(")):
            # A named-fd redirection or a process substitution where the image
            # should be: this reader cannot place what follows, so it says so.
            raise AssertionError(f"cannot read this docker run past {tok!r}")
        break
    return found


def _strip_comment(line: str) -> str:
    """Drop a shell comment: a `#` that starts a word outside quotes."""
    quote = None
    i = 0
    while i < len(line):
        ch = line[i]
        if ch == "\\" and quote != "'":
            i += 2
            continue
        if quote:
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1] in " \t;&|()"):
            return line[:i]
        i += 1
    return line


def _docker_run_args(script: str) -> list[list[str]]:
    """The argument list of every `docker [global options] [container]
    run|create` in a shell script, continuations joined, comments dropped.

    Each command is read from where `docker` starts and only as far as needed:
    a `docker exec ... -c "` that opens a multi-line SQL string is recognised
    as exec and left alone, and so is `docker` inside a quoted message. A
    run|create whose words cannot be read fails the test rather than being
    skipped. Redirections (`2>/dev/null`, `>&2`, `&>log`) are dropped wherever
    they stand: bash allows them before the options too."""
    import re
    import shlex

    runs: list[list[str]] = []
    for line in script.replace("\\\n", " ").splitlines():
        line = _strip_comment(line)
        for match in re.finditer(r"(?<![\w/.$-])(?:[\w./-]*/)?docker(?![\w.-])", line):
            start = match.start()
            # A quoted command name (`"docker" run`) is still a command: start
            # at the opening quote. `eval "docker run ..."` has no closing
            # quote right after the name, so it is still read from `docker`
            # and still fails below.
            if (start and line[start - 1] in "'\""
                    and line[match.end():match.end() + 1] == line[start - 1]):
                start -= 1
            # An fd number written onto its redirection (`2>`) is part of it.
            text = re.sub(r"(?<=\s)\d+(?=[<>])", "", line[start:])
            lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
            lexer.whitespace_split = True
            lexer.commenters = ""
            words = iter(lexer)
            try:
                next(words)                                   # docker
                word = next(words, "")
                while word.startswith("-"):
                    if word in _DOCKER_GLOBAL_VALUE_OPTS:
                        next(words, "")
                    word = next(words, "")
                if word == "container":
                    word = next(words, "")
            except ValueError:
                continue                  # not a command (a quoted message)
            if word not in ("run", "create"):
                continue
            args = []
            try:
                for word in words:
                    if word in ("(", "((") and args and args[-1].endswith("$"):
                        # An unquoted $(...) / $((...)): its `(` would read as
                        # a separator and hide every option after it.
                        raise ValueError("command substitution")
                    if word.count("`") % 2:
                        raise ValueError("backtick substitution")
                    if word in _SEPARATORS:
                        break
                    if word in ("<(", ">("):
                        # A process substitution: its `)` would read as the
                        # end of the command and hide every option after it.
                        raise ValueError("process substitution")
                    if set(word) <= set("<>&|") and set(word) & set("<>"):
                        next(words, "")                       # and its target
                        continue
                    args.append(word)
            except ValueError as exc:
                raise AssertionError(f"cannot read this docker command: {line.strip()!r}") from exc
            runs.append(args)
    return runs


def _published_ports(script: str) -> list[str]:
    return [opt for args in _docker_run_args(script) for opt in _publish_options(args)]


def test_the_publish_reader_sees_the_spellings_in_its_probe():
    """Each of these would bring back a fixed, all-interface or host-network
    port, and each must be visible to the reader."""
    for line, expected in [
        ('docker run -d -p "$PORT:5432" postgres:16-alpine', ["$PORT:5432"]),
        ("docker run -d --publish 55432:5432 postgres:16-alpine", ["55432:5432"]),
        ("docker run -d --publish=0.0.0.0:6379:6379 valkey/valkey:8-alpine", ["0.0.0.0:6379:6379"]),
        ("docker run -dp 0.0.0.0:6379:6379 img", ["0.0.0.0:6379:6379"]),
        ("docker run -d -p56379:6379 img", ["56379:6379"]),
        ('docker run -d -p"56379:6379" img', ["56379:6379"]),
        ("docker run -d -P img", ["-P"]),
        ("docker run -dP img", ["-P"]),
        ("docker run -d --publish-all img", ["--publish-all"]),
        ("docker run -d --publish-all=true img", ["--publish-all"]),
        ("docker run -d --network host img", ["--network=host"]),
        ("docker run -d --net=host img", ["--network=host"]),
        ('docker run -d --name x \\\n  -p "56379:6379" redis:7-alpine', ["56379:6379"]),
        ("docker \\\n  run -p 1:2 img", ["1:2"]),
        ("docker  run -p 1:2 img", ["1:2"]),
        ("docker container run -p 1:2 img", ["1:2"]),
        ("docker create -p 1:2 img", ["1:2"]),
        ("docker -H unix:///x.sock run -p 1:2 img", ["1:2"]),
        ("/usr/bin/docker run -p 1:2 img", ["1:2"]),
        ("docker run -d -e X=a#b -p 56379:6379 img", ["56379:6379"]),
        ("docker run -d -e 'A=1' --name n -v /a:/b -p 1:2 img", ["1:2"]),
        # Boolean flags must not swallow the option after them (review r2).
        ('docker run -d --name x -p "127.0.0.1::6379" -q -p 0.0.0.0:6379:6379 img',
         ["127.0.0.1::6379", "0.0.0.0:6379:6379"]),
        ("docker run -d --use-api-socket -p 0.0.0.0:6379:6379 img", ["0.0.0.0:6379:6379"]),
        ("docker run -dq --network host img", ["--network=host"]),
        ("docker run -d --some-future-flag -p 1:2 img", ["1:2"]),
        # A redirection before the options does not end them.
        ("docker run 2>/dev/null -d -p 0.0.0.0:6379:6379 img", ["0.0.0.0:6379:6379"]),
        ("docker run >/dev/null -p 1:2 img", ["1:2"]),
        ("docker run &>/tmp/log -p 1:2 img 2>&1", ["1:2"]),
        # ${#...} is a length, not a comment.
        ("docker run -e N=${#ARR[@]} -p 0.0.0.0:6379:6379 img", ["0.0.0.0:6379:6379"]),
        # A negative value ("unlimited") is a value, not a flag (review r3).
        ("docker run -d --pids-limit -1 -p 0.0.0.0:6379:6379 img", ["0.0.0.0:6379:6379"]),
        ("docker run -d --memory-swap -1 --network host img", ["--network=host"]),
        ("docker run -d --oom-score-adj -500 -p 1:2 img", ["1:2"]),
        # A known value option keeps a dash-letter value, as Docker's parser does (r4).
        ("docker run -d --entrypoint -x -p 0.0.0.0:6379:6379 img", ["0.0.0.0:6379:6379"]),
        ("docker run -d --hostname -h --network host img", ["--network=host"]),
        # A quoted substitution is one word and reads fine.
        ('docker run -d -e "T=$(date +%s)" -p 1:2 img', ["1:2"]),
        # A quoted command name is still the command.
        ('"docker" run -d -p 0.0.0.0:6379:6379 img', ["0.0.0.0:6379:6379"]),
        ("'docker' run -d -p 1:2 img", ["1:2"]),
        ('"/usr/bin/docker" run -p 1:2 img', ["1:2"]),
    ]:
        assert _published_ports(line) == expected, line


def test_the_publish_reader_reads_only_dockers_own_options():
    """Not another command's -p on the line, not the container's command after
    the image, not a comment."""
    assert _published_ports('mkdir -p "$DIR" && docker run -d -p "127.0.0.1::5432" pg') == ["127.0.0.1::5432"]
    assert _published_ports("docker run --rm img sh -c 'x' || mkdir -p /tmp/x") == []
    assert _published_ports("docker run -d -p 127.0.0.1::5432 img & mkdir -p /tmp/x") == ["127.0.0.1::5432"]
    assert _published_ports("docker run --rm -p 127.0.0.1::5432 postgres:16-alpine psql -h db -p 5432") == ["127.0.0.1::5432"]
    assert _published_ports("docker run -d img  # -p 1:2 was the old way") == []
    assert _published_ports("docker run -d --publish-all=false img") == []
    assert _published_ports("docker port c 6379/tcp; docker exec c redis-cli -p 6379 ping") == []
    # `docker` in a quoted message is not a command and must not fail the test.
    assert _published_ports('echo "  could not start docker"') == []
    assert _published_ports('echo "is docker running?"; docker run -p 127.0.0.1::1 img') == ["127.0.0.1::1"]
    # A run whose words cannot be read still fails loudly.
    for unreadable in ('eval "docker run -d -p 56379:6379 img"',
                       "docker run {fd}>/tmp/x -p 1:2 img",
                       "docker run --rm <(env) -p 1:2 img",
                       "docker run --env-file <(env) -p 1:2 img",
                       "docker run -d -e T=$(date +%s) -p 0.0.0.0:6379:6379 img",
                       "docker run -d -e N=$((1+2)) -p 1:2 img",
                       "docker run -d -e T=`date +%s` -p 1:2 img",
                       '"docker" run -d -e T=$(date +%s) -p 0.0.0.0:6379:6379 img'):
        try:
            _published_ports(unreadable)
        except AssertionError:
            pass
        else:
            pytest.fail(f"an unreadable docker run was skipped: {unreadable}")


def test_the_drill_publishes_no_fixed_host_port():
    """A fixed port (55432) sits in Linux's ephemeral range, where an outbound
    connection on the runner can already hold it: CI run 44803825496 failed
    "address already in use". The drill reaches its clusters by docker exec,
    so Docker picks a free loopback port, and a cluster that cannot start
    ends the drill instead of producing follow-on FAILs."""
    assert _published_ports(DRILL) == ["127.0.0.1::5432", "127.0.0.1::5432"]
    assert DRILL.count('|| { bad "could not start the') == 2
    assert DRILL.count('never accepted connections"; exit 1; }') == 2


def test_the_redis_drill_publishes_on_loopback_as_written():
    assert _published_ports(REDIS_DRILL) == ["127.0.0.1::6379", "127.0.0.1::6379"]
    assert "PORT=56379" not in REDIS_DRILL and "56379:6379" not in REDIS_DRILL


# --- the Redis drill, run against a stub docker -------------------------------

_STUB_DOCKER = r"""#!/usr/bin/env bash
{ for a in "$@"; do printf '%s\037' "$a"; done; printf '\n'; } >> "$STUB_LOG"
case "$1" in
  run)  [ -n "$STUB_RUN_OK" ] || exit 1; echo 0123456789ab ;;
  port) [ -n "$STUB_PORT" ] && printf '%s\n' "$STUB_PORT" ;;
  exec) echo PONG ;;
esac
exit 0
"""
# Stands in for the venv python: records the URL the drill hands its checks.
_STUB_PYTHON = """#!/usr/bin/env bash
cat >/dev/null
echo "REDIS_URL=$REDIS_URL" >> "$STUB_LOG.python"
exit 0
"""


def _run_redis_drill(tmp_path, *, run_ok: bool, port: str = ""):
    """Run the real script with `docker` and the venv python replaced, and
    return (exit code, docker argument lists, python's REDIS_URL lines)."""
    import os
    import shutil
    import subprocess

    (tmp_path / "scripts").mkdir()
    (tmp_path / "backend").mkdir()
    (tmp_path / "venv" / "bin").mkdir(parents=True)
    (tmp_path / "bin").mkdir()
    shutil.copy(REPO / "scripts" / "dr-drill-redis.sh", tmp_path / "scripts")
    for path, body in ((tmp_path / "bin" / "docker", _STUB_DOCKER),
                       (tmp_path / "venv" / "bin" / "python", _STUB_PYTHON)):
        path.write_text(body)
        path.chmod(0o755)
    log = tmp_path / "docker.log"
    env = {**os.environ, "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
           "STUB_LOG": str(log), "STUB_RUN_OK": "1" if run_ok else "", "STUB_PORT": port}
    env.pop("DOCKER_HOST", None)
    proc = subprocess.run(["bash", str(tmp_path / "scripts" / "dr-drill-redis.sh")],
                          env=env, capture_output=True, text=True, timeout=60)
    calls = [line.split("\x1f")[:-1] for line in log.read_text().splitlines()] if log.exists() else []
    py_log = tmp_path / "docker.log.python"
    urls = py_log.read_text().splitlines() if py_log.exists() else []
    return proc.returncode, calls, urls


def test_the_redis_drill_asks_docker_for_a_loopback_port_and_nothing_wider(tmp_path):
    """Both attempts (valkey, then the redis fallback) as Docker receives them,
    after the shell has expanded every variable: one loopback publish each, no
    host networking. Both failing ends the drill before any check runs."""
    code, calls, urls = _run_redis_drill(tmp_path, run_ok=False)
    runs = [c[1:] for c in calls if c[:1] == ["run"]]

    assert code == 2
    assert [r[-1] for r in runs] == ["valkey/valkey:8-alpine", "redis:7-alpine"]
    assert [_publish_options(r) for r in runs] == [["127.0.0.1::6379"], ["127.0.0.1::6379"]]
    # The half-created valkey container is removed before the fallback reuses its name.
    rm_between = [c for c in calls[calls.index(["run", *runs[0]]) + 1:calls.index(["run", *runs[1]])]
                  if c[:2] == ["rm", "-fv"]]
    assert rm_between
    assert urls == []


@pytest.mark.parametrize("published, port", [
    ("127.0.0.1:32770", "32770"),
    ("[::1]:32771", "32771"),
])
def test_the_redis_drill_connects_to_the_port_docker_chose(tmp_path, published, port):
    code, calls, urls = _run_redis_drill(tmp_path, run_ok=True, port=published)

    assert code == 0
    assert ["port", "neoh-dr-valkey", "6379/tcp"] in calls
    assert urls == [f"REDIS_URL=redis://127.0.0.1:{port}/0"] * 2


def test_the_redis_drill_stops_when_docker_published_no_port(tmp_path):
    code, _, urls = _run_redis_drill(tmp_path, run_ok=True, port="")
    assert code == 1
    assert urls == []
