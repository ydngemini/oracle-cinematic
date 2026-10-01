#!/usr/bin/env python3
"""§36/§37 DURABLE JOB QUEUE — throughput, backlog age, and exactly-once.

Seeds N `loadtest:noop` jobs (registered only in ORACLE_ENV=loadtest) straight
into automation_jobs for the perf tenants, then samples every second until the
queue drains:

  claim/completion throughput (jobs/s), oldest queued age, running count,
  retries/dead letters, DB CPU (from monitor.py samples), and — from Valkey —
  how many logical jobs executed MORE THAN ONCE (must be 0).

    NEOH_LOAD_TEST_ALLOWED=1 python3 performance/jobs_load.py --jobs 1000 --ms 50 [--queue default]

Run a second worker (topology: PERF_WORKERS=2) to test SKIP LOCKED across
processes; the default single worker tests the production shape.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import time

DIND = "oracle-sypher-docker"
R = pathlib.Path(__file__).resolve().parent / "out" / "results"


def psql(sql: str) -> str:
    return subprocess.run(["docker", "exec", DIND, "docker", "exec", "oracle-db-1", "psql", "-U", "postgres",
                           "-d", "oracle", "-Atc", sql], capture_output=True, text=True, check=True).stdout.strip()


def valkey(*args: str) -> str:
    return subprocess.run(["docker", "exec", DIND, "docker", "exec", "oracle-perf-valkey", "valkey-cli", *args],
                          capture_output=True, text=True, check=True).stdout.strip()


def main(argv) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", type=int, default=1000)
    ap.add_argument("--ms", type=int, default=50, help="simulated work per job")
    ap.add_argument("--queue", default="default")
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--label", default="")
    a = ap.parse_args(argv[1:])
    if os.environ.get("NEOH_LOAD_TEST_ALLOWED") != "1":
        print("REFUSING: NEOH_LOAD_TEST_ALLOWED=1 is required", file=sys.stderr)
        return 1
    # The target must say it is a load-test environment (same rule as guard.py).
    env = subprocess.run(["docker", "exec", DIND, "docker", "exec", "oracle-perf-worker", "python", "-c",
                          "import config;print(config.ORACLE_ENV)"], capture_output=True, text=True).stdout.strip()
    if env != "loadtest":
        print(f"REFUSING: worker reports ORACLE_ENV={env!r}", file=sys.stderr)
        return 1

    run = f"jobs_load-{a.jobs}{('-' + a.label) if a.label else ''}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
    valkey("DEL", "perf:noop:executed", "perf:noop:duplicates")
    psql("DELETE FROM automation_jobs WHERE job_type = 'loadtest:noop'")
    t0 = time.time()
    psql(f"""
      INSERT INTO automation_jobs (tenant_id, job_type, queue_name, state, payload, priority,
                                   idempotency_key, created_by, max_attempts)
      SELECT t.id, 'loadtest:noop', '{a.queue}', 'queued',
             jsonb_build_object('key', 'k' || g, 'ms', {a.ms}), 50,
             'perf-noop-{int(t0)}-' || g, 'perf-jobs-load', 3
        FROM generate_series(1, {a.jobs}) g
        JOIN LATERAL (SELECT id FROM tenants WHERE slug LIKE 'perf-%'
                       ORDER BY id OFFSET (g % 10) LIMIT 1) t ON true""")
    enq = time.time() - t0

    mon = subprocess.Popen([sys.executable, str(pathlib.Path(__file__).parent / "monitor.py"),
                            str(R / f"{run}.resources.jsonl"), "2"])
    samples = []
    try:
        while time.time() - t0 < a.timeout:
            row = psql("""SELECT json_build_object(
                'succeeded', count(*) FILTER (WHERE state='succeeded'),
                'running', count(*) FILTER (WHERE state IN ('leased','running')),
                'queued', count(*) FILTER (WHERE state IN ('queued','failed')),
                'dead', count(*) FILTER (WHERE state='dead_letter'),
                'oldest_queued_s', COALESCE(EXTRACT(EPOCH FROM now()-min(created_at)
                                   FILTER (WHERE state IN ('queued','failed'))),0))
                FROM automation_jobs WHERE job_type='loadtest:noop'""")
            s = json.loads(row)
            s["t"] = round(time.time() - t0, 1)
            samples.append(s)
            if s["succeeded"] + s["dead"] >= a.jobs:
                break
            time.sleep(1)
    finally:
        mon.terminate()

    total = samples[-1]["t"]
    attempts = psql("""SELECT json_build_object('attempts', count(*), 'jobs_with_multiple_attempts',
                         (SELECT count(*) FROM (SELECT job_id FROM automation_job_attempts a
                            JOIN automation_jobs j ON j.id=a.job_id WHERE j.job_type='loadtest:noop'
                           GROUP BY job_id HAVING count(*)>1) x))
                       FROM automation_job_attempts a JOIN automation_jobs j ON j.id=a.job_id
                      WHERE j.job_type='loadtest:noop'""")
    res = {
        "run": run, "jobs": a.jobs, "work_ms": a.ms, "queue": a.queue,
        "enqueue_s": round(enq, 2), "drain_s": total,
        "throughput_jobs_per_s": round(samples[-1]["succeeded"] / total, 1) if total else None,
        "peak_oldest_queued_s": round(max(s["oldest_queued_s"] for s in samples), 1),
        "peak_running": max(s["running"] for s in samples),
        "succeeded": samples[-1]["succeeded"], "dead_letter": samples[-1]["dead"],
        "executed_unique": int(valkey("SCARD", "perf:noop:executed") or 0),
        "executed_duplicates": int(valkey("GET", "perf:noop:duplicates") or 0),
        **json.loads(attempts),
        "timeline": samples[:: max(1, len(samples) // 30)],
    }
    subprocess.run([sys.executable, str(pathlib.Path(__file__).parent / "monitor.py"), "--summarize",
                    str(R / f"{run}.resources.jsonl")], stdout=open(R / f"{run}.resources.json", "w"))
    res["verdict"] = "PASS" if (res["executed_duplicates"] == 0 and res["dead_letter"] == 0
                                and res["succeeded"] == a.jobs) else "FAIL"
    (R / f"{run}.result.json").write_text(json.dumps(res, indent=1))
    print(json.dumps({k: v for k, v in res.items() if k != "timeline"}, indent=1))
    return 0 if res["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
