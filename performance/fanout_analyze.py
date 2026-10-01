#!/usr/bin/env python3
"""Join ws_fanout.js sends to receipts: latency, missed and duplicate events.

    fanout_analyze.py <samples.csv.gz> [--json out.json]

For every successful send (a PATCH that broadcast DOSSIER_MOVED to its
tenant), every listener socket of that tenant should receive exactly one
event. A receipt is matched to the latest send of the same lead at or before
it. Latency is split by whether the listener sat on the SAME replica as the
PATCH (local delivery) or the OTHER one (only reachable through NOTIFY).
"""

from __future__ import annotations

import bisect
import csv
import gzip
import json
import sys
from collections import defaultdict


def _tags(row: dict) -> dict:
    out = {}
    for kv in (row.get("extra_tags") or "").split("&"):
        if "=" in kv:
            k, v = kv.split("=", 1)
            out[k] = v
    return out


def pct(v: list[float], p: float) -> float:
    if not v:
        return 0.0
    s = sorted(v)
    return s[max(0, min(len(s) - 1, int(round(p / 100 * (len(s) - 1)))))]


def analyze(path: str) -> dict:
    sends = defaultdict(list)            # lead -> [(t, tenant, replica)]
    recvs = []                           # (t, lead, tenant, vu)
    listeners = {}                       # vu -> (tenant, replica)
    with gzip.open(path, "rt", newline="") as fh:
        for row in csv.DictReader(fh):
            name = row["metric_name"]
            if name not in ("fanout_sent", "fanout_recv", "fanout_listener"):
                continue
            t = _tags(row)
            if name == "fanout_sent" and t.get("ok") == "true":
                sends[t["lead"]].append((float(row["metric_value"]), t["tenant"], t.get("replica", "?")))
            elif name == "fanout_recv":
                recvs.append((float(row["metric_value"]), t["lead"], t["tenant"], t["vu"]))
            elif name == "fanout_listener":
                listeners[t["vu"]] = (t["tenant"], t.get("replica", "?"))
    for lead in sends:
        sends[lead].sort()
    times = {lead: [s[0] for s in v] for lead, v in sends.items()}

    delivered = defaultdict(set)         # (lead, send_t) -> {vu}
    duplicates = 0
    unmatched = 0
    lat_local, lat_cross, lat_unknown = [], [], []
    for t, lead, tenant, vu in recvs:
        i = bisect.bisect_right(times.get(lead, []), t + 5) - 1   # 5 ms clock slack
        if i < 0:
            unmatched += 1
            continue
        send_t, send_tenant, send_replica = sends[lead][i]
        key = (lead, send_t)
        if vu in delivered[key]:
            duplicates += 1
            continue
        delivered[key].add(vu)
        latency = max(0.0, t - send_t)
        rep = listeners.get(vu, (None, "?"))[1]
        (lat_unknown if "?" in (rep, send_replica) else
         lat_local if rep == send_replica else lat_cross).append(latency)

    by_tenant = defaultdict(set)
    for vu, (tenant, _) in listeners.items():
        by_tenant[tenant].add(vu)
    expected = sum(len(by_tenant[tenant]) for v in sends.values() for (_, tenant, _) in v)
    got = sum(len(v) for v in delivered.values())

    def stats(v):
        return {"n": len(v), "p50_ms": round(pct(v, 50), 1), "p95_ms": round(pct(v, 95), 1),
                "p99_ms": round(pct(v, 99), 1), "max_ms": round(max(v), 1) if v else 0}

    return {
        "sends": sum(len(v) for v in sends.values()),
        "listeners": len(listeners),
        "listener_replicas": {r: sum(1 for _, (_, x) in listeners.items() if x == r)
                              for r in {x for _, x in listeners.values()}},
        "expected_deliveries": expected,
        "delivered": got,
        "missed": max(0, expected - got),
        "duplicates": duplicates,
        "unmatched_receipts": unmatched,
        "latency_local": stats(lat_local),
        "latency_cross_replica": stats(lat_cross),
        "latency_unattributed": stats(lat_unknown),
    }


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    result = analyze(argv[1])
    if "--json" in argv:
        with open(argv[argv.index("--json") + 1], "w") as fh:
            json.dump(result, fh, indent=1)
    print(json.dumps(result, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
