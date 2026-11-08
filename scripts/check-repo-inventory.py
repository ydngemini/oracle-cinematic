#!/usr/bin/env python3
"""Build and check the file-level repository inventory (prompt 01, T01.01,
T01.13, T01.15, AC01.01; ledger L01-01).

    python scripts/check-repo-inventory.py generate [--sha HEAD]
        writes docs/audit/repo-inventory-<sha>.csv
    python scripts/check-repo-inventory.py check docs/audit/repo-inventory-<sha>.csv
        exit 0 only if it reconciles with `git ls-files` at that SHA

Every tracked file at the pinned SHA gets one row: path, extension, lines,
size, domain, last revision, class, the evidence for the class, the audit
state and the lines actually read.

**Class is decided by evidence, not by name** (T01.01):

* `production_entry`: a process the deployment starts, cited from the file
  that starts it (backend/Dockerfile CMD and infra/digitalocean/app.yaml for
  api, worker and migrate; the RunPod handler; index.html -> main.jsx).
* `runtime_dependency`: reached from an entry through the static import graph
  (Python `import` / `from` anywhere in a module, including function-local
  imports and importlib calls with a literal name; JS/JSX static imports,
  re-exports and literal dynamic `import()`), every SQL migration (applied
  by run_migrations.py), files under oracle-app/public (served as-is) and
  any file whose repository path appears literally in reachable code.
* `test`: collected by the runner's own rule (pytest `test_*.py` under a
  tests directory, vitest `*.test.*`), plus fixtures in tests directories.
* `infrastructure`: named by CI, a Dockerfile, the app spec, docker-compose
  or a package.json script, or in infra/ or .github/.
* `documentation`: Markdown and text outside the code trees.
* `optional_integration`: an operator script or module not reached from an
  entry, but named in documentation (run by hand).
* `archived_or_prototype`: the root Next.js prototype (README.md "What does
  not run").
* `orphaned`: none of the above found. A candidate, not a verdict: nothing
  is deleted on this evidence alone (F01.07).

**Lines read are never inferred** (T01.13). They come only from recorded
ranges in docs/audit/lines-read.csv (path, start, end, sha, reviewer, note).
A file with none is `listed`, not reviewed; files listed, files read and
lines read are reported as three separate numbers.
"""

from __future__ import annotations

import argparse
import ast
import csv
import io
import os
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import PurePosixPath

ROOT = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True,
                      check=True).stdout.strip()
LINES_READ = "docs/audit/lines-read.csv"

CLASSES = ("production_entry", "runtime_dependency", "test", "infrastructure", "documentation",
           "optional_integration", "archived_or_prototype", "orphaned")
AUDIT_STATES = ("listed", "partly_read", "read")
FIELDS = ("path", "ext", "lines", "bytes", "domain", "last_revision", "class", "class_evidence",
          "audit_state", "lines_read")

# Entry points, each with the file that starts it.
PY_ENTRIES = {
    "backend/server.py": "backend/Dockerfile CMD uvicorn server:app (api); app.yaml worker boots the same server.py",
    "backend/run_migrations.py": "infra/digitalocean/app.yaml migrate PRE_DEPLOY run_command",
    "infra/reconstruction-runpod/handler.py": "infra/reconstruction-runpod/Dockerfile CMD",
}
JS_ENTRIES = {
    "oracle-app/src/main.jsx": "oracle-app/index.html <script type=module src=/src/main.jsx>",
    "observability-platform/src/main.jsx": "observability-platform/index.html; image built by infra/scripts/build-images.sh",
}
#: Pages that load code for development only. What they reach and nothing
#: else reaches is `optional_integration`, not production.
DEV_JS_ENTRIES = {"oracle-app/src/dev/neohSpaceHarness.jsx": "oracle-app/neoh-space-harness.html (dev harness page)"}
#: Configuration a tool finds by its own file name (no other file names it).
TOOL_CONFIGS = {"eslint.config.js": "eslint", "eslint.config.mjs": "eslint", "vite.config.js": "vite",
                "vitest.config.js": "vitest", "pytest.ini": "pytest", "setup.cfg": "pytest/flake8",
                ".coderabbit.yaml": "CodeRabbit review bot", ".mcp.json": "Claude Code MCP servers",
                ".gitignore": "git", ".gitattributes": "git", ".dockerignore": "docker",
                ".bandit-baseline.json": "bandit (CI -b)", ".editorconfig": "editors",
                "postcss.config.js": "postcss", ".nvmrc": "nvm", ".python-version": "pyenv"}
