#!/usr/bin/env python3
"""Summarize one drill: what users saw while the fault was in, how fast health
noticed it, and how fast the system came back. Writes <run>.summary.json."""

from __future__ import annotations

import json
import sys


def main(run: str) -> dict:
    samples = [json.loads(l) for l in open(run + ".jsonl") if l.startswith("{")]
    marks = {m["mark"]: m["wall"] for m in map(json.loads, open(run + ".marks.jsonl"))}
    inject, restore = marks["inject"], marks["restore"]
    before = [s for s in samples if s["wall"] < inject]
    during = [s for s in samples if inject <= s["wall"] < restore]
    after = [s for s in samples if s["wall"] >= restore]

    def ok(s, key):
        return s[key] == 200

    baseline_status = before[-1]["status"] if before else None
    detected = next((s["wall"] - inject for s in during
                     if not ok(s, "ready") or s["status"] != baseline_status), None)
    recovered = next((s["wall"] - restore for s in after
                      if ok(s, "ready") and ok(s, "crm") and s["status"] == baseline_status), None)
    crm_during = [s for s in during]
    summary = {
        "run": run.rsplit("/", 1)[-1],
        "samples": len(samples),
        "live_always_200": all(s["live"] == 200 for s in samples),
        "baseline_status": baseline_status,
        "statuses_during": sorted({str(s["status"]) for s in during}),
        "ready_during": sorted({str(s["ready"]) for s in during}),
        "crm_ok_during_pct": round(100 * sum(ok(s, "crm") for s in crm_during) / max(1, len(crm_during)), 1),
        "crm_results_during": sorted({str(s["crm"]) for s in during}),
        "crm_max_ms_during": max((s["crm_ms"] for s in during), default=None),
        "detected_after_s": None if detected is None else round(detected, 1),
        "recovered_after_s": None if recovered is None else round(recovered, 1),
        "crm_ok_after_pct": round(100 * sum(ok(s, "crm") for s in after) / max(1, len(after)), 1),
    }
    import os

    if os.path.exists(run + ".components.jsonl"):
        comps = [json.loads(l) for l in open(run + ".components.jsonl") if l.startswith("{")]
        base = next((c for c in reversed(comps) if c["wall"] < inject), None)
        changes = {}
        for name in (base or {}):
            if name == "wall":
                continue
            det = next((c["wall"] - inject for c in comps
                        if inject <= c["wall"] < restore + 30 and c.get(name) != base[name]), None)
            if det is None:
                continue
            during = sorted({c.get(name) for c in comps if inject <= c["wall"] < restore + 30})
            rec = next((c["wall"] - restore for c in comps if c["wall"] >= restore and c.get(name) == base[name]), None)
            changes[name] = {"baseline": base[name], "states": during, "detected_after_s": round(det, 1),
                             "recovered_after_s": None if rec is None else round(rec, 1)}
        summary["components"] = changes
    if os.path.exists(run + ".chat.jsonl"):
        summary["chat_turns"] = [json.loads(l) for l in open(run + ".chat.jsonl") if l.startswith("{")]
    json.dump(summary, open(run + ".summary.json", "w"), indent=1)
    print(json.dumps(summary, indent=1))
    return summary


if __name__ == "__main__":
    main(sys.argv[1])
