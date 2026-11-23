"""Spend ledger: reserve before a cycle, settle to reported cost after it.

Replaces the old daily_reserved_usd counter, which charged a fixed $11.75 per
cycle whatever the cycle cost and so capped Noah at 4 cycles/day while
reported spend was ~$2/day.

Rules (owner correction 4):
* reserve = the item's budget; the SDK hard cap per session never exceeds
  what remains of it, so a cycle cannot spend more than its reserve;
* settle = sum of reported session costs; a session with NO reported cost
  (crash, refused connection, timeout) is charged its full allocation;
* a contingency (ratio x settled) stays charged for contingency_hours, for
  delayed or unreported provider usage;
* open reservations count fully until settled; windows are rolling 24 h / 7 d.

Cost lines are authenticated with a per-cycle nonce the supervisor gives the
build process; agent-written tests run without that environment, so test
output cannot forge a cheaper cycle.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import time
from contextlib import contextmanager
from pathlib import Path

DAY = 86400.0
WEEK = 7 * DAY


def cost_markers(log_text: str, nonce: str) -> tuple[float, float, int]:
    """(reported_usd, unreported_charge_usd, sessions) from nonce-tagged lines.

    `[noah-session:<nonce>] start <n> alloc=<usd>` precedes each model session
    and `[noah-cost:<nonce>] session=<n> reported=<usd|none>` follows it."""
    if not re.fullmatch(r"[0-9a-f]{16,64}", nonce or ""):
        raise ValueError("cost nonce must be hex")
    starts: dict[str, float] = {}
    reported: dict[str, float | None] = {}
    for match in re.finditer(rf"^\[noah-session:{nonce}\] start (\d+) alloc=([0-9.]+)$", log_text, re.M):
        starts[match.group(1)] = float(match.group(2))
    for match in re.finditer(rf"^\[noah-cost:{nonce}\] session=(\d+) reported=([0-9.]+|none)$", log_text, re.M):
        value = match.group(2)
        reported[match.group(1)] = None if value == "none" else float(value)
    total_reported = 0.0
    unreported = 0.0
    for session, alloc in starts.items():
        value = reported.get(session)
        if value is None:
            unreported += alloc
        else:
            total_reported += value
    return round(total_reported, 6), round(unreported, 6), len(starts)


class Budget:
    def __init__(self, state_dir: Path, policy: dict, clock=time.time):
        self.dir = Path(state_dir)
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.dir / "budget.jsonl"
        self.lock_path = self.dir / "budget.lock"
        self.policy = policy
        self.clock = clock

    @contextmanager
    def _locked(self):
        with open(self.lock_path, "a+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _append(self, event: dict) -> None:
        line = json.dumps({"ts": self.clock(), **event}, sort_keys=True) + "\n"
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            if os.fstat(fd).st_size:
                with open(fd, "rb", closefd=False) as reader:
                    reader.seek(-1, os.SEEK_END)
                    if reader.read(1) != b"\n":
                        line = "\n" + line  # a crash left a torn line: never extend it
            os.write(fd, line.encode())
            os.fsync(fd)
        finally:
            os.close(fd)

    def _events(self) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and event.get("exec_id"):
                out.append(event)
        return out

    def _charges(self) -> list[tuple[float, float]]:
        """(timestamp, usd) for every charge still counted."""
        now = self.clock()
        per_exec: dict[str, dict] = {}
        for event in self._events():
            entry = per_exec.setdefault(event["exec_id"], {})
            entry[event["kind"]] = event
        charges = []
        hold = float(self.policy["contingency_hours"]) * 3600.0
        ratio = float(self.policy["contingency_ratio"])
        for entry in per_exec.values():
            reserve = entry.get("reserve")
            settle = entry.get("settle")
            if settle:
                spent = float(settle["reported_usd"]) + float(settle["unreported_usd"])
                charges.append((settle["ts"], spent))
                if now - settle["ts"] < hold:
                    charges.append((settle["ts"], spent * ratio))
            elif reserve:
                charges.append((reserve["ts"], float(reserve["usd"])))
        return charges

    def committed(self, window: float) -> float:
        now = self.clock()
        return round(sum(usd for ts, usd in self._charges() if now - ts < window), 6)

    def headroom(self) -> float:
        day = float(self.policy["daily_usd"]) - self.committed(DAY)
        week = float(self.policy["weekly_usd"]) - self.committed(WEEK)
        return max(0.0, min(day, week, float(self.policy["per_task_usd"])))

    def reserve(self, exec_id: str, usd: float) -> bool:
        if usd <= 0 or usd > float(self.policy["per_task_usd"]) + 1e-9:
            return False
        with self._locked():
            if usd > self.headroom() + 1e-9:
                return False
            self._append({"kind": "reserve", "exec_id": exec_id, "usd": round(usd, 6)})
            return True

    def settle(self, exec_id: str, reported_usd: float, unreported_usd: float) -> None:
        with self._locked():
            if not any(e["exec_id"] == exec_id and e["kind"] == "reserve" for e in self._events()):
                raise ValueError(f"settle without a reservation: {exec_id}")
            self._append({"kind": "settle", "exec_id": exec_id,
                          "reported_usd": round(max(0.0, reported_usd), 6),
                          "unreported_usd": round(max(0.0, unreported_usd), 6)})

    def spent_total(self) -> dict:
        reported = unreported = 0.0
        for event in self._events():
            if event["kind"] == "settle":
                reported += float(event["reported_usd"])
                unreported += float(event["unreported_usd"])
        return {"reported_usd": round(reported, 4), "unreported_charged_usd": round(unreported, 4)}