OTHER_ENTRIES = {
    "oracle-app/index.html": "vite build input (oracle-app/Dockerfile, npm run build)",
    "observability-platform/index.html": "vite build input (observability-platform/Dockerfile)",
    "infra/reconstruction/run.sh": "infra/reconstruction/Dockerfile ENTRYPOINT",
    "infra/reconstruction-runpod/pipeline.sh": "infra/reconstruction-runpod/Dockerfile COPY, run by handler.py",
}
INFRA_REFERRERS = (".github/workflows/ci.yml", "backend/Dockerfile", "oracle-app/Dockerfile",
                   "infra/digitalocean/app.yaml", "docker-compose.yml", "oracle-app/package.json",
                   "package.json", "start.sh")
PROTOTYPE_ROOT = ("src/", "public/", "next.config.ts", "next-env.d.ts", "postcss.config.mjs",
                  "eslint.config.mjs", "tsconfig.json", "package.json", "package-lock.json")
JS_EXTS = (".js", ".jsx", ".ts", ".tsx", ".mjs")


def git(*args: str) -> str:
    return subprocess.run(["git", "-C", ROOT, *args], capture_output=True, text=True, check=True).stdout


def blob(sha: str, path: str) -> bytes:
    return subprocess.run(["git", "-C", ROOT, "show", f"{sha}:{path}"], capture_output=True,
                          check=True).stdout


def tracked(sha: str) -> dict[str, int]:
    """path -> size in bytes, for every blob at the SHA (symlinks included)."""
    out = {}
    for line in git("ls-tree", "-r", "-l", "--full-tree", sha).splitlines():
        meta, path = line.split("\t", 1)
        parts = meta.split()
        if parts[1] != "blob":
            continue
        out[path] = int(parts[3]) if parts[3].isdigit() else 0
    return out


def last_revisions(sha: str, paths: set[str]) -> dict[str, str]:
    """The newest commit at or before SHA that touched each path, in one walk."""
    found: dict[str, str] = {}
    current = None
    for line in git("log", "--format=@%h", "--name-only", sha).splitlines():
        if line.startswith("@"):
            current = line[1:]
        elif line and line in paths and line not in found:
            found[line] = current
        if len(found) == len(paths):
            break
    return found


# ── Python import graph ─────────────────────────────────────────────────────

def python_modules(paths: set[str], base: str) -> dict[str, str]:
    """dotted module name -> path, for .py files under `base`."""
    mods = {}
    for p in paths:
        if p.startswith(base + "/") and p.endswith(".py"):
            rel = p[len(base) + 1:-3]
            name = rel.replace("/", ".")
            if name.endswith(".__init__"):
                name = name[: -len(".__init__")]
            mods[name] = p
    return mods


