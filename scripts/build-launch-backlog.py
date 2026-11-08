#!/usr/bin/env python3
"""Build one dependency-ordered launch backlog from the ledger (prompt 20,
T20.02, AC20.02; ledger L20-02).

    python3 scripts/build-launch-backlog.py          # writes docs/claude-prompts/LAUNCH-BACKLOG.md
    python3 scripts/build-launch-backlog.py --check  # exit 1 if the file is stale or a row is malformed

Reads every fenced ```yaml row in docs/claude-prompts/LEDGER.md and the audit
cross-reference table (A01-A10). Every row that is not production_verified is
open — a tests_pass or staging row is never "done" (CONTRACT C12) — and appears
exactly once, under the README build order (P0 release truth -> release
gate), with its status, environment, who unblocks it and its next step.

Who unblocks it is read from the row, not guessed:
  owner        any blocker typed `owner` or `decided`-pending, or the next
               step naming the owner / an approval
  deploy       the next step or a blocker is a deploy / push approval
  device       a real device, phone, browser or real call is owed
  engineering  everything else
A row can need several; all are listed. The backlog is a reading aid: the
ledger stays the only status record (C10).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
LEDGER = REPO / "docs/claude-prompts/LEDGER.md"
OUT = REPO / "docs/claude-prompts/LAUNCH-BACKLOG.md"

#: README "Build order", as category numbers.
ORDER: list[tuple[str, tuple[str, ...]]] = [
    ("P0 release truth", ("01", "02")),
    ("P0 trust and permissions", ("17", "03", "04")),
    ("P0 business and operations", ("18", "19")),
    ("P1 shared intelligence", ("05", "06", "07", "08")),
    ("P1 revenue workflows", ("09", "10", "11", "13", "14")),
    ("P1/P2 experience", ("16", "12", "15")),
    ("Release gate", ("20",)),
]
REQUIRED = ("id", "status", "env", "next_task")
_HEAD = re.compile(r"^#### (L\d{2}-\d{2}) (.*)$", re.M)
_NEXT = re.compile(r"^#{2,4} ", re.M)
_YAML = re.compile(r"```yaml\n(.*?)\n```", re.S)
_OWNER = re.compile(r"\bowner\b|approval|decid", re.I)
_DEPLOY = re.compile(r"\bdeploy|\bpush\b.*approval|merge.*approval|C4\.4", re.I)
_DEVICE = re.compile(r"real (device|phone|call|browser|house|mailbox)|iphone|android|playwright|phone check", re.I)


def rows(text: str) -> list[dict]:
    out = []
    for m in _HEAD.finditer(text):
        rid, title = m.group(1), m.group(2).strip()
        # The row's own section: up to the next heading of any level.
        nxt = _NEXT.search(text, m.end())
        section = text[m.end(): nxt.start() if nxt else len(text)]
        block = _YAML.search(section)
        if not block:
            raise SystemExit(f"{rid}: no yaml block under its heading")
        body = block.group(1)
        try:
            data = yaml.safe_load(body) or {}
        except yaml.YAMLError as exc:
            raise SystemExit(f"{rid}: ledger yaml does not parse: {exc}") from None
        data["_title"] = title
        if str(data.get("id")) != rid:
            raise SystemExit(f"{rid}: heading and id disagree ({data.get('id')!r})")
        missing = [k for k in REQUIRED if k not in data]
        if missing:
            raise SystemExit(f"{rid}: missing {missing}")
        out.append(data)
    return out


def _blockers(row: dict) -> list[str]:
    out = []
    for b in row.get("blockers") or []:
        if isinstance(b, dict):
            out.append(f"{b.get('type', '')}: {b.get('note', '')}".strip(": "))
        else:
            out.append(str(b))
    return out


def unblockers(row: dict) -> list[str]:
    text = " ".join([str(row.get("next_task") or ""), *_blockers(row)])
    who = []
    typed = {str(b.get("type")) for b in row.get("blockers") or [] if isinstance(b, dict)}
    if "owner" in typed or _OWNER.search(text):
        who.append("owner")
    if _DEPLOY.search(text):
        who.append("deploy")
    if _DEVICE.search(text):
        who.append("device")
    if not who or str(row.get("status")) in ("source_verified", "implemented_untested", "not_inspected"):
        who.append("engineering")
    return list(dict.fromkeys(who))


def audit(text: str) -> list[tuple[str, str, str]]:
    section = text[text.index("## Audit cross-reference"):]
    out = []
    for line in section.splitlines():
        m = re.match(r"^\| (A\d{2}) \| (.*?) \| (.*?) \| (.*?) \|", line)
        if m:
            out.append((m.group(1), m.group(2), m.group(4)))
    return out


def _cell(value: object, limit: int = 240) -> str:
    text = " ".join(str(value or "").split()).replace("|", "/")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def build(text: str) -> str:
    all_rows = rows(text)
    open_rows = [r for r in all_rows if r.get("status") != "production_verified"]
    by_cat: dict[str, list[dict]] = {}
    for r in open_rows:
        by_cat.setdefault(str(r["id"])[1:3], []).append(r)
    counts: dict[str, int] = {}
    for r in open_rows:
        for w in unblockers(r):
            counts[w] = counts.get(w, 0) + 1
    lines = [
        "# Launch backlog (generated)",
        "",
        "Generated by `python3 scripts/build-launch-backlog.py` from [LEDGER.md](LEDGER.md); do not edit by hand. "
        "The ledger is the only status record (CONTRACT C10). Ledger: L20-02.",
        "",
        f"{len(all_rows)} ledger rows; **{len(open_rows)} open** (not `production_verified`; `production_verified` "
        f"rows: {len(all_rows) - len(open_rows)}). Who unblocks them: "
        + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) + ".",
        "",
    ]
    seen: set[str] = set()
    for phase, cats in ORDER:
        lines += [f"## {phase}", "", "| Row | Status | Env | Unblocked by | Next step | Blockers |", "|---|---|---|---|---|---|"]
        for cat in cats:
            for r in sorted(by_cat.get(cat, []), key=lambda r: r["id"]):
                seen.add(r["id"])
                lines.append(
                    f"| **{r['id']}** {_cell(r['_title'], 90)} | `{r['status']}` | {r['env']} | "
                    f"{', '.join(unblockers(r))} | {_cell(r.get('next_task'))} | {_cell('; '.join(_blockers(r)), 300)} |")
        lines.append("")
    leftover = [r for r in open_rows if r["id"] not in seen]
    if leftover:
        raise SystemExit(f"rows outside the build order: {[r['id'] for r in leftover]}")
    lines += ["## Audit findings A01-A10", "", "| ID | Finding | Status (ledger cross-reference) |", "|---|---|---|"]
    for aid, finding, status in audit(text):
        lines.append(f"| {aid} | {_cell(finding, 80)} | {_cell(status, 300)} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    text = LEDGER.read_text()
    out = build(text)
    if "--check" in argv:
        if not OUT.exists() or OUT.read_text() != out:
            print("LAUNCH-BACKLOG.md is stale: run python3 scripts/build-launch-backlog.py", file=sys.stderr)
            return 1
        print("LAUNCH-BACKLOG.md is current")
        return 0
    OUT.write_text(out)
    print(f"wrote {OUT.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
