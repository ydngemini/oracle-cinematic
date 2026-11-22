"""Which provider callbacks a production release MUST have working (audit 30.4).

The production smoke test only WARNED when a webhook answered "signing secret
not configured" (DigitalOcean shows that 503 as 504 + x-do-orig-status: 503),
unless the provider was listed in the repository variable
REQUIRE_CALLBACK_PROVIDERS, which was never set. A release that lost
STRIPE_WEBHOOK_SECRET therefore passed while every subscription event bounced.

Two halves, both exercised here:
  * scripts/callback-requirements.py derives the set from the spec the release
    deployed (Stripe while billing is on, Telnyx/Plivo when declared), unioned
    with the variable;
  * infra/digitalocean/smoke-test.sh, run against a local stub that answers
    every callback the way DigitalOcean shows an unconfigured one, FAILS in
    production on a required provider or on an empty requirement set (unless a
    named waiver is given), and keeps WARN on staging.
"""

from __future__ import annotations

import copy
import http.server
import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import sys
import threading

import pytest
import yaml

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO / "scripts" / "callback-requirements.py"
SMOKE = REPO / "infra" / "digitalocean" / "smoke-test.sh"
ROUTES = REPO / "infra" / "digitalocean" / "callback-routes.txt"

_r = importlib.util.spec_from_file_location("render_app_spec_cbreq", REPO / "scripts" / "render-app-spec.py")
render = importlib.util.module_from_spec(_r)
_r.loader.exec_module(render)

D = "sha256:" + "a" * 64


def _route_entries():
    out = []
    for line in ROUTES.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        method, path, status, provider = line.split()
        out.append((method, path, int(status), provider))
    return out


def _derive(tmp_path, spec, also: str | None = None):
    p = tmp_path / "spec.yaml"
    p.write_text(spec if isinstance(spec, str) else yaml.safe_dump(spec), encoding="utf-8")
    cmd = [sys.executable, str(SCRIPT), "--spec", str(p)]
    if also is not None:
        cmd += ["--also", also]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60)


def _prod() -> dict:
    return render.render("production", D, D)


def _api(spec: dict) -> dict:
    return next(c for c in spec["services"] if c["name"] == "api")


def _set_env(comp: dict, key: str, entry: dict | None):
    envs = [e for e in comp.get("envs") or [] if e.get("key") != key]
    if entry is not None:
        envs.append({"key": key, **entry})
    comp["envs"] = envs


def _drop(spec: dict, *keys: str, components=("api", "worker")) -> dict:
    spec = copy.deepcopy(spec)
    for kind in ("services", "workers"):
        for c in spec.get(kind) or []:
            if c["name"] in components:
                c["envs"] = [e for e in c.get("envs") or [] if e.get("key") not in keys]
    return spec


# ── scripts/callback-requirements.py ────────────────────────────────────────

def test_the_committed_production_spec_requires_stripe_and_the_declared_carriers(tmp_path):
    """Today's spec bills (ORACLE_ENV=prod, enforcement not turned off) and
    declares the Telnyx and Plivo secrets, so all three must answer."""
    proc = _derive(tmp_path, _prod())
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.split() == ["plivo", "stripe", "telnyx"]


def test_every_derived_provider_is_one_the_smoke_test_probes(tmp_path):
    proc = _derive(tmp_path, _prod())
    assert set(proc.stdout.split()) <= {e[3] for e in _route_entries()}


@pytest.mark.parametrize("off", ["0", "false", "no", "off", " OFF "])
def test_billing_turned_off_in_the_spec_drops_stripe(tmp_path, off):
    spec = _prod()
    _set_env(_api(spec), "ORACLE_BILLING_ENFORCED", {"value": off})
    proc = _derive(tmp_path, spec)
    assert proc.returncode == 0, proc.stderr
    assert "stripe" not in proc.stdout.split()


@pytest.mark.parametrize("on", ["1", "true", "yes", ""])
def test_billing_left_on_keeps_stripe(tmp_path, on):
    spec = _prod()
    _set_env(_api(spec), "ORACLE_BILLING_ENFORCED", {"value": on})
    assert "stripe" in _derive(tmp_path, spec).stdout.split()


