"""Find Neoh's next engineering task from a freshly fetched GitHub main commit."""
from __future__ import annotations

import re
import subprocess

import runner

PROMPTS = "docs/claude-prompts"
PRIORITY = ("02", "01", "03", "17", "04", "18", "19", "05", "06",
            "07", "08", "09", "10", "11", "13", "14", "16", "12", "15")
SHA_RE = re.compile(r"[0-9a-f]{40}$")
BRANCH_RE = re.compile(r"refs/heads/auto/neoh-(\d{2})-\d{8}T\d{6}Z-[0-9a-f]{6}$")
FILE_RE = re.compile(r"docs/claude-prompts/(\d{2})-[^/]+\.md$")
ORIGIN_RE = re.compile(
    r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
    r"ydngemini/oracle-cinematic(?:\.git)?", re.I)
OWNER_GATES = ("owner:", "owner decision", "owner decides", "counsel sign",
               "production app", "provider credentials", "purchase", "deploy production")


def git(*args: str, timeout: int = 120) -> str:
    result = subprocess.run(["git", "-C", str(runner.ROOT), *args],
                            capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"Git main refresh/inspection failed (exit {result.returncode}).")
    return result.stdout.strip()


def verify_origin() -> None:
    for args in (("remote", "get-url", "origin"),
                 ("remote", "get-url", "--push", "origin")):
        if not ORIGIN_RE.fullmatch(git(*args)):
            raise ValueError("Origin must point to ydngemini/oracle-cinematic on GitHub.")


def remote_main_sha() -> str:
    entries = git("ls-remote", "origin", "refs/heads/main").split()
    if (len(entries) != 2 or entries[1] != "refs/heads/main"
            or not SHA_RE.fullmatch(entries[0])):
        raise RuntimeError("Could not validate the current remote main SHA.")
    return entries[0]


def fetch_latest_main() -> tuple[str, list[str]]:
    verify_origin()
    old_head = git("rev-parse", "HEAD")
    git("fetch", "--no-tags", "--no-recurse-submodules", "origin",
        "+refs/heads/main:refs/remotes/origin/main", timeout=180)
    sha = git("rev-parse", "refs/remotes/origin/main")
    if sha != remote_main_sha():
        raise RuntimeError("Main changed while fetching. Retry with fresh instructions.")
    modified = git("diff", "--name-only", old_head, sha, "--", PROMPTS).splitlines()
    return sha, modified


def read_pinned(sha: str, path: str) -> str:
    if not SHA_RE.fullmatch(sha) or not path.startswith(PROMPTS + "/"):
        raise ValueError("Invalid pinned Neoh prompt path.")
    return git("show", f"{sha}:{path}")


def summary_rows(ledger: str) -> dict[str, dict[str, str]]:
    section = ledger.split("## Category summary\n", 1)
    if len(section) != 2:
        raise ValueError("LEDGER category summary missing.")
    summary = section[1].split("\n## ", 1)[0]
    result: dict[str, dict[str, str]] = {}
    for line in summary.splitlines():
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) >= 8 and cells[0] in {f"{n:02d}" for n in range(1,21)}:
            result[cells[0]] = {
                "status": cells[3].replace(chr(96), ""),
                "blockers": cells[6],
                "next": cells[7],
            }
    if len(result) != 20:
        raise ValueError("Expected 20 category rows in main LEDGER.")
    return result


def category_dependencies(readme: str) -> dict[str, list[str]]:
    section = readme.split("## The 20 categories\n", 1)
    if len(section) != 2:
        raise ValueError("Category index missing from README.")
    result: dict[str, list[str]] = {}
    for line in section[1].split("\n## ", 1)[0].splitlines():
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) >= 4 and re.fullmatch(r"\d{2}", cells[0]):
            if cells[0] == "20":
                result[cells[0]] = [f"{n:02d}" for n in range(1,20)]
            else:
                result[cells[0]] = re.findall(r"\b(?:0[1-9]|1[0-9]|20)\b", cells[3])
    if len(result) != 20:
        raise ValueError("Expected 20 indexed category dependencies.")
    return result


def pending_categories() -> set[str]:
    branches = git("ls-remote", "--heads", "origin", "refs/heads/auto/neoh-*")
    result = set()
    for line in branches.splitlines():
        fields = line.split()
        if len(fields) == 2:
            match = BRANCH_RE.fullmatch(fields[1])
            if match:
                result.add(match.group(1))
    return result


def select_work(sha: str, category: str = "auto", task: str = "",
                pending: set[str] | None = None) -> dict[str, str]:
    if task.strip() and category == "auto":
        raise ValueError("Use --category with --task, or omit both for self-directed work.")
    if category != "auto" and category not in PRIORITY:
        raise ValueError("Category must be auto or 01-19.")
    paths = git("ls-tree", "-r", "--name-only", sha, PROMPTS).splitlines()
    found: dict[str, str] = {}
    for path in paths:
        match = FILE_RE.fullmatch(path)
        if match:
            if match.group(1) in found:
                raise ValueError("Duplicate Neoh category file.")
            found[match.group(1)] = path
    if len(found) != 20:
        raise ValueError("The latest main must include exactly 20 category prompts.")
    for name in ("README.md", "CONTRACT.md", "LEDGER.md"):
        if f"{PROMPTS}/{name}" not in paths:
            raise ValueError(f"Missing critical Neoh instruction: {name}")
    readme = read_pinned(sha, f"{PROMPTS}/README.md")
    contract = read_pinned(sha, f"{PROMPTS}/CONTRACT.md")
    ledger = read_pinned(sha, f"{PROMPTS}/LEDGER.md")
    if "C4." not in contract or "C10." not in contract:
        raise ValueError("Neoh contract format changed; review before automated work.")
    rows = summary_rows(ledger)
    dependencies = category_dependencies(readme)
    busy = pending if pending is not None else pending_categories()
    for candidate in ((category,) if category != "auto" else PRIORITY):
        row = rows[candidate]
        if candidate in busy:
            continue
        if row["status"] in ("blocked", "external_configuration_required", "production_verified"):
            continue
        if row["next"] in ("", "—", "-"):
            continue
        if any(gate in row["next"].lower() for gate in OWNER_GATES):
            continue
        # Category 02 is explicitly permitted to begin without category 01.
        required = () if candidate == "02" else dependencies[candidate]
        if any(rows[dep]["status"] in ("not_inspected", "blocked", "external_configuration_required")
               for dep in required):
            continue
        if "Contract:" not in read_pinned(sha, found[candidate]):
            raise ValueError(f"Category {candidate} contract marker is missing.")
        return {"category": candidate, "task": task.strip() or row["next"],
                "main_sha": sha, "prompt": found[candidate]}
    raise ValueError("No safe actionable work in latest LEDGER. Review blockers or side branches.")
