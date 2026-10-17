"""Heuristic valuation targets are labelled as such and cannot be promoted as a
valuation (prompt 11, T11.07; audit A08; ledger L11-05).

The state underwriter adapter is trained on synthetic contracts whose ARV,
rehab and offer-ceiling targets come from fixed constants (a county $/sqft
table and the 70% rule). Nothing here makes those numbers better; it makes
every artifact say what they are, and keeps a model trained on them — or on
targets nobody recorded — from being activated as an underwriter.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from uuid import UUID

import pytest
from fastapi import HTTPException

import models_api
from ml_forge import cot_synthesizer
from ml_forge.edge_forge import train_lora
from tenancy import Role, TenantContext

TENANT_ID = "11111111-1111-1111-1111-111111111111"
OWNER = TenantContext(agent_id="broker@tenant.test", tenant_id=TENANT_ID, role=Role.BROKER_OWNER)
MODEL_ID = UUID("22222222-2222-4222-8222-222222222222")

RECORD = {
    "property": {"sqft": 1500, "county": "Kent", "year_built": 1950},
    "original_contract": {"purchase_price": 120000, "earnest_money": 1000, "contingencies": ["inspection"]},
}


# --------------------------------------------------------------------------- #
# Training artifacts say where the targets came from
# --------------------------------------------------------------------------- #

def test_the_underwriting_manifest_labels_its_targets_heuristic_and_claims_no_licence(tmp_path):
    data = tmp_path / "contracts.jsonl"
    data.write_text(json.dumps(RECORD) + "\n", encoding="utf-8")
    manifest = train_lora.underwriting_manifest(str(data))
    assert "licensed" not in manifest["kind"], "synthetic contracts are not a licensed dataset"
    assert manifest["kind"] == "synthetic_heuristic_underwriting"
    targets = manifest["targets"]
    assert targets["provenance"] == train_lora.HEURISTIC_TARGETS
    assert targets["validated_against_transactions"] is False
    assert targets["rules"]["arv_usd_per_sqft_by_county"] == train_lora._COUNTY_PPSF
    assert targets["rules"]["mao"] == "0.70 * ARV - rehab"
    assert manifest["dataset_sha256"] == train_lora.artifact_sha256(str(data))


def test_the_reported_rules_are_the_ones_the_targets_were_computed_with():
    """No drift between the label and the arithmetic: recompute one target
    from the manifest's own numbers."""
    rules = train_lora.underwriting_targets()["rules"]
    out = train_lora._underwrite(RECORD)
    arv = train_lora._round_k(1500 * rules["arv_usd_per_sqft_by_county"]["Kent"])
    rehab_psf = (rules["rehab_usd_per_sqft"]["base"] + rules["rehab_usd_per_sqft"]["built_40_years_ago"]
                 + rules["rehab_usd_per_sqft"]["inspection_contingency"])
    rehab = train_lora._round_k(1500 * rehab_psf)
    assert (out["arv_estimate"], out["rehab_estimate"]) == (arv, rehab)
    assert out["mao"] == train_lora._round_k(rules["mao_factor"] * arv - rehab)


def test_the_underwriting_model_card_forbids_presenting_it_as_a_valuation(tmp_path):
    data = tmp_path / "contracts.jsonl"
    data.write_text(json.dumps(RECORD) + "\n", encoding="utf-8")
    card = train_lora.model_card_for(train_lora.underwriting_manifest(str(data)), name="de-lora", steps=60)
    assert card["intended_use"] != "Agent-approved tone and formatting preferences only."
    assert "not a valuation" in card["intended_use"]
    prohibited = " ".join(card["prohibited_use"])
    for phrase in ("appraisal", "automated valuation", "verified deal price"):
        assert phrase in prohibited
    assert card["dataset"]["targets"]["provenance"] == "heuristic"
    assert any("per-region error and uncertainty" in line for line in card["limitations"])
    assert card["metrics"]["evaluation_status"] == "pending"


def test_a_style_adapter_card_is_unchanged():
    card = train_lora.model_card_for({"kind": "consented_agent_style"}, name="agent-a-lora", steps=10)
    assert card["intended_use"] == "Agent-approved tone and formatting preferences only."
    assert card["prohibited_use"] == [
        "personality or distress profiling",
        "protected-class inference",
        "autonomous legal, financial, outreach, or bidding decisions",
    ]


def test_a_cot_sample_names_the_rules_that_wrote_it_not_a_model():
    record = {
        "record_id": "DE-ASG-0001",
        "property": {"address": "1 Main St", "city": "Dover", "county": "Kent", "sqft": 1400,
                     "year_built": 1960, "bedrooms": 3, "bathrooms": 1},
        "original_contract": {"purchase_price": 90000, "assignment_fee": 10000},
    }
    meta = cot_synthesizer.build_training_sample(record, "DE-ASG-0001.json")["_meta"]
    assert "model" not in meta, "the samples are computed by rules; no model wrote them"
    assert meta["generator"] == "rules:cot_synthesizer"
    assert meta["targets"] == "heuristic"


