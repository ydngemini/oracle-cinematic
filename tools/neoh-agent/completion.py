"""Independent completion (owner correction 7, decision 4).

A queue item reaches CLOSED only when ALL hold:
  (a) acceptance: an owner-context/acceptances.json record naming a reviewer
      who is not Noah, the finding(s), evidence, the exact revision and the
      decision "accepted"; owner_required items need reviewer_role "owner";
  (b) integration: the accepted PR is merged and its merge commit is an
      ancestor of main (regular, squash and rebase merges all qualify), or a
      patch-id of the accepted change appears on main;
  (c) re-verification: the item's verify_commands pass at that main SHA.
Noah can write none of these inputs: acceptances live in the read-only
owner-context mount and integration is read from GitHub and git.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Callable

REQUIRED = ("item", "exec_id", "finding_ids", "reviewer", "reviewer_role", "revision",
            "evidence", "decision", "pr")
FORBIDDEN_REVIEWERS = {"noah", "neoh-agent", "auto"}


def load_acceptances(path: Path) -> list[dict]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        return []  # unreadable acceptances accept nothing (fail closed)
    return [a for a in data.get("acceptances", []) if isinstance(a, dict)] if isinstance(data, dict) else []


def acceptance_for(item: dict, exec_id: str, acceptances: list[dict]) -> tuple[dict | None, str]:
    for record in acceptances:
        if record.get("item") != item["id"] or record.get("exec_id") != exec_id:
            continue
        missing = [k for k in REQUIRED if not record.get(k)]
        if missing:
            return None, f"acceptance missing {missing}"
        if str(record["reviewer"]).strip().lower() in FORBIDDEN_REVIEWERS:
            return None, "Noah cannot accept its own work"
        if record.get("implementer") and record["implementer"] == record["reviewer"]:
            return None, "reviewer implemented this change"
        if not re.fullmatch(r"[0-9a-f]{40}", str(record["revision"])):
            return None, "acceptance revision must be a full SHA"
        if set(item["finding_ids"]) - set(record["finding_ids"]):
            return None, "acceptance does not cover every finding"
        if item["review_class"] == "owner_required" and record["reviewer_role"] != "owner":
            return None, "this item needs the owner's acceptance"
        if record["decision"] != "accepted":
            return None, f"decision is {record['decision']!r}"
        return record, "accepted"
    return None, "no acceptance recorded"


def default_run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, timeout=120)


def integrated(record: dict, main_sha: str,
               run: Callable[[list[str]], subprocess.CompletedProcess[str]] = default_run) -> tuple[bool, str]:
    view = run(["gh", "pr", "view", str(record["pr"]), "--json", "state,mergeCommit,headRefOid"])
    if view.returncode:
        return False, "cannot read the PR (fail closed)"
    try:
        pr = json.loads(view.stdout)
    except ValueError:
        return False, "unreadable PR data"
    if pr.get("state") != "MERGED" or not (pr.get("mergeCommit") or {}).get("oid"):
        return False, f"PR #{record['pr']} is {pr.get('state')}"
    if pr.get("headRefOid") != record["revision"]:
        return False, "the merged revision is not the accepted revision"
    merge = pr["mergeCommit"]["oid"]
    ancestor = run(["git", "merge-base", "--is-ancestor", merge, main_sha])
    if ancestor.returncode:
        return False, "merge commit is not on main"
    return True, f"PR #{record['pr']} merged as {merge[:12]}"
