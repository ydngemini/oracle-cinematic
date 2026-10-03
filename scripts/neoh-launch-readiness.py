#!/usr/bin/env python3
"""Neoh launch readiness: one command, one report.

    python3 scripts/neoh-launch-readiness.py --env production
    python3 scripts/neoh-launch-readiness.py --env staging --json out.json

Answers "could Brokerage #1 use this environment tomorrow?" for one
DigitalOcean environment, and says PASS / WARN / BLOCKED for every launch
requirement with a one-line reason.

NON-DESTRUCTIVE. It only ever runs read-only commands (`doctl ... list/get`,
`gh api` GETs, `gh secret list` — names only) and HTTPS GETs. It never creates,
updates, deploys, sends, or migrates anything, and needs no database access.

SECRETS ARE NEVER PRINTED. App Platform returns SECRET env values encrypted
(EV[...]) and `doctl databases list` returns connection passwords; this tool
reads only key NAMES and presence, and every line of output is additionally
scrubbed of every sensitive string it saw. Secret *strength* is enforced at
boot by config.validate_or_die (production refuses weak/placeholder secrets),
so a healthy /health is the evidence that it passed — this tool cannot and does
not look at values.

Classification (docs/launch-state.md, mission §6):
  BLOCKED  a requirement for real customers is unmet or cannot be shown to be
           met: no app/database, missing core secret, recovery mode on in
           production, security gate FAIL, worker absent, wrong release,
           billing missing while charging, an open owner blocker.
  WARN     an optional or not-yet-needed capability: optional providers, 3D,
           MLS awaiting licensing, alerts not routed, checks needing an
           operator token that was not given.
  PASS     shown to be met.

Exit code: 2 if anything is BLOCKED, 1 if only WARNs, 0 if all PASS.

Optional operator session (enables the operator-console checks — component
health, MLS feeds, billing exceptions): export NEOH_OPERATOR_TOKEN=<session
token>. It is sent as a Bearer header to --base-url only and never printed.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import importlib.util
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from typing import Any, Callable, Optional

REPO = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_REPO_SLUG = "ydngemini/oracle-cinematic"
TOOL_VERSION = "1"

PASS, WARN, BLOCKED = "PASS", "WARN", "BLOCKED"
_RANK = {PASS: 0, WARN: 1, BLOCKED: 2}

# What the rendered spec must look like, when render-app-spec.py cannot be
# imported (it needs PyYAML). Kept minimal; the renderer is the real source.
_FALLBACK_EXPECTED = {
    "production": {"name": "neoh", "region": "nyc",
                   "clusters": {"neoh-postgres": "neoh-postgres", "neoh-redis": "neoh-redis"},
                   "bucket": "neoh-media",
                   "sizes": {"api": (2, "apps-s-2vcpu-4gb"), "worker": (1, "apps-s-2vcpu-4gb"),
                             "web": (2, "apps-s-1vcpu-1gb")}},
    "staging": {"name": "neoh-staging", "region": "nyc",
                "clusters": {"neoh-postgres": "neoh-postgres-staging",
                             "neoh-redis": "neoh-redis-staging"},
                "bucket": "neoh-media-staging",
                "sizes": {"api": (2, "apps-s-1vcpu-1gb"), "worker": (1, "apps-s-1vcpu-2gb"),
                          "web": (1, "apps-s-1vcpu-0.5gb")}},
}

# Secrets a backend cannot run safely without. Missing any = BLOCKED.
CORE_SECRETS = {
    "api": ["ORACLE_SECRET_KEY", "ORACLE_ENCRYPTION_MASTER_KEY", "ORACLE_DB_PASSWORD",
            "ORACLE_DB_PLATFORM_PASSWORD", "REDIS_URL", "ORACLE_S3_ACCESS_KEY_ID",
            "ORACLE_S3_SECRET_ACCESS_KEY", "ORACLE_ADMIN_ID", "ORACLE_ADMIN_PASSPHRASE"],
    "worker": ["ORACLE_SECRET_KEY", "ORACLE_ENCRYPTION_MASTER_KEY", "ORACLE_DB_PASSWORD",
               "ORACLE_DB_PLATFORM_PASSWORD", "REDIS_URL", "ORACLE_S3_ACCESS_KEY_ID",
               "ORACLE_S3_SECRET_ACCESS_KEY"],
}
OPERATOR_2FA = ("ORACLE_ADMIN_TOTP_SECRET", "ORACLE_ADMIN_OTP_EMAIL")
BILLING_SECRETS = {"api": ["STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET", "STRIPE_PRICE_ID"],
                   "worker": ["STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET"]}
EMAIL_SECRETS = ["ORACLE_SMTP_HOST", "ORACLE_SMTP_USERNAME", "ORACLE_SMTP_PASSWORD"]
# Any ONE complete set configures the capability.
VOICE_SETS = [("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN"), ("PLIVO_AUTH_ID", "PLIVO_AUTH_TOKEN")]
MESSAGING_SETS = [("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN"), ("TELNYX_API_KEY",),
                  ("PLIVO_AUTH_ID", "PLIVO_AUTH_TOKEN")]
AI_SETS = [("ORACLE_FIREWORKS_API_KEY",), ("ORACLE_FOUNDRY_PROJECT_ENDPOINT",),
           ("BEDROCK_AWS_ACCESS_KEY_ID",)]
OPTIONAL_PROVIDERS = {
    "hosted SMS (Telnyx)": ("TELNYX_API_KEY", "TELNYX_PUBLIC_KEY"),
    "alternate voice carrier (Plivo)": ("PLIVO_AUTH_ID", "PLIVO_AUTH_TOKEN"),
    "Calendar / Google sign-in": ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"),
}
# The CI deploy jobs refuse to start without these on the GitHub environment
# (.github/workflows/ci.yml "Required secrets are present").
CI_ENV_SECRETS = ["DIGITALOCEAN_ACCESS_TOKEN", "DIGITALOCEAN_REGISTRY", "DIGITALOCEAN_APP_ID",
                  "ORACLE_DB_HOST", "ORACLE_DB_PORT", "ORACLE_DB_NAME", "ORACLE_DB_ADMIN_USER",
                  "ORACLE_DB_ADMIN_PASSWORD", "ORACLE_DB_PLATFORM_PASSWORD", "NEOH_PUBLIC_API_BASE"]

BACKUP_MAX_AGE_H = 26
_DEMO_FLAGS = ("ORACLE_ENABLE_DEMO_LOGINS",)
_DEMO_CREDS = ("ORACLE_DEMO_USER", "ORACLE_DEMO_PASS")
_TRUTHY = {"1", "true", "yes", "on"}


@dataclasses.dataclass
class Check:
    id: str
    area: str
    title: str
    status: str
    reason: str
    blocking: bool  # when unmet: BLOCKED (True) or WARN (False)

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


# ── I/O seams (replaced in tests) ──────────────────────────────────────────

Runner = Callable[[list], tuple]          # argv -> (returncode, stdout, stderr)
Http = Callable[[str, dict], tuple]       # url, headers -> (status, headers{lower: v}, body bytes)


def default_runner(argv: list) -> tuple:
    if shutil.which(argv[0]) is None:
        return 127, "", f"{argv[0]} not installed"
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return 124, "", "timed out"
    return p.returncode, p.stdout, p.stderr


def default_http(url: str, headers: dict) -> tuple:
    req = urllib.request.Request(url, headers={"User-Agent": "neoh-launch-readiness/1", **headers})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 — https GET only
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read(1_000_000)
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, e.read(200_000)
    except Exception as e:  # noqa: BLE001 — unreachable is an answer
        return 0, {}, type(e).__name__.encode()


# ── Redaction ──────────────────────────────────────────────────────────────

class Redactor:
    """Collects every sensitive string seen and scrubs it from output."""

    _KEYS = re.compile(r"(?i)password|secret|token|uri|private|key$|passphrase|connection")

    def __init__(self) -> None:
        self.values: set = set()

    def add(self, v: Any) -> None:
        if isinstance(v, str) and len(v) >= 6:
            self.values.add(v)

    def harvest(self, obj: Any, parent_key: str = "") -> None:
        """Record values under sensitive-looking keys, and every EV[...] value."""
        if isinstance(obj, dict):
            if "key" in obj and ("value" in obj or "type" in obj):
                # An app-spec env item: its NAME is printable, its value only
                # when it is plain config.
                v = obj.get("value")
                if obj.get("type") == "SECRET" or (isinstance(v, str) and v.startswith("EV[")):
                    self.add(v)
                return
            for k, v in obj.items():
                if isinstance(v, (dict, list)):
                    self.harvest(v, k)
                elif k != "key" and (self._KEYS.search(str(k)) or (isinstance(v, str) and v.startswith("EV["))):
                    self.add(v)
        elif isinstance(obj, list):
            for x in obj:
                self.harvest(x, parent_key)

    def scrub(self, text: str) -> str:
        for v in sorted(self.values, key=len, reverse=True):
            text = text.replace(v, "[redacted]")
        return re.sub(r"EV\[[^\]]*\]", "[redacted]", text)


# ── Helpers ────────────────────────────────────────────────────────────────

def _json(text: str) -> Any:
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001
        return None


def _components(spec: dict) -> dict:
    out = {}
    for kind in ("services", "workers", "static_sites", "jobs"):
        for c in spec.get(kind) or []:
            out[c.get("name")] = {**c, "_kind": kind}
    return out


def _envs(comp: dict) -> dict:
    """key -> (present: bool, plain value or None). Never keeps secret values."""
    out = {}
    for e in comp.get("envs") or []:
        v = e.get("value")
        present = v not in (None, "")
        plain = None if e.get("type") == "SECRET" else v
        out[e.get("key")] = (present, plain)
    return out


def _all_envs(spec: dict, names=("api", "worker")) -> dict:
    comps = _components(spec)
    return {n: _envs(comps[n]) for n in names if n in comps}


def _present(envs: dict, key: str) -> bool:
    return bool(envs.get(key, (False, None))[0])


def _parse_time(s: str) -> Optional[dt.datetime]:
    try:
        return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return None


def load_expected(env: str) -> dict:
    """Expected shape, from the renderer itself when it can be imported."""
    exp = json.loads(json.dumps(_FALLBACK_EXPECTED[env]))
    try:
        spec_ = importlib.util.spec_from_file_location("render_app_spec", REPO / "scripts" / "render-app-spec.py")
        r = importlib.util.module_from_spec(spec_)
        spec_.loader.exec_module(r)
        fake = "sha256:" + "0" * 64
        rendered = r.render(env, fake, fake)
    except Exception:  # noqa: BLE001 — PyYAML absent: use the fallback
        return exp
    comps = _components(rendered)
    exp["name"] = rendered.get("name")
    exp["region"] = rendered.get("region")
    exp["clusters"] = {d["name"]: d.get("cluster_name") for d in rendered.get("databases") or []}
    exp["sizes"] = {n: (c.get("instance_count"), c.get("instance_size_slug")) for n, c in comps.items()}
    exp["secret_slots"] = {n: sorted(e["key"] for e in c.get("envs") or [] if e.get("type") == "SECRET")
                           for n, c in comps.items()}
    return exp


# ── The audit ──────────────────────────────────────────────────────────────

class Audit:
    def __init__(self, env: str, *, base_url: str = "", token: str = "", manifest: Optional[dict] = None,
                 repo_slug: str = DEFAULT_REPO_SLUG, charging: bool = True,
                 runner: Runner = default_runner, http: Http = default_http,
                 now: Optional[dt.datetime] = None, repo_root: pathlib.Path = REPO) -> None:
        self.env = env
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.manifest = manifest
        self.repo_slug = repo_slug
        self.charging = charging
        self.run = runner
        self.http = http
        self.now = now or dt.datetime.now(dt.timezone.utc)
        self.root = repo_root
        self.red = Redactor()
        self.red.add(token)
        self.checks: list = []
        self.expected = load_expected(env)
        self.app: Optional[dict] = None
        self.spec: Optional[dict] = None
        self.dbs: list = []
        self.version: Optional[dict] = None

    # recording
    def add(self, id_, area, title, ok, reason, *, blocking=True, warn=False):
        """ok=True → PASS; warn=True → WARN regardless; else BLOCKED/WARN by class."""
        if warn:
            status = WARN
        elif ok:
            status = PASS
        else:
            status = BLOCKED if blocking else WARN
        self.checks.append(Check(id_, area, title, status, self.red.scrub(reason), blocking))

    # data
    def _cmd_json(self, argv):
        rc, out, _ = self.run(argv)
        data = _json(out) if rc == 0 else None
        if data is not None:
            self.red.harvest(data)
        return rc, data

    def _get(self, path, auth=False, extra=None):
        if not self.base_url:
            return None, {}, None
        headers = dict(extra or {})
        if auth and self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        status, hdrs, body = self.http(self.base_url + path, headers)
        return status, hdrs, _json(body.decode("utf-8", "replace") if isinstance(body, bytes) else body)

    # ── checks ──
    def run_all(self) -> list:
        self.check_doctl()
        self.check_app()
        self.check_spec_shape()
        self.check_images()
        self.check_databases()
        self.check_registry()
        self.check_spaces()
        self.check_secrets()
        self.check_safety_flags()
        self.check_providers()
        self.check_live()
        self.check_operator_console()
        self.check_backups()
        self.check_github()
        self.check_gates()
        return self.checks

    def check_doctl(self):
        rc, data = self._cmd_json(["doctl", "account", "get", "-o", "json"])
        self.doctl_ok = rc == 0
        self.add("doctl_access", "platform", "DigitalOcean account readable", rc == 0,
                 "doctl authenticated" if rc == 0 else
                 "doctl is missing or not authenticated — nothing on DigitalOcean can be verified")

    def check_app(self):
        name = self.expected["name"]
        if not self.doctl_ok:
            self.add("app_exists", "platform", f"App '{name}' exists", False, "not verifiable: no doctl access")
            return
        rc, apps = self._cmd_json(["doctl", "apps", "list", "-o", "json"])
        apps = apps or []
        match = [a for a in apps if (a.get("spec") or {}).get("name") == name]
        if not match:
            others = sorted({(a.get("spec") or {}).get("name") for a in apps} - {None})
            self.add("app_exists", "platform", f"App '{name}' exists", False,
                     f"no App Platform app named '{name}' — this environment has never been "
                     f"deployed ({len(apps)} app(s) on the account"
                     + (f": {', '.join(others)}" if others else "") + ")")
            return
        self.app = match[0]
        app_id = self.app.get("id", "")
        rc, spec = self._cmd_json(["doctl", "apps", "spec", "get", app_id, "--format", "json"])
        self.spec = spec if isinstance(spec, dict) else (self.app.get("spec") or {})
        if not self.base_url:
            live = self.app.get("live_url") or ""
            if live.startswith("https://"):
                self.base_url = live.rstrip("/")
        phase = ((self.app.get("active_deployment") or {}).get("phase") or "").upper()
        self.add("app_exists", "platform", f"App '{name}' exists", True,
                 f"app {app_id[:8]}…, active deployment phase {phase or 'none'}")

    def _need_spec(self, id_, area, title, blocking=True) -> bool:
        if self.spec is None:
            self.add(id_, area, title, False, "not verifiable: the app does not exist", blocking=blocking)
            return False
        return True

    def check_spec_shape(self):
        if not self._need_spec("region", "platform", "Region"):
            for i, t in (("components", "Expected components api/web/worker"),
                         ("api_replicas", "API replicas"), ("worker_count", "Exactly one worker")):
                self._need_spec(i, "platform", t)
            return
        region = self.spec.get("region") or ((self.app or {}).get("region") or {}).get("slug")
        self.add("region", "platform", "Region", region == self.expected["region"],
                 f"region {region!r} (expected {self.expected['region']!r})")
        comps = _components(self.spec)
        want = {"api": "services", "web": "services", "worker": "workers"}
        missing = [n for n in want if n not in comps]
        wrong = [n for n, k in want.items() if n in comps and comps[n]["_kind"] != k]
        self.add("components", "platform", "Expected components api/web/worker",
                 not missing and not wrong,
                 "api, web (services) and worker (worker) present" if not (missing or wrong) else
                 f"missing: {', '.join(missing) or '-'}; wrong kind: {', '.join(wrong) or '-'}")
        exp_api = (self.expected["sizes"].get("api") or (2, None))[0] or 2
        api_n = int((comps.get("api") or {}).get("instance_count") or 0)
        self.add("api_replicas", "platform", "API replicas", api_n >= max(2, exp_api),
                 f"api runs {api_n} instance(s); needs ≥{max(2, exp_api)} (no single point of failure, "
                 f"cross-replica fan-out exercised)")
        w_n = int((comps.get("worker") or {}).get("instance_count") or 0) if "worker" in comps else 0
        self.add("worker_count", "platform", "Exactly one worker", w_n == 1,
                 f"worker instance_count {w_n}; must be exactly 1 (voice/reconstruction queues are "
                 f"in-process — a second worker silently loses work; zero runs no background work)")
        drift = []
        for n, (cnt, size) in self.expected["sizes"].items():
            c = comps.get(n)
            if c and (c.get("instance_size_slug") != size or int(c.get("instance_count") or 0) != cnt):
                drift.append(f"{n} {c.get('instance_count')}×{c.get('instance_size_slug')} "
                             f"(rendered: {cnt}×{size})")
        self.add("sizing", "platform", "Sizing matches the rendered spec", not drift,
                 "every component matches render-app-spec.py" if not drift else "; ".join(drift),
                 blocking=False)

    def check_images(self):
        if not self._need_spec("images_pinned", "release", "Images pinned by digest"):
            return
        comps = _components(self.spec)
        bad, digests = [], {}
        for n in ("api", "worker", "web"):
            img = (comps.get(n) or {}).get("image") or {}
            d = img.get("digest") or ""
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", d) or img.get("registry_type") != "DOCR":
                bad.append(n)
            digests[n] = d
        same = digests.get("api") == digests.get("worker")
        self.add("images_pinned", "release", "Images pinned by digest", not bad and same,
                 "api/worker/web pinned to DOCR digests; api and worker run the same image"
                 if not bad and same else
                 (f"not digest-pinned: {', '.join(bad)}" if bad else "api and worker run DIFFERENT images"))
        if self.manifest:
            ok = (digests.get("api") == self.manifest.get("backend_digest")
                  and digests.get("web") == self.manifest.get("frontend_digest"))
            self.add("release_manifest", "release", "Running digests are the intended release", ok,
                     f"spec digests {'match' if ok else 'DO NOT match'} release manifest "
                     f"{str(self.manifest.get('git_sha', '?'))[:12]}")

    def check_databases(self):
        if self.doctl_ok:
            _, dbs = self._cmd_json(["doctl", "databases", "list", "-o", "json"])
            self.dbs = dbs or []
        by_name = {d.get("name"): d for d in self.dbs}
        for logical, engine, label in (("neoh-postgres", "pg", "Postgres"), ("neoh-redis", "valkey", "Valkey")):
            cluster = self.expected["clusters"].get(logical)
            c = by_name.get(cluster)
            attached = None
            if self.spec is not None:
                for d in self.spec.get("databases") or []:
                    if d.get("name") == logical:
                        attached = d.get("cluster_name")
            parts, ok = [], True
            if not self.doctl_ok:
                ok = False
                parts.append("not verifiable: no doctl access")
            elif c is None:
                ok = False
                parts.append(f"cluster '{cluster}' does not exist")
            else:
                status = str(c.get("status") or "")
                if status != "online":
                    ok = False
                parts.append(f"cluster '{cluster}' {status} ({c.get('engine')} {c.get('version')}, "
                             f"{c.get('size')}, {c.get('region')})")
                if engine == "pg" and str(c.get("version")) != "16":
                    ok = False
                    parts.append("expected Postgres 16")
            if self.spec is None:
                ok = False
                parts.append("not attached: the app does not exist")
            elif attached != cluster:
                ok = False
                parts.append(f"app attaches {attached!r}, expected {cluster!r}")
            else:
                parts.append("attached to the app")
            self.add(f"{label.lower()}_attached", "data", f"{label} exists, online, attached", ok, "; ".join(parts))
            if c is not None and self.doctl_ok:
                rc, rules = self._cmd_json(["doctl", "databases", "firewalls", "list", c.get("id", ""), "-o", "json"])
                n = len(rules or []) if rc == 0 else None
                self.add(f"{label.lower()}_trusted_sources", "security", f"{label} trusted sources",
                         bool(n), f"{n} trusted-source rule(s)" if n else
                         ("no trusted sources — reachable from any IP with the password; restrict to the app"
                          if rc == 0 else "could not read firewall rules"), blocking=self.env == "production")

    def check_registry(self):
        if not self.doctl_ok:
            self.add("registry", "release", "Container registry", False, "not verifiable: no doctl access")
            return
        rc, reg = self._cmd_json(["doctl", "registry", "get", "-o", "json"])
        name = (reg[0] if isinstance(reg, list) and reg else reg or {}).get("name") if rc == 0 else None
        if not name:
            self.add("registry", "release", "Container registry", False, "no DigitalOcean container registry")
            return
        rc, repos = self._cmd_json(["doctl", "registry", "repository", "list-v2", "-o", "json"])
        have = {r.get("name") for r in repos or []}
        missing = sorted({"neoh-backend", "neoh-frontend"} - have)
        self.add("registry", "release", "Container registry", not missing,
                 f"registry '{name}'; " + ("neoh-backend and neoh-frontend pushed" if not missing
                                           else f"no image pushed yet for: {', '.join(missing)}"))

    def check_spaces(self):
        if not self._need_spec("spaces", "data", "Spaces object storage configured"):
            return
        envs = _all_envs(self.spec)
        problems = []
        for comp, e in envs.items():
            if e.get("ORACLE_STORAGE_BACKEND", (0, None))[1] != "s3":
                problems.append(f"{comp}: ORACLE_STORAGE_BACKEND≠s3")
            if e.get("ORACLE_S3_BUCKET", (0, None))[1] != self.expected["bucket"]:
                problems.append(f"{comp}: bucket {e.get('ORACLE_S3_BUCKET', (0, None))[1]!r}≠{self.expected['bucket']!r}")
            for k in ("ORACLE_S3_ENDPOINT_URL", "ORACLE_S3_ACCESS_KEY_ID", "ORACLE_S3_SECRET_ACCESS_KEY"):
                if not _present(e, k):
                    problems.append(f"{comp}: {k} unset")
        self.add("spaces", "data", "Spaces object storage configured", not problems,
                 f"bucket {self.expected['bucket']} configured on api and worker (privacy/listing is "
                 f"checked by smoke-test.sh)" if not problems else "; ".join(problems))

    def check_secrets(self):
        if not self._need_spec("core_secrets", "security", "Core secrets present"):
            return
        envs = _all_envs(self.spec)
        missing = [f"{c}:{k}" for c, keys in CORE_SECRETS.items() for k in keys
                   if c in envs and not _present(envs[c], k)]
        if "api" in envs and not any(_present(envs["api"], k) for k in OPERATOR_2FA):
            missing.append("api:ORACLE_ADMIN_TOTP_SECRET|ORACLE_ADMIN_OTP_EMAIL")
        self.add("core_secrets", "security", "Core secrets present", not missing,
                 "all core secrets set (names checked; values never read — strength is enforced at "
                 "boot by config.validate_or_die)" if not missing else f"unset: {', '.join(missing)}")
        slots = self.expected.get("secret_slots") or {}
        absent_slots = [f"{c}:{k}" for c, keys in slots.items() if c in envs for k in keys
                        if k not in envs[c]]
        if absent_slots:
            self.add("spec_secret_slots", "release", "Running spec has every secret slot", False,
                     f"running app lacks keys the repo spec declares (next deploy adds them, unset): "
                     f"{', '.join(absent_slots)}", blocking=False)

    def check_safety_flags(self):
        if not self._need_spec("recovery_mode", "safety", "Recovery mode"):
            self._need_spec("demo_login_off", "safety", "Demo login off")
            self._need_spec("oracle_env", "safety", "ORACLE_ENV")
            self._need_spec("cors", "security", "CORS origins")
            return
        envs = _all_envs(self.spec)
        rm = {c: str(e.get("ORACLE_RECOVERY_MODE", (0, None))[1] or "") for c, e in envs.items()}
        if self.env == "production":
            on = [c for c, v in rm.items() if v]
            self.add("recovery_mode", "safety", "Recovery mode OFF in production", not on,
                     "off on api and worker" if not on else
                     f"ORACLE_RECOVERY_MODE set on {', '.join(on)} — production silently refuses every "
                     f"call, text, email and charge")
        else:
            off = [c for c, v in rm.items() if v != "1"]
            self.add("recovery_mode", "safety", "Recovery mode ON in staging", not off,
                     "on for api and worker: staging cannot text, call, email or charge anyone"
                     if not off else f"ORACLE_RECOVERY_MODE not 1 on {', '.join(off)} — staging could "
                     f"contact real people")
        demo = [f"{c}:{k}" for c, e in envs.items() for k in _DEMO_FLAGS
                if str(e.get(k, (0, None))[1] or "").lower() in _TRUTHY or
                (e.get(k, (0, None))[0] and e.get(k, (0, None))[1] is None)]
        demo += [f"{c}:{k}" for c, e in envs.items() for k in _DEMO_CREDS if _present(e, k)]
        self.add("demo_login_off", "safety", "Demo login off", not demo,
                 "ORACLE_ENABLE_DEMO_LOGINS and ORACLE_DEMO_USER/PASS absent" if not demo else
                 f"demo login configured: {', '.join(demo)}", blocking=self.env == "production")
        want = "prod" if self.env == "production" else "staging"
        wrong = [c for c, e in envs.items() if e.get("ORACLE_ENV", (0, None))[1] != want]
        self.add("oracle_env", "safety", f"ORACLE_ENV={want}", not wrong,
                 f"ORACLE_ENV={want} on api and worker" if not wrong else
                 f"ORACLE_ENV wrong on {', '.join(wrong)} (production validation depends on it)")
        cors = (envs.get("api") or {}).get("ORACLE_CORS_ORIGINS", (False, None))
        val = str(cors[1] or "")
        bad = (not cors[0]) or any(t in val for t in ("localhost", "127.0.0.1", "*", "http://"))
        self.add("cors", "security", "CORS origins", not bad,
                 f"ORACLE_CORS_ORIGINS={val!r}" if cors[1] is not None or not cors[0] else
                 "ORACLE_CORS_ORIGINS set (secret)", blocking=self.env == "production")

    def _capability(self, id_, title, sets, area="providers", blocking=False, unmet=""):
        envs = _all_envs(self.spec).get("api") or {}
        hit = next((s for s in sets if all(_present(envs, k) for k in s)), None)
        self.add(id_, area, title, hit is not None,
                 f"configured via {'+'.join(hit)}" if hit else unmet, blocking=blocking)

    def check_providers(self):
        if self.spec is None:
            for i, t, b in (("billing", "Billing configured", self.charging), ("email", "Email configured", True),
                            ("ai_provider", "AI provider configured", True), ("voice", "Voice provider", False),
                            ("messaging", "Messaging provider", False), ("mls_credentials", "MLS feed credential", False),
                            ("alert_routing", "Alerts reach a person", False)):
                self._need_spec(i, "providers", t, blocking=b)
            return
        envs = _all_envs(self.spec)
        missing = [f"{c}:{k}" for c, keys in BILLING_SECRETS.items() for k in keys
                   if c in envs and not _present(envs[c], k)]
        self.add("billing", "billing", "Billing configured", not missing,
                 ("Stripe key, webhook secret and price set" +
                  (" (test mode enforced at boot: billing.py refuses sk_live_ outside production)"
                   if self.env != "production" else ""))
                 if not missing else f"unset: {', '.join(missing)}"
                 + ("" if self.charging else " (not charging yet — run without --not-charging once you bill)"),
                 blocking=self.charging)
        if self.env == "production" and not missing:
            self.add("stripe_live_mode", "billing", "Stripe key is LIVE mode", False,
                     "not verifiable remotely (value is encrypted) — confirm sk_live_ and the live "
                     "webhook endpoint in the Stripe dashboard", warn=True)
        e_missing = [k for k in EMAIL_SECRETS if not _present(envs.get("api") or {}, k)]
        self.add("email", "providers", "Email configured", not e_missing,
                 "SMTP host/username/password set (invites, password reset and operator codes depend "
                 "on it)" if not e_missing else f"unset: {', '.join(e_missing)} — invites and password "
                 "reset cannot send", blocking=True)
        self._capability("ai_provider", "AI provider configured", AI_SETS, blocking=True,
                         unmet="no AI provider key — Neoh cannot answer")
        self._capability("voice", "Voice provider configured", VOICE_SETS,
                         unmet="no voice carrier credentials — calling unavailable (CRM still works)")
        self._capability("messaging", "Messaging provider configured", MESSAGING_SETS,
                         unmet="no messaging credentials — texting unavailable")
        self._capability("mls_credentials", "MLS feed credential", [("ORACLE_BRIDGE_ACCESS_TOKEN",)],
                         unmet="no MLS feed credential — listing data unavailable (fine until a "
                               "brokerage needs MLS)")
        api_envs = envs.get("api") or {}
        self.add("alert_routing", "operations", "Alerts reach a person", _present(api_envs, "ORACLE_ALERT_EMAIL"),
                 "ORACLE_ALERT_EMAIL set" if _present(api_envs, "ORACLE_ALERT_EMAIL") else
                 "ORACLE_ALERT_EMAIL unset — alerts go only to logs", blocking=False)
        absent = [label for label, keys in OPTIONAL_PROVIDERS.items()
                  if not all(_present(api_envs, k) for k in keys)]
        self.add("optional_providers", "providers", "Optional provider support", not absent,
                 "all optional providers configured" if not absent else
                 f"not configured (optional): {', '.join(absent)}", blocking=False)
        gpu = _present(envs.get("worker") or {}, "RUNPOD_API_KEY")
        self.add("gpu_3d", "providers", "3D reconstruction (GPU)", gpu,
                 "RUNPOD_API_KEY set on worker" if gpu else "3D reconstruction unavailable (no RUNPOD_API_KEY)",
                 blocking=False)

    def check_live(self):
        if not self.base_url:
            for i, t in (("health", "Current release healthy"), ("version", "Release identity"),
                         ("worker_alive", "Worker alive on this release"),
                         ("web_security_headers", "Web security headers")):
                self.add(i, "release", t, False, "not verifiable: no URL (no app, and no --base-url given)")
            return
        # /live is NOT probed: it is not routed publicly (in-container liveness
        # only), so from outside it falls through to the SPA and answers 200
        # whatever the API is doing. /health must return the API's JSON — an
        # HTML 200 means ingress sent it to the SPA.
        st, _, body = self._get("/health")
        is_api = isinstance(body, dict)
        self.add("health", "release", "Current release healthy", st == 200 and is_api,
                 (f"/health {st or 'unreachable'}"
                  + (f" ({body.get('status')})" if is_api and body.get("status") else "")
                  + ("" if is_api or not st else " — not the API's JSON: check ingress routing")))
        st, _, v = self._get("/version")
        self.version = v if isinstance(v, dict) else None
        sha = (self.version or {}).get("git_sha") or "unknown"
        ok = st == 200 and sha != "unknown"
        reason = f"git_sha {sha[:12]}, migration_head {(self.version or {}).get('migration_head')}"
        if ok and self.manifest and self.manifest.get("git_sha") != sha:
            ok, reason = False, f"running {sha[:12]} but the intended release is {str(self.manifest.get('git_sha'))[:12]}"
        self.add("version", "release", "Release identity", ok, reason if st == 200 else f"/version {st or 'unreachable'}")
        st, _, w = self._get("/health/workers")
        w = w if isinstance(w, dict) else {}
        shas = w.get("live_git_shas") or []
        ok = bool(w.get("healthy")) and (sha == "unknown" or shas == [sha])
        self.add("worker_alive", "release", "Worker alive on this release", ok,
                 f"{w.get('live_workers', 0)} live worker(s) on {', '.join(s[:12] for s in shas) or 'nothing'}"
                 + ("" if ok or not w.get("healthy") else f" — expected only {sha[:12]}"))
        probe = "https://readiness-probe.invalid"
        st, h, _ = self._get("/health", extra={"Origin": probe})
        acao = (h or {}).get("access-control-allow-origin", "")
        self.add("cors_live", "security", "CORS refuses a foreign origin", acao not in (probe, "*"),
                 "foreign origin not echoed" if acao not in (probe, "*") else f"ACAO {acao!r} for a foreign origin")
        st, h, _ = self._get("/")
        csp = (h or {}).get("content-security-policy", "")
        ok = st == 200 and "frame-ancestors" in csp and "strict-transport-security" in (h or {})
        self.add("web_security_headers", "security", "Web security headers", ok,
                 "CSP frame-ancestors and HSTS present" if ok else f"GET / {st}: CSP frame-ancestors "
                 f"{'present' if 'frame-ancestors' in csp else 'MISSING'}, HSTS "
                 f"{'present' if 'strict-transport-security' in (h or {}) else 'MISSING'}",
                 blocking=self.env == "production")

    def check_operator_console(self):
        if not (self.token and self.base_url):
            self.add("operator_console", "operations", "Operator-console checks", False,
                     "not checked — set NEOH_OPERATOR_TOKEN (and a reachable URL) for component health, "
                     "MLS feeds and billing exceptions", warn=True)
            return
        st, _, d = self._get("/api/admin/health/components", auth=True)
        if st == 200 and isinstance(d, dict):
            comps = d.get("components") or {}
            down = sorted(k for k, v in comps.items() if str((v or {}).get("state", "")).upper() in ("DOWN", "UNHEALTHY", "FAILED"))
            degraded = sorted(k for k, v in comps.items() if str((v or {}).get("state", "")).upper() == "DEGRADED")
            self.add("component_health", "operations", "Component health", not down and not degraded,
                     "all components healthy" if not (down or degraded) else
                     f"down: {', '.join(down) or '-'}; degraded: {', '.join(degraded) or '-'}",
                     blocking=bool(down))
        else:
            self.add("component_health", "operations", "Component health", False,
                     f"/api/admin/health/components → {st or 'unreachable'}", warn=True)
        st, _, d = self._get("/api/admin/mls/feeds", auth=True)
        if st == 200:
            feeds = d if isinstance(d, list) else (d or {}).get("feeds") or []
            not_ready = []
            for f in feeds:
                state = str(f.get("state") or f.get("status") or f.get("health") or "").upper()
                if state and state not in ("READY", "OK", "HEALTHY", "LIVE"):
                    not_ready.append(f"{f.get('mls_id') or f.get('id') or f.get('name')}={state}")
            self.add("mls_state", "providers", "Licensed MLS state honest", bool(feeds) and not not_ready,
                     (f"{len(feeds)} feed(s), all ready" if feeds and not not_ready else
                      ("no MLS feeds configured" if not feeds else f"not ready: {', '.join(not_ready)}")),
                     blocking=False)
        else:
            self.add("mls_state", "providers", "Licensed MLS state honest", False,
                     f"not checked: /api/admin/mls/feeds → {st or 'unreachable'}"
                     + (" (not in this release)" if st == 404 else ""), warn=True)
        st, _, d = self._get("/api/admin/billing/exceptions", auth=True)
        if st == 200:
            items = d if isinstance(d, list) else (d or {}).get("exceptions") or (d or {}).get("items") or []
            self.add("billing_exceptions", "billing", "Billing exceptions", not items,
                     "none" if not items else f"{len(items)} open billing exception(s) — review in the operator console",
                     blocking=False)
        else:
            self.add("billing_exceptions", "billing", "Billing exceptions", False,
                     f"not checked: /api/admin/billing/exceptions → {st or 'unreachable'}", warn=True)

    def check_backups(self):
        cluster = self.expected["clusters"].get("neoh-postgres")
        c = next((d for d in self.dbs if d.get("name") == cluster), None)
        prod = self.env == "production"
        if c is None:
            self.add("backups", "data", f"Backups newer than {BACKUP_MAX_AGE_H} h", False,
                     f"no cluster '{cluster}' — nothing is backed up", blocking=prod)
            return
        rc, backups = self._cmd_json(["doctl", "databases", "backups", c.get("id", ""), "-o", "json"])
        times = sorted(t for t in (_parse_time(b.get("created_at")) for b in backups or []) if t)
        if not times:
            created = _parse_time(c.get("created_at"))
            young = created and (self.now - created).total_seconds() < BACKUP_MAX_AGE_H * 3600
            self.add("backups", "data", f"Backups newer than {BACKUP_MAX_AGE_H} h", False,
                     "no backup yet" + (" — cluster is less than a day old; the first daily backup is pending"
                                        if young else ""), blocking=prod and not young)
            return
        age_h = (self.now - times[-1]).total_seconds() / 3600
        self.add("backups", "data", f"Backups newer than {BACKUP_MAX_AGE_H} h", age_h <= BACKUP_MAX_AGE_H,
                 f"newest backup {age_h:.1f} h old ({len(times)} kept; DO retention 7 days, restore only to a "
                 f"NEW cluster)", blocking=prod)

    def check_github(self):
        gh_env = "production" if self.env == "production" else "staging"
        rc, envd = self._cmd_json(["gh", "api", f"repos/{self.repo_slug}/environments/{gh_env}"])
        if rc == 127:
            self.add("github_environment", "release", f"GitHub environment '{gh_env}'", False,
                     "gh not installed — CI deploy configuration not verified", warn=True)
            return
        if not isinstance(envd, dict):
            self.add("github_environment", "release", f"GitHub environment '{gh_env}'", False,
                     f"GitHub environment '{gh_env}' does not exist — CI cannot deploy here")
            return
        rules = envd.get("protection_rules") or []
        reviewers = any(r.get("type") == "required_reviewers" for r in rules)
        branch = (envd.get("deployment_branch_policy") or {})
        ok = reviewers or gh_env == "staging"
        self.add("github_environment", "release", f"GitHub environment '{gh_env}'", ok,
                 f"exists; required reviewers {'on' if reviewers else 'OFF'}; branch policy "
                 f"{'set' if branch else 'none'}", blocking=gh_env == "production")
        rc, secrets = self._cmd_json(["gh", "secret", "list", "--env", gh_env, "--repo", self.repo_slug,
                                      "--json", "name"])
        names = {s.get("name") for s in secrets or []}
        missing = [n for n in CI_ENV_SECRETS if n not in names]
        self.add("github_env_secrets", "release", f"CI secrets on '{gh_env}'", not missing,
                 "all CI deploy secrets set (names only)" if not missing else
                 f"unset: {', '.join(missing)} — the deploy job refuses to start")
        rc, evars = self._cmd_json(["gh", "variable", "list", "--env", gh_env, "--repo", self.repo_slug,
                                    "--json", "name,value"])
        dom = next((v.get("value") for v in evars or [] if v.get("name") == "NEOH_DOMAIN"), "")
        self.add("domain", "release", "Domain configured (NEOH_DOMAIN)", bool(dom),
                 f"NEOH_DOMAIN={dom}" if dom else
                 "NEOH_DOMAIN unset — the app answers only on its ondigitalocean.app URL",
                 blocking=gh_env == "production")
        if gh_env == "staging":
            rc, rvars = self._cmd_json(["gh", "variable", "list", "--repo", self.repo_slug, "--json", "name,value"])
            se = next((v.get("value") for v in rvars or [] if v.get("name") == "STAGING_ENABLED"), "")
            self.add("staging_enabled", "release", "STAGING_ENABLED", se == "true",
                     "pushes to main deploy to staging" if se == "true" else
                     f"STAGING_ENABLED={se or 'unset'} — main is not staged automatically (dispatch "
                     f"confirm=stage still works)", blocking=False)

    def check_gates(self):
        gate_path = self.root / "docs" / "security-launch-gate.json"
        gate = _json(gate_path.read_text(encoding="utf-8")) if gate_path.exists() else None
        if not isinstance(gate, dict):
            self.add("security_gate", "security", "Security launch gate", False, "docs/security-launch-gate.json missing")
        else:
            failing = [c.get("id") for c in gate.get("conditions") or [] if not c.get("pass")]
            ok = gate.get("verdict") == "PASS" and not failing
            self.add("security_gate", "security", "Security launch gate", ok,
                     f"verdict {gate.get('verdict')} ({gate.get('date')})"
                     + (f"; failing: {', '.join(failing)}" if failing else ""))
            rls = next((c for c in gate.get("conditions") or [] if c.get("id") == "rls_attack_suite"), {})
            self.add("rls_gate", "security", "RLS / cross-tenant suite", bool(rls.get("pass")),
                     rls.get("evidence", "no rls_attack_suite condition recorded"))
            manual = gate.get("external_or_manual") or []
            if manual:
                self.add("security_manual_controls", "security", "Manual security controls", False,
                         f"{len(manual)} control(s) only a person can confirm — see docs/launch-state.md",
                         warn=True)
        owner = self.root / "docs" / "launch-readiness" / "owner-gates.json"
        data = _json(owner.read_text(encoding="utf-8")) if owner.exists() else None
        for g in (data or {}).get("gates") or []:
            envs = g.get("environments") or ["production", "staging"]
            if self.env not in envs:
                continue
            done = g.get("status") == "done"
            self.add(f"gate:{g.get('id')}", g.get("area", "owner"), g.get("title", g.get("id")), done,
                     ("done: " if done else "open: ") + str(g.get("how_to_close", "")),
                     blocking=g.get("class") == "blocker")


# ── Output ─────────────────────────────────────────────────────────────────

def summarize(checks: list) -> dict:
    counts = {PASS: 0, WARN: 0, BLOCKED: 0}
    for c in checks:
        counts[c.status] += 1
    verdict = BLOCKED if counts[BLOCKED] else (WARN if counts[WARN] else PASS)
    return {"verdict": verdict, **counts}


def render_text(audit: Audit, checks: list) -> str:
    s = summarize(checks)
    color = sys.stdout.isatty() and not os.getenv("NO_COLOR")
    paint = {PASS: "\033[32m", WARN: "\033[33m", BLOCKED: "\033[31m"}

    def tag(st):
        return f"{paint[st]}{st:<7}\033[0m" if color else f"{st:<7}"

    lines = [f"Neoh launch readiness — {audit.env} ({audit.expected['name']})",
             f"  target: {audit.base_url or '(no URL)'}   at {audit.now.strftime('%Y-%m-%d %H:%M UTC')}", ""]
    area = None
    for c in sorted(checks, key=lambda c: (c.area, -_RANK[c.status])):
        if c.area != area:
            area = c.area
            lines.append(f"[{area}]")
        lines.append(f"  {tag(c.status)} {c.title}: {c.reason}")
    lines += ["", f"VERDICT: {s['verdict']}   PASS {s[PASS]}  WARN {s[WARN]}  BLOCKED {s[BLOCKED]}"]
    blockers = [c for c in checks if c.status == BLOCKED]
    roots = [c for c in blockers if not c.reason.startswith("not verifiable")]
    unverifiable = len(blockers) - len(roots)
    if blockers:
        lines.append("Blockers (root causes, in the order they unblock each other):")
        for c in roots:
            lines.append(f"  - {c.title}: {c.reason}")
        if unverifiable:
            lines.append(f"  + {unverifiable} more requirement(s) cannot be verified until the above exist")
    return audit.red.scrub("\n".join(lines))


def report_json(audit: Audit, checks: list) -> dict:
    return {
        "tool": "scripts/neoh-launch-readiness.py", "tool_version": TOOL_VERSION,
        "environment": audit.env, "app_name": audit.expected["name"],
        "app_id": (audit.app or {}).get("id"), "base_url": audit.base_url or None,
        "generated_at": audit.now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "summary": summarize(checks),
        "checks": [c.as_dict() for c in checks],
    }


def exit_code(checks: list) -> int:
    return {PASS: 0, WARN: 1, BLOCKED: 2}[summarize(checks)["verdict"]]


def main(argv: Optional[list] = None, *, runner: Runner = default_runner, http: Http = default_http,
         now: Optional[dt.datetime] = None) -> int:
    ap = argparse.ArgumentParser(description="Non-destructive Neoh launch readiness check.")
    ap.add_argument("--env", required=True, choices=["production", "staging"])
    ap.add_argument("--base-url", default="", help="https URL of the environment (default: the app's live URL)")
    ap.add_argument("--json", dest="json_path", help="write the machine-readable report here")
    ap.add_argument("--manifest", help="release-manifest.json of the release that SHOULD be running")
    ap.add_argument("--repo", default=DEFAULT_REPO_SLUG, help="GitHub owner/name for the CI environment checks")
    ap.add_argument("--not-charging", action="store_true",
                    help="billing not live yet: missing Stripe config is a WARN, not a blocker")
    args = ap.parse_args(argv)
    if args.base_url and not args.base_url.startswith("https://"):
        ap.error("--base-url must be https://")
    manifest = None
    if args.manifest:
        manifest = _json(pathlib.Path(args.manifest).read_text(encoding="utf-8"))
    audit = Audit(args.env, base_url=args.base_url, token=os.getenv("NEOH_OPERATOR_TOKEN", ""),
                  manifest=manifest, repo_slug=args.repo, charging=not args.not_charging,
                  runner=runner, http=http, now=now)
    checks = audit.run_all()
    print(render_text(audit, checks))
    if args.json_path:
        text = audit.red.scrub(json.dumps(report_json(audit, checks), indent=2))
        pathlib.Path(args.json_path).write_text(text + "\n", encoding="utf-8")
    return exit_code(checks)


if __name__ == "__main__":
    sys.exit(main())
