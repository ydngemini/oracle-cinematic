"""Provider-registered callback URLs stay mounted and reachable.

A release that moves or unmounts one fails silently: the provider gets errors,
we get no exception, and calls, payments or messages quietly stop arriving.
This is exactly how every outbound Plivo call came to fetch a 404 for its
instructions (tests/test_plivo_outbound_webhooks.py).

The list lives in infra/digitalocean/callback-routes.txt, shared with the
release smoke test, which checks the same URLs on the deployed app.
"""

from __future__ import annotations

import json
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
LIST = REPO / "infra" / "digitalocean" / "callback-routes.txt"


def _entries():
    out = []
    for line in LIST.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        method, path, status, provider = line.split()
        out.append((method, path, int(status), provider))
    return out


ENTRIES = _entries()


def test_the_list_covers_every_provider_that_calls_us():
    providers = {e[3] for e in ENTRIES}
    assert {"stripe", "plivo", "telnyx", "twilio", "google-oauth"} <= providers


@pytest.mark.parametrize("method, path, status, provider", ENTRIES,
                         ids=[f"{e[3]}:{e[1]}" for e in ENTRIES])
def test_each_callback_is_mounted(method, path, status, provider):
    import server

    for route in server.app.routes:
        rx = "^" + re.sub(r"\\{[^}]+\\}", "[^/]+", re.escape(getattr(route, "path", "") or "")) + "$"
        if re.match(rx, path) and method in (getattr(route, "methods", None) or set()):
            return
    pytest.fail(f"{provider} is registered to call {method} {path}, and no route serves it")


@pytest.mark.parametrize("method, path, status, provider",
                         [e for e in ENTRIES if e[0] == "POST"],
                         ids=[f"{e[3]}:{e[1]}" for e in ENTRIES if e[0] == "POST"])
def test_each_posted_callback_is_csrf_exempt(method, path, status, provider):
    """A provider has no session and no CSRF token. A webhook that is mounted
    but not exempt answers every real delivery with 403."""
    from csrf_middleware import CSRFMiddleware

    assert CSRFMiddleware(app=None)._is_exempt(path), (
        f"{provider}'s {path} is not CSRF-exempt — every delivery would get 403"
    )


def test_the_smoke_test_reads_this_list():
    smoke = (REPO / "infra" / "digitalocean" / "smoke-test.sh").read_text(encoding="utf-8")
    assert "callback-routes.txt" in smoke


# ── The production smoke test is told which callbacks it MUST see (audit 30.4) ──

CI = REPO / ".github" / "workflows" / "ci.yml"
_SPEC_PATH = "/tmp/app.production.yaml"


def _promote_step(name: str) -> dict:
    import yaml

    steps = yaml.safe_load(CI.read_text(encoding="utf-8"))["jobs"]["promote"]["steps"]
    return next(s for s in steps if s.get("name") == name)


def _run_production_smoke_step(tmp_path, variable: str = "", spec_text: str | None = None):
    """Execute the promote job's real smoke step with a stand-in smoke script
    that records what it was given. The derivation script and the rendered
    spec are the real ones; only the spec's path is redirected into tmp_path."""
    import importlib.util
    import os
    import shutil
    import subprocess
    import sys

    step = _promote_step("Smoke test (production)")
    run = step["run"].replace(_SPEC_PATH, str(tmp_path / "app.production.yaml"))
    run = re.sub(r"\$\{\{\s*env\.PROMOTE_SHA\s*\}\}", "promotesha", run)
    assert "${{" not in run, "the smoke step interpolates an expression this test does not model: " + run

    if spec_text is None:
        _r = importlib.util.spec_from_file_location("render_app_spec_cbroutes", REPO / "scripts" / "render-app-spec.py")
        render = importlib.util.module_from_spec(_r)
        _r.loader.exec_module(render)
        import yaml

        spec_text = yaml.safe_dump(render.render("production", "sha256:" + "a" * 64, "sha256:" + "b" * 64))
    if spec_text != "<absent>":
        (tmp_path / "app.production.yaml").write_text(spec_text, encoding="utf-8")

    work = tmp_path / "work"
    (work / "scripts").mkdir(parents=True)
    (work / "infra" / "digitalocean").mkdir(parents=True)
    shutil.copy(REPO / "scripts" / "callback-requirements.py", work / "scripts")
    seen = tmp_path / "seen.json"
    fake = work / "infra" / "digitalocean" / "smoke-test.sh"
    fake.write_text(
        "#!/bin/sh\n"
        f"{sys.executable} -c 'import json,os,sys; json.dump({{k: os.environ.get(k) for k in "
        "(\"SMOKE_ENV\",\"REQUIRE_CALLBACK_PROVIDERS\",\"ALLOW_UNBILLED_PRODUCTION\",\"APP_URL\",\"EXPECTED_GIT_SHA\")}, "
        f"open(\"{seen}\",\"w\"))'\n")
    fake.chmod(0o755)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "python3").symlink_to(sys.executable)   # the job installs PyYAML first

    env = {"PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}",
           "HOME": str(tmp_path), "NEOH_PUBLIC_API_BASE": "https://api.example.test"}
    # The step's own env block, with the repository variables substituted.
    for k, v in (step.get("env") or {}).items():
        m = re.fullmatch(r"\$\{\{\s*vars\.(\w+)\s*\}\}", str(v))
        assert m, f"step env {k} is not a plain repository variable: {v}"
        env[k] = variable if m.group(1) == "REQUIRE_CALLBACK_PROVIDERS" else ""
    proc = subprocess.run(["bash", "-c", run], cwd=work, env=env, capture_output=True, text=True, timeout=120)
    got = json.loads(seen.read_text()) if seen.exists() else None
    return proc, got


def test_the_promote_step_derives_the_requirement_from_the_rendered_spec():
    render_step = _promote_step("Render the production spec")["run"]
    assert f"> {_SPEC_PATH}" in render_step, "the smoke step reads the spec the render step writes"
    smoke = _promote_step("Smoke test (production)")
    assert "scripts/callback-requirements.py" in smoke["run"] and _SPEC_PATH in smoke["run"]
    assert "set -euo pipefail" in smoke["run"], "a failed derivation must stop the step, not pass an empty list"


def test_the_promote_step_passes_the_derived_requirement_to_the_smoke_test(tmp_path):
    proc, got = _run_production_smoke_step(tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert got["SMOKE_ENV"] == "production"
    assert "stripe" in got["REQUIRE_CALLBACK_PROVIDERS"].split()
    assert got["EXPECTED_GIT_SHA"] == "promotesha"


def test_the_repository_variable_still_adds_providers(tmp_path):
    proc, got = _run_production_smoke_step(tmp_path, variable="twilio")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert {"stripe", "twilio"} <= set(got["REQUIRE_CALLBACK_PROVIDERS"].split())


def test_a_derivation_that_cannot_read_the_spec_stops_before_the_smoke_test(tmp_path):
    proc, got = _run_production_smoke_step(tmp_path, spec_text="<absent>")
    assert proc.returncode != 0 and got is None, proc.stdout + proc.stderr


def test_the_staging_smoke_step_is_not_told_it_is_production():
    import yaml

    steps = yaml.safe_load(CI.read_text(encoding="utf-8"))["jobs"]["release"]["steps"]
    staging = next(s for s in steps if s.get("name") == "Smoke test (staging)")
    assert "SMOKE_ENV=production" not in staging["run"]
