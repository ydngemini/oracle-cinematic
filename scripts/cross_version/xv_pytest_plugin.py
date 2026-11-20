"""pytest plugin for the cross-version contract job (external audit 35-06).

Loaded with ``-p xv_pytest_plugin`` into the PREVIOUS release's own pytest,
from the CURRENT checkout (scripts/cross_version is put on PYTHONPATH), so it
works even when the previous tree predates it. It changes no outcome: it only
records, per test, what failed and where, so evaluate.py can tell a schema
incompatibility in the previous APP from a previous TEST that pins behaviour.

Inputs (environment):
  XV_PREVIOUS_BACKEND  the previous tree's backend/ directory
  XV_RESULTS           JSONL file to append rows to

Each row is one of:
  {"nodeid", "phase", "outcome": "passed"|"skipped"|"failed", ...}
  failed rows add {"kind": "incompatible"|"drift"|"infra", "sqlstate",
                   "log_sqlstates", "exc_type", "app_frame", "message"}
  {"type": "collect_error", "nodeid", "message"}
  {"type": "collect_skip", "nodeid", "message"}    a whole module skipped at
                                                   import: never silently "run"
  {"type": "totals", "collected", "passed", "failed", "skipped",
   "collect_errors", "collect_skipped", "exitstatus"}

Messages are cut to 300 characters and scrubbed of credentials: DSN user
info and the values of the database password variables never reach a row.
"""
from __future__ import annotations

import json
import os
import re
import traceback

import pytest

# SQLSTATE classes that mean "the statement no longer fits this schema":
# 42 syntax/access rule (undefined column, 42501 privilege), 23 integrity
# constraint, 22 data exception, 21 cardinality, 0A feature not supported,
# 2B dependent objects, P0 PL/pgSQL raise.
CONTRACT_CLASSES = ("42", "23", "22", "21", "0A", "2B", "P0")
# Connection, authorization, resources, operator intervention, system error.
INFRA_CLASSES = ("08", "28", "53", "57", "58")
TIMEOUT_SQLSTATE = "57014"  # query_canceled: statement_timeout

_ASYNCPG_NAME = re.compile(r"asyncpg\.exceptions\.([A-Z][A-Za-z0-9]+)")
_DSN_USERINFO = re.compile(r"(postgres(?:ql)?://)[^@\s/]+@", re.I)
_SECRET_ENV = ("ORACLE_DB_PASSWORD", "ORACLE_DB_PLATFORM_PASSWORD")


def _secrets() -> list[str]:
    values = [os.environ.get(name, "") for name in _SECRET_ENV]
    dsn = os.environ.get("ORACLE_LIVE_DB_ADMIN_DSN", "")
    found = re.match(r"postgres(?:ql)?://[^:@/\s]+:([^@/\s]+)@", dsn, re.I)
    if found:
        values.append(found.group(1))
    return [v for v in values if len(v) >= 4]


def scrub(text: str, secrets: list[str] | None = None) -> str:
    text = _DSN_USERINFO.sub(r"\1***@", text or "")
    for value in secrets if secrets is not None else _secrets():
        text = text.replace(value, "***")
    return text[:300]


def exception_chain(exc: BaseException | None) -> list[BaseException]:
    """exc, then every __cause__ / __context__ / group member, each once."""
    out: list[BaseException] = []
    seen: set[int] = set()
    queue = [exc]
    while queue:
        item = queue.pop(0)
        if item is None or id(item) in seen:
            continue
        seen.add(id(item))
        out.append(item)
        queue.extend([item.__cause__, item.__context__])
        if isinstance(item, BaseExceptionGroup):
            queue.extend(item.exceptions)
    return out


def sqlstate_of(chain: list[BaseException]) -> str | None:
    for item in chain:
        state = getattr(item, "sqlstate", None)
        if isinstance(state, str) and len(state) == 5:
            return state
    return None


def _is_app_path(path: str, backend: str) -> bool:
    """True only for a source file of the previous APP: an absolute path that
    exists, under backend/, outside tests/ and any virtualenv.

    Absolute, because compiled extensions report RELATIVE names: asyncpg's
    frames say `asyncpg/protocol/protocol.pyx`. Resolved against the working
    directory, which is the previous backend, such a name looked like app
    code, so every asyncpg error raised by a test's own SQL counted as an
    app frame (the canary of CI run 44741248526 passed for that reason)."""
    if not backend or not path or not os.path.isabs(path) or not os.path.isfile(path):
        return False
    real = os.path.realpath(path)
    root = os.path.realpath(backend) + os.sep
    if not real.startswith(root):
        return False
    rel = real[len(root):]
    parts = rel.split(os.sep)
    return parts[0] not in ("tests", "venv", ".venv") and "site-packages" not in parts


