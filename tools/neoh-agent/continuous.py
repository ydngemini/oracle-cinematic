#!/usr/bin/env python3
"""Persistent supervisor for Neoh's automatic development runner.

One task per isolated cycle. Refresh main, discover work, build, test,
repair, and publish to an auto/neoh side branch. Never merge/deploy.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import runner
import intelligence
import work_discovery

ROOT = runner.ROOT
STATE_DIR = ROOT / ".neoh-agent-state"
STATE_FILE = STATE_DIR / "continuous.json"
LOCK_FILE = STATE_DIR / "supervisor.lock"
STOP = False


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def on_shutdown(_signum, _frame) -> None:
    global STOP
    STOP = True


def read_state() -> dict:
    if not STATE_FILE.exists():
        return {"cycles": 0, "successes": 0, "failures": 0,
                "blocked": {}, "history": []}
    try:
        content = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(content, dict):
            raise ValueError("Invalid state object")
        content.setdefault("blocked", {})
        content.setdefault("history", [])
        return content
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError(
            "Continuous worker state is unreadable. Review it manually "
            "rather than forgetting failed tasks."
        ) from error


def write_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n",
                   encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, STATE_FILE)


def acquire_lock():
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle = LOCK_FILE.open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        handle.close()
        raise RuntimeError("Another Neoh continuous supervisor is running.") from error
    return handle


def sleep_interruptible(seconds: int) -> None:
    deadline = time.monotonic() + seconds
    while not STOP and time.monotonic() < deadline:
        time.sleep(min(2, max(0, deadline - time.monotonic())))


def git(*args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(ROOT), *args], check=False, capture_output=True,
        text=True, timeout=30,
    )
    if completed.returncode:
        raise RuntimeError(f"Git checkout inspection failed ({completed.returncode}).")
    return completed.stdout.strip()


def branch_is_clean() -> bool:
    return not git("status", "--porcelain", "--untracked-files=all")


def ensure_supervisor_checkout() -> str:
    branch = git("branch", "--show-current")
    if not branch or branch.startswith("auto/"):
        raise RuntimeError(
            "Launch the continuous supervisor from clean main or the "
            "development runner branch, not from an auto/neoh work branch."
        )
    if not branch_is_clean():
        raise RuntimeError(
            "Continuous supervisor checkout has uncommitted changes. "
            "Resolve them manually before starting."
        )
    return branch


def cycle_status(code: int, stopped: bool = False) -> str:
    if code == 0:
        return "passed"
    if stopped or code in (-signal.SIGTERM, -signal.SIGINT):
        return "interrupted"
    return "failed"


def storage_ready(min_free_gib: float) -> bool:
    return shutil.disk_usage(ROOT).free >= min_free_gib * 1024 ** 3


def preflight_work(args, skip: set[str]) -> dict:
    main_sha, _changes = work_discovery.fetch_latest_main()
    pending = work_discovery.pending_categories() | skip
    selection = work_discovery.select_work(main_sha, category=args.category, task=args.task, pending=pending)
    selection["task_id"] = intelligence.task_identity(selection["category"], selection["task"], main_sha)
    selection["risk"] = intelligence.risk_profile(selection["category"], selection["task"])
    return selection


def held_categories(args) -> set[str]:
    return set(filter(None, getattr(args, "hold_categories", "").split(",")))


def idle(state: dict, reason: str, seconds: int) -> None:
    if state.get("idle_reason") != reason:
        state["idle_reason"] = reason
        state.pop("active_task", None)
        write_state(state)
        print(f"[continuous] idle: {reason}", flush=True)
    sleep_interruptible(seconds)


def run_cycle(args, skip: set[str], selection: dict) -> tuple[int, str, str]:
    """Use an ephemeral worktree so a failed run never contaminates supervisor."""
    parent = STATE_DIR / "worktrees"
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    name = f"cycle-{int(time.time())}-{os.getpid()}"
    path = parent / name
    base = git("rev-parse", "HEAD")
    work_branch = "supervisor/neoh-" + name
    result = subprocess.run(
        ["git", "-C", str(ROOT), "worktree", "add", "-b", work_branch, str(path), base],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode:
        raise RuntimeError(f"Cannot create isolated Neoh worktree ({result.returncode}).")
    try:
        command = [
            sys.executable, str(path / "tools/neoh-agent/autobuild.py"),
            "--category", selection["category"], "--budget-usd", str(args.budget_usd),
            "--max-repairs", str(args.max_repairs),
            "--max-turns", str(args.max_turns),
            "--model", args.model,
            "--test-timeout", str(args.test_timeout),
            "--backend-test-timeout", str(args.backend_test_timeout),
            "--agent-timeout", str(args.agent_timeout),
        ]
        command += ["--task", selection["task"]]
        if skip:
            command += ["--skip-categories", ",".join(sorted(skip))]
        if not args.push:
            command += ["--no-push"]
        print(f"[continuous] isolated cycle started: {name}", flush=True)
        env = os.environ.copy()
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["NEOH_PLANNED_MAIN"] = selection["main_sha"]
        env["NEOH_RECENT_FAILURES"] = json.dumps(selection.get("recent_failures", []))
        log = STATE_DIR / (name + ".log")
        output = log.open("w", encoding="utf-8")
        # Every worktree is its own checkout; npm dependencies from the
        # supervisor's checkout are not automatically available here.
        # This executes repo dependency scripts: use an isolated dev host
        # with no production credentials and no customer data.
        # NPM packages may execute installation scripts. Never expose the
        # Anthropic key, GitHub tokens, or existing developer credentials.
        dependency_env = {
            k: v for k, v in env.items()
            if not any(marker in k.upper() for marker in (
                "API_KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL",
                "PRIVATE_KEY", "AUTH", "AWS_", "AZURE_", "GOOGLE_",
                "ORACLE_DB_", "DIGITALOCEAN_", "GITHUB_",
            ))
        }
        dependency_install = subprocess.run(
            ["npm", "ci", "--no-audit", "--no-fund"],
            cwd=str(path / "oracle-app"), env=dependency_env, stdout=output,
            stderr=subprocess.STDOUT, timeout=900,
        )
        if dependency_install.returncode:
            output.close()
            print(f"[continuous] dependency installation failed in {path}", flush=True)
            return 1, name, ""
        child = subprocess.Popen(command, cwd=str(path), env=env,
                                 stdout=output, stderr=subprocess.STDOUT,
                                 start_new_session=True)
        stop_requested_at = None
        while child.poll() is None:
            if STOP:
                if stop_requested_at is None:
                    os.killpg(child.pid, signal.SIGTERM)
                    stop_requested_at = time.monotonic()
                elif time.monotonic() - stop_requested_at > 20:
                    os.killpg(child.pid, signal.SIGKILL)
            time.sleep(1)
        output.flush()
        output.close()
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
        category = ""
        for line in lines:
            if line.startswith("[discovery] category="):
                category = line.split("category=", 1)[1][:2]
        for line in lines[-12:]:
            print(line[:400], flush=True)
        receipt = {"cycle": name, "exit_code": child.returncode, "task_id": selection["task_id"]}
        for artifact in ("checks", "handoff"):
            source = path / ".neoh-agent-state" / f"{artifact}.json"
            if source.is_file() and not source.is_symlink() and source.resolve().is_relative_to(path.resolve()):
                if source.stat().st_size <= 65536:
                    try:
                        receipt[artifact] = json.loads(source.read_text())
                    except (ValueError, OSError):
                        receipt[artifact] = {"unavailable": True}
        intelligence.save_report(ROOT, f"receipt-{name}", receipt)
        if child.returncode == 0:
            # Do not leave a long-lived checkout with altered branch state.
            removal = subprocess.run(
                ["git", "-C", str(ROOT), "worktree", "remove", "--force", str(path)],
                capture_output=True, timeout=60,
            )
            if removal.returncode == 0:
                subprocess.run(
                    ["git", "-C", str(ROOT), "branch", "-D", work_branch],
                    capture_output=True, timeout=30,
                )
        else:
            print(f"[continuous] failed worktree retained for inspection: {path}", flush=True)
        return child.returncode, name, category
    finally:
        # Failed worktrees are intentionally retained for recovery; successful
        # ones are removed after local checks/push finish.
        pass


def supervise(args) -> int:
    ensure_supervisor_checkout()
    lock = acquire_lock()
    try:
        state = read_state()
        limit = args.max_cycles
        executed = 0
        while not STOP and (limit == 0 or executed < limit):
            # Failed category cooldowns persist across supervisor restarts.
            now = time.time()
            state["blocked"] = {
                category: until for category, until in state["blocked"].items()
                if isinstance(until, (int, float)) and until > now
            }
            day = datetime.now(timezone.utc).date().isoformat()
            if state.get("budget_day") != day:
                state["budget_day"] = day
                state["daily_reserved_usd"] = 0.0
            if state["daily_reserved_usd"] + args.budget_usd > args.daily_budget_usd:
                idle(state, "daily_budget", args.poll_seconds)
                continue
            if not storage_ready(args.min_free_gib):
                idle(state, "low_disk", args.error_poll_seconds)
                continue
            excluded = set(state["blocked"]) | held_categories(args)
            try:
                selection = preflight_work(args, excluded)
            except work_discovery.NoActionableWork:
                idle(state, "no_eligible_work", args.error_poll_seconds)
                continue
            except (RuntimeError, OSError, subprocess.TimeoutExpired, ValueError):
                idle(state, "preflight_unavailable", args.error_poll_seconds)
                continue
            selection["recent_failures"] = intelligence.recent_failures(state["history"], selection["category"])
            # Reserve the upper per-run estimated budget BEFORE starting the model.
            # This is a conservative accounting guard, not a provider-side hard cap.
            state["daily_reserved_usd"] += args.budget_usd
            state["idle_reason"] = ""
            state["active_task"] = {key: selection[key] for key in ("category", "task_id", "risk")}
            write_state(state)
            code, worktree_name, category = run_cycle(args, excluded, selection)
            executed += 1
            state["cycles"] = int(state.get("cycles", 0)) + 1
            status = cycle_status(code, STOP)
            passed = status == "passed"
            counter = {"passed": "successes", "failed": "failures", "interrupted": "interruptions"}[status]
            state[counter] = int(state.get(counter, 0)) + 1
            state["history"] = (
                state["history"] + [{
                    "time": utc_now(), "worktree": worktree_name,
                    "exit_code": code, "status": status,
                    "category": category or selection["category"], "task_id": selection["task_id"],
                    "failure_kind": ("provider" if code == 4 else "infrastructure" if code == 3 else status),
                }]
            )[-100:]
            state["last_cycle"] = utc_now()
            state.pop("active_task", None)
            write_state(state)
            if status == "failed" and category and code not in (3, 4):
                state["blocked"][category] = time.time() + args.failure_cooldown_seconds
                write_state(state)
            if code == 4:
                # The provider rejected the session before implementation, so
                # do not consume the day budget or punish the selected task.
                state["daily_reserved_usd"] = max(0.0, state["daily_reserved_usd"] - args.budget_usd)
                state["idle_reason"] = "provider_credit"
                write_state(state)
                print("[continuous] provider credit unavailable; waiting before retry.", flush=True)
                sleep_interruptible(args.provider_poll_seconds)
                continue
            if STOP:
                break
            if passed:
                print("[continuous] completed cycle; refreshing latest prompts before next task.", flush=True)
                sleep_interruptible(args.poll_seconds)
            else:
                # Pause after failures. Persistent duplicate branches are
                # excluded by work_discovery.pending_categories().
                print("[continuous] cycle failed or no eligible work; retry after idle interval.", flush=True)
                sleep_interruptible(args.error_poll_seconds)
        return 0
    finally:
        lock.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Neoh autonomous development continuously")
    parser.add_argument("--category", default="auto")
    parser.add_argument("--task", default="")
    parser.add_argument(
        "--hold-categories", default="",
        help="Comma-separated categories to leave alone, such as work finished on a branch "
             "the main ledger cannot see yet.",
    )
    parser.add_argument("--model", default="sonnet")
    parser.add_argument("--budget-usd", type=float, default=12)
    parser.add_argument("--daily-budget-usd", type=float, default=48)
    parser.add_argument("--max-repairs", type=int, default=2)
    parser.add_argument("--max-turns", type=int, default=24)
    parser.add_argument("--test-timeout", type=int, default=360)
    parser.add_argument(
        "--backend-test-timeout", type=int, default=2700,
        help="Full backend-suite ceiling in seconds; use measured suite duration plus headroom.",
    )
    parser.add_argument("--agent-timeout", type=int, default=1800)
    parser.add_argument("--poll-seconds", type=int, default=120)
    parser.add_argument("--error-poll-seconds", type=int, default=600)
    parser.add_argument("--provider-poll-seconds", type=int, default=21600)
    parser.add_argument("--failure-cooldown-seconds", type=int, default=3600)
    parser.add_argument("--max-cycles", type=int, default=0, help="0 means run indefinitely")
    parser.add_argument("--min-free-gib", type=float, default=0.5)
    parser.add_argument("--push", action=argparse.BooleanOptionalAction, default=True)
    args = parser.parse_args()
    if args.category == "auto" and args.task:
        parser.error("To supply --task, also specify --category.")
    if any(part not in {f"{n:02d}" for n in range(1, 21)} for part in held_categories(args)):
        parser.error("--hold-categories expects comma-separated categories 01-20.")
    if not 0 < args.min_free_gib < float("inf"):
        parser.error("Minimum free disk space must be finite and positive.")
    if (args.max_cycles < 0 or args.poll_seconds < 10 or args.error_poll_seconds < 30
            or args.provider_poll_seconds < 60 or args.failure_cooldown_seconds < 60):
        parser.error("Invalid cycle or polling controls.")
    if (args.budget_usd <= 0 or args.daily_budget_usd < args.budget_usd
            or args.max_turns <= 0 or args.max_repairs < 0
            or args.test_timeout <= 0 or args.backend_test_timeout <= 0):
        parser.error("Invalid model budget or repair settings.")
    signal.signal(signal.SIGINT, on_shutdown)
    signal.signal(signal.SIGTERM, on_shutdown)
    try:
        raise SystemExit(supervise(args))
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as error:
        print(f"[continuous] stopped safely: {error}", file=sys.stderr)
        raise SystemExit(2) from error


if __name__ == "__main__":
    main()