# --------------------------------------------------------------------------- #
# A valuation model is promoted only on observed targets with per-region error
# --------------------------------------------------------------------------- #

PER_REGION = {"per_region": {"New Castle": {"n": 140, "error": 0.081, "uncertainty": 0.12},
                             "Kent": {"n": 60, "error": 0.104, "uncertainty": 0.17}}}
OBSERVED_CARD = {"dataset": {"targets": {"provenance": "observed_transactions"}}}


class _Conn:
    def __init__(self, model, metrics=()):
        self.model, self.metrics, self.statements = model, list(metrics), []

    async def fetchrow(self, query, *args):
        self.statements.append(query)
        if "SELECT * FROM model_registry" in query:
            return self.model
        if "UPDATE model_registry" in query and "RETURNING" in query:
            return {**self.model, "status": "active"}
        return None

    async def fetchval(self, query, *_a):
        self.statements.append(query)
        return True if "model_evaluations" in query else None

    async def fetch(self, query, *_a):
        """Only a query that asks for passed AND reviewed evaluations gets the
        metrics, so dropping either predicate makes the gate refuse."""
        self.statements.append(query)
        flat = " ".join(query.split())
        reviewed = all(p in flat for p in ("passed=true", "leakage_reviewed=true", "geographic_bias_reviewed=true"))
        return [{"metrics": json.dumps(m)} for m in self.metrics] if "model_evaluations" in flat and reviewed else []

    async def execute(self, query, *_a):
        self.statements.append(query)


def _activate(monkeypatch, model, metrics=()):
    conn = _Conn({"id": MODEL_ID, "scope_type": "state", "scope_key": "DE", "status": "validated",
                  "rollback_model_id": None, **model}, metrics)

    @asynccontextmanager
    async def tx(_ctx):
        yield conn

    monkeypatch.setattr(models_api, "tenant_tx", tx)
    monkeypatch.setattr(models_api, "require_feature", lambda *_a: None)
    result = asyncio.run(models_api.activate_model(
        model_id=MODEL_ID, body=models_api.Decision(reason="promote after evaluation"), ctx=OWNER))
    return result, conn


@pytest.mark.parametrize("card", [
    {"dataset": {"targets": {"provenance": "heuristic"}}},
    {"dataset": {"kind": "licensed_state_underwriting"}},  # what the old trainer wrote: no provenance
    {},
    None,
    "not json",
])
def test_an_underwriter_trained_on_heuristic_or_unrecorded_targets_never_activates(monkeypatch, card):
    with pytest.raises(HTTPException) as exc:
        _activate(monkeypatch, {"model_kind": "state_lora",
                                "model_card": json.dumps(card) if isinstance(card, dict) else card}, [PER_REGION])
    assert exc.value.status_code == 409
    assert "heuristic or unrecorded targets" in exc.value.detail


@pytest.mark.parametrize("metrics", [
    [],
    [{"accuracy": 0.91}],
    [{"per_region": {}}],
    [{"per_region": {"Kent": {"n": 60, "error": 0.1}}}],                     # no uncertainty
    [{"per_region": {"Kent": {"n": 0, "error": 0.1, "uncertainty": 0.2}}}],  # nothing measured
    [{"per_region": {"Kent": {"n": 60, "error": "low", "uncertainty": 0.2}}}],
    [{"per_region": {"Kent": {"n": 60, "error": True, "uncertainty": 0.2}}}],
    [{"per_region": {"Kent": {"n": 60, "error": float("nan"), "uncertainty": 0.2}}}],
])
def test_an_underwriter_without_per_region_error_and_uncertainty_never_activates(monkeypatch, metrics):
    with pytest.raises(HTTPException) as exc:
        _activate(monkeypatch, {"model_kind": "state_lora", "model_card": json.dumps(OBSERVED_CARD)}, metrics)
    assert exc.value.status_code == 409
    assert "per-region error and uncertainty" in exc.value.detail


def test_an_underwriter_on_observed_targets_with_published_error_activates(monkeypatch):
    result, conn = _activate(monkeypatch, {"model_kind": "state_lora", "model_card": OBSERVED_CARD}, [PER_REGION])
    assert result["model"]["status"] == "active"
    assert any("SELECT metrics FROM model_evaluations" in s and "passed=true" in s for s in conn.statements), \
        "only passed, reviewed evaluations count"


def test_a_style_adapter_is_not_held_to_the_valuation_gate(monkeypatch):
    result, conn = _activate(monkeypatch, {"model_kind": "agent_style_lora", "scope_type": "agent",
                                           "scope_key": "agent-a", "model_card": None})
    assert result["model"]["status"] == "active"
    assert not any("SELECT metrics FROM model_evaluations" in s for s in conn.statements)