def app_frame(chain: list[BaseException], backend: str) -> str | None:
    """The deepest previous-APP frame (under backend/, not under tests/) on
    the first exception in the chain that passed through app code."""
    for item in chain:
        frames = [f for f in traceback.extract_tb(item.__traceback__)
                  if _is_app_path(f.filename, backend)]
        if frames:
            last = frames[-1]
            rel = os.path.relpath(os.path.realpath(last.filename), os.path.realpath(backend))
            return f"{rel}:{last.lineno} in {last.name}"
    return None


def log_sqlstates(sections: list[tuple[str, str]]) -> list[str]:
    """SQLSTATEs of asyncpg exceptions named in captured logs/output."""
    try:
        import asyncpg.exceptions as pgexc
    except Exception:  # noqa: BLE001 — the previous venv always has asyncpg
        return []
    states: list[str] = []
    for title, content in sections:
        if not title.startswith("Captured"):
            continue
        for name in _ASYNCPG_NAME.findall(content or ""):
            state = getattr(getattr(pgexc, name, None), "sqlstate", None)
            if isinstance(state, str) and state not in states:
                states.append(state)
    return states


def _is_assertion(exc: BaseException) -> bool:
    return isinstance(exc, (AssertionError, pytest.fail.Exception))


def classify(exc: BaseException, sections: list[tuple[str, str]], backend: str) -> dict:
    chain = exception_chain(exc)
    state = sqlstate_of(chain)
    frame = app_frame(chain, backend)
    logged = log_sqlstates(sections)
    logged_contract = [s for s in logged if s[:2] in CONTRACT_CLASSES]
    if state == TIMEOUT_SQLSTATE or TIMEOUT_SQLSTATE in logged:
        kind = "incompatible"            # evaluate.py requires it candidate-only
    elif state and state[:2] in CONTRACT_CLASSES and frame:
        kind = "incompatible"            # raised through a previous-app frame
    elif logged_contract:
        kind = "incompatible"            # logged by the app (swallowed error)
    elif frame and not _is_assertion(exc):
        kind = "incompatible"            # e.g. KeyError after a result shape changed
    elif _is_assertion(exc):
        kind = "drift"                   # assert / pytest.fail / DID NOT RAISE
    elif (state and state[:2] in INFRA_CLASSES) or isinstance(exc, (ConnectionError, TimeoutError)):
        kind = "infra"
    else:
        kind = "drift"                   # raised only from test frames
    return {
        "kind": kind,
        "sqlstate": state or (logged_contract[0] if logged_contract else None),
        "log_sqlstates": logged,
        "exc_type": f"{type(exc).__module__}.{type(exc).__qualname__}",
        "app_frame": frame,
        "message": scrub(str(exc).splitlines()[0] if str(exc) else ""),
    }


class _Recorder:
    def __init__(self, path: str, backend: str):
        self.path = path
        self.backend = backend
        self.status: dict[str, str] = {}
        self.collected = 0
        self.collect_errors = 0
        self.collect_skipped = 0

    def _write(self, row: dict) -> None:
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True) + "\n")

    def pytest_collectreport(self, report):
        if report.failed or report.skipped:
            lines = str(report.longrepr or "").splitlines() or [""]
            kind = "collect_error" if report.failed else "collect_skip"
            if report.failed:
                self.collect_errors += 1
            else:
                self.collect_skipped += 1
            self._write({"type": kind, "nodeid": report.nodeid, "message": scrub(lines[-1])})

    def pytest_collection_finish(self, session):
        self.collected = len(session.items)

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_makereport(self, item, call):
        outcome = yield
        report = outcome.get_result()
        row = {"nodeid": report.nodeid, "phase": report.when}
        if report.skipped:
            row["outcome"] = "skipped"
            self.status.setdefault(report.nodeid, "skipped")
        elif report.failed:
            row["outcome"] = "failed"
            exc = call.excinfo.value if call.excinfo is not None else AssertionError("failed")
            row.update(classify(exc, list(report.sections), self.backend))
            self.status[report.nodeid] = "failed"
        elif report.when == "call":
            row["outcome"] = "passed"
            self.status.setdefault(report.nodeid, "passed")
        else:
            return
        self._write(row)

    def pytest_sessionfinish(self, session, exitstatus):
        values = list(self.status.values())
        self._write({
            "type": "totals",
            "collected": self.collected,
            "passed": values.count("passed"),
            "failed": values.count("failed"),
            "skipped": values.count("skipped"),
            "collect_errors": self.collect_errors,
            "collect_skipped": self.collect_skipped,
            "exitstatus": int(exitstatus),
        })


def pytest_configure(config):
    path = os.environ.get("XV_RESULTS", "")
    if not path:
        raise pytest.UsageError("xv_pytest_plugin needs XV_RESULTS")
    config.pluginmanager.register(
        _Recorder(path, os.environ.get("XV_PREVIOUS_BACKEND", "")), "xv_recorder")
