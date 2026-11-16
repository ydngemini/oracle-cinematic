#!/usr/bin/env python3
"""Noah operations report from the real records only.

Sources: continuous.json (cycle history), task-events.jsonl (lifecycle),
budget.jsonl (reserved/settled spend), evidence/*.json, the queue, and disk.
Every figure states its sample size; small samples are flagged, not dressed up.
"""
from __future__ import annotations

import json
import os
import shutil
import statistics
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import audit_queue  # noqa: E402
import budget as budget_mod  # noqa: E402
import claims as claims_mod  # noqa: E402
import continuous  # noqa: E402

SMALL_SAMPLE = 10


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))]


def build(state_dir: Path, owner_context: Path) -> dict:
    state = json.loads((state_dir / "continuous.json").read_text()) if (state_dir / "continuous.json").exists() else {}
    history = state.get("history", [])
    evidence = []
    for path in sorted((state_dir / "evidence").glob("*.json")):
        try:
            evidence.append(json.loads(path.read_text()))
        except (OSError, ValueError):
            continue
    queue_cycles = [e for e in evidence if e.get("item")]
    passed = [e for e in queue_cycles if e.get("final_state") == "REVIEW_REQUIRED"]
    durations = [e["duration_s"] for e in queue_cycles if isinstance(e.get("duration_s"), (int, float))]
    spent_ok = sum(e["cost"]["reported_usd"] + e["cost"]["unreported_charged_usd"] for e in passed)
    spent_all = sum(e["cost"]["reported_usd"] + e["cost"]["unreported_charged_usd"] for e in queue_cycles)
    items = claims_mod.ClaimStore(state_dir).replay()
    try:
        policy = audit_queue.load_policy(owner_context / "noah-policy.json")
        spend = budget_mod.Budget(state_dir, policy)
        budget_view = {"committed_24h": spend.committed(budget_mod.DAY), "committed_7d": spend.committed(budget_mod.WEEK),
                       "headroom": spend.headroom(), **spend.spent_total(),
                       "caps": {k: policy[k] for k in ("per_task_usd", "daily_usd", "weekly_usd")}}
    except audit_queue.QueueInvalid as error:
        budget_view = {"error": str(error)}
    try:
        queue = audit_queue.load_queue(owner_context / "audit-queue.json")
        depth = Counter(i["priority"] for i in queue if items.get(i["id"], {}).get("state", "PENDING")
                        in {"PENDING", "RETRYABLE", "FAILED", "STALE"})
        blocked = {i["id"]: items[i["id"]]["state"] for i in queue
                   if items.get(i["id"], {}).get("state") in {"BLOCKED", "ESCALATED", "NOT_REPRODUCED"}}
    except audit_queue.QueueInvalid as error:
        depth, blocked = {"error": str(error)}, {}
    disk = {label: round(shutil.disk_usage(path).free / 1024 ** 3, 2)
            for label, path in (("sypher_core2_free_gib", state_dir), ("root_free_gib", "/"))}
    worktrees = list((state_dir / "worktrees").glob("cycle-*"))
    return {
        "legacy_history": {"cycles": len(history),
                           "passed": sum(1 for h in history if h.get("status") == "passed"),
                           "failed_by_kind": dict(Counter(h.get("failure_kind") for h in history
                                                          if h.get("status") != "passed"))},
        "queue_cycles": {
            "n": len(queue_cycles), "review_required": len(passed),
            "by_class": dict(Counter(e.get("class") for e in queue_cycles)),
            "by_final_state": dict(Counter(e.get("final_state") for e in queue_cycles)),
            "retries": sum(1 for i in items.values() for h in i["history"] if h.get("to") == "RETRYABLE"),
            "duration_s": {"median": statistics.median(durations) if durations else None,
                           "p95": percentile(durations, 0.95)},
            "cost_per_passing_cycle_usd": round(spent_ok / len(passed), 4) if passed else None,
            "spend_on_unsuccessful_cycles_usd": round(spent_all - spent_ok, 4),
            "small_sample": len(queue_cycles) < SMALL_SAMPLE,
        },
        "items": {k: v["state"] for k, v in items.items()},
        "closed": sorted(k for k, v in items.items() if v["state"] == "CLOSED"),
        "queue_depth_by_priority": dict(depth), "blocked_or_escalated": blocked,
        "budget": budget_view,
        "disk": {**disk, "retained_worktrees": len(worktrees)},
        "note": "A passing cycle is REVIEW_REQUIRED, not a resolved finding: a finding counts as "
                "resolved only when CLOSED (accepted, merged, re-verified on main).",
    }


def main() -> None:
    owner = Path(os.environ.get("NEOH_OWNER_CONTEXT", continuous.ROOT.parent / "owner-context"))
    print(json.dumps(build(continuous.STATE_DIR, owner), indent=2, default=str))


if __name__ == "__main__":
    main()