@pytest.mark.parametrize("metrics", [
    # Every region must be measured, not just one of them (review L11-06).
    [{"per_region": {"New Castle": {"n": 140, "error": 0.08, "uncertainty": 0.12},
                     "Kent": {"n": 60, "error": 0.1}}}],
    [{"per_region": {"Kent": {"n": 60, "error": 0.1, "uncertainty": -1}}}],     # negative
    [{"per_region": {"Kent": {"n": 60, "error": -0.1, "uncertainty": 0.2}}}],
    [{"per_region": {"Kent": {"n": 0.5, "error": 0.1, "uncertainty": 0.2}}}],    # not a count
    [{"per_region": {"Kent": {"n": 10 ** 400, "error": 0.1, "uncertainty": 0.2}}}],  # overflows a float
    [{"per_region": {"Kent": [60, 0.1, 0.2]}}],
    [{"per_region": "Kent: fine"}],
])
def test_a_partial_or_impossible_per_region_report_is_refused_not_crashed(monkeypatch, metrics):
    with pytest.raises(HTTPException) as exc:
        _activate(monkeypatch, {"model_kind": "state_lora", "model_card": json.dumps(OBSERVED_CARD)}, metrics)
    assert exc.value.status_code == 409 and "per-region error and uncertainty" in exc.value.detail


@pytest.mark.parametrize("card", [{"dataset": "s3://bucket/de.jsonl"}, {"dataset": ["x"]},
                                  {"dataset": {"targets": "observed_transactions"}}, [1, 2]])
def test_a_malformed_card_is_a_409_with_the_reason_not_a_500(monkeypatch, card):
    with pytest.raises(HTTPException) as exc:
        _activate(monkeypatch, {"model_kind": "state_lora", "model_card": json.dumps(card)}, [PER_REGION])
    assert exc.value.status_code == 409 and "heuristic or unrecorded targets" in exc.value.detail


@pytest.mark.parametrize("kind", ["forecast", "scoring", "base"])
def test_a_card_that_declares_heuristic_targets_is_gated_under_any_kind(monkeypatch, kind):
    """Registering the trainer's heuristic underwriter as another kind does not
    escape the gate: the card itself says what the targets are."""
    card = {"dataset": {"kind": "synthetic_heuristic_underwriting", "targets": {"provenance": "heuristic"}}}
    with pytest.raises(HTTPException) as exc:
        _activate(monkeypatch, {"model_kind": kind, "model_card": card}, [PER_REGION])
    assert exc.value.status_code == 409


def test_a_missing_year_built_uses_the_reported_default_age():
    rules = train_lora.underwriting_targets()["rules"]
    record = {"property": {"sqft": 1000, "county": "Atlantis"}, "original_contract": {"purchase_price": 1}}
    out = train_lora._underwrite(record)
    age = rules["age_years"]
    assumed = age["assumed_when_year_built_missing"]
    extra = (rules["rehab_usd_per_sqft"]["built_40_years_ago"] if assumed >= age["old"]
             else rules["rehab_usd_per_sqft"]["built_25_years_ago"] if assumed >= age["aging"] else 0)
    assert out["arv_estimate"] == train_lora._round_k(1000 * rules["arv_usd_per_sqft_default"])
    assert out["rehab_estimate"] == train_lora._round_k(1000 * (rules["rehab_usd_per_sqft"]["base"] + extra))
    assert age["reference_year"] == train_lora._CURRENT_YEAR


def test_prebuilt_chat_rows_are_unrecorded_targets_not_this_trainers_rules(tmp_path):
    """--data training_data/swarm_textbook_bedrock_v2.jsonl brings targets
    cot_synthesizer computed with other constants; the manifest must not
    claim this file's rules for them."""
    data = tmp_path / "prebuilt.jsonl"
    data.write_text(json.dumps({"messages": [{"role": "user", "content": "x"}]}) + "\n"
                    + json.dumps(RECORD) + "\n", encoding="utf-8")
    manifest = train_lora.underwriting_manifest(str(data))
    assert manifest["targets"]["provenance"] == "unrecorded" and "rules" not in manifest["targets"]
    assert manifest["rows"] == {"raw_contracts": 1, "prebuilt_chat_rows": 1}
    card = train_lora.model_card_for(manifest, name="de-lora", steps=1)
    assert "not a valuation" in card["intended_use"] and "unrecorded" in card["intended_use"]
    assert any("not recorded" in line for line in card["limitations"])


