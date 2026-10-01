#!/usr/bin/env python3
"""Turn a k6 raw-sample CSV into per-endpoint numbers that can be trusted.

k6's end-of-run summary only reports a tagged sub-metric when a threshold
names it, so a per-endpoint table built from the summary silently comes back
empty. This reads every sample instead and computes, per endpoint:

    count, error rate, p50 / p95 / p99 / max latency, median payload

plus how requests were distributed across API replicas (from the balancer's
X-Perf-Upstream header). Stdlib only.

Usage:  analyze.py <samples.csv[.gz]> [--json out.json]
"""

from __future__ import annotations

import csv
import gzip
import json
import sys
from collections import defaultdict


def pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round(p / 100 * (len(s) - 1)))))
    return s[k]


def analyze(path: str) -> dict:
    opener = gzip.open if path.endswith(".gz") else open
    lat = defaultdict(list)
    failed = defaultdict(int)
    reqs = defaultdict(int)
    payload = defaultdict(list)
    replicas = defaultdict(float)
    statuses = defaultdict(lambda: defaultdict(int))
    with opener(path, "rt", newline="") as fh:
        for row in csv.DictReader(fh):
            metric, name = row["metric_name"], row.get("name") or ""
            val = float(row["metric_value"] or 0)
            if metric == "http_req_duration":
                lat[name].append(val)
                reqs[name] += 1
                statuses[name][row.get("status") or "?"] += 1
            elif metric == "http_req_failed" and val >= 1:
                failed[name] += 1
            elif metric == "payload_bytes":
                payload[name].append(val)
            elif metric == "replica_hits":
                extra = row.get("extra_tags") or ""
                rep = next((kv.split("=", 1)[1] for kv in extra.split("&") if kv.startswith("replica=")), "?")
                replicas[rep] += val
    endpoints = {}
    for name in sorted(lat):
        v = lat[name]
        endpoints[name] = {
            "count": reqs[name],
            "error_rate": round(failed[name] / reqs[name], 4) if reqs[name] else 0,
            "p50_ms": round(pct(v, 50), 1), "p95_ms": round(pct(v, 95), 1),
            "p99_ms": round(pct(v, 99), 1), "max_ms": round(max(v), 1),
            "payload_kb_med": round(pct(payload.get(name, [0]), 50) / 1024, 1),
            "statuses": dict(statuses[name]),
        }
    total = sum(replicas.values()) or 1
    return {
        "endpoints": endpoints,
        "replica_distribution": {k: round(v / total, 3) for k, v in sorted(replicas.items())},
        "total_requests": sum(reqs.values()),
    }


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    result = analyze(argv[1])
    if "--json" in argv:
        out = argv[argv.index("--json") + 1]
        with open(out, "w") as fh:
            json.dump(result, fh, indent=1)
    print(f"\n{'endpoint':50} {'n':>5} {'err%':>5} {'p50':>7} {'p95':>7} {'p99':>7} {'max':>7} {'KB':>7}")
    for name, e in result["endpoints"].items():
        print(f"{name[:50]:50} {e['count']:5d} {e['error_rate']*100:5.1f} {e['p50_ms']:7.1f} "
              f"{e['p95_ms']:7.1f} {e['p99_ms']:7.1f} {e['max_ms']:7.1f} {e['payload_kb_med']:7.1f}")
    print(f"\nrequests: {result['total_requests']}   replica distribution: {result['replica_distribution']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
