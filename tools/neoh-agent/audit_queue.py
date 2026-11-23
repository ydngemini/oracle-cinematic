"""Owner-controlled audit queue and Noah operating policy (fail closed).

Both files live in neoh-autonomous/owner-context, which launch.py binds READ-ONLY
into Noah's sandbox: Noah can read its assignments but never write, reorder,
approve or delete them. Anything malformed halts selection; only a VALID queue
with no eligible item may lead anywhere else, and in queue mode that "anywhere
else" is stop-and-report (owner decision 3, 2026-11-16: no legacy fallback).

Standard library only.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Callable

SHA_RE = re.compile(r"[0-9a-f]{40}")
ID_RE = re.compile(r"NQ-\d{2,4}")
CATEGORY_RE = re.compile(r"(0[1-9]|1[0-9]|20)")
PRIORITIES = {"P0": 0, "P1": 1, "P2": 2}
SAFE_ARG_RE = re.compile(r"[A-Za-z0-9_./@:=+-]{1,200}")
REVIEW_CLASSES = {"p2_claude_ok", "owner_required"}
MODES = {"stopped", "canary_single_cycle", "queue"}
# verify_commands are data for a FIXED runner, never shell text from a file.
COMMAND_KINDS = {
    "vitest": ("oracle-app", ["npx", "vitest", "run"]),
    "eslint": ("oracle-app", ["npx", "eslint"]),
    "pytest": ("backend", None),  # sys.executable -m pytest, filled by the caller
}
ITEM_FIELDS = {
    "id": str, "category": str, "finding_ids": list, "source": str, "verified_sha": str,
    "priority": str, "severity": str, "status": str, "root_cause": str,
    "allowed_paths": list, "restricted_paths": list, "depends_on": list,
    "conflicts_with": list, "requires_decisions": list, "failing_test": dict,
    "expected_behaviour": str, "acceptance": list, "verify_commands": list,
    "budget": dict, "retry": dict, "escalate_after": int, "live_provider": bool,
    "spends_money": bool, "evidence": dict, "review_class": str,
}
POLICY_FIELDS = {
    "mode": str, "legacy_fallback": bool, "per_task_usd": (int, float),
    "daily_usd": (int, float), "weekly_usd": (int, float),
    "contingency_ratio": (int, float), "contingency_hours": (int, float),
    "max_concurrent": int, "max_retained_failed_worktrees": int,
    "disk_floors": dict, "canary_item": (str, type(None)),
}


class QueueInvalid(ValueError):
    """The queue or policy cannot be trusted: halt, report, never fall back."""


def _relative_path_ok(value) -> bool:
    if not isinstance(value, str) or not value or value.startswith(("/", "~")):
        return False
    parts = Path(value).parts
    return ".." not in parts and "\\" not in value


def validate_item(item) -> list[str]:
    if not isinstance(item, dict):
        return ["item is not an object"]
    problems = []
    for name, kind in ITEM_FIELDS.items():
        if name not in item:
            problems.append(f"missing {name}")
        elif not isinstance(item[name], kind) or (kind is int and isinstance(item[name], bool)):
            problems.append(f"{name} must be {kind.__name__}")
    unknown = set(item) - set(ITEM_FIELDS)
    if unknown:
        problems.append(f"unknown fields {sorted(unknown)}")
    if problems:
        return problems
    if not ID_RE.fullmatch(item["id"]):
        problems.append("id must look like NQ-01")
    if not CATEGORY_RE.fullmatch(item["category"]):
        problems.append("category must be 01-20")
    if not SHA_RE.fullmatch(item["verified_sha"]):
        problems.append("verified_sha must be a full 40-hex SHA")
    if item["priority"] not in PRIORITIES:
        problems.append("priority must be P0, P1 or P2")
    if item["status"] != "confirmed_open":
        problems.append("only confirmed_open items may be queued")
    if item["review_class"] not in REVIEW_CLASSES:
        problems.append(f"review_class must be one of {sorted(REVIEW_CLASSES)}")
    if item["live_provider"] or item["spends_money"]:
        problems.append("Noah items may not use live providers or spend money")
    if not item["finding_ids"] or not all(isinstance(f, str) and f for f in item["finding_ids"]):
        problems.append("finding_ids must be non-empty strings")
    for field in ("allowed_paths", "restricted_paths"):
        if not all(_relative_path_ok(p) for p in item[field]):
            problems.append(f"{field} must be repository-relative paths")
    if not item["allowed_paths"]:
        problems.append("allowed_paths must not be empty")
    test = item["failing_test"]
    if not (_relative_path_ok(test.get("file")) and isinstance(test.get("name"), str)
            and isinstance(test.get("intent"), str) and test["name"] and test["intent"]):
        problems.append("failing_test needs file, name and intent")
    elif not any(test["file"] == p or test["file"].startswith(p.rstrip("/") + "/")
                 for p in item["allowed_paths"]):
        problems.append("failing_test.file must be inside allowed_paths")
    for command in item["verify_commands"]:
        if not (isinstance(command, dict) and command.get("kind") in COMMAND_KINDS
                and isinstance(command.get("args"), list)
                and all(isinstance(a, str) and SAFE_ARG_RE.fullmatch(a) and ".." not in a
                        and not a.startswith("/") for a in command["args"])):
            problems.append(f"verify_commands entry not allowed: {command!r}")
    if not item["verify_commands"]:
        problems.append("verify_commands must not be empty")
    budget = item["budget"]
    for key in ("usd", "minutes", "max_turns", "disk_gib"):
        value = budget.get(key)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
            problems.append(f"budget.{key} must be positive")
    retry = item["retry"]
    if not (isinstance(retry.get("max"), int) and 0 <= retry["max"] <= 3):
        problems.append("retry.max must be 0-3")
    if item["escalate_after"] < 1:
        problems.append("escalate_after must be >= 1")
    evidence = item["evidence"]
    if not (isinstance(evidence.get("paths"), list) and evidence["paths"]
            and all(_relative_path_ok(p) for p in evidence["paths"])):
        problems.append("evidence.paths must list the reproduction's source paths")
    blobs = evidence.get("blobs", {})
    if not isinstance(blobs, dict) or not all(
            _relative_path_ok(k) and isinstance(v, str) and re.fullmatch(r"[0-9a-f]{40}", v)
            for k, v in blobs.items()):
        problems.append("evidence.blobs must map paths to 40-hex blob ids")
    return problems


def _dependency_cycle(items: dict) -> str | None:
    state: dict[str, int] = {}

    def visit(node: str, trail: list[str]) -> str | None:
        if state.get(node) == 1:
            return " -> ".join(trail + [node])
        if state.get(node) == 2:
            return None
        state[node] = 1
        for dep in items[node]["depends_on"]:
            found = visit(dep, trail + [node])
            if found:
                return found
        state[node] = 2
        return None

    for node in items:
        found = visit(node, [])
        if found:
            return found
    return None


def load_queue(path: Path) -> list[dict]:
    """Every failure raises QueueInvalid; never returns a partial queue."""
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise QueueInvalid(f"queue file missing: {path}") from error
    except OSError as error:
        raise QueueInvalid(f"queue file unreadable: {type(error).__name__}") from error
    try:
        data = json.loads(raw)
    except ValueError as error:
        raise QueueInvalid(f"queue is not valid JSON: {error}") from error
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("items"), list):
        raise QueueInvalid("queue must be {\"version\": 1, \"items\": [...]}")
    seen: dict[str, dict] = {}
    problems = []
    for index, item in enumerate(data["items"]):
        for problem in validate_item(item):
            problems.append(f"item {index}: {problem}")
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            if item["id"] in seen:
                problems.append(f"duplicate id {item['id']}")
            seen[item["id"]] = item
    if problems:
        raise QueueInvalid("; ".join(problems[:20]))
    for item in seen.values():
        for dep in item["depends_on"] + item["conflicts_with"]:
            if dep not in seen:
                raise QueueInvalid(f"{item['id']} references unknown item {dep}")
    cycle = _dependency_cycle(seen)
    if cycle:
        raise QueueInvalid(f"dependency cycle: {cycle}")
    return list(data["items"])


def load_policy(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise QueueInvalid(f"policy file missing: {path}") from error
    except (OSError, ValueError) as error:
        raise QueueInvalid(f"policy unreadable: {type(error).__name__}: {error}") from error
    if not isinstance(data, dict):
        raise QueueInvalid("policy must be an object")
    problems = []
    for name, kind in POLICY_FIELDS.items():
        if name not in data:
            problems.append(f"missing {name}")
        elif not isinstance(data[name], kind) or (kind is int and isinstance(data[name], bool)):
            problems.append(f"{name} has the wrong type")
    if problems:
        raise QueueInvalid("policy: " + "; ".join(problems))
    if data["mode"] not in MODES:
        raise QueueInvalid(f"policy mode must be one of {sorted(MODES)}")
    if data["legacy_fallback"]:
        raise QueueInvalid("legacy_fallback is disabled by owner decision 3 (2026-11-16)")
    if data["max_concurrent"] != 1:
        raise QueueInvalid("max_concurrent must be 1")
    for key in ("per_task_usd", "daily_usd", "weekly_usd"):
        if not 0 < data[key] <= 100:
            raise QueueInvalid(f"{key} must be in (0, 100]")
    if not (data["per_task_usd"] <= data["daily_usd"] <= data["weekly_usd"]):
        raise QueueInvalid("caps must satisfy per_task <= daily <= weekly")
    if not 0 <= data["contingency_ratio"] <= 1 or data["contingency_hours"] < 0:
        raise QueueInvalid("contingency settings out of range")
    floors = data["disk_floors"]
    for key in ("core2_free_gib", "root_free_gib", "inode_free_ratio", "mem_available_gib"):
        if not isinstance(floors.get(key), (int, float)) or floors[key] < 0:
            raise QueueInvalid(f"disk_floors.{key} missing or negative")
    if data["mode"] == "canary_single_cycle" and not data["canary_item"]:
        raise QueueInvalid("canary mode needs canary_item")
    return data


def default_git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], capture_output=True, text=True, timeout=60)


def freshness(item: dict, main_sha: str,
              git: Callable[..., subprocess.CompletedProcess[str]] = default_git) -> tuple[bool, str]:
    """Ancestor-of-main is necessary, not sufficient: the reproduction's source
    paths must be unchanged since verification, and pinned blobs must match."""
    verified = item["verified_sha"]
    ancestor = git("merge-base", "--is-ancestor", verified, main_sha)
    if ancestor.returncode == 1:
        return False, f"verified_sha {verified[:12]} is not an ancestor of main"
    if ancestor.returncode:
        return False, "cannot check ancestry (fail closed)"
    if verified != main_sha:
        diff = git("diff", "--name-only", verified, main_sha, "--", *item["evidence"]["paths"])
        if diff.returncode:
            return False, "cannot diff evidence paths (fail closed)"
        changed = [line for line in diff.stdout.splitlines() if line.strip()]
        if changed:
            return False, f"STALE: evidence changed since verification: {', '.join(changed[:5])}"
    for path, blob in item["evidence"].get("blobs", {}).items():
        current = git("rev-parse", f"{main_sha}:{path}")
        if current.returncode or current.stdout.strip() != blob:
            return False, f"STALE: {path} blob differs from the verified one"
    return True, "fresh"
