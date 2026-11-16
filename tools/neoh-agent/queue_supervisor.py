"""Queue-only supervisor for Noah (owner decisions 2026-11-16).

One owner-assigned audit item per cycle. Never merges, never deploys, never
falls back to the legacy ledger. When nothing is eligible — or the queue or
policy is invalid, or a floor/limit is reached — it records why and stops
spending. Canary mode runs exactly one cycle and exits.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import audit_queue
import budget as budget_mod
import claims as claims_mod
import completion
import continuous
import eligibility
import failures
import intelligence
import resources
import work_discovery

ROOT = continuous.ROOT
STATE_DIR = continuous.STATE_DIR
OWNER_CONTEXT = Path(os.environ.get("NEOH_OWNER_CONTEXT", ROOT.parent / "owner-context"))
ARCHIVE = Path(os.environ.get("NEOH_ARCHIVE", ROOT.parent / "failed-cycles-archive"))
EVIDENCE_DIR = STATE_DIR / "evidence"
LEASE_SECONDS = 4 * 3600
BRANCH_PREFIXES = ("refs/heads/work/", "refs/heads/auto/neoh-", "refs/heads/review/")
PHASES = ("TESTING", "IMPLEMENTING", "VERIFYING")


def owner_file(name: str) -> Path:
    return OWNER_CONTEXT / name


def load_reservations() -> list[dict]:
    path = owner_file("reservations.json")
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))  # malformed -> raise (fail closed)
    if not isinstance(data, dict) or not isinstance(data.get("reservations"), list):
        raise audit_queue.QueueInvalid("reservations.json must be {\"reservations\": [...]}")
    return data["reservations"]


def open_branch_paths(main_sha: str, run=subprocess.run) -> dict[str, list[str]]:
    """Files each open remote branch changes relative to main."""
    listing = run(["git", "-C", str(ROOT), "ls-remote", "--heads", "origin"],
                  capture_output=True, text=True, timeout=60)
    if listing.returncode:
        raise RuntimeError("cannot list remote branches (fail closed)")
    result: dict[str, list[str]] = {}
    for line in listing.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2 or not parts[1].startswith(BRANCH_PREFIXES):
            continue
        sha, ref = parts
        fetched = run(["git", "-C", str(ROOT), "fetch", "-q", "--no-tags", "origin", sha],
                      capture_output=True, text=True, timeout=120)
        if fetched.returncode:
            raise RuntimeError(f"cannot fetch open branch {ref} (fail closed)")
        diff = run(["git", "-C", str(ROOT), "diff", "--name-only", f"{main_sha}...{sha}"],
                   capture_output=True, text=True, timeout=60)
        if diff.returncode:
            raise RuntimeError(f"cannot diff open branch {ref} (fail closed)")
        changed = [p for p in diff.stdout.splitlines() if p.strip()]
        if changed:
            result[ref.removeprefix("refs/heads/")] = changed
    return result


def phases_seen(log_text: str, nonce: str) -> list[str]:
    return re.findall(rf"^\[noah-phase:{nonce}\] (\w+)$", log_text, re.M)


def write_evidence(exec_id: str, data: dict) -> Path:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = EVIDENCE_DIR / f"{exec_id}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return path


def archive_and_remove(path: Path, name: str, floors: dict, retained_max: int) -> str:
    """Patch (tracked + untracked, via a scratch index) + log, apply-checked
    against HEAD BEFORE removal, on SYPHER_CORE2 (same partition: net frees)."""
    destination = ARCHIVE / name
    destination.mkdir(parents=True, exist_ok=True)
    head = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True,
                          text=True, timeout=30).stdout.strip()
    with tempfile.TemporaryDirectory(dir=STATE_DIR) as scratch:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(scratch) / "index")}
        for argv in (["read-tree", "HEAD"], ["add", "-A"]):
            subprocess.run(["git", "-C", str(path), *argv], env=env, check=True,
                           capture_output=True, timeout=120)
        patch = subprocess.run(["git", "-C", str(path), "diff", "--cached", "--binary", "HEAD"],
                               env=env, check=True, capture_output=True, timeout=120).stdout
        (destination / f"{name}.patch").write_bytes(patch)
        check_env = {**os.environ, "GIT_INDEX_FILE": str(Path(scratch) / "check")}
        subprocess.run(["git", "-C", str(ROOT), "read-tree", head], env=check_env, check=True,
                       capture_output=True, timeout=60)
        applies = subprocess.run(["git", "-C", str(ROOT), "apply", "--cached", "--check",
                                  str(destination / f"{name}.patch")], env=check_env,
                                 capture_output=True, timeout=60).returncode == 0
    log = STATE_DIR / f"{name}.log"
    if log.is_file():
        shutil.copy2(log, destination / "cycle.log")
    leaked = re.search(rb"(sk-ant-|sk_live_|ghp_|github_pat_|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY)",
                       patch)
    with open(ARCHIVE / "INDEX.tsv", "a", encoding="utf-8") as index:
        index.write(f"{name}\thead={head}\tpatch_bytes={len(patch)}\t"
                    f"secret_hits={1 if leaked else 0}\t{'apply_check_ok' if applies else 'APPLY_CHECK_FAILED'}\n")
    if not applies or leaked:
        return f"kept {path}: archive {'did not apply' if not applies else 'matched a secret pattern'}"
    retained = sorted((p for p in (STATE_DIR / "worktrees").glob("cycle-*") if p != path),
                      key=lambda p: p.stat().st_mtime)
    if len(retained) < retained_max:
        return f"archived {name}; worktree retained ({len(retained) + 1}/{retained_max})"
    subprocess.run(["git", "-C", str(ROOT), "worktree", "remove", "--force", str(path)],
                   capture_output=True, timeout=120)
    return f"archived {name} and removed the worktree"


def run_queue_cycle(args, item: dict, main_sha: str, exec_id: str, nonce: str) -> tuple[int, str, Path]:
    parent = STATE_DIR / "worktrees"
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = parent / exec_id
    base = continuous.git("rev-parse", "HEAD")
    work_branch = "supervisor/neoh-" + exec_id
    created = subprocess.run(["git", "-C", str(ROOT), "worktree", "add", "-b", work_branch, str(path), base],
                             capture_output=True, text=True, timeout=60)
    if created.returncode:
        raise RuntimeError(f"Cannot create isolated Neoh worktree ({created.returncode}).")
    log = STATE_DIR / f"{exec_id}.log"
    with log.open("w", encoding="utf-8") as output:
        dependency_env = {k: v for k, v in os.environ.items()
                          if not any(m in k.upper() for m in ("API_KEY", "TOKEN", "SECRET", "PASSWORD",
                                                              "CREDENTIAL", "PRIVATE_KEY", "AUTH", "GITHUB_"))}
        if any(p.startswith("oracle-app/") for p in item["allowed_paths"]):
            install = subprocess.run(["npm", "ci", "--no-audit", "--no-fund"], cwd=str(path / "oracle-app"),
                                     env=dependency_env, stdout=output, stderr=subprocess.STDOUT, timeout=900)
            if install.returncode:
                return failures_exit_infra(), log.read_text(errors="replace"), path
        env = os.environ.copy()
        env.update({
            "PYTHONDONTWRITEBYTECODE": "1",
            "NEOH_WORKTREE": str(path),
            "NEOH_QUEUE_ITEM": json.dumps(item),
            "NEOH_PLANNED_MAIN": main_sha,
            "NEOH_COST_NONCE": nonce,
            "NEOH_STATE_DIR": str(STATE_DIR),
            "NEOH_BASELINE_CACHE": str(STATE_DIR / "baseline"),
            "NEOH_VERIFY_HIDE": os.pathsep.join([str(OWNER_CONTEXT), "/home/agent"]),
        })
        # The SUPERVISOR's copy (read-only overlay), never the worktree's.
        command = [sys.executable, str(Path(__file__).with_name("autobuild.py")),
                   "--category", item["category"], "--budget-usd", f"{float(item['budget']['usd']):.4f}",
                   "--max-repairs", str(args.max_repairs), "--max-turns", str(int(item["budget"]["max_turns"])),
                   "--model", args.model, "--test-timeout", str(args.test_timeout),
                   "--backend-test-timeout", str(args.backend_test_timeout),
                   "--agent-timeout", str(min(args.agent_timeout, int(item["budget"]["minutes"]) * 60))]
        if not args.push:
            command.append("--no-push")
        print(f"[queue] cycle {exec_id}: {item['id']} at {main_sha[:12]}", flush=True)
        child = subprocess.Popen(command, cwd=str(path), env=env, stdout=output,
                                 stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.monotonic() + int(item["budget"]["minutes"]) * 60 + 3600  # tests may run long
        stop_at = None
        while child.poll() is None:
            if continuous.STOP or time.monotonic() > deadline:
                if stop_at is None:
                    os.killpg(child.pid, signal.SIGTERM)
                    stop_at = time.monotonic()
                elif time.monotonic() - stop_at > 20:
                    os.killpg(child.pid, signal.SIGKILL)
            time.sleep(1)
    return child.returncode, log.read_text(encoding="utf-8", errors="replace"), path


def failures_exit_infra() -> int:
    return 3


def completion_pass(items: dict, store: claims_mod.ClaimStore, main_sha: str) -> list[str]:
    notes = []
    acceptances = completion.load_acceptances(owner_file("acceptances.json"))
    for item_id, entry in store.replay().items():
        item = items.get(item_id)
        if not item or entry["state"] not in {"REVIEW_REQUIRED", "INTEGRATED"}:
            continue
        record, why = completion.acceptance_for(item, entry.get("exec_id"), acceptances)
        if not record:
            notes.append(f"{item_id}: {why}")
            continue
        merged, how = completion.integrated(record, main_sha)
        if not merged:
            notes.append(f"{item_id}: accepted, not integrated ({how})")
            continue
        if entry["state"] == "REVIEW_REQUIRED":
            store.transition(item_id, entry.get("exec_id"), "INTEGRATED", by_supervisor=True, reason=how)
        # (c) re-verification at main is recorded by the operator report until
        # an automated main-SHA verify run lands; CLOSED waits for it.
        notes.append(f"{item_id}: INTEGRATED ({how}); CLOSED after re-verification at {main_sha[:12]}")
    return notes


def supervise_queue(args) -> int:
    continuous.ensure_supervisor_checkout()
    lock = continuous.acquire_lock()
    try:
        state = continuous.read_state()
        store = claims_mod.ClaimStore(STATE_DIR)
        while not continuous.STOP:
            try:
                policy = audit_queue.load_policy(owner_file("noah-policy.json"))
                queue = audit_queue.load_queue(owner_file("audit-queue.json"))
                reservations = load_reservations()
            except (audit_queue.QueueInvalid, OSError, ValueError) as error:
                continuous.idle(state, "queue_invalid", args.error_poll_seconds, intelligence.redact(str(error))[:300])
                if args.canary:
                    return 2
                continue
            if policy["mode"] == "canary_single_cycle" and not args.canary:
                # A reboot or a stray `continuous` start must not turn the
                # owner's one-cycle canary into a loop (owner decision 6).
                continuous.idle(state, "canary_requires_explicit_invocation", args.error_poll_seconds)
                continue
            if policy["mode"] == "stopped":
                continuous.idle(state, "policy_stopped", args.error_poll_seconds)
                if args.canary:
                    return 2
                continue
            for item_id in store.recover_stale():
                print(f"[queue] recovered expired claim: {item_id}", flush=True)
            disk_ok, disk_problems = resources.check_floors(policy["disk_floors"], str(ROOT))
            try:
                main_sha, _ = work_discovery.fetch_latest_main()
                branches = open_branch_paths(main_sha)
            except (RuntimeError, OSError, subprocess.TimeoutExpired, ValueError) as error:
                continuous.idle(state, "preflight_unavailable", args.error_poll_seconds,
                                intelligence.redact(f"{type(error).__name__}: {error}")[:300])
                if args.canary:
                    return 2
                continue
            by_id = {item["id"]: item for item in queue}
            for note in completion_pass(by_id, store, main_sha):
                print(f"[completion] {note}", flush=True)
            spend = budget_mod.Budget(STATE_DIR, policy)
            ctx = eligibility.Context(
                main_sha=main_sha, reservations=reservations, open_branch_paths=branches,
                item_states=store.replay(), held_categories=continuous.held_categories(args),
                answered_decisions=set(policy.get("answered_decisions", [])),
                budget_headroom_usd=spend.headroom(), disk_ok=disk_ok,
                disk_detail="; ".join(disk_problems) or "ok")
            candidates = [by_id[policy["canary_item"]]] if policy["mode"] == "canary_single_cycle" \
                and policy["canary_item"] in by_id else ([] if policy["mode"] == "canary_single_cycle" else queue)
            chosen, report = eligibility.choose(
                candidates, ctx, lambda item: audit_queue.freshness(item, main_sha))
            state["queue_report"] = report
            if chosen is None:
                detail = "; ".join(f"{k}: {v[0]}" for k, v in report.items() if v)[:300] or "queue empty"
                continuous.idle(state, "queue_exhausted", args.error_poll_seconds, detail)
                if args.canary:
                    return 3
                continue
            exec_id = f"cycle-{int(time.time())}-{os.getpid()}"
            usd = float(chosen["budget"]["usd"])
            if not spend.reserve(exec_id, usd):
                continuous.idle(state, "budget", args.error_poll_seconds, f"cannot reserve ${usd:.2f}")
                if args.canary:
                    return 3
                continue
            if not store.claim(chosen["id"], exec_id, LEASE_SECONDS):
                spend.settle(exec_id, 0.0, 0.0)
                continuous.idle(state, "claim_conflict", args.poll_seconds, chosen["id"])
                continue
            nonce = secrets.token_hex(16)
            state.update(idle_reason="", idle_detail="", active_task={"item": chosen["id"], "exec_id": exec_id})
            continuous.write_state(state)
            started = time.time()
            code, log_text, worktree = run_queue_cycle(args, chosen, main_sha, exec_id, nonce)
            reported, unreported, sessions = budget_mod.cost_markers(log_text, nonce)
            if code != 0 and sessions == 0 and "[noah-session:" not in log_text:
                unreported = 0.0  # stopped before any model session: nothing was spent
            spend.settle(exec_id, reported, unreported)
            seen = phases_seen(log_text, nonce)
            current = "CLAIMED"
            for name in PHASES:
                if name in seen:
                    store.transition(chosen["id"], exec_id, name, reason="phase marker")
                    current = name
            kind = failures.classify(code, log_text)
            sig = failures.signature(kind, log_text)
            entry = store.replay().get(chosen["id"], {})
            infra_attempts = sum(1 for h in entry.get("history", []) if h.get("reason") == "infrastructure")
            verdict, delay = failures.decide(kind, sig, entry.get("signatures", []), infra_attempts,
                                             chosen["escalate_after"], chosen["retry"]["max"])
            final = {"done": "REVIEW_REQUIRED", "retry": "RETRYABLE", "escalate": "ESCALATED",
                     "fail": "FAILED", "halt": "BLOCKED"}[verdict]
            if kind == "not_reproduced":
                final = "NOT_REPRODUCED"
            if final == "REVIEW_REQUIRED" and current != "VERIFYING":
                final = "FAILED"  # a pass without the full lifecycle is not trusted
            store.transition(chosen["id"], exec_id, final, reason=kind if kind != "passed" else "verified",
                             signature=sig if kind != "passed" else None)
            disposition = ""
            if code != 0 and worktree.exists():
                disposition = archive_and_remove(worktree, exec_id, policy["disk_floors"],
                                                 policy["max_retained_failed_worktrees"])
            elif worktree.exists():
                subprocess.run(["git", "-C", str(ROOT), "worktree", "remove", "--force", str(worktree)],
                               capture_output=True, timeout=120)
            red = re.search(r"^\[red\] .*?(?=^\[|\Z)", log_text, re.M | re.S)
            evidence = write_evidence(exec_id, {
                "item": chosen["id"], "finding_ids": chosen["finding_ids"], "exec_id": exec_id,
                "main_sha": main_sha, "exit_code": code, "class": kind, "signature": sig,
                "final_state": final, "phases": seen, "duration_s": round(time.time() - started, 1),
                "cost": {"reported_usd": reported, "unreported_charged_usd": unreported, "sessions": sessions,
                         "reserved_usd": usd},
                "red_evidence": intelligence.redact(red.group(0))[:4000] if red else None,
                "verify": re.findall(r"^\[verify\] .*$", log_text, re.M),
                "branch": (re.findall(r"^\[ready\] .*branch=(\S+);", log_text, re.M) or [None])[0],
                "worktree": disposition or "removed",
                "time": datetime.now(timezone.utc).isoformat(),
            })
            state["cycles"] = int(state.get("cycles", 0)) + 1
            state["history"] = (state["history"] + [{
                "time": continuous.utc_now(), "worktree": exec_id, "exit_code": code,
                "status": "passed" if code == 0 else "failed", "category": chosen["category"],
                "task_id": chosen["id"], "failure_kind": kind}])[-100:]
            state.pop("active_task", None)
            continuous.write_state(state)
            print(f"[queue] {chosen['id']} -> {final} ({kind}); evidence {evidence}", flush=True)
            if args.canary or verdict == "halt":
                return 0 if final == "REVIEW_REQUIRED" else 1
            continuous.sleep_interruptible(delay or args.poll_seconds)
        return 0
    finally:
        lock.close()


def dry_run(args) -> int:
    """What would be chosen right now, and why every other item was refused."""
    policy = audit_queue.load_policy(owner_file("noah-policy.json"))
    queue = audit_queue.load_queue(owner_file("audit-queue.json"))
    reservations = load_reservations()
    store = claims_mod.ClaimStore(STATE_DIR)
    disk_ok, disk_problems = resources.check_floors(policy["disk_floors"], str(ROOT))
    main_sha, _ = work_discovery.fetch_latest_main()
    branches = open_branch_paths(main_sha)
    spend = budget_mod.Budget(STATE_DIR, policy)
    ctx = eligibility.Context(
        main_sha=main_sha, reservations=reservations, open_branch_paths=branches,
        item_states=store.replay(), held_categories=continuous.held_categories(args),
        answered_decisions=set(policy.get("answered_decisions", [])),
        budget_headroom_usd=spend.headroom(), disk_ok=disk_ok,
        disk_detail="; ".join(disk_problems) or "ok")
    by_id = {item["id"]: item for item in queue}
    candidates = [by_id[policy["canary_item"]]] if policy["mode"] == "canary_single_cycle" else queue
    chosen, report = eligibility.choose(candidates, ctx, lambda item: audit_queue.freshness(item, main_sha))
    print(json.dumps({"mode": policy["mode"], "main_sha": main_sha, "budget_headroom_usd": spend.headroom(),
                      "disk": disk_problems or "ok", "open_branches": sorted(branches),
                      "chosen": chosen["id"] if chosen else None, "refusals": report}, indent=2))
    return 0 if chosen else 3
