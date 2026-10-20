"""Deterministic planning, evidence, and recovery controls for Noah."""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

PLAN = ".neoh-agent-state/plan.json"
MAX_FILES = 12
MAX_LINES = 1500
MAX_FILE_BYTES = 1024 * 1024


class InfrastructureFailure(RuntimeError):
    pass


def redact(text: str) -> str:
    text = re.sub(r"\b(?:sk-ant-|sk-|ghp_|github_pat_)[A-Za-z0-9_-]{12,}", "[REDACTED]", text)
    return re.sub(r"(?i)(authorization:\s*bearer|(?:api[_-]?key|password|token)\s*[=:])\s*\S+",
                  r"\1 [REDACTED]", text)


def task_identity(category: str, task: str, main_sha: str) -> str:
    return hashlib.sha256(json.dumps([category, task.strip(), main_sha]).encode()).hexdigest()[:20]


def risk_profile(category: str, task: str) -> dict:
    sensitive = category in {"03", "07", "13", "17", "18", "19"}
    sensitive |= bool(re.search(r"\b(auth|permission|billing|payment|migration|encryption)\b", task, re.I))
    return {
        "level": "high" if sensitive else "normal",
        "review": "tenant isolation, authorization, negative tests and rollback" if sensitive
                  else "regression behavior, edge cases and rollback",
    }


def local_file(root: Path, name: str) -> Path:
    if not isinstance(name, str) or not name or Path(name).is_absolute():
        raise ValueError("Expected a repository-relative path")
    path = root / name
    if any(part == ".." or part == ".git" or part.startswith(".env") for part in Path(name).parts):
        raise ValueError("Protected source path")
    if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Path escapes the checkout")
    return path


def read_plan(root: Path, category: str, paths: list[str]) -> dict:
    path = local_file(root, PLAN)
    if not path.is_file() or path.stat().st_size > 32768:
        raise ValueError(f"Write the bounded plan at {PLAN} before requesting tests")
    plan = json.loads(path.read_text())
    if not isinstance(plan, dict) or plan.get("category") != category:
        raise ValueError("Plan category must match the selected task")
    for field in ("goal", "rollback", "self_review"):
        if not isinstance(plan.get(field), str) or not 10 <= len(plan[field].strip()) <= 2000:
            raise ValueError(f"Plan needs a concrete {field} description")
    criteria = plan.get("acceptance")
    if not isinstance(criteria, list) or not 1 <= len(criteria) <= 8 or any(
        not isinstance(item, str) or not 10 <= len(item.strip()) <= 500 for item in criteria
    ):
        raise ValueError("Plan needs 1-8 explicit acceptance criteria")
    evidence = plan.get("evidence")
    if not isinstance(evidence, list) or not 1 <= len(evidence) <= 8:
        raise ValueError("Plan needs source evidence")
    for item in evidence:
        if not isinstance(item, dict):
            raise ValueError("Evidence must identify a source file and line")
        source = local_file(root, item.get("path"))
        line = item.get("line")
        if not source.is_file() or source.stat().st_size > MAX_FILE_BYTES:
            raise ValueError("Evidence file missing or too large")
        if type(line) is not int or line < 1 or line > len(source.read_bytes().splitlines()):
            raise ValueError("Evidence line does not exist")
    allowed = plan.get("files")
    if not isinstance(allowed, list) or not 1 <= len(allowed) <= MAX_FILES:
        raise ValueError("Plan must name at most 12 files")
    for name in allowed:
        local_file(root, name)
    if len(set(paths)) > MAX_FILES or set(paths) - set(allowed):
        raise ValueError("Changes exceed the declared file scope")
    return plan


def check_churn(root: Path, paths: list[str], numstat: str, untracked: list[str]) -> None:
    lines = 0
    for row in numstat.splitlines():
        added, removed, _name = row.split("\t", 2)
        if added == "-" or removed == "-":
            raise ValueError("Binary changes require manual review")
        lines += int(added) + int(removed)
    for name in paths:
        path = local_file(root, name)
        if path.exists() and path.stat().st_size > MAX_FILE_BYTES:
            raise ValueError("Changed file exceeds the 1 MiB review limit")
    for name in untracked:
        path = local_file(root, name)
        if not path.is_file():
            continue
        data = path.read_bytes()
        if b"\0" in data:
            raise ValueError("New binary files require manual review")
        lines += len(data.splitlines())
    if lines > MAX_LINES:
        raise ValueError("Change exceeds the 1500-line review limit; split the task")


