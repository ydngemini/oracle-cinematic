"""Turn one cross-version run into a verdict, a record and an exit code.

Part of the cross-version contract job (external audit 35-06). Standard
library only; it imports the CURRENT backend/migration_safety.py to show the
classifier's verdict on the gap next to what actually happened. It never
changes that verdict: the classifier stays the only authority over
application-only rollback (DEP-05), and nothing here — no file, flag or
environment variable — can turn a failure into a pass.

    evaluate.py record  --work DIR --out cross-version-report.json [--summary FILE]
    evaluate.py trivial --result no_gap|no_previous --previous SHA --current SHA \
                        --reason TEXT --out FILE [--summary FILE] [--run-id N --run-attempt N]

Per failing previous test (first run on candidate C, reruns on a freshly
built candidate C' and on the control B, which has the previous schema only):
  UNREPRODUCED  passes on C'                      warning
  PRE-EXISTING  fails on B too                    warning
  ATTRIBUTABLE  fails on C and C', passes on B:
    INCOMPATIBLE  the plugin saw a schema SQLSTATE through a previous-app
                  frame or in the app's log, a non-assertion exception in an
                  app frame, or a statement timeout              exit 1
    DRIFT         an assertion, pytest.fail / DID NOT RAISE, or an error
                  raised only from test frames                   warning

Exit codes (normal run):
  3 DEPLOY_WOULD_FAIL  run_migrations.py --precheck-then-migrate, run as the
                       NOSUPERUSER BYPASSRLS owner, exited non-zero
  2 HARNESS            a dependency install or build failed, a boot check of
                       the previous app fails on its own schema (B), zero
                       tests were collected, every test skipped, pytest and
                       the plugin disagree, a rerun gave no answer, or this
                       script crashed
  1 INCOMPATIBLE       an attributable INCOMPATIBLE test (an attributable
                       infrastructure-class failure counts too: stricter), a
                       boot check of the previous app failing on C while it
                       passes on B, or a schema error the previous app logs on
                       C but not on B; likewise for the new app on C
  0 otherwise; contract_result is "pass" only with no failure, no skip and no
    schema error in any boot log (one the previous app also logs on B is a
    warning: not_proven).

Canary run: exits 0 only when the gate itself fails on the canary, that is,
contract_result is "incompatible" and at least one INCOMPATIBLE verdict (an
attributable test, or the P@C boot) carries SQLSTATE 23514. DRIFT alone is a
warning, not a failure, so it does not prove the gate can fail. Otherwise 1
("the gate cannot fail"), or 2 when the canary could not run at all.

The record never holds a password: DSN user info and `password=` values are
redacted, and so is every value of the run's own password variables.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import sys
from pathlib import Path

SCHEMA = 1
CANARY_SQLSTATE = "23514"
CANARY_FILE = "9999_xv_canary.sql"
SETUP_STEPS = ("venv", "build_control", "build_candidate")
BOOTS = ("P@B", "P@C", "N@C")
_SEVERITY = {"incompatible": 3, "infra": 2, "drift": 1}
_DSN = re.compile(r"(postgres(?:ql)?://)[^@\s/]+@", re.I)
_PASSWORD_KV = re.compile(r"(password\s*[=:]\s*)\S+", re.I)
# The run's passwords (cross-version-contract.sh exports them to this
# process). Read only to redact them; none of them affects a verdict.
SECRET_ENV = ("XV_SU_PW", "XV_DOADMIN_PW", "XV_APP_PW", "XV_PLATFORM_PW",
              "XV_PG_SUPERUSER_PASSWORD")


# ── inputs ────────────────────────────────────────────────────────────────

def read_jsonl(path: Path) -> list[dict] | None:
    if not path.exists():
        return None
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def load(work: Path) -> dict:
    steps: dict[str, int] = {}
    for row in read_jsonl(work / "steps.jsonl") or []:
        steps[row["step"]] = int(row["exit"])
    return {
        "meta": read_json(work / "meta.json") or {},
        "gap": read_json(work / "gap.json") or [],
        "steps": steps,
        "boots": {name: read_json(work / f"boot-{name}.json") for name in BOOTS},
        "run1": read_jsonl(work / "run1.jsonl"),
        "rerun_candidate": read_jsonl(work / "rerun-candidate.jsonl"),
        "rerun_control": read_jsonl(work / "rerun-control.jsonl"),
    }


def _migration_safety(repo: Path):
    spec = importlib.util.spec_from_file_location(
        "xv_migration_safety", repo / "backend" / "migration_safety.py")
    module = importlib.util.module_from_spec(spec)
    # Registered before it runs: its dataclasses resolve their (string)
    # annotations through sys.modules, and fail on a module missing there.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def describe_gap(entries: list[dict], repo: Path) -> tuple[list[dict], dict]:
    """Each gap file with its sha256 and the classifier's verdict, and the
    classifier's answer for the whole gap."""
    safety = _migration_safety(repo)
    out, verdicts, blocking_removed = [], [], []
    for entry in entries:
        name, status = entry["name"], entry["status"]
        item = {"name": name, "status": status, "sha256": None,
                "classification": None, "reasons": []}
        if entry.get("synthetic"):
            item["synthetic"] = True
        path = Path(entry["path"]) if entry.get("path") else repo / name
        if status == "D":
            if name.endswith(".sql"):
                item["classification"] = "removed"
                item["reasons"] = ["removed from the release; the PRE_DEPLOY precheck "
                                   "refuses a database that records it"]
                blocking_removed.append(f"{Path(name).name}: removed")
        elif path.exists():
            text = path.read_text(encoding="utf-8")
            item["sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
            if name.endswith(".sql"):
                verdict = safety.classify_sql(Path(name).name, text)
                verdicts.append(verdict)
                item["classification"] = verdict.classification
                item["reasons"] = list(verdict.reasons)
            else:
                item["reasons"] = ["runner change; not a migration, not classified"]
        out.append(item)
    safe, blocking = safety.rollback_is_safe(verdicts)
    sql_entries = [e for e in out if e["name"].endswith(".sql")]
    summary = {"rollback_safe": bool(safe and not blocking_removed),
               "blocking": blocking + blocking_removed,
               "whole_gap_additive": bool(sql_entries) and bool(safe) and not blocking_removed}
    return out, summary


def migration_head(directory: str | None) -> str | None:
    if not directory or not Path(directory).is_dir():
        return None
    names = sorted(p.name for p in Path(directory).glob("*.sql"))
    return names[-1] if names else None


# ── the decision ──────────────────────────────────────────────────────────

def _status_map(rows: list[dict] | None) -> dict[str, str]:
    status: dict[str, str] = {}
    for row in rows or []:
        if row.get("type"):
            continue
        node, outcome = row["nodeid"], row["outcome"]
        if outcome == "failed":
            status[node] = "failed"
        elif outcome == "skipped":
            status.setdefault(node, "skipped")
        elif outcome == "passed":
            status.setdefault(node, "passed")
    return status


def _signature(hit: dict) -> str:
    return hit.get("signature") or f"{hit.get('sqlstate')} {hit.get('exception') or hit.get('line', '')}"


def _failed_rows(rows: list[dict] | None, node: str) -> list[dict]:
    return [r for r in rows or [] if r.get("nodeid") == node and r.get("outcome") == "failed"]


def _totals(rows: list[dict] | None) -> dict | None:
    found = [r for r in rows or [] if r.get("type") == "totals"]
    return found[-1] if found else None


def decide(inputs: dict) -> tuple[dict, int]:
    """Pure: the parsed inputs of one run → (record fields, exit code)."""
    meta = inputs.get("meta") or {}
    steps = inputs.get("steps") or {}
    boots = inputs.get("boots") or {}
    run1 = inputs.get("run1")
    canary = bool(meta.get("canary"))
    harness: list[str] = []
    errors: list[str] = []
    warnings: list[str] = []
    tests: dict[str, dict] = {}
    deploy = {"command": "python run_migrations.py --precheck-then-migrate",
              "applied_as": "nosuperuser-bypassrls-owner", "exit": steps.get("deploy_rehearsal")}
    boot_result: dict[str, dict] = {}
    incompatible_boot = new_boot_failed = False
    pc_new_states: set = set()
    totals = _totals(run1)

    for step in SETUP_STEPS:
        if step not in steps:
            harness.append(f"setup step '{step}' did not run")
        elif steps[step] != 0:
            harness.append(f"setup step '{step}' failed (exit {steps[step]})")

    deploy_failed = False
    if not harness:
        if deploy["exit"] is None:
            harness.append("the deploy rehearsal did not run")
        elif deploy["exit"] != 0:
            deploy_failed = True
            errors.append(
                f"DEPLOY_WOULD_FAIL: run_migrations.py --precheck-then-migrate exited "
                f"{deploy['exit']} as the NOSUPERUSER BYPASSRLS owner — the staging "
                f"PRE_DEPLOY job would fail on this gap")
    deploy["result"] = (None if deploy["exit"] is None else
                        "ok" if deploy["exit"] == 0 else "failed")

    if not harness and not deploy_failed:
        for name in BOOTS:
            boot = boots.get(name)
            if boot is None:
                harness.append(f"boot {name} produced no result")
                continue
            boot_result[name] = {"ok": bool(boot.get("ok")), "harness": bool(boot.get("harness")),
                                 "contract_hits": list(boot.get("contract_hits") or []),
                                 "harness_hits": boot.get("harness_hits", []),
                                 "failed_checks": sorted(k for k, v in (boot.get("checks") or {}).items()
                                                         if not v.get("ok"))}
            if boot.get("harness"):
                harness.append(f"boot {name}: probe environment problem "
                               f"({', '.join(boot.get('harness_hits') or ['unknown'])})")
        pb, pc, nc = (boot_result.get(n) for n in BOOTS)
        # Log hits are attributed like tests: what the previous app also logs
        # on its OWN schema (B) is not this change. A failed check on B is.
        baseline = {_signature(h) for h in pb["contract_hits"]} if pb else set()
        for boot in (pc, nc):
            if boot:
                boot["new_hits"] = [h for h in boot["contract_hits"] if _signature(h) not in baseline]
        pb_usable = bool(pb) and not pb["harness"] and not pb["failed_checks"]
        if pb and not pb["harness"] and pb["failed_checks"]:
            harness.append("the previous app does not boot on its OWN schema (control B): "
                           + ", ".join(pb["failed_checks"]))
        elif pb_usable and baseline:
            warnings.append("not proven: the previous app logs schema errors on its own schema (B) too: "
                            + "; ".join(sorted(baseline))[:400])
        if pb_usable and pc and not pc["harness"]:
            if pc["failed_checks"] or pc["new_hits"]:
                incompatible_boot = True
                errors.append("INCOMPATIBLE: the previous app boots on its own schema but not on "
                              "the schema this change leaves behind (P@C): "
                              + ", ".join(pc["failed_checks"] + [_signature(h) for h in pc["new_hits"]]))
            elif not pc["ok"]:
                warnings.append("not proven: the previous app on the new schema (P@C) logs only what "
                                "it logs on its own schema (B)")
        if nc and not nc["harness"]:
            if nc["failed_checks"] or nc["new_hits"]:
                new_boot_failed = True
                errors.append("the new app does not boot cleanly on the incrementally upgraded schema "
                              "(N@C): " + ", ".join(nc["failed_checks"] + [_signature(h) for h in nc["new_hits"]]))
            elif not nc["ok"]:
                warnings.append("not proven: the new app on the upgraded schema (N@C) logs only what "
                                "the previous app logs on its own schema (B)")
        pc_new_states = {h.get("sqlstate") for h in (pc or {}).get("new_hits", [])}
        for boot in boot_result.values():          # the record keeps a sample, not the log
            boot["contract_hits"] = boot["contract_hits"][:10]
            if "new_hits" in boot:
                boot["new_hits"] = boot["new_hits"][:10]

        suite_rc = steps.get("suite")
        status = _status_map(run1)
        failing = sorted(n for n, s in status.items() if s == "failed")
        if suite_rc is None or totals is None:
            harness.append("the previous live suite did not finish")
        elif suite_rc == 5 or not totals.get("collected"):
            harness.append("zero previous live tests were collected")
        elif suite_rc not in (0, 1):
            harness.append(f"the previous live suite was interrupted (pytest exit {suite_rc})")
        elif totals.get("collect_errors"):
            harness.append(f"{totals['collect_errors']} previous live test file(s) failed to collect")
        elif totals.get("skipped", 0) >= totals.get("collected", 0):
            harness.append("every previous live test was skipped")
        elif len(status) != totals.get("collected"):
            harness.append(f"the plugin recorded {len(status)} of {totals.get('collected')} "
                           f"collected previous live tests")
        elif suite_rc == 1 and not failing:
            harness.append("pytest reported a failure the plugin did not record")
        elif suite_rc == 0 and failing:
            harness.append("the plugin recorded a failure pytest did not report")
        for row in run1 or []:
            if row.get("type") == "collect_skip":
                warnings.append(f"not proven: {row.get('nodeid')} was skipped at collection")

        rerun_c = _status_map(inputs.get("rerun_candidate"))
        rerun_b = _status_map(inputs.get("rerun_control"))
        if failing and not harness:
            for step in ("build_candidate_r", "deploy_rehearsal_r"):
                if steps.get(step) != 0:
                    harness.append(f"the fresh candidate C' for reruns was not built ({step})")
            for step in ("rerun_candidate", "rerun_control"):
                if steps.get(step) not in (0, 1):
                    harness.append(f"the {step.replace('_', ' ')} did not run cleanly "
                                   f"(pytest exit {steps.get(step)})")
        for node, state in sorted(status.items()):
            entry = {"verdict": state.upper(), "kind": None, "sqlstate": None,
                     "log_sqlstates": [], "app_frame": None, "exc_type": None, "message": None}
            if state == "skipped":
                warnings.append(f"not proven: {node} was skipped")
            if state == "failed":
                rows = _failed_rows(run1, node) + _failed_rows(inputs.get("rerun_candidate"), node)
                worst = max(rows, key=lambda r: _SEVERITY.get(r.get("kind"), 0))
                entry.update({k: worst.get(k) for k in
                              ("kind", "sqlstate", "app_frame", "exc_type", "message")})
                entry["log_sqlstates"] = sorted({s for r in rows for s in r.get("log_sqlstates") or []})
                c, b = rerun_c.get(node), rerun_b.get(node)
                if harness:
                    entry["verdict"] = "UNATTRIBUTED"
                elif c is None or b is None or "skipped" in (c, b):
                    harness.append(f"a rerun gave no pass/fail answer for {node}")
                    entry["verdict"] = "UNATTRIBUTED"
                elif c == "passed":
                    entry["verdict"] = "UNREPRODUCED"
                    warnings.append(f"not proven: {node} failed once on the candidate and "
                                    f"passed on a fresh one (flaky or order-dependent)")
                elif b == "failed":
                    entry["verdict"] = "PRE-EXISTING"
                    warnings.append(f"not proven: {node} also fails on the previous schema (B)")
                elif entry["kind"] in ("incompatible", "infra"):
                    entry["verdict"] = "INCOMPATIBLE"
                    errors.append(f"INCOMPATIBLE: {node} — {entry['exc_type']} "
                                  f"sqlstate={entry['sqlstate']} at {entry['app_frame'] or 'app log'}")
                else:
                    entry["verdict"] = "DRIFT"
                    warnings.append(f"not proven (DRIFT): {node} — the previous test's expectation "
                                    f"no longer holds on the new schema ({entry['exc_type']})")
            tests[node] = entry

    verdicts = [t["verdict"] for t in tests.values()]
    if deploy_failed:
        result, code = "deploy_would_fail", 3
    elif harness:
        result, code = "harness", 2
    elif incompatible_boot or "INCOMPATIBLE" in verdicts:
        result, code = "incompatible", 1
    elif new_boot_failed:
        result, code = "new_boot_failed", 1
    elif warnings or not tests:
        result, code = "not_proven", 0
    else:
        result, code = "pass", 0

    record = {
        "deploy_rehearsal": deploy,
        "boots": boot_result,
        "tests": tests,
        "totals": totals,
        "contract_result": result,
        "harness_reasons": harness,
        "errors": errors,
        "warnings": warnings,
        "has_drift": "DRIFT" in verdicts,
    }
    if canary:
        def _canary_hit(t: dict) -> bool:
            return t["sqlstate"] == CANARY_SQLSTATE or CANARY_SQLSTATE in t["log_sqlstates"]

        # Only what makes the gate FAIL counts: an INCOMPATIBLE verdict. A
        # DRIFT verdict is a warning (exit 0), so it proves nothing here.
        evidence = [n for n, t in tests.items() if t["verdict"] == "INCOMPATIBLE" and _canary_hit(t)]
        drift_only = [n for n, t in tests.items() if t["verdict"] == "DRIFT" and _canary_hit(t)]
        if incompatible_boot and CANARY_SQLSTATE in pc_new_states:
            evidence.append("boot P@C")
        if result in ("harness", "deploy_would_fail"):
            canary_code, verdict = 2, "the canary could not run, so the gate is unproven"
        elif evidence and result == "incompatible":
            canary_code, verdict = 0, (f"canary detected: {len(evidence)} attributable 23514 "
                                       f"incompatibility(ies); the gate fails on it")
        else:
            canary_code, verdict = 1, (
                "the gate cannot fail: the canary's CHECK (false) NOT VALID constraints produced no "
                "attributable 23514 INCOMPATIBLE verdict (no P@C boot hit, no incompatible test)"
                + (f" ({len(drift_only)} DRIFT-only 23514 failure(s) in previous tests: a warning, "
                   f"not a failure)" if drift_only else ""))
        record["canary_result"] = {"detected": canary_code == 0, "evidence": evidence[:20],
                                   "drift_only": drift_only[:20], "verdict": verdict}
        code = canary_code
    return record, code


# ── output ────────────────────────────────────────────────────────────────

def _secret_values() -> list[str]:
    return [v for v in (os.environ.get(name, "") for name in SECRET_ENV) if len(v) >= 4]


def redact(value, secrets: list[str] | None = None):
    secrets = _secret_values() if secrets is None else secrets
    if isinstance(value, str):
        text = _PASSWORD_KV.sub(r"\1***", _DSN.sub(r"\1***@", value))
        for secret in secrets:
            text = text.replace(secret, "***")
        return text
    if isinstance(value, list):
        return [redact(v, secrets) for v in value]
    if isinstance(value, dict):
        return {redact(k, secrets): redact(v, secrets) for k, v in value.items()}
    return value


def build_record(inputs: dict, repo: Path) -> tuple[dict, int]:
    meta = inputs.get("meta") or {}
    gap, classifier = describe_gap(inputs.get("gap") or [], repo)
    fields, code = decide(inputs)
    record = {
        "schema": SCHEMA,
        "mode": "canary" if meta.get("canary") else "gap",
        "canary": bool(meta.get("canary")),
        "previous_sha": meta.get("previous_sha"),
        "current_sha": meta.get("current_sha"),
        "prev_head": migration_head(meta.get("prev_migrations")),
        "new_head": migration_head(meta.get("new_migrations")),
        "gap": gap,
        "classifier": classifier,
        "classifier_miss": bool(classifier["whole_gap_additive"]
                                and (fields["contract_result"] == "incompatible"
                                     or fields["has_drift"])),
        "pg_server_version": meta.get("pg_server_version"),
        "migrations_applied_as": "nosuperuser-bypassrls-owner",
        "app_role": "oracle_app_login",
        "run_id": meta.get("run_id"),
        "run_attempt": meta.get("run_attempt"),
        **fields,
        "exit_code": code,
    }
    record["rollback_app_only_supported"] = bool(
        not record["canary"] and classifier["rollback_safe"] and record["contract_result"] == "pass")
    return redact(record), code


def summary_markdown(record: dict) -> str:
    prev = (record.get("previous_sha") or "")[:7]
    cur = (record.get("current_sha") or "")[:7]
    title = "Cross-version contract" + (" — CANARY self-test" if record.get("canary") else "")
    lines = [f"## {title}", ""]
    if record.get("classifier_miss"):
        lines += ["### CLASSIFIER MISS",
                  "migration_safety.py calls this whole gap ADDITIVE, yet the previous release "
                  "did not keep working on it. The classifier is unchanged and still decides "
                  "rollback; this is the evidence it missed something.", ""]
    lines.append(f"**Result: `{record['contract_result']}`** (exit {record['exit_code']}): "
                 f"previous `{prev}` on the schema `{cur}` leaves behind.")
    if record.get("canary_result"):
        lines.append(f"**Canary:** {record['canary_result']['verdict']}")
    lines += ["", "| check | result |", "|---|---|"]
    deploy = record.get("deploy_rehearsal") or {}
    lines.append(f"| PRE_DEPLOY rehearsal (`--precheck-then-migrate`, NOSUPERUSER BYPASSRLS owner) "
                 f"| {deploy.get('result')} (exit {deploy.get('exit')}) |")
    for name, boot in (record.get("boots") or {}).items():
        state = "ok" if boot["ok"] else ("harness" if boot["harness"] else "FAILED")
        logged = boot["new_hits"] if "new_hits" in boot else boot.get("contract_hits") or []
        detail = ", ".join((boot.get("failed_checks") or []) + [_signature(h) for h in logged])[:300]
        lines.append(f"| boot {name} | {state}{(': ' + detail) if detail else ''} |")
    totals = record.get("totals") or {}
    if totals:
        lines.append(f"| previous live suite on C | collected {totals.get('collected')}, passed "
                     f"{totals.get('passed')}, failed {totals.get('failed')}, skipped "
                     f"{totals.get('skipped')} |")
    bad = {n: t for n, t in (record.get("tests") or {}).items() if t["verdict"] != "PASSED"}
    if bad:
        lines += ["", "| previous test | verdict | kind | sqlstate | app frame |", "|---|---|---|---|---|"]
        for node, t in sorted(bad.items())[:60]:
            lines.append(f"| `{node}` | {t['verdict']} | {t['kind']} | {t['sqlstate']} | "
                         f"{t['app_frame'] or ''} |")
    if record.get("gap"):
        lines += ["", "| gap file | status | classifier |", "|---|---|---|"]
        for g in record["gap"]:
            lines.append(f"| `{g['name']}` | {g['status']} | {g['classification']} |")
    for reason in record.get("harness_reasons") or []:
        lines.append(f"\n- HARNESS: {reason}")
    lines.append("")
    return "\n".join(lines)


def _emit(record: dict, out: Path, summary: str | None) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    text = summary_markdown(record) if "exit_code" in record and "tests" in record else (
        f"## Cross-version contract\n\n**Result: `{record['contract_result']}`**: "
        f"{record.get('reason', '')}\n")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(text + "\n")
    print(text)


def main(argv: list[str] | None = None) -> int:
    """Any crash is a HARNESS failure (exit 2), never a pass and never a
    misleading 1."""
    try:
        return _main(argv)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        print(f"::error::HARNESS: cross-version evaluate crashed: {type(exc).__name__}")
        return 2


def _main(argv: list[str] | None) -> int:
    parser = argparse.ArgumentParser(description="cross-version contract verdict")
    sub = parser.add_subparsers(dest="command", required=True)
    rec = sub.add_parser("record")
    rec.add_argument("--work", required=True)
    rec.add_argument("--repo", required=True)
    rec.add_argument("--out", required=True)
    rec.add_argument("--summary")
    triv = sub.add_parser("trivial")
    triv.add_argument("--result", required=True, choices=("no_gap", "no_previous"))
    triv.add_argument("--previous", default="")
    triv.add_argument("--current", required=True)
    triv.add_argument("--reason", required=True)
    triv.add_argument("--out", required=True)
    triv.add_argument("--summary")
    triv.add_argument("--run-id", default="")
    triv.add_argument("--run-attempt", default="")
    args = parser.parse_args(argv)

    if args.command == "trivial":
        record = {"schema": SCHEMA, "mode": args.result, "canary": False,
                  "previous_sha": args.previous or None, "current_sha": args.current,
                  "gap": [], "contract_result": args.result, "reason": args.reason,
                  "run_id": args.run_id, "run_attempt": args.run_attempt,
                  "rollback_app_only_supported": None, "exit_code": 0}
        _emit(redact(record), Path(args.out), args.summary)
        return 0

    try:
        record, code = build_record(load(Path(args.work)), Path(args.repo))
    except Exception as exc:  # noqa: BLE001 — unreadable inputs are a harness failure
        print("::error::HARNESS: cross-version evaluate could not read its inputs: "
              + redact(f"{type(exc).__name__}: {exc}"))
        return 2
    for text in record.get("errors") or []:
        print(f"::error::{text}")
    for text in record.get("harness_reasons") or []:
        print(f"::error::HARNESS: {text}")
    for text in record.get("warnings") or []:
        print(f"::warning::{text}")
    if record.get("canary_result"):
        level = "notice" if record["canary_result"]["detected"] else "error"
        print(f"::{level}::{record['canary_result']['verdict']}")
    _emit(record, Path(args.out), args.summary)
    return code


if __name__ == "__main__":
    sys.exit(main())
