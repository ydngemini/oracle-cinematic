#!/usr/bin/env python3
"""Sample resources while a load test runs, so latency can be tied to a cause.

Every INTERVAL seconds, records per container (CPU %, memory) and, from the
database's own catalog, connection counts by state, how many backends are
waiting on locks, and the automation-job backlog (depth + oldest queued age).
Writes one JSON object per line; `summarize()` reduces a file to peaks and
means for the result artifact.

The perf topology has no metrics stack of its own, and §71 says not to invent
one: in DigitalOcean these same numbers come from App Platform insights and
the Managed Postgres dashboard. This is the local stand-in, reading the same
things those dashboards read (cgroup CPU/memory, pg_stat_activity).

Usage:  monitor.py <out.jsonl> [interval_s]     (runs until killed)
        monitor.py --summarize <out.jsonl>
"""

from __future__ import annotations

import json
import subprocess
import sys
import time

DIND = "oracle-sypher-docker"
CONTAINERS = ["oracle-db-1", "oracle-perf-web-1", "oracle-perf-web-2", "oracle-perf-mock",
              "oracle-perf-worker", "oracle-perf-valkey", "oracle-perf-lb"]

DB_SQL = """
SELECT json_build_object(
  'conns_total',  (SELECT count(*) FROM pg_stat_activity WHERE datname = 'oracle'),
  'conns_active', (SELECT count(*) FROM pg_stat_activity WHERE datname = 'oracle' AND state = 'active'),
  'conns_idle_tx',(SELECT count(*) FROM pg_stat_activity WHERE datname = 'oracle' AND state LIKE 'idle in transaction%'),
  'lock_waits',   (SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock'),
  'jobs_queued',  (SELECT count(*) FROM automation_jobs WHERE state = 'queued' AND scheduled_at <= now()),
  'jobs_running', (SELECT count(*) FROM automation_jobs WHERE state IN ('leased','running')),
  'oldest_queued_s', (SELECT COALESCE(EXTRACT(EPOCH FROM now() - min(scheduled_at)), 0)
                        FROM automation_jobs WHERE state = 'queued' AND scheduled_at <= now())
)
"""


def _docker(*args: str, timeout: float = 20) -> str:
    return subprocess.run(["docker", "exec", DIND, "docker", *args], capture_output=True,
                          text=True, timeout=timeout).stdout


def _mem_mib(text: str) -> float:
    used = text.split("/")[0].strip()
    for unit, mult in (("GiB", 1024), ("MiB", 1), ("KiB", 1 / 1024), ("B", 1 / 1024 / 1024)):
        if used.endswith(unit):
            return round(float(used[: -len(unit)]) * mult, 1)
    return 0.0


def sample() -> dict:
    out: dict = {"t": round(time.time(), 1), "containers": {}}
    stats = _docker("stats", "--no-stream", "--format", "{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}", *CONTAINERS)
    for line in stats.splitlines():
        parts = line.split("|")
        if len(parts) == 3:
            out["containers"][parts[0]] = {"cpu_pct": float(parts[1].rstrip("%") or 0),
                                          "mem_mib": _mem_mib(parts[2])}
    try:
        raw = _docker("exec", "oracle-db-1", "psql", "-U", "postgres", "-d", "oracle", "-Atc", DB_SQL)
        out["db"] = json.loads(raw.strip().splitlines()[-1])
    except (IndexError, ValueError):
        out["db"] = None
    return out


def summarize(path: str) -> dict:
    rows = [json.loads(line) for line in open(path) if line.strip()]
    if not rows:
        return {}
    result: dict = {"samples": len(rows), "duration_s": round(rows[-1]["t"] - rows[0]["t"], 1),
                    "containers": {}, "db": {}}
    names = {n for r in rows for n in r["containers"]}
    for n in sorted(names):
        cpu = [r["containers"][n]["cpu_pct"] for r in rows if n in r["containers"]]
        mem = [r["containers"][n]["mem_mib"] for r in rows if n in r["containers"]]
        result["containers"][n] = {
            "cpu_pct_mean": round(sum(cpu) / len(cpu), 1), "cpu_pct_peak": max(cpu),
            "mem_mib_first": mem[0], "mem_mib_last": mem[-1], "mem_mib_peak": max(mem)}
    dbrows = [r["db"] for r in rows if r.get("db")]
    for k in (dbrows[0].keys() if dbrows else []):
        vals = [float(d[k] or 0) for d in dbrows]
        result["db"][k] = {"peak": max(vals), "mean": round(sum(vals) / len(vals), 1), "last": vals[-1]}
    return result


def main(argv: list[str]) -> int:
    if len(argv) >= 3 and argv[1] == "--summarize":
        print(json.dumps(summarize(argv[2]), indent=1))
        return 0
    if len(argv) < 2:
        print(__doc__)
        return 2
    interval = float(argv[2]) if len(argv) > 2 else 5.0
    with open(argv[1], "a") as fh:
        while True:
            start = time.time()
            try:
                fh.write(json.dumps(sample()) + "\n")
                fh.flush()
            except subprocess.TimeoutExpired:
                pass
            time.sleep(max(0.0, interval - (time.time() - start)))


if __name__ == "__main__":
    sys.exit(main(sys.argv))
