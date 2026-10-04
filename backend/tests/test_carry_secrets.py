"""scripts/carry-secrets.py — a deploy must never wipe the app's secrets.

`doctl apps update --spec` with a `type: SECRET` env var that has no value
WIPES that secret (proven on neoh-staging, 2026-10-03: the backend refused to
boot without ORACLE_SECRET_KEY and DO rolled back automatically). CI renders
from a committed spec whose secrets are blank, so every deploy goes through
this carry step first.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import pathlib

import pytest
import yaml

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
_s = importlib.util.spec_from_file_location("carry_secrets", REPO / "scripts" / "carry-secrets.py")
cs = importlib.util.module_from_spec(_s)
_s.loader.exec_module(cs)
_r = importlib.util.spec_from_file_location("render_app_spec_carry", REPO / "scripts" / "render-app-spec.py")
render = importlib.util.module_from_spec(_r)
_r.loader.exec_module(render)

D = "sha256:" + "a" * 64
EV = "EV[1:SENTINEL-ciphertext-{}]"


def _rendered(env="staging"):
    return render.render(env, D, D)


def _running_from(rendered: dict) -> dict:
    """What a healthy running app looks like: every SECRET set (EV)."""
    run = copy.deepcopy(rendered)
    for c in run["services"] + run["workers"]:
        for e in c.get("envs") or []:
            if e.get("type") == "SECRET" and not e.get("value"):
                e["value"] = EV.format(f"{c['name']}-{e['key']}")
    return run


def _secret(spec, comp, key):
    for c in spec["services"] + spec["workers"]:
        if c["name"] == comp:
            return next((e.get("value") for e in c.get("envs") or [] if e["key"] == key), None)


def test_blank_rendered_secrets_take_the_running_values():
    rendered = _rendered()
    out, report = cs.carry(_running_from(rendered), copy.deepcopy(rendered), "staging")
    assert _secret(out, "api", "ORACLE_SECRET_KEY") == EV.format("api-ORACLE_SECRET_KEY")
    assert _secret(out, "worker", "ORACLE_SECRET_KEY") == EV.format("worker-ORACLE_SECRET_KEY")
    assert "api:ORACLE_SECRET_KEY" in report["carried"]
    # no SECRET left blank that the running app had
    for c in out["services"] + out["workers"]:
        for e in c.get("envs") or []:
            if e.get("type") == "SECRET":
                assert e.get("value"), (c["name"], e["key"])


def test_bindings_and_explicit_values_are_never_replaced():
    rendered = _rendered()
    running = _running_from(rendered)
    for c in running["services"] + running["workers"]:
        for e in c.get("envs") or []:
            if e["key"] in ("ORACLE_DB_PASSWORD", "REDIS_URL"):
                e["value"] = "EV[1:stale-resolved-binding]"
    out, report = cs.carry(running, copy.deepcopy(rendered))
    assert _secret(out, "api", "ORACLE_DB_PASSWORD") == "${neoh-postgres.PASSWORD}"
    assert _secret(out, "worker", "REDIS_URL") == "${neoh-redis.DATABASE_URL}"
    assert "api:ORACLE_DB_PASSWORD" in report["kept"]


def test_only_the_same_component_and_key_is_carried():
    rendered = _rendered()
    running = _running_from(rendered)
    running["workers"][0]["envs"] = [e for e in running["workers"][0]["envs"]
                                     if e["key"] != "ORACLE_FIREWORKS_API_KEY"]
    out, report = cs.carry(running, copy.deepcopy(rendered))
    assert not _secret(out, "worker", "ORACLE_FIREWORKS_API_KEY")       # not borrowed from api
    assert "worker:ORACLE_FIREWORKS_API_KEY" in report["optional_unset"]


def test_a_required_secret_missing_everywhere_refuses_and_names_only_keys():
    rendered = _rendered()
    running = _running_from(rendered)
    for e in running["services"][0]["envs"]:
        if e["key"] == "ORACLE_ENCRYPTION_MASTER_KEY":
            e.pop("value")
    with pytest.raises(cs.CarryError) as exc:
        cs.carry(running, copy.deepcopy(rendered))
    msg = str(exc.value)
    assert "api:ORACLE_ENCRYPTION_MASTER_KEY" in msg
    assert "SENTINEL" not in msg


def test_stripe_key_without_its_webhook_secret_refuses():
    rendered = _rendered()
    running = _running_from(rendered)
    for e in running["workers"][0]["envs"]:
        if e["key"] == "STRIPE_WEBHOOK_SECRET":
            e.pop("value")
    with pytest.raises(cs.CarryError, match="worker:STRIPE_WEBHOOK_SECRET"):
        cs.carry(running, copy.deepcopy(rendered))


def test_production_operator_needs_a_second_factor():
    rendered = _rendered("production")
    running = _running_from(rendered)
    for e in running["services"][0]["envs"]:
        if e["key"] in cs.OPERATOR_2FA:
            e.pop("value")
    with pytest.raises(cs.CarryError, match="second factor"):
        cs.carry(running, copy.deepcopy(rendered), "production")


def test_carries_from_the_newest_ACTIVE_deployment_never_a_failed_one():
    """After a blank-secret update the APP spec can hold EV values that encrypt
    EMPTY strings; the ACTIVE deployment is what actually booted."""
    good = {"services": [{"name": "api", "envs": [{"key": "K", "type": "SECRET", "value": "EV[good]"}]}]}
    bad = {"services": [{"name": "api", "envs": [{"key": "K", "type": "SECRET", "value": "EV[empty]"}]}]}
    deps = [
        {"id": "newest-error", "phase": "ERROR", "created_at": "2026-10-03T20:01:25Z", "spec": bad},
        {"id": "active-rollback", "phase": "ACTIVE", "created_at": "2026-10-03T20:02:29Z", "spec": good},
        {"id": "old", "phase": "SUPERSEDED", "created_at": "2026-10-03T19:54:06Z", "spec": bad},
    ]
    spec, dep_id = cs.active_deployment_spec(deps)
    assert dep_id == "active-rollback" and spec is good


def test_no_active_deployment_refuses():
    with pytest.raises(cs.CarryError, match="no ACTIVE deployment"):
        cs.active_deployment_spec([{"id": "x", "phase": "ERROR", "spec": {}}])


def test_cli_never_prints_a_value(tmp_path, capsys):
    rendered = _rendered()
    deps = [{"id": "dep-1", "phase": "ACTIVE", "created_at": "2026-10-03T20:00:00Z",
             "spec": _running_from(rendered)}]
    (tmp_path / "deps.json").write_text(json.dumps(deps))
    (tmp_path / "app.yaml").write_text(yaml.safe_dump(rendered))
    code = cs.main(["--deployments", str(tmp_path / "deps.json"), "--rendered", str(tmp_path / "app.yaml"),
                    "--out", str(tmp_path / "deploy.yaml"), "--env", "staging"])
    out = capsys.readouterr()
    assert code == 0
    assert "SENTINEL" not in out.out + out.err
    assert "SENTINEL" in (tmp_path / "deploy.yaml").read_text()   # the ciphertexts went to the file only


# ── --inject-from-env: secrets the app has never had ───────────────────────

def _without(spec: dict, key: str) -> dict:
    """A running app on which `key` was never set."""
    for c in spec["services"] + spec["workers"]:
        c["envs"] = [e for e in c.get("envs") or [] if e["key"] != key]
    return spec


def test_a_never_set_secret_is_filled_from_the_job_environment():
    rendered = _rendered()
    running = _without(_running_from(rendered), "PLIVO_AUTH_TOKEN")
    out, report = cs.carry(running, copy.deepcopy(rendered), "staging",
                           inject={"PLIVO_AUTH_TOKEN": "plain-INJECTED-value"})
    assert _secret(out, "api", "PLIVO_AUTH_TOKEN") == "plain-INJECTED-value"
    assert _secret(out, "worker", "PLIVO_AUTH_TOKEN") == "plain-INJECTED-value"
    assert "api:PLIVO_AUTH_TOKEN" in report["injected"]


def test_a_carried_value_wins_unless_overridden():
    rendered = _rendered()
    running = _running_from(rendered)
    out, _ = cs.carry(running, copy.deepcopy(rendered), "staging",
                      inject={"PLIVO_AUTH_TOKEN": "plain-INJECTED-value"})
    assert _secret(out, "api", "PLIVO_AUTH_TOKEN") == EV.format("api-PLIVO_AUTH_TOKEN")
    out, _ = cs.carry(running, copy.deepcopy(rendered), "staging",
                      inject={"PLIVO_AUTH_TOKEN": "plain-INJECTED-value"},
                      override=frozenset({"PLIVO_AUTH_TOKEN"}))
    assert _secret(out, "api", "PLIVO_AUTH_TOKEN") == "plain-INJECTED-value"


def test_an_empty_job_variable_injects_nothing():
    rendered = _rendered()
    running = _without(_running_from(rendered), "TELNYX_API_KEY")
    out, report = cs.carry(running, copy.deepcopy(rendered), "staging", inject={"TELNYX_API_KEY": ""})
    assert not _secret(out, "api", "TELNYX_API_KEY")
    assert "api:TELNYX_API_KEY" in report["optional_unset"]


def test_only_listed_keys_may_be_injected_and_the_allowlist_never_into_production():
    with pytest.raises(cs.CarryError):
        cs.check_injectable(["ORACLE_SECRET_KEY"], "staging")
    with pytest.raises(cs.CarryError):
        cs.check_injectable(["ORACLE_DEMO_RECIPIENT_ALLOWLIST"], "production")
    assert cs.check_injectable(["ORACLE_DEMO_RECIPIENT_ALLOWLIST"], "staging")


def test_the_demo_allowlist_has_no_target_in_a_production_spec():
    rendered = _rendered("production")
    out, _ = cs.carry(_running_from(rendered), copy.deepcopy(rendered), "production",
                      inject={"PLIVO_AUTH_ID": "x"})
    assert all(e["key"] != "ORACLE_DEMO_RECIPIENT_ALLOWLIST"
               for c in out["services"] + out["workers"] for e in c.get("envs") or [])


def test_cli_injects_without_printing_the_value(tmp_path, capsys, monkeypatch):
    rendered = _rendered()
    running = _without(_running_from(rendered), "ORACLE_DEMO_RECIPIENT_ALLOWLIST")
    deps = [{"id": "dep-1", "phase": "ACTIVE", "created_at": "2026-10-04T20:00:00Z", "spec": running}]
    (tmp_path / "deps.json").write_text(json.dumps(deps))
    (tmp_path / "app.yaml").write_text(yaml.safe_dump(rendered))
    monkeypatch.setenv("ORACLE_DEMO_RECIPIENT_ALLOWLIST", "+15555550100")
    monkeypatch.delenv("PLIVO_AUTH_ID", raising=False)
    code = cs.main(["--deployments", str(tmp_path / "deps.json"), "--rendered", str(tmp_path / "app.yaml"),
                    "--out", str(tmp_path / "deploy.yaml"), "--env", "staging",
                    "--inject-from-env", "ORACLE_DEMO_RECIPIENT_ALLOWLIST,PLIVO_AUTH_ID"])
    out = capsys.readouterr()
    assert code == 0
    assert "+15555550100" not in out.out + out.err
    assert "api:ORACLE_DEMO_RECIPIENT_ALLOWLIST" in out.out
    assert "+15555550100" in (tmp_path / "deploy.yaml").read_text()


def test_cli_refuses_an_unlisted_key(tmp_path):
    rendered = _rendered()
    deps = [{"id": "dep-1", "phase": "ACTIVE", "created_at": "2026-10-04T20:00:00Z",
             "spec": _running_from(rendered)}]
    (tmp_path / "deps.json").write_text(json.dumps(deps))
    (tmp_path / "app.yaml").write_text(yaml.safe_dump(rendered))
    code = cs.main(["--deployments", str(tmp_path / "deps.json"), "--rendered", str(tmp_path / "app.yaml"),
                    "--out", str(tmp_path / "deploy.yaml"), "--env", "staging",
                    "--inject-from-env", "ORACLE_SECRET_KEY"])
    assert code == 1
