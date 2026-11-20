"""Boot one release's API against one database and say whether it served.

Part of the cross-version contract job (external audit 35-06). Run with the
interpreter of the release being booted (its venv has asyncpg and uvicorn):

    boot_probe.py --name P@C --app-dir <tree>/backend --port 5432 --db xv_candidate \
                  --expect-head 0181_x.sql --out boot-P@C.json --log-dir <dir>

The database passwords come from ORACLE_DB_PASSWORD and
ORACLE_DB_PLATFORM_PASSWORD in this process's environment; they are handed to
the server and never written anywhere.

The server runs with ORACLE_ENV=staging on purpose. Under dev, a pool or
cross-replica WebSocket-listener failure is logged and swallowed
(server.py lifespan); outside dev it aborts startup, so a schema the release
cannot use shows up as a boot that never answers. ORACLE_RECOVERY_MODE=1
blocks every outbound side effect; no provider key or Redis URL is set.

Two phases, each a fresh process:
  web     ORACLE_PROCESS_ROLE=web: /health answers 200 within the timeout,
          /version reports the expected migration head, an unauthenticated
          GET /api/commands answers 401, 403 or 422.
  worker  ORACLE_PROCESS_ROLE=worker: /health answers 200, then the process
          stays up for the hold with background work running, then SIGTERM.

The log of each phase is scanned for asyncpg exceptions whose SQLSTATE class
means the schema does not fit (contract hits) and for the two startup
failure messages. A boot that never came up with an ImportError or a
missing/weak-secret refusal in its log is a HARNESS problem (the probe's
environment, not the schema).

The result JSON: {"name", "ok", "harness", "checks": {...},
"contract_hits": [...], "harness_hits": [...]}. Exit status is 0 whenever the
result file was written; evaluate.py reads the file, not the status.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

CONTRACT_CLASSES = ("42", "23", "22", "21", "0A", "2B", "P0")
CONTRACT_MARKERS = ("DB pool init failed", "Production database initialization failed")
HARNESS_MARKERS = re.compile(
    r"\b(?:ImportError|ModuleNotFoundError)\b"
    r"|missing required production secret"
    r"|placeholder or weak production secret")
_ASYNCPG_NAME = re.compile(r"asyncpg\.exceptions\.([A-Z][A-Za-z0-9]+)")
AUTH_GATE_CODES = (401, 403, 422)
APP_LOGIN = "oracle_app_login"
PLATFORM_LOGIN = "oracle_platform_login"


def child_env(*, role: str, port: int, db: str, app_password: str,
              platform_password: str) -> dict[str, str]:
    """The complete environment the server gets. Nothing else is inherited
    except PATH/HOME/LANG, so no runner variable or credential leaks in."""
    env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "TMPDIR") if k in os.environ}
    env.update({
        "ORACLE_ENV": "staging",
        "ORACLE_PROCESS_ROLE": role,
        "ORACLE_RECOVERY_MODE": "1",
        "ORACLE_SKIP_DOTENV": "1",
        "ORACLE_ALERTS_ENABLED": "0",
        "LITELLM_LOCAL_MODEL_COST_MAP": "True",
        # litellm reads the working directory's .env on import otherwise.
        "LITELLM_MODE": "PRODUCTION",
        "PYTHONDONTWRITEBYTECODE": "1",
        # The four secrets validate_or_die requires outside dev, fresh per run.
        "ORACLE_SECRET_KEY": secrets.token_hex(32),
        "ORACLE_ENCRYPTION_MASTER_KEY": secrets.token_hex(32),
        "ORACLE_JWT_ISSUER": "ci-cross-version",
        "ORACLE_JWT_AUDIENCE": "ci-cross-version",
        "ORACLE_DB_HOST": "localhost",
        "ORACLE_DB_PORT": str(port),
        "ORACLE_DB_NAME": db,
        "ORACLE_DB_PASSWORD": app_password,
        "ORACLE_DB_PLATFORM_PASSWORD": platform_password,
        "ORACLE_DB_SSLMODE": "disable",
    })
    return env


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _get(url: str, timeout: float = 5.0) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 — loopback only
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read() or b""
    except (urllib.error.URLError, OSError, TimeoutError):
        return 0, b""


def scan_log(text: str, booted: bool) -> tuple[list[dict], list[str]]:
    """(contract hits, harness hits). Harness markers count only when the
    process never came up: a warning that mentions ImportError on a healthy
    server is not a broken environment."""
    try:
        import asyncpg.exceptions as pgexc
    except Exception:  # noqa: BLE001
        pgexc = None
    hits: list[dict] = []
    for line in text.splitlines():
        for name in _ASYNCPG_NAME.findall(line):
            state = getattr(getattr(pgexc, name, None), "sqlstate", None) if pgexc else None
            if isinstance(state, str) and state[:2] in CONTRACT_CLASSES:
                hits.append({"exception": name, "sqlstate": state, "line": line.strip()[:240]})
        for marker in CONTRACT_MARKERS:
            if marker in line:
                hits.append({"exception": None, "sqlstate": None, "line": line.strip()[:240]})
    harness = [] if booted else sorted({m.group(0) for m in HARNESS_MARKERS.finditer(text)})
    return hits, harness


def _wait_healthy(proc: subprocess.Popen, base: str, timeout: float) -> tuple[bool, int, float]:
    started = time.monotonic()
    status = 0
    while time.monotonic() - started < timeout:
        if proc.poll() is not None:
            return False, status, time.monotonic() - started
        status, _ = _get(base + "/health", timeout=3.0)
        if status == 200:
            return True, status, time.monotonic() - started
        time.sleep(1.0)
    return False, status, time.monotonic() - started


def _stop(proc: subprocess.Popen) -> int | None:
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=10)
    return proc.returncode


def run_phase(args, role: str, app_password: str, platform_password: str) -> dict:
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    log_path = Path(args.log_dir) / f"boot-{args.name}-{role}.log"
    env = child_env(role=role, port=args.port, db=args.db,
                    app_password=app_password, platform_password=platform_password)
    checks: dict = {}
    with open(log_path, "w", encoding="utf-8") as log:
        proc = subprocess.Popen(
            [args.python, "-m", "uvicorn", "server:app", "--host", "127.0.0.1",
             "--port", str(port)],
            cwd=args.app_dir, env=env, stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True)
        try:
            healthy, status, waited = _wait_healthy(proc, base, args.timeout)
            checks["health_200"] = {"ok": healthy, "last_status": status,
                                    "seconds": round(waited, 1)}
            if healthy and role == "web":
                code, body = _get(base + "/version")
                try:
                    head = json.loads(body or b"{}").get("migration_head")
                except ValueError:
                    head = None
                checks["version_head"] = {"ok": code == 200 and head == args.expect_head,
                                          "status": code, "reported": head,
                                          "expected": args.expect_head}
                code, _ = _get(base + "/api/commands")
                checks["commands_auth_gate"] = {"ok": code in AUTH_GATE_CODES, "status": code}
            if healthy and role == "worker":
                time.sleep(args.hold)
                checks["alive_after_hold"] = {"ok": proc.poll() is None,
                                              "hold_seconds": args.hold}
        finally:
            exit_code = _stop(proc)
    text = log_path.read_text(encoding="utf-8", errors="replace")
    hits, harness = scan_log(text, booted=checks["health_200"]["ok"])
    return {"role": role, "checks": checks, "exit_code": exit_code,
            "contract_hits": hits[:20], "harness_hits": harness, "log": log_path.name}


async def _login_roles(port: int, db: str, app_password: str, platform_password: str) -> dict:
    import asyncpg

    out = {}
    for login, password in ((APP_LOGIN, app_password), (PLATFORM_LOGIN, platform_password)):
        conn = await asyncpg.connect(host="localhost", port=port, user=login,
                                     password=password, database=db, ssl=False, timeout=10)
        try:
            row = await conn.fetchrow(
                "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
        finally:
            await conn.close()
        out[login] = {"ok": bool(row) and not row["rolsuper"] and not row["rolbypassrls"],
                      "rolsuper": bool(row and row["rolsuper"]),
                      "rolbypassrls": bool(row and row["rolbypassrls"])}
    return out


def probe(args) -> dict:
    app_password = os.environ.get("ORACLE_DB_PASSWORD", "")
    platform_password = os.environ.get("ORACLE_DB_PLATFORM_PASSWORD", "")
    result: dict = {"name": args.name, "db": args.db, "ok": False, "harness": False,
                    "checks": {}, "phases": {}, "contract_hits": [], "harness_hits": []}
    if not app_password or not platform_password:
        result["harness"] = True
        result["harness_hits"].append("probe started without database passwords")
        return result
    try:
        roles = asyncio.run(_login_roles(args.port, args.db, app_password, platform_password))
        result["checks"]["login_roles_rls_bound"] = {
            "ok": all(r["ok"] for r in roles.values()), "roles": roles}
    except Exception as exc:  # noqa: BLE001 — a login that fails is a finding, recorded
        result["checks"]["login_roles_rls_bound"] = {"ok": False, "error": type(exc).__name__}
    for role in ("web", "worker"):
        phase = run_phase(args, role, app_password, platform_password)
        result["phases"][role] = phase
        for name, check in phase["checks"].items():
            result["checks"][f"{role}.{name}"] = check
        result["contract_hits"].extend(phase["contract_hits"])
        result["harness_hits"].extend(phase["harness_hits"])
    result["harness"] = bool(result["harness_hits"])
    result["ok"] = (not result["harness"] and not result["contract_hits"]
                    and all(c.get("ok") for c in result["checks"].values()))
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--name", required=True)
    parser.add_argument("--app-dir", required=True)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--port", type=int, required=True, help="database port")
    parser.add_argument("--db", required=True)
    parser.add_argument("--expect-head", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--log-dir", required=True)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--hold", type=float, default=30.0)
    args = parser.parse_args(argv)
    try:
        result = probe(args)
    except Exception as exc:  # noqa: BLE001 — the probe itself broke: harness
        result = {"name": args.name, "ok": False, "harness": True, "checks": {},
                  "contract_hits": [], "harness_hits": [f"probe error: {type(exc).__name__}"]}
    Path(args.out).write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    print(f"boot {args.name}: {'ok' if result['ok'] else 'FAILED'}"
          f"{' (harness)' if result.get('harness') else ''}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