def syntax_errors(root: Path, paths: list[str]) -> list[str]:
    errors = []
    for name in paths:
        path = local_file(root, name)
        if name.endswith(".py") and path.is_file():
            try:
                ast.parse(path.read_bytes(), filename=name)
            except (SyntaxError, ValueError) as error:
                errors.append(f"{name}:{getattr(error, 'lineno', 0)}: invalid Python syntax")
    return errors


def focused_tests(root: Path, paths: list[str]) -> dict[str, list[str]]:
    candidates = set()
    for name in paths:
        if name.startswith("backend/tests/test_") and name.endswith(".py"):
            candidates.add(name)
        elif name.startswith("backend/") and name.endswith(".py"):
            candidates.add(f"backend/tests/test_{Path(name).stem}.py")
        if name.startswith("oracle-app/src/"):
            path = Path(name)
            if re.search(r"\.(test|spec)\.[jt]sx?$", name):
                candidates.add(name)
            else:
                for suffix in (".test.js", ".test.jsx", ".test.ts", ".test.tsx"):
                    candidates.add(str(path.with_suffix("")) + suffix)
    result = {"backend": [], "frontend": []}
    for name in sorted(candidates):
        path = local_file(root, name)
        if not path.is_file():
            continue
        group = "backend" if name.startswith("backend/") else "frontend"
        result[group].append(name.split("/", 1)[1])
    return {group: names[:20] for group, names in result.items()}


def failure_kind(output: str, code: int) -> str:
    if re.search(r"No space left on device|\bENOSPC\b|\[Errno 28\]", output, re.I):
        return "disk"
    if "test_no_tracked_file_is_owner_only" in output and "unreadable" in output:
        return "permissions"
    if code == 124:
        return "timeout"
    return "test"


def failure_fingerprint(feedback: str) -> str:
    normalized = redact(feedback)
    normalized = re.sub(r"\b\d+(?:\.\d+)?\s*(?:seconds?|ms|s)\b", "<duration>", normalized)
    normalized = re.sub(r"cycle-\d+-\d+", "<cycle>", normalized)
    return hashlib.sha256(normalized.encode()).hexdigest()


def change_fingerprint(root: Path, paths: list[str]) -> str:
    digest = hashlib.sha256()
    for name in sorted(paths):
        path = local_file(root, name)
        digest.update(name.encode())
        if path.is_file():
            if path.stat().st_size > MAX_FILE_BYTES:
                raise ValueError("Changed file too large to fingerprint")
            digest.update(path.read_bytes())
        else:
            digest.update(b"<deleted>")
    return digest.hexdigest()


def recent_failures(history: list[dict], category: str) -> list[dict]:
    return [
        {key: entry.get(key) for key in ("category", "task_id", "exit_code", "failure_kind")}
        for entry in history
        if entry.get("category") == category and entry.get("status") == "failed"
    ][-3:]


def fresh_answers(answers, category: str, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    result = []
    if not isinstance(answers, list):
        return result
    for entry in answers[-100:]:
        if not isinstance(entry, dict) or entry.get("category") != category:
            continue
        try:
            answered = datetime.fromisoformat(entry["answered_at"])
            age = (now - answered).total_seconds()
        except (KeyError, TypeError, ValueError):
            continue
        if not 0 <= age <= 30 * 86400 or not isinstance(entry.get("answer"), str):
            continue
        result.append({key: redact(str(entry.get(key, "")))[:2000]
                       for key in ("category", "task", "blocker", "answer", "answered_at")})
    return result[-5:]


def save_report(root: Path, name: str, data: dict) -> None:
    directory = local_file(root, ".neoh-agent-state")
    directory.mkdir(mode=0o700, exist_ok=True)
    target = local_file(root, f".neoh-agent-state/{name}.json")
    temporary = target.with_suffix(".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump({"schema": 1, **data}, stream, indent=2)
        stream.write("\n")
    os.replace(temporary, target)
