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
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import runner

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


def run_cycle(args, skip: set[str]) -> tuple[int, str]:
    """Use an ephemeral worktree so a failed run never contaminates supervisor."""
    parent = STATE_DIR / "worktrees"
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    name = f"cycle-{int(time.time())}-{os.getpid()}"
    path = parent / name
    base = git("rev-parse", "HEAD")
    result = subprocess.run(
        ["git", "-C", str(ROOT), "worktree", "add", "--detach", str(path), base],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode:
        raise RuntimeError(f"Cannot create isolated Neoh worktree ({result.returncode}).")
    try:
        command = [
            sys.executable, str(path / "tools/neoh-agent/autobuild.py"),
            "--category", args.category, "--budget-usd", str(args.budget_usd),
            "--max-repairs", str(args.max_repairs),
            "--max-turns", str(args.max_turns),
            "--model", args.model,
            "--test-timeout", str(args.test_timeout),
            "--agent-timeout", str(args.agent_timeout),
        ]
        if args.task:
            command += ["--task", args.task]
        if skip:
            command += ["--skip-categories", ",".join(sorted(skip))]
        if not args.push:
            command += ["--no-push"]
        print(f"[continuous] isolated cycle started: {name}", flush=True)
        env = os.environ.copy()
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        child = subprocess.Popen(command, cwd=str(path), env=env,
                                 start_new_session=True)
        stop_requested_at = None
        while child.poll() is None:
            if STOP:
                if stop_requested_at is None:
                    child.terminate()
                    stop_requested_at = time.monotonic()
                elif time.monotonic() - stop_requested_at > 20:
                    child.kill()
            time.sleep(1)
        return child.returncode, name
    finally:
        # Keep failed cycles for forensic review and manual salvage.
        # Successful cycles can be removed only when clean, after publication.
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
            excluded = set(state["blocked"])
            code, worktree_name = run_cycle(args, excluded)
            executed += 1
            state["cycles"] = int(state.get("cycles", 0)) + 1
            passed = code == 0
            state["successes" if passed else "failures"] = (
                int(state.get("successes" if passed else "failures", 0)) + 1
            )
            state["history"] = (
                state["history"] + [{
                    "time": utc_now(), "worktree": worktree_name,
                    "exit_code": code, "status": "passed" if passed else "failed",
                }]
            )[-100:]
            state["last_cycle"] = utc_now()
            write_state(state)
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
    parser.add_argument("--model", default="sonnet")
    parser.add_argument("--budget-usd", type=float, default=12)
    parser.add_argument("--max-repairs", type=int, default=2)
    parser.add_argument("--max-turns", type=int, default=24)
    parser.add_argument("--test-timeout", type=int, default=360)
    parser.add_argument("--agent-timeout", type=int, default=1800)
    parser.add_argument("--poll-seconds", type=int, default=120)
    parser.add_argument("--error-poll-seconds", type=int, default=600)
    parser.add_argument("--max-cycles", type=int, default=0, help="0 means run indefinitely")
    parser.add_argument("--push", action=argparse.BooleanOptionalAction, default=True)
    args = parser.parse_args()
    if args.category == "auto" and args.task:
        parser.error("To supply --task, also specify --category.")
    if args.max_cycles < 0 or args.poll_seconds < 10 or args.error_poll_seconds < 30:
        parser.error("Invalid cycle or polling controls.")
    if args.budget_usd <= 0 or args.max_turns <= 0 or args.max_repairs < 0:
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