def python_imports(source: str, package: str = "") -> set[str]:
    """Absolute module names a module imports. `package` is the importing
    module's package (dotted), used to resolve `from . import x` and
    `from .mod import y`."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                anchor = package.split(".") if package else []
                anchor = anchor[: len(anchor) - (node.level - 1)] if node.level > 1 else anchor
                module = ".".join([*anchor, *([node.module] if node.module else [])])
            else:
                module = node.module or ""
            if module:
                names.add(module)
                names.update(f"{module}.{a.name}" for a in node.names)
        elif isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant) \
                and isinstance(node.args[0].value, str):
            fn = node.func
            fname = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if fname in ("import_module", "__import__"):
                names.add(node.args[0].value)
    return names


def python_reachable(sha: str, paths: set[str], entries: list[str], base: str) -> dict[str, str]:
    """path -> the module that first imported it, from the entries."""
    mods = python_modules(paths, base)
    names = {v: k for k, v in mods.items()}
    reached = {e: "entry" for e in entries if e in paths}
    queue = list(reached)
    while queue:
        cur = queue.pop()
        mod = names.get(cur, "")
        package = mod if cur.endswith("/__init__.py") else mod.rpartition(".")[0]
        for name in python_imports(blob(sha, cur).decode("utf-8", "replace"), package):
            parts = name.split(".")
            for i in range(len(parts), 0, -1):  # a.b.c -> a.b.c, a.b, a (packages too)
                target = mods.get(".".join(parts[:i]))
                if target and target not in reached:
                    reached[target] = cur
                    queue.append(target)
    return reached


# ── JS import graph ─────────────────────────────────────────────────────────

_JS_IMPORT = re.compile(
    r"""(?:import|export)\s[^'"]*?from\s*['"]([^'"]+)['"]|import\s*['"]([^'"]+)['"]|import\(\s*['"]([^'"]+)['"]\s*\)""")


def js_reachable(sha: str, paths: set[str], entries: list[str]) -> dict[str, str]:
    reached = {e: "entry" for e in entries if e in paths}
    queue = list(reached)
    while queue:
        cur = queue.pop()
        if not cur.endswith(JS_EXTS):
            continue
        text = blob(sha, cur).decode("utf-8", "replace")
        for m in _JS_IMPORT.finditer(text):
            spec = next(g for g in m.groups() if g)
            if not spec.startswith("."):
                continue  # a package, not a repository file
            base = os.path.normpath(str(PurePosixPath(cur).parent / spec))
            for cand in (base, *(base + e for e in JS_EXTS),
                         *(f"{base}/index{e}" for e in JS_EXTS)):
                if cand in paths:
                    if cand not in reached:
                        reached[cand] = cur
                        queue.append(cand)
                    break
    return reached


# ── classification ──────────────────────────────────────────────────────────

def classify(sha: str, sizes: dict[str, int]) -> dict[str, tuple[str, str]]:
    paths = set(sizes)
    out: dict[str, tuple[str, str]] = {}

    py = python_reachable(sha, paths, [p for p in PY_ENTRIES if p.startswith("backend/")], "backend")
    py.update(python_reachable(sha, paths, ["infra/reconstruction-runpod/handler.py"],
                               "infra/reconstruction-runpod"))
    js = js_reachable(sha, paths, list(JS_ENTRIES))
    dev_js = {k: v for k, v in js_reachable(sha, paths, list(DEV_JS_ENTRIES)).items() if k not in js}

    texts_of_reachable = "\n".join(
        blob(sha, p).decode("utf-8", "replace") for p in sorted(set(py) | set(js)) if sizes[p] < 2_000_000)
    infra_text = "\n".join(blob(sha, p).decode("utf-8", "replace") for p in INFRA_REFERRERS if p in paths)
    docs = [p for p in paths if p.endswith(".md")]
    docs_text = "\n".join(blob(sha, p).decode("utf-8", "replace") for p in docs)

    basenames: dict[str, int] = defaultdict(int)
    for q in paths:
        basenames[PurePosixPath(q).name] += 1

    def named_in(text: str, path: str, quoted: bool = False) -> bool:
        """The full path, the path below its top folder, or — only when no
        other tracked file shares it — the bare file name. `quoted`: only as
        (the end of) a string literal, so a path mentioned in a comment is
        not counted as code that reads the file."""
        name = PurePosixPath(path).name
        tail = path.split("/", 1)[1] if "/" in path else path
        candidates = [path] + ([tail] if "/" in tail else [])
        if basenames[name] == 1 and len(name) > 6:
            candidates.append(name)
        for cand in candidates:
            if quoted:
                pattern = rf"""['"`/]{re.escape(cand)}['"`]"""
            elif cand == name:
                pattern = rf"(?<![\w./-]){re.escape(cand)}(?![\w-])"
            else:
                pattern = re.escape(cand)
            if re.search(pattern, text):
                return True
        return False

    for p in sorted(paths):
        name = PurePosixPath(p).name
        parts = p.split("/")
        if p in PY_ENTRIES:
            out[p] = ("production_entry", PY_ENTRIES[p])
        elif p in JS_ENTRIES:
            out[p] = ("production_entry", JS_ENTRIES[p])
        elif p in OTHER_ENTRIES:
            out[p] = ("production_entry", OTHER_ENTRIES[p])
        elif ("tests" in parts or "__tests__" in parts) and (name.startswith("test_") or name == "conftest.py"):
            out[p] = ("test", "collected by pytest (test_*.py / conftest.py in a tests directory)")
        elif re.search(r"\.(test|spec)\.(js|jsx|ts|tsx|mjs)$", name):
            out[p] = ("test", "collected by vitest (*.test.* / *.spec.*)")
        elif "tests" in parts or "fixtures" in parts:
            out[p] = ("test", "fixture or helper in a tests directory")
        elif p in py:
            out[p] = ("runtime_dependency", f"imported from {py[p]}")
        elif p in js:
            out[p] = ("runtime_dependency", f"imported from {js[p]}")
        elif p in DEV_JS_ENTRIES:
            out[p] = ("optional_integration", DEV_JS_ENTRIES[p])
        elif p in dev_js:
            out[p] = ("optional_integration", f"reached only from a dev harness, imported from {dev_js[p]}")
        elif name in TOOL_CONFIGS:
            out[p] = ("infrastructure", f"configuration found by name by {TOOL_CONFIGS[name]}")
        elif p.startswith("backend/db/migrations/") and p.endswith(".sql"):
            out[p] = ("runtime_dependency", "applied in order by backend/run_migrations.py")
        elif p.startswith("oracle-app/public/"):
            out[p] = ("runtime_dependency", "served as-is from oracle-app/public by the vite build")
        elif p in INFRA_REFERRERS or parts[0] in ("infra", ".github") or name in ("Dockerfile", ".dockerignore"):
            out[p] = ("infrastructure", "deployment or CI configuration")
        elif parts[0] not in ("backend", "oracle-app", "scripts") and (p in PROTOTYPE_ROOT or p.startswith(PROTOTYPE_ROOT)):
            out[p] = ("archived_or_prototype", "root Next.js prototype: README.md 'What does not run'")
        elif named_in(texts_of_reachable, p, quoted=True):
            out[p] = ("runtime_dependency", "its path is a string literal in reachable code")
        elif not p.endswith((".py", ".md")) and len(parts) > 2 and len(parts[-2]) > 4 and re.search(
                rf"""['"/]{re.escape(parts[-2])}['"/]""", texts_of_reachable):
            out[p] = ("runtime_dependency", f"its directory '{parts[-2]}' is read by reachable code")
        elif named_in(infra_text, p):
            out[p] = ("infrastructure", "named by CI, a Dockerfile, the app spec, compose or a package.json script")
        elif p.endswith((".md", ".txt", ".rst")) or parts[0] == "docs":
            out[p] = ("documentation", "documentation file")
        elif named_in(docs_text, p):
            out[p] = ("optional_integration", "not reached from an entry; named in documentation (run by hand)")
        else:
            out[p] = ("orphaned", "no entry, import, CI, infra or documentation reference found (candidate only)")
    return out


# ── lines read ──────────────────────────────────────────────────────────────

def read_ranges(sha: str) -> dict[str, list[tuple[int, int]]]:
    """Recorded line ranges, by path. Only ranges recorded at this SHA count."""
    path = os.path.join(ROOT, LINES_READ)
    ranges: dict[str, list[tuple[int, int]]] = defaultdict(list)
    if not os.path.exists(path):
        return ranges
    with open(path, newline="") as fh:
        for row in csv.DictReader(fh):
            if not sha.startswith(row["sha"]) and not row["sha"].startswith(sha):
                continue
            ranges[row["path"]].append((int(row["start"]), int(row["end"])))
    return ranges


def covered(ranges: list[tuple[int, int]], lines: int) -> int:
    seen: set[int] = set()
    for start, end in ranges:
        seen.update(range(max(1, start), min(end, lines) + 1))
    return len(seen)


def domain(path: str) -> str:
    parts = path.split("/")
    if parts[0] in ("backend", "oracle-app", "infra") and len(parts) > 2:
        return "/".join(parts[:2]) if parts[0] != "oracle-app" or parts[1] != "src" else "/".join(parts[:3])
    return parts[0] if len(parts) > 1 else "(root)"


# ── commands ────────────────────────────────────────────────────────────────

def generate(sha_arg: str) -> str:
    sha = git("rev-parse", "--short", sha_arg).strip()
    full = git("rev-parse", sha_arg).strip()
    sizes = tracked(full)
    classes = classify(full, sizes)
    revs = last_revisions(full, set(sizes))
    ranges = read_ranges(sha)
    rows = []
    for p in sorted(sizes):
        data = blob(full, p)
        lines = data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)
        if b"\0" in data[:8000]:
            lines = 0  # binary
        read = covered(ranges.get(p, []), lines)
        state = "listed" if read == 0 else ("read" if read >= lines else "partly_read")
        cls, evidence = classes[p]
        rows.append({"path": p, "ext": PurePosixPath(p).suffix or "(none)", "lines": lines,
                     "bytes": sizes[p], "domain": domain(p), "last_revision": revs.get(p, ""),
                     "class": cls, "class_evidence": evidence, "audit_state": state, "lines_read": read})
    out = os.path.join(ROOT, f"docs/audit/repo-inventory-{sha}.csv")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {os.path.relpath(out, ROOT)}: {len(rows)} rows")
    print(summary(rows))
    return out


