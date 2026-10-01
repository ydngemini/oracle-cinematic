#!/usr/bin/env python3
"""Assemble the one artifact a load-test run leaves behind (§70).

    result.py <run-name> <guard.json> <analysis.json> <resources.json> <k6-exit-code> [scenario-args...]

Writes out/results/<run-name>.result.json with: the target's identity as the
GUARD read it from the server (environment, git SHA, migration head), the
scenario and its arguments, the data scale, per-endpoint latency, replica
distribution, resource peaks, and the PASS/FAIL verdict — which is k6's
threshold verdict, never re-derived here.

Secrets never enter it: tokens, passwords and DSNs are not inputs to this
script, and `scrub()` drops any key that looks like one if a future input
carries it.
"""

from __future__ import annotations

import datetime as _dt
import json
import pathlib
import platform
import sys

SECRET_MARKERS = ("token", "password", "passphrase", "secret", "dsn", "cookie", "authorization", "api_key")


def scrub(value):
    """Remove any key that names a credential, at any depth."""
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items()
                if not any(m in str(k).lower() for m in SECRET_MARKERS)}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    return value


def _load(path: str) -> dict:
    p = pathlib.Path(path)
    if not p.exists() or not p.read_text().strip():
        return {}
    return json.loads(p.read_text())


def data_scale(users_file: pathlib.Path) -> dict:
    if not users_file.exists():
        return {}
    fixture = json.loads(users_file.read_text())
    users = fixture.get("users", [])
    tenants = {u["tenant_slug"]: u["shape"] for u in users}
    shapes: dict[str, int] = {}
    for shape in tenants.values():
        shapes[shape] = shapes.get(shape, 0) + 1
    return {"profile": fixture.get("profile"), "tenants": len(tenants), "users": len(users),
            "tenants_by_shape": shapes}


def build(run: str, guard: dict, analysis: dict, resources: dict, k6_exit: int,
          scenario_args: list[str], users_file: pathlib.Path) -> dict:
    return scrub({
        "run": run,
        "scenario": run.split("-")[0],
        "scenario_args": scenario_args,
        "verdict": "PASS" if k6_exit == 0 else "FAIL",
        "k6_exit_code": k6_exit,
        "recorded_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "environment": {
            **guard,
            "load_generator_host": platform.node(),
            "topology": "local production-shaped (2 web replicas + 1 worker + nginx LB + "
                        "Postgres 16 + Valkey 8) on one 4-core host — NOT DigitalOcean",
        },
        "data_scale": data_scale(users_file),
        "latency": analysis.get("endpoints", {}),
        "total_requests": analysis.get("total_requests"),
        "replica_distribution": analysis.get("replica_distribution", {}),
        "resources": resources,
    })


def main(argv: list[str]) -> int:
    if len(argv) < 6:
        print(__doc__)
        return 2
    run, guard_p, analysis_p, resources_p, k6_exit = argv[1:6]
    here = pathlib.Path(__file__).resolve().parent
    result = build(run, _load(guard_p), _load(analysis_p), _load(resources_p), int(k6_exit),
                   argv[6:], here / "out" / "users.json")
    out = here / "out" / "results" / f"{run}.result.json"
    out.write_text(json.dumps(result, indent=1))
    print(f"  result: {result['verdict']} → {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