def test_an_unreadable_enforcement_value_keeps_stripe(tmp_path):
    """A SECRET-typed switch has no readable value in the rendered spec: fail
    closed and require the webhook."""
    spec = _prod()
    _set_env(_api(spec), "ORACLE_BILLING_ENFORCED", {"type": "SECRET"})
    assert "stripe" in _derive(tmp_path, spec).stdout.split()


def test_development_does_not_bill_unless_enforcement_is_switched_on(tmp_path):
    """Mirrors billing.billing_enforced(): unset means on everywhere but dev."""
    spec = _prod()
    _set_env(_api(spec), "ORACLE_ENV", {"value": "dev"})
    assert "stripe" not in _derive(tmp_path, spec).stdout.split()
    _set_env(_api(spec), "ORACLE_BILLING_ENFORCED", {"value": "1"})
    assert "stripe" in _derive(tmp_path, spec).stdout.split()


def test_no_declared_webhook_secret_means_stripe_is_not_derived(tmp_path):
    spec = _drop(_prod(), "STRIPE_WEBHOOK_SECRET")
    assert "stripe" not in _derive(tmp_path, spec).stdout.split()


def test_a_webhook_secret_only_on_the_worker_does_not_count(tmp_path):
    """The api serves /billing/webhook; the worker's copy verifies nothing."""
    spec = _drop(_prod(), "STRIPE_WEBHOOK_SECRET", components=("api",))
    assert "stripe" not in _derive(tmp_path, spec).stdout.split()


def test_an_app_level_declaration_counts_for_the_api(tmp_path):
    spec = _drop(_prod(), "STRIPE_WEBHOOK_SECRET")
    spec["envs"] = [{"key": "STRIPE_WEBHOOK_SECRET", "type": "SECRET"}]
    assert "stripe" in _derive(tmp_path, spec).stdout.split()


@pytest.mark.parametrize("keep, provider", [
    ("TELNYX_API_KEY", "telnyx"), ("TELNYX_PUBLIC_KEY", "telnyx"),
    ("PLIVO_AUTH_ID", "plivo"), ("PLIVO_AUTH_TOKEN", "plivo"),
])
def test_a_carrier_is_required_when_any_of_its_secrets_is_declared(tmp_path, keep, provider):
    carriers = {"TELNYX_API_KEY", "TELNYX_PUBLIC_KEY", "PLIVO_AUTH_ID", "PLIVO_AUTH_TOKEN"}
    spec = _drop(_prod(), *(carriers - {keep}))
    assert _derive(tmp_path, spec).stdout.split() == sorted({"stripe", provider})


def test_undeclared_carriers_are_not_required(tmp_path):
    spec = _drop(_prod(), "TELNYX_API_KEY", "TELNYX_PUBLIC_KEY", "PLIVO_AUTH_ID", "PLIVO_AUTH_TOKEN")
    assert _derive(tmp_path, spec).stdout.split() == ["stripe"]


def test_a_production_that_neither_bills_nor_declares_a_carrier_derives_nothing(tmp_path):
    spec = _drop(_prod(), "TELNYX_API_KEY", "TELNYX_PUBLIC_KEY", "PLIVO_AUTH_ID", "PLIVO_AUTH_TOKEN")
    _set_env(_api(spec), "ORACLE_BILLING_ENFORCED", {"value": "0"})
    proc = _derive(tmp_path, spec)
    assert proc.returncode == 0 and proc.stdout.strip() == ""


def test_the_repository_variable_is_unioned_not_replaced(tmp_path):
    proc = _derive(tmp_path, _prod(), also=" twilio, stripe  twilio ")
    assert proc.stdout.split() == ["plivo", "stripe", "telnyx", "twilio"]


@pytest.mark.parametrize("spec", ["", "services: []\n", "services:\n  - name: web\n", "{not: [valid"])
def test_a_spec_without_an_api_is_refused_not_read_as_no_requirement(tmp_path, spec):
    proc = _derive(tmp_path, spec)
    assert proc.returncode == 2 and proc.stdout == "", (proc.stdout, proc.stderr)