def summary(rows: list[dict]) -> str:
    by_class: dict[str, int] = defaultdict(int)
    for r in rows:
        by_class[r["class"]] += 1
    files_read = sum(1 for r in rows if int(r["lines_read"]) > 0)
    lines_total = sum(int(r["lines"]) for r in rows)
    lines_read = sum(int(r["lines_read"]) for r in rows)
    return ("files listed: %d; files read: %d; lines read: %d of %d\n" % (len(rows), files_read, lines_read, lines_total)
            + "\n".join(f"  {c}: {by_class.get(c, 0)}" for c in CLASSES))


def check(csv_path: str) -> int:
    m = re.search(r"repo-inventory-([0-9a-f]{7,40})\.csv$", csv_path)
    if not m:
        print("inventory file name must be repo-inventory-<sha>.csv", file=sys.stderr)
        return 2
    sha = m.group(1)
    with open(csv_path, newline="") as fh:
        rows = list(csv.DictReader(fh))
    problems = []
    expected = set(git("ls-tree", "-r", "--name-only", "--full-tree", sha).splitlines())
    sizes = tracked(sha)
    expected = {p for p in expected if p in sizes}
    got = [r["path"] for r in rows]
    if len(got) != len(set(got)):
        problems.append("duplicate paths")
    missing, extra = expected - set(got), set(got) - expected
    if missing:
        problems.append(f"{len(missing)} tracked file(s) missing, e.g. {sorted(missing)[:3]}")
    if extra:
        problems.append(f"{len(extra)} row(s) not tracked at {sha}, e.g. {sorted(extra)[:3]}")
    ranges = read_ranges(sha)
    for r in rows:
        if r["class"] not in CLASSES:
            problems.append(f"{r['path']}: unknown class {r['class']!r}")
        if not r["class_evidence"]:
            problems.append(f"{r['path']}: class without evidence")
        if r["audit_state"] not in AUDIT_STATES:
            problems.append(f"{r['path']}: unknown audit state {r['audit_state']!r}")
        lines, read = int(r["lines"]), int(r["lines_read"])
        recorded = covered(ranges.get(r["path"], []), lines)
        if read != recorded:
            problems.append(f"{r['path']}: lines_read {read} but recorded ranges cover {recorded} (F01.06)")
        if read > lines:
            problems.append(f"{r['path']}: more lines read than it has")
        want = "listed" if read == 0 else ("read" if read >= lines else "partly_read")
        if r["audit_state"] != want:
            problems.append(f"{r['path']}: audit_state {r['audit_state']} but lines read say {want}")
    if problems:
        print(f"{csv_path}: {len(problems)} problem(s)", file=sys.stderr)
        for p in problems[:50]:
            print("  " + p, file=sys.stderr)
        return 1
    print(f"{os.path.relpath(csv_path, ROOT)} reconciles with git ls-tree at {sha}: {len(rows)} rows")
    print(summary(rows))
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate")
    g.add_argument("--sha", default="HEAD")
    c = sub.add_parser("check")
    c.add_argument("csv")
    args = ap.parse_args(argv)
    if args.cmd == "generate":
        generate(args.sha)
        return 0
    return check(args.csv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
