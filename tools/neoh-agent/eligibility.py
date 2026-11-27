"""One eligibility check for every task Noah might run (owner correction 2).

Pure: the caller gathers the facts (reservations, open branches, claims,
budget, disk, freshness) and this decides. An empty reason list means
eligible; anything else is a refusal with the reasons recorded.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import audit_queue

# Never writable by an autonomous cycle, whatever a queue item says.
PROTECTED_PATHS = (
    "tools/", ".github/", ".claude/", "infra/", "backend/db/migrations/",
    "docs/noah/", "owner-context/", "CLAUDE.md", "docs/claude-prompts/",
    ".env", ".gitleaksignore",
)
ACTIVE_STATES = {"CLAIMED", "TESTING", "IMPLEMENTING", "VERIFYING"}
DONE_STATES = {"REVIEW_REQUIRED", "INTEGRATED", "CLOSED"}
TERMINAL_BLOCKING = {"ESCALATED", "NOT_REPRODUCED", "BLOCKED"}


def _norm(path: str) -> str:
    return path.strip().lstrip("./")


def paths_overlap(a: str, b: str) -> bool:
    """Prefix-aware: a directory entry (trailing '/') covers what is under it."""
    a, b = _norm(a), _norm(b)
    if a == b:
        return True
    if a.endswith("/") and b.startswith(a):
        return True
    if b.endswith("/") and a.startswith(b):
        return True
    return False


def any_overlap(left, right) -> list[tuple[str, str]]:
    return [(x, y) for x in left for y in right if paths_overlap(x, y)]


@dataclass
class Context:
    main_sha: str
    reservations: list = field(default_factory=list)       # [{"paths":[], "ids":[], "expires": epoch}]
    open_branch_paths: dict = field(default_factory=dict)  # branch -> [paths changed vs main]
    item_states: dict = field(default_factory=dict)        # item_id -> {"state":..., "lease_until":...}
    held_categories: set = field(default_factory=set)
    answered_decisions: set = field(default_factory=set)
    budget_headroom_usd: float = 0.0
    disk_ok: bool = False
    disk_detail: str = "not checked"
    now: float = field(default_factory=time.time)


def check(item: dict, ctx: Context, fresh: tuple[bool, str]) -> list[str]:
    reasons: list[str] = []
    problems = audit_queue.validate_item(item)
    if problems:
        return [f"invalid item: {p}" for p in problems]
    item_id = item["id"]
    state = ctx.item_states.get(item_id, {}).get("state", "PENDING")
    if state in DONE_STATES:
        reasons.append(f"already {state}")
    if state in TERMINAL_BLOCKING:
        reasons.append(f"{state}: needs a human before another attempt")
    lease = ctx.item_states.get(item_id, {}).get("lease_until", 0)
    if state in ACTIVE_STATES and lease > ctx.now:
        reasons.append(f"claimed ({state}) until {int(lease)}")
    if not fresh[0]:
        reasons.append(fresh[1])
    if item["category"] in ctx.held_categories:
        reasons.append(f"category {item['category']} is held")
    missing = [d for d in item["requires_decisions"] if d not in ctx.answered_decisions]
    if missing:
        reasons.append(f"owner decision(s) outstanding: {', '.join(missing)}")
    for dep in item["depends_on"]:
        if ctx.item_states.get(dep, {}).get("state") != "CLOSED":
            reasons.append(f"depends on {dep} (not CLOSED)")
    for other in item["conflicts_with"]:
        other_state = ctx.item_states.get(other, {}).get("state", "PENDING")
        if other_state in ACTIVE_STATES | {"REVIEW_REQUIRED"}:
            reasons.append(f"conflicts with {other} ({other_state})")
    protected = any_overlap(item["allowed_paths"], PROTECTED_PATHS)
    if protected:
        reasons.append(f"allowed_paths touch protected paths: {protected[:3]}")
    for reservation in ctx.reservations:
        if reservation.get("expires", 0) and reservation["expires"] < ctx.now:
            continue
        clash = any_overlap(item["allowed_paths"], reservation.get("paths", []))
        if clash or item_id in reservation.get("ids", []) or \
                set(item["finding_ids"]) & set(reservation.get("ids", [])):
            reasons.append(f"reserved by {reservation.get('holder', 'interactive session')}")
    for branch, changed in ctx.open_branch_paths.items():
        clash = any_overlap(item["allowed_paths"], changed)
        if clash:
            reasons.append(f"open branch {branch} changes {clash[0][1]}")
    if item["budget"]["usd"] > ctx.budget_headroom_usd + 1e-9:
        reasons.append(f"budget: needs ${item['budget']['usd']:.2f}, headroom ${ctx.budget_headroom_usd:.2f}")
    if not ctx.disk_ok:
        reasons.append(f"disk: {ctx.disk_detail}")
    return reasons


def order(items: list[dict]) -> list[dict]:
    """Priority first, then the owner's queue order (stable)."""
    indexed = list(enumerate(items))
    indexed.sort(key=lambda pair: (audit_queue.PRIORITIES.get(pair[1].get("priority"), 9), pair[0]))
    return [item for _index, item in indexed]


def choose(items: list[dict], ctx: Context, freshness_of) -> tuple[dict | None, dict]:
    """First eligible item in order, plus every item's refusal reasons."""
    report: dict[str, list[str]] = {}
    for item in order(items):
        reasons = check(item, ctx, freshness_of(item))
        report[item.get("id", "?")] = reasons
        if not reasons:
            return item, report
    return None, report
