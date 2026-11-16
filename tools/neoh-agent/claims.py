"""Task claims and lifecycle as an append-only, fsynced journal.

The journal IS the state: replay rebuilds every item's state, so there is no
second file to drift from it, and a crash mid-append leaves at worst a torn
final line, which replay ignores. Every mutation happens under an exclusive
flock, so two workers can never both claim the same item.

Lives in .neoh-agent-state (writable by the supervisor only; agent-written
tests run in the VERIFY sandbox, where this directory is not visible).
"""
from __future__ import annotations

import fcntl
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path

ACTIVE = {"CLAIMED", "TESTING", "IMPLEMENTING", "VERIFYING"}
TRANSITIONS = {
    "PENDING": {"CLAIMED"},
    "RETRYABLE": {"CLAIMED", "ESCALATED"},
    "FAILED": {"CLAIMED", "ESCALATED"},
    "CLAIMED": {"TESTING", "FAILED", "RETRYABLE", "BLOCKED", "STALE", "ESCALATED"},
    "TESTING": {"IMPLEMENTING", "NOT_REPRODUCED", "FAILED", "RETRYABLE", "ESCALATED"},
    "IMPLEMENTING": {"VERIFYING", "FAILED", "RETRYABLE", "ESCALATED"},
    "VERIFYING": {"REVIEW_REQUIRED", "FAILED", "RETRYABLE", "ESCALATED"},
    "REVIEW_REQUIRED": {"INTEGRATED", "FAILED"},
    "INTEGRATED": {"CLOSED"},
    "STALE": {"CLAIMED"},
    "NOT_REPRODUCED": {"ESCALATED"},
    "BLOCKED": set(), "ESCALATED": set(), "CLOSED": set(),
}
# Only the supervisor's completion check may move work past review.
SUPERVISOR_ONLY = {"INTEGRATED", "CLOSED"}


class ClaimError(RuntimeError):
    pass


class ClaimStore:
    def __init__(self, state_dir: Path, clock=time.time):
        self.dir = Path(state_dir)
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.journal = self.dir / "task-events.jsonl"
        self.lock_path = self.dir / "claims.lock"
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
        event = {"ts": self.clock(), **event}
        line = json.dumps(event, sort_keys=True) + "\n"
        fd = os.open(self.journal, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
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

    def events(self) -> list[dict]:
        if not self.journal.exists():
            return []
        out = []
        for line in self.journal.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue  # a torn final line from a crash
            if isinstance(event, dict) and "item" in event and "to" in event:
                out.append(event)
        return out

    def replay(self) -> dict[str, dict]:
        items: dict[str, dict] = {}
        for event in self.events():
            entry = items.setdefault(event["item"], {"state": "PENDING", "history": []})
            entry["state"] = event["to"]
            entry["exec_id"] = event.get("exec_id", entry.get("exec_id"))
            entry["lease_until"] = event.get("lease_until", entry.get("lease_until", 0))
            entry["history"].append({k: event[k] for k in ("ts", "to", "exec_id", "reason") if k in event})
            if "signature" in event:
                entry.setdefault("signatures", []).append(event["signature"])
        return items

    def claim(self, item_id: str, exec_id: str, lease_seconds: float) -> bool:
        with self._locked():
            current = self.replay().get(item_id, {"state": "PENDING"})
            if current["state"] in ACTIVE and current.get("lease_until", 0) > self.clock():
                return False
            if current["state"] in ACTIVE:  # expired lease: recover first, on the record
                self._append({"item": item_id, "to": "RETRYABLE", "exec_id": current.get("exec_id"),
                              "reason": "lease_expired"})
                current = {"state": "RETRYABLE"}
            if "CLAIMED" not in TRANSITIONS.get(current["state"], set()):
                return False
            self._append({"item": item_id, "to": "CLAIMED", "exec_id": exec_id,
                          "lease_until": self.clock() + lease_seconds})
            return True

    def heartbeat(self, item_id: str, exec_id: str, lease_seconds: float) -> bool:
        with self._locked():
            current = self.replay().get(item_id, {})
            if current.get("exec_id") != exec_id or current.get("state") not in ACTIVE:
                return False
            self._append({"item": item_id, "to": current["state"], "exec_id": exec_id,
                          "lease_until": self.clock() + lease_seconds, "reason": "heartbeat"})
            return True

    def transition(self, item_id: str, exec_id: str | None, to: str, *, by_supervisor: bool = False,
                   reason: str = "", signature: str | None = None) -> None:
        if to in SUPERVISOR_ONLY and not by_supervisor:
            raise ClaimError(f"{to} is set only by the supervisor's completion check")
        with self._locked():
            current = self.replay().get(item_id, {"state": "PENDING"})
            if to not in TRANSITIONS.get(current["state"], set()):
                raise ClaimError(f"{item_id}: {current['state']} -> {to} is not allowed")
            if current["state"] in ACTIVE and current.get("exec_id") != exec_id:
                raise ClaimError(f"{item_id} is claimed by another execution")
            event = {"item": item_id, "to": to, "exec_id": exec_id, "reason": reason,
                     "lease_until": 0 if to not in ACTIVE else current.get("lease_until", 0)}
            if signature:
                event["signature"] = signature
            self._append(event)

    def recover_stale(self) -> list[str]:
        recovered = []
        with self._locked():
            for item_id, entry in self.replay().items():
                if entry["state"] in ACTIVE and entry.get("lease_until", 0) <= self.clock():
                    self._append({"item": item_id, "to": "RETRYABLE", "exec_id": entry.get("exec_id"),
                                  "reason": "lease_expired"})
                    recovered.append(item_id)
        return recovered