def test_the_cot_text_a_model_learns_claims_no_market_source():
    record = {
        "record_id": "DE-ASG-0001",
        "property": {"address": "1 Main St", "city": "Wilmington", "county": "New Castle", "sqft": 1400,
                     "year_built": 1960, "bedrooms": 3, "bathrooms": 1},
        "original_contract": {"purchase_price": 90000, "assignment_fee": 10000},
    }
    sample = cot_synthesizer.build_training_sample(record, "DE-ASG-0001.json")
    assert "County median" not in sample["output"]
    assert "highest median values" not in sample["output"]
    assert "unsourced" in sample["output"]
    assert sample["_meta"]["inputs"] == "synthetic"


def test_a_generated_contract_names_the_model_that_answered(monkeypatch):
    from ml_forge import synthetic_lawyer

    calls = []

    def invoke(model_id, prompt, max_tokens=2048):
        calls.append(model_id)
        return None if model_id == synthetic_lawyer.PRIMARY_MODEL else '{"record_id": "DE-ASG-0009"}'

    monkeypatch.setattr(synthetic_lawyer, "invoke_bedrock_model", invoke)
    record = synthetic_lawyer.generate_record(9)
    assert calls == [synthetic_lawyer.PRIMARY_MODEL, synthetic_lawyer.SECONDARY_MODEL]
    assert record["_meta"]["model"] == synthetic_lawyer.SECONDARY_MODEL
    assert record["_meta"]["synthetic"] is True


# --------------------------------------------------------------------------- #
# A decided version's artifact and card are never swapped (review L11-06)
# --------------------------------------------------------------------------- #

class _RegisterConn:
    def __init__(self, existing_status):
        self.existing_status, self.statements = existing_status, []

    async def fetchrow(self, query, *args):
        self.statements.append(" ".join(query.split()))
        guarded = "WHERE model_registry.status IN ('candidate','rejected')" in self.statements[-1]
        if self.existing_status in (None, "candidate", "rejected") or not guarded:
            return {"id": MODEL_ID, "status": self.existing_status or "candidate", "model_card": args[9]}
        return None

    async def fetchval(self, query, *_a):
        return self.existing_status


def _register(monkeypatch, existing_status):
    conn = _RegisterConn(existing_status)

    @asynccontextmanager
    async def tx(_ctx):
        yield conn

    monkeypatch.setattr(models_api, "tenant_tx", tx)
    monkeypatch.setattr(models_api, "require_feature", lambda *_a: None)
    body = models_api.ModelRegister(
        name="de-uw", version="1", model_kind="state_lora", scope_type="state", scope_key="DE",
        base_model="base", artifact_uri="s3://b/heuristic", artifact_sha256="b" * 64,
        model_card={"dataset": {"targets": {"provenance": "heuristic"}}})
    return asyncio.run(models_api.register_model(body=body, ctx=OWNER)), conn


@pytest.mark.parametrize("status", ["validated", "canary", "active", "fallback", "retired"])
def test_re_registering_a_decided_version_is_refused(monkeypatch, status):
    with pytest.raises(HTTPException) as exc:
        _register(monkeypatch, status)
    assert exc.value.status_code == 409
    assert status in exc.value.detail and "new version" in exc.value.detail


@pytest.mark.parametrize("status", [None, "candidate", "rejected"])
def test_a_new_or_undecided_version_can_still_be_registered(monkeypatch, status):
    row, _conn = _register(monkeypatch, status)
    assert row["id"] == str(MODEL_ID)


def test_a_training_run_never_swaps_a_decided_versions_artifact(monkeypatch):
    statements = []

    class Conn:
        async def execute(self, query, *args):
            statements.append((" ".join(query.split()), args))

        async def fetchrow(self, query, *_a):
            statements.append((" ".join(query.split()), ()))
            return None  # ON CONFLICT ... WHERE status IN ('candidate','rejected') matched nothing

    @asynccontextmanager
    async def tx(_ctx):
        yield Conn()

    async def train(_manifest, _reporter):
        return {"model_card": {"version": "fixed"}, "artifact_uri": "s3://b/a", "artifact_sha256": "c" * 64}

    class Reporter:
        job = {"tenant_id": TENANT_ID}

    monkeypatch.setattr(models_api, "tenant_tx", tx)
    monkeypatch.setattr(models_api, "runpod_train", train)
    payload = {"training_run_id": "run-1", "manifest": {"scope_type": "state", "scope_key": "DE",
                                                        "base_model": "base", "agent_id": None}}
    with pytest.raises(ValueError, match="new version"):
        asyncio.run(models_api._training_job(payload, Reporter()))
    upsert = [q for q, _ in statements if "INSERT INTO model_registry" in q][0]
    assert "WHERE model_registry.status IN ('candidate','rejected')" in upsert
    assert not any("status='succeeded'" in q for q, _ in statements)
    failed = [a for q, a in statements if "status='failed'" in q]
    assert failed and "new version" in failed[-1][1]
