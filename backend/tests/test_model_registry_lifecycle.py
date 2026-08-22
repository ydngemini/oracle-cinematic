"""The model lifecycle, characterized before automation is built on top of it.

`models_api` already implements register → evaluate → activate → rollback, and
`activate_model` already enforces a real gate: a model may only go live if some
`model_evaluations` row has `passed AND leakage_reviewed AND geographic_bias_reviewed`.
That gate is the safety property the automated training loop will inherit, and it
had **no test coverage at all** — so "don't regress the suite" said nothing about
the one behaviour that matters most here.

These tests pin the current behaviour exactly. When the loop later activates
models without a human clicking approve, it must go through this same gate; if a
change makes any of these pass for the wrong reason, the automation is ungoverned.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

import pytest

import models_api
from tenancy import Role, TenantContext


TENANT_ID = "11111111-1111-1111-1111-111111111111"
OWNER = TenantContext(agent_id="broker@tenant.test", tenant_id=TENANT_ID, role=Role.BROKER_OWNER)
AGENT = TenantContext(agent_id="agent@tenant.test", tenant_id=TENANT_ID, role=Role.AGENT)

MODEL_ID = UUID("22222222-2222-4222-8222-222222222222")
OLD_ID = UUID("33333333-3333-4333-8333-333333333333")


def _model(**overrides):
    base = {
        "id": MODEL_ID,
        "model_kind": "state_lora",
        "scope_type": "state",
        "scope_key": "DE",
        "status": "validated",
        "rollback_model_id": None,
        "artifact_uri": "s3://bucket/adapter.safetensors",
    }
    base.update(overrides)
    return base


class _Conn:
    """Dispatches on query text and records every statement executed."""

    def __init__(self, *, model=None, gate_passed=True, existing_active=None):
        self._model = model
        self._gate_passed = gate_passed
        self._existing_active = existing_active
        self.statements: list[str] = []

    async def fetchrow(self, query, *args):
        self.statements.append(query)
        if "SELECT * FROM model_registry" in query:
            return self._model
        if "WHERE model_kind=$1" in query:
            return self._existing_active
        if "UPDATE model_registry" in query and "RETURNING" in query:
            # The row as it looks after activation.
            return {**(self._model or {}), "status": "active", "rollback_model_id": args[1] if len(args) > 1 else None}
        return None

    async def fetchval(self, query, *_args):
        self.statements.append(query)
        if "model_evaluations" in query:
            return self._gate_passed
        return None

    async def execute(self, query, *_args):
        self.statements.append(query)

    def ran(self, needle: str) -> bool:
        return any(needle in statement for statement in self.statements)


def _patch(monkeypatch, conn):
    @asynccontextmanager
    async def _tx(_ctx):
        yield conn

    monkeypatch.setattr(models_api, "tenant_tx", _tx)
    return conn


def _activate(ctx=OWNER):
    return asyncio.run(
        models_api.activate_model(
            model_id=MODEL_ID, body=models_api.Decision(reason="promoting after evaluation"), ctx=ctx
        )
    )


# ---------------------------------------------------------------------------
# The gate — the property the automated loop must inherit unchanged
# ---------------------------------------------------------------------------

def test_activation_requires_a_passed_evaluation(monkeypatch):
    """No evaluation, no activation. This is the whole safety story."""
    conn = _patch(monkeypatch, _Conn(model=_model(), gate_passed=False))

    with pytest.raises(models_api.HTTPException) as excinfo:
        _activate()

    assert excinfo.value.status_code == 409
    assert "evaluation gate" in str(excinfo.value.detail).lower()
    assert not conn.ran("SET status='active'"), "nothing may be promoted when the gate fails"


def test_the_gate_requires_leakage_and_bias_review_together(monkeypatch):
    """The SQL demands passed AND leakage_reviewed AND geographic_bias_reviewed.

    Pinned as text because an automated evaluator will later set these; if the
    predicate is ever loosened, the loop silently starts shipping unreviewed
    models and every other test here would still pass.
    """
    conn = _patch(monkeypatch, _Conn(model=_model()))
    _activate()

    gate = next(s for s in conn.statements if "model_evaluations" in s)
    assert "passed=true" in gate
    assert "leakage_reviewed=true" in gate
    assert "geographic_bias_reviewed=true" in gate


def test_only_validated_or_canary_models_can_activate(monkeypatch):
    for status in ("candidate", "retired", "fallback", "active"):
        conn = _patch(monkeypatch, _Conn(model=_model(status=status)))
        with pytest.raises(models_api.HTTPException) as excinfo:
            _activate()
        assert excinfo.value.status_code == 409, f"status {status!r} should not activate"
        assert not conn.ran("SET status='active'")


def test_a_canary_may_be_promoted(monkeypatch):
    """`canary` is already an accepted input state — the automated promotion
    path in the plan depends on this and does not need a schema change."""
    conn = _patch(monkeypatch, _Conn(model=_model(status="canary")))

    result = _activate()

    assert result["model"]["status"] == "active"
    assert conn.ran("SET status='active'")


def test_activation_is_refused_for_an_unknown_model(monkeypatch):
    _patch(monkeypatch, _Conn(model=None))

    with pytest.raises(models_api.HTTPException) as excinfo:
        _activate()

    assert excinfo.value.status_code == 404


# ---------------------------------------------------------------------------
# Scope exclusivity — what makes "the active model" a single answer
# ---------------------------------------------------------------------------

def test_activating_demotes_the_previous_active_model_in_the_same_scope(monkeypatch):
    """Two active models for one scope would make resolution ambiguous.

    The demotion is to `fallback`, not `retired` — the outgoing model becomes the
    rollback target, which is what makes automatic rollback possible later.
    """
    conn = _patch(monkeypatch, _Conn(model=_model(), existing_active={"id": OLD_ID}))

    result = _activate()

    assert conn.ran("SET status='fallback'"), "the outgoing model must be demoted"
    # _row() stringifies UUIDs on the way out, which is the wire contract.
    assert result["model"]["rollback_model_id"] == str(OLD_ID), "and recorded as the rollback target"


def test_the_previous_active_is_looked_up_by_kind_scope_and_key(monkeypatch):
    """Scope is (model_kind, scope_type, scope_key) — a state LoRA for DE must
    not demote the one for TX, nor an agent-style adapter."""
    conn = _patch(monkeypatch, _Conn(model=_model(), existing_active={"id": OLD_ID}))
    _activate()

    lookup = next(s for s in conn.statements if "WHERE model_kind=$1" in s)
    assert "scope_type=$2" in lookup
    assert "scope_key=$3" in lookup
    assert "status='active'" in lookup


def test_first_activation_in_a_scope_has_no_rollback_target(monkeypatch):
    conn = _patch(monkeypatch, _Conn(model=_model(), existing_active=None))

    result = _activate()

    assert result["model"]["rollback_model_id"] is None
    assert not conn.ran("SET status='fallback'")


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------

def _rollback(ctx=OWNER):
    return asyncio.run(
        models_api.rollback_model(
            model_id=MODEL_ID, body=models_api.Decision(reason="regression observed live"), ctx=ctx
        )
    )


def test_rollback_retires_the_active_model_and_restores_its_predecessor(monkeypatch):
    conn = _patch(monkeypatch, _Conn(model=_model(status="active", rollback_model_id=OLD_ID)))

    result = _rollback()

    assert conn.ran("SET status='retired'"), "the regressing model is retired, not merely demoted"
    assert result["rolled_back_from"] == str(MODEL_ID)


def test_rollback_refuses_when_there_is_nothing_to_roll_back_to(monkeypatch):
    """Better to stay on a regressing model than to fail into no model at all."""
    conn = _patch(monkeypatch, _Conn(model=_model(status="active", rollback_model_id=None)))

    with pytest.raises(models_api.HTTPException) as excinfo:
        _rollback()

    assert excinfo.value.status_code == 409
    assert not conn.ran("SET status='retired'")


def test_rollback_refuses_a_model_that_is_not_active(monkeypatch):
    _patch(monkeypatch, _Conn(model=_model(status="fallback", rollback_model_id=OLD_ID)))

    with pytest.raises(models_api.HTTPException) as excinfo:
        _rollback()

    assert excinfo.value.status_code == 409


# ---------------------------------------------------------------------------
# Authorisation
# ---------------------------------------------------------------------------

def test_an_agent_cannot_activate_or_roll_back_a_model(monkeypatch):
    """Both routes require BROKER_OWNER. When the loop activates models without
    a human, it must still run under a context that legitimately holds this."""
    _patch(monkeypatch, _Conn(model=_model()))
    with pytest.raises(models_api.HTTPException) as excinfo:
        _activate(ctx=AGENT)
    assert excinfo.value.status_code == 403

    _patch(monkeypatch, _Conn(model=_model(status="active", rollback_model_id=OLD_ID)))
    with pytest.raises(models_api.HTTPException) as excinfo:
        _rollback(ctx=AGENT)
    assert excinfo.value.status_code == 403


# ---------------------------------------------------------------------------
# The gap this whole workstream exists to close
# ---------------------------------------------------------------------------

def test_no_inference_path_reads_the_model_registry_yet():
    """An activated model currently serves nothing.

    `model_registry` is referenced only by `models_api` itself — a model can be
    registered, evaluated, activated, and never answer a single request. This
    test records that gap deliberately and is expected to be **inverted** when
    `model_resolver` lands: at that point the registry gains real readers and
    this assertion should be replaced by one proving inference consults it.
    """
    import pathlib

    backend = pathlib.Path(__file__).resolve().parent.parent
    readers = sorted(
        path.name
        for path in backend.glob("*.py")
        if "model_registry" in path.read_text(encoding="utf-8")
    )

    assert readers == ["models_api.py"], (
        f"model_registry now has readers beyond models_api: {readers}. "
        "If model_resolver has landed, replace this test with one asserting "
        "that the resolved model reaches the provider call."
    )