def test_a_missing_spec_is_refused(tmp_path):
    proc = subprocess.run([sys.executable, str(SCRIPT), "--spec", str(tmp_path / "absent.yaml")],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 2 and proc.stdout == ""


def test_never_prints_a_secret_value(tmp_path):
    """Run against a carried spec by mistake, it still prints names only."""
    marker = "EV[1:" + "canary-" + "9f3a" * 4 + "]"
    spec = _prod()
    for kind in ("services", "workers"):
        for c in spec[kind]:
            for e in c.get("envs") or []:
                if e.get("type") == "SECRET":
                    e["value"] = marker
    proc = _derive(tmp_path, spec)
    assert proc.returncode == 0
    assert "canary" not in proc.stdout + proc.stderr


# ── infra/digitalocean/smoke-test.sh against a local stub ───────────────────

_SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'",
    "X-Frame-Options": "DENY",
    "Strict-Transport-Security": "max-age=63072000",
    "X-Content-Type-Options": "nosniff",
}


class _Stub(http.server.BaseHTTPRequestHandler):
    """A healthy deploy on which every provider callback answers the way
    DigitalOcean's edge shows an unconfigured one: 504 + x-do-orig-status: 503.
    `configured` names providers whose probe gets its expected status."""

    configured: set = set()

    def log_message(self, *a):  # keep pytest output clean
        pass

    def _reply(self, code: int, body: bytes = b"", ctype: str = "application/json", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in {**_SECURITY_HEADERS, **(extra or {})}.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _route(self, method: str):
        path = self.path.split("?")[0]
        for m, p, want, provider in _route_entries():
            if m == method and p == path:
                if provider in self.configured:
                    return self._reply(want)
                return self._reply(504, b"<html>504</html>", "text/html", {"x-do-orig-status": "503"})
        if method == "GET" and path == "/health":
            return self._reply(200, b'{"status":"ok"}')
        if method == "GET" and path == "/version":
            return self._reply(200, b'{"git_sha":"stubsha","migration_head":"0183"}')
        if method == "GET" and path == "/health/workers":
            return self._reply(200, json.dumps({"healthy": True, "live_git_shas": ["stubsha"]}).encode())
        if method == "GET" and path == "/health/storage":
            return self._reply(200, b'{"state":"healthy"}')
        if method == "GET" and path == "/api/commands":
            return self._reply(401, b'{"detail":"auth"}')
        if method == "GET" and path == "/":
            return self._reply(200, b"<html></html>", "text/html")
        return self._reply(404, b"", "text/html")

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")


@pytest.fixture
def stub():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Stub)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield srv
    finally:
        _Stub.configured = set()
        srv.shutdown()
        srv.server_close()


def _smoke(stub, tmp_path, **env):
    # A fake `aws` that fails: the Spaces check then only warns, and no test
    # ever reaches DigitalOcean (CI runners ship the real aws CLI).
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    aws = fake_bin / "aws"
    aws.write_text("#!/bin/sh\nexit 1\n")
    aws.chmod(0o755)
    base = {
        "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}",
        "HOME": str(tmp_path),
        "APP_URL": f"http://127.0.0.1:{stub.server_address[1]}",
        "EXPECTED_GIT_SHA": "stubsha",
        "WORKER_WAIT_SECONDS": "0",
    }
    base.update({k: v for k, v in env.items() if v is not None})
    proc = subprocess.run(["bash", str(SMOKE)], env=base, capture_output=True, text=True, timeout=120)
    return proc.returncode, proc.stdout + proc.stderr


def _lines(out: str, word: str) -> list[str]:
    return [ln for ln in out.splitlines() if word in ln]


needs_tools = pytest.mark.skipif(not (shutil.which("bash") and shutil.which("curl")),
                                 reason="smoke-test.sh needs bash and curl")


@needs_tools
def test_the_stub_is_otherwise_a_passing_deploy(stub, tmp_path):
    """Guard for the tests below: with every provider configured, nothing fails."""
    _Stub.configured = {e[3] for e in _route_entries()}
    code, out = _smoke(stub, tmp_path, SMOKE_ENV="production", REQUIRE_CALLBACK_PROVIDERS="stripe")
    assert code == 0, out
    assert not _lines(out, "FAIL"), out


