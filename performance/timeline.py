#!/usr/bin/env python3
"""Latency over time for one endpoint pattern, from a k6 CSV.

A p95 over a whole run hides WHEN the slow requests happened: a start-up
burst and steady-state queueing look identical in the aggregate but need
opposite fixes.   timeline.py <samples.csv.gz> <name-substring> [bucket_s]
"""
import csv, gzip, sys


def pct(v, p):
    v = sorted(v)
    return v[int(round(p / 100 * (len(v) - 1)))]


def main(argv):
    path, needle = argv[1], argv[2]
    step = int(argv[3]) if len(argv) > 3 else 15
    rows = [(int(r["timestamp"]), float(r["metric_value"]))
            for r in csv.DictReader(gzip.open(path, "rt"))
            if r["metric_name"] == "http_req_duration" and needle in r["name"]]
    if not rows:
        print("no samples match", needle)
        return 1
    t0 = min(t for t, _ in rows)
    end = max(t for t, _ in rows) - t0
    for lo in range(0, end + 1, step):
        v = [x for t, x in rows if lo <= t - t0 < lo + step]
        if v:
            print(f"{lo:4}-{lo + step:<4}s n={len(v):4} p50={pct(v, 50):8.1f} p95={pct(v, 95):8.1f} max={max(v):8.1f}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
