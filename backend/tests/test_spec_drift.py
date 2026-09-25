"""`doctl apps update --spec` replaces the whole app spec, so anything changed
through the DigitalOcean console since the last deploy is silently destroyed
by the next one. spec-drift.py refuses that."""

from __future__ import annotations

import copy
import importlib.util
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent.parent


def _load(name, file):
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


drift = _load("spec_drift", "spec-drift.py")
render = _load("render_app_spec_d", "render-app-spec.py")
D = "sha256:" + "a" * 64


@pytest.fixture
def spec():
    return render.render("production", D, D)


def test_identical_specs_have_no_drift(spec):
    d = drift.diff(spec, spec)
    assert d == {"removals": [], "changes": [], "additions": []}


def test_a_new_digest_is_not_drift(spec):
    """Every release changes the digest; that is the point of releasing."""
    running = copy.deepcopy(spec)
    running["services"][0]["image"]["digest"] = "sha256:" + "f" * 64
    assert drift.diff(running, spec)["removals"] == []


def test_a_console_added_env_var_would_be_removed(spec):
    running = copy.deepcopy(spec)
    running["services"][0]["envs"].append({"key": "SENTRY_DSN", "value": "x", "type": "SECRET"})
    assert "env var SENTRY_DSN on 'api'" in drift.diff(running, spec)["removals"]


def test_a_console_attached_domain_would_be_removed(spec):
    """The worst removal there is: the public URL every provider calls."""
    running = copy.deepcopy(spec)
    running["domains"] = [{"domain": "neohrs.com", "type": "PRIMARY"}]
    assert "domain neohrs.com" in drift.diff(running, spec)["removals"]


def test_a_removed_component_ingress_or_binding_is_caught(spec):
    new = copy.deepcopy(spec)
    new["workers"] = []
    new["databases"] = new["databases"][:1]
    new["ingress"]["rules"] = new["ingress"]["rules"][1:]
    removals = drift.diff(spec, new)["removals"]
    assert any("component 'worker'" in r for r in removals)
    assert any("database binding" in r for r in removals)
    assert any("ingress rule" in r for r in removals)


def test_shape_changes_are_reported_not_fatal(spec):
    new = copy.deepcopy(spec)
    new["services"][0]["instance_count"] = 3
    d = drift.diff(spec, new)
    assert d["removals"] == [] and any("instance_count" in c for c in d["changes"])


def test_values_are_never_printed(tmp_path, spec, capsys):
    import yaml

    running = copy.deepcopy(spec)
    running["services"][0]["envs"].append({"key": "LEAKY", "value": "s3cr3t-value", "type": "SECRET"})
    (tmp_path / "r.yaml").write_text(yaml.safe_dump({"spec": running}))
    (tmp_path / "n.yaml").write_text(yaml.safe_dump(spec))
    rc = drift.main(["--running", str(tmp_path / "r.yaml"), "--new", str(tmp_path / "n.yaml")])
    out = capsys.readouterr().out
    assert rc == 1 and "LEAKY" in out and "s3cr3t-value" not in out


def test_removals_can_be_acknowledged(tmp_path, spec, monkeypatch):
    import yaml

    running = copy.deepcopy(spec)
    running["domains"] = [{"domain": "old.example.com"}]
    (tmp_path / "r.yaml").write_text(yaml.safe_dump(running))
    (tmp_path / "n.yaml").write_text(yaml.safe_dump(spec))
    monkeypatch.setenv("ALLOW_SPEC_REMOVALS", "1")
    assert drift.main(["--running", str(tmp_path / "r.yaml"), "--new", str(tmp_path / "n.yaml")]) == 0


def test_the_domain_comes_from_config_so_deploys_keep_it():
    """The fix for the domain removal: the render carries it."""
    spec = render.render("production", D, D, domain="neohrs.com")
    assert spec["domains"] == [{"domain": "neohrs.com", "type": "PRIMARY"}]
    running = copy.deepcopy(spec)
    assert drift.diff(running, spec)["removals"] == []


def test_a_malformed_domain_is_refused():
    with pytest.raises(render.RenderError):
        render.render("production", D, D, domain="not a host")


# ── §43: the workflow asks for no more than it needs ───────────────────────

def test_no_job_can_write_to_the_repository():
    import yaml

    wf = yaml.safe_load((REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    assert wf["permissions"] == {"contents": "read"}
    for name, job in wf["jobs"].items():
        for scope, level in (job.get("permissions") or {}).items():
            assert level == "read", f"job {name} requests {scope}: {level}"