@needs_tools
def test_production_with_an_empty_requirement_set_fails(stub, tmp_path):
    """The audit's case: the variable unset, so a lost Stripe secret was a WARN."""
    code, out = _smoke(stub, tmp_path, SMOKE_ENV="production", REQUIRE_CALLBACK_PROVIDERS="")
    assert code != 0, out
    assert any("REQUIRE_CALLBACK_PROVIDERS is empty" in ln for ln in _lines(out, "FAIL")), out


@needs_tools
def test_production_with_the_variable_absent_fails_too(stub, tmp_path):
    code, out = _smoke(stub, tmp_path, SMOKE_ENV="production")
    assert code != 0, out


@needs_tools
@pytest.mark.parametrize("smoke_env", [None, "staging"])
def test_staging_keeps_an_unconfigured_provider_a_warning(stub, tmp_path, smoke_env):
    code, out = _smoke(stub, tmp_path, SMOKE_ENV=smoke_env, REQUIRE_CALLBACK_PROVIDERS="")
    assert code == 0, out
    assert any("stripe callback" in ln and "not configured on this environment" in ln
               for ln in _lines(out, "WARN")), out
    assert not _lines(out, "FAIL"), out


@needs_tools
def test_production_fails_on_a_required_provider_that_is_not_configured(stub, tmp_path):
    code, out = _smoke(stub, tmp_path, SMOKE_ENV="production", REQUIRE_CALLBACK_PROVIDERS="stripe")
    assert code != 0, out
    assert any("stripe callback" in ln and "requires stripe" in ln for ln in _lines(out, "FAIL")), out
    # A provider that is NOT required stays a warning, even in production.
    assert any("telnyx callback" in ln for ln in _lines(out, "WARN")), out


@needs_tools
def test_a_named_waiver_accepts_an_unbilled_production_and_says_so(stub, tmp_path):
    code, out = _smoke(stub, tmp_path, SMOKE_ENV="production", REQUIRE_CALLBACK_PROVIDERS="",
                       ALLOW_UNBILLED_PRODUCTION="ops lead: production does not bill until the pilot signs")
    assert code == 0, out
    assert any("WAIVED" in ln and "ops lead" in ln for ln in _lines(out, "WARN")), out


@needs_tools
@pytest.mark.parametrize("waiver", ["yes", "ops lead:", ": because", "   "])
def test_a_waiver_without_who_and_why_is_refused(stub, tmp_path, waiver):
    code, out = _smoke(stub, tmp_path, SMOKE_ENV="production", REQUIRE_CALLBACK_PROVIDERS="",
                       ALLOW_UNBILLED_PRODUCTION=waiver)
    assert code != 0, out


@needs_tools
def test_a_waiver_does_not_excuse_a_required_provider(stub, tmp_path):
    code, out = _smoke(stub, tmp_path, SMOKE_ENV="production", REQUIRE_CALLBACK_PROVIDERS="stripe",
                       ALLOW_UNBILLED_PRODUCTION="ops lead: not billing")
    assert code != 0, out


@needs_tools
def test_a_required_provider_the_smoke_test_cannot_probe_fails(stub, tmp_path):
    """A typo in the variable must not read as a requirement that is met."""
    _Stub.configured = {e[3] for e in _route_entries()}
    code, out = _smoke(stub, tmp_path, SMOKE_ENV="staging", REQUIRE_CALLBACK_PROVIDERS="strpe")
    assert code != 0, out
    assert any("strpe" in ln for ln in _lines(out, "FAIL")), out


@needs_tools
def test_an_unknown_smoke_env_fails_rather_than_reading_as_staging(stub, tmp_path):
    _Stub.configured = {e[3] for e in _route_entries()}
    code, out = _smoke(stub, tmp_path, SMOKE_ENV="prod", REQUIRE_CALLBACK_PROVIDERS="stripe")
    assert code != 0, out


@needs_tools
def test_a_comma_separated_variable_is_read_like_a_spaced_one(stub, tmp_path):
    code, out = _smoke(stub, tmp_path, SMOKE_ENV="production", REQUIRE_CALLBACK_PROVIDERS="telnyx,stripe")
    assert code != 0
    assert any("requires stripe" in ln for ln in _lines(out, "FAIL")), out
    assert any("requires telnyx" in ln for ln in _lines(out, "FAIL")), out
