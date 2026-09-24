"""The switch that stops a restored Neoh from contacting real customers.

A restored copy is a complete copy: same Telnyx key, same Plivo credentials,
same Stripe key, same SMTP settings, because they all came out of the same
environment. Start it to check the data looks right and its scheduler will text
real clients about deals that may already have closed.

These tests exist because that is a failure you only get to have once.
"""

from __future__ import annotations

import ast
import inspect
import io
import pathlib

import pytest

import recovery_mode
from recovery_mode import MUST_BE_GUARDED, RecoveryModeBlocked

BACKEND = pathlib.Path(__file__).resolve().parent.parent


# ── the switch itself ───────────────────────────────────────────────────────

def test_unset_means_normal_operation(monkeypatch):
    monkeypatch.delenv(recovery_mode.ENV_VAR, raising=False)
    assert recovery_mode.is_recovery_mode() is False
    recovery_mode.guard("send an SMS")  # must not raise


@pytest.mark.parametrize("value", ["", "0", "false", "FALSE", "no", "off", " off "])
def test_recognised_off_values_mean_normal_operation(monkeypatch, value):
    monkeypatch.setenv(recovery_mode.ENV_VAR, value)
    assert recovery_mode.is_recovery_mode() is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", "ture", "y", "please"])
def test_anything_else_set_means_blocked(monkeypatch, value):
    """Fail closed. A typo must protect the customer, not expose them.

    `ORACLE_RECOVERY_MODE=ture` costs an operator a confusing error. The other
    reading of the same typo costs a client a phone call from a system that
    should not have been talking to anyone.
    """
    monkeypatch.setenv(recovery_mode.ENV_VAR, value)
    assert recovery_mode.is_recovery_mode() is True
    with pytest.raises(RecoveryModeBlocked):
        recovery_mode.guard("send an SMS")


def test_the_refusal_is_its_own_exception_type(monkeypatch):
    """Not a generic provider error. A caller that treats every provider
    failure as "retry later" would otherwise queue the message up to be sent
    the moment recovery mode is lifted — the same customer contact, one
    restart later."""
    monkeypatch.setenv(recovery_mode.ENV_VAR, "1")
    with pytest.raises(RecoveryModeBlocked) as excinfo:
        recovery_mode.guard("place a call to +15551234567")

    assert excinfo.value.action == "place a call to +15551234567"
    assert recovery_mode.ENV_VAR in str(excinfo.value)
    # It is a RuntimeError, so existing broad handlers still catch it, but it
    # is distinguishable for anything that wants to.
    assert isinstance(excinfo.value, RuntimeError)


def test_reads_the_environment_every_time(monkeypatch):
    """Not cached at import. An operator who realises mid-incident that they
    are on the wrong instance sets the variable and restarts a worker; they
    should not also have to reason about module import order."""
    monkeypatch.delenv(recovery_mode.ENV_VAR, raising=False)
    assert recovery_mode.is_recovery_mode() is False
    monkeypatch.setenv(recovery_mode.ENV_VAR, "1")
    assert recovery_mode.is_recovery_mode() is True


def test_describe_reports_state_for_the_banner(monkeypatch):
    monkeypatch.setenv(recovery_mode.ENV_VAR, "1")
    state = recovery_mode.describe()
    assert state["recovery_mode"] is True
    assert state["outbound_side_effects"] == "blocked"
    assert "send_message" in state["blocks"]

    monkeypatch.delenv(recovery_mode.ENV_VAR, raising=False)
    assert recovery_mode.describe()["recovery_mode"] is False


# ── the part that actually matters: every egress point is wired ────────────

def _concrete_provider_methods(module_name: str, base_name: str):
    """Yield (class, method, source) for each concrete provider method."""
    path = BACKEND / f"{module_name}.py"
    tree = ast.parse(io.open(path, encoding="utf-8").read())
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        base_names = {getattr(b, "id", getattr(b, "attr", "")) for b in node.bases}
        if base_name not in base_names:
            continue  # the abstract base itself, or an unrelated class
        for fn in node.body:
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield node.name, fn.name, ast.unparse(fn)


@pytest.mark.parametrize(
    "module_name, base_name",
    [("messaging_provider", "MessagingProvider"), ("voice_provider", "VoiceProvider")],
)
def test_every_egress_method_on_every_provider_calls_the_guard(module_name, base_name):
    """The invariant, and the reason the guard is not on the factory.

    `get_messaging_provider()` looks like the natural choke point — no provider,
    no send. But `messaging_api.py` constructs `TelnyxMessagingProvider()`
    directly, so a factory guard would have had a hole in it from day one, and
    the hole would have been invisible: everything would still look guarded.

    So the guard is on the methods, and this test is what keeps it there. A new
    provider, or a new method on an existing one, fails here until it either
    calls the guard or is deliberately left out of MUST_BE_GUARDED because it
    only reads.
    """
    unguarded = [
        f"{cls}.{name}"
        for cls, name, src in _concrete_provider_methods(module_name, base_name)
        if name in MUST_BE_GUARDED and "recovery_mode.guard" not in src
    ]
    assert not unguarded, (
        "These methods reach the outside world but do not call "
        "recovery_mode.guard(), so a restored copy of Neoh would use them "
        "against real customers:\n  " + "\n  ".join(unguarded)
    )


@pytest.mark.parametrize(
    "module_name, base_name",
    [("messaging_provider", "MessagingProvider"), ("voice_provider", "VoiceProvider")],
)
def test_the_guarded_list_has_not_gone_stale(module_name, base_name):
    """MUST_BE_GUARDED naming a method nobody implements is a dead entry that
    reads as coverage."""
    implemented = {name for _cls, name, _src in _concrete_provider_methods(module_name, base_name)}
    # Only assert for this module's own surface: the two providers between them
    # must implement something for each name they are responsible for.
    named_here = {
        name for _cls, name, src in _concrete_provider_methods(module_name, base_name)
        if "recovery_mode.guard" in src
    }
    assert named_here <= MUST_BE_GUARDED, (
        f"{module_name} guards methods that MUST_BE_GUARDED does not list: "
        f"{sorted(named_here - MUST_BE_GUARDED)}"
    )
    assert named_here <= implemented


def test_smtp_send_is_guarded():
    import smtp_mailer

    assert "recovery_mode.guard" in inspect.getsource(smtp_mailer.send), (
        "every invitation, digest and notification goes through this one function"
    )


def test_stripe_mutations_are_guarded():
    import billing

    for fn in (billing.create_checkout_session, billing.create_portal_session):
        assert "recovery_mode.is_recovery_mode()" in inspect.getsource(fn), (
            f"{fn.__name__} reaches Stripe with production's live key"
        )


def test_the_scheduler_refuses_to_start_in_recovery_mode():
    from data_integrations.periodic import PeriodicScheduler

    source = inspect.getsource(PeriodicScheduler.start)
    assert "recovery_mode.is_recovery_mode()" in source
    # Recovery mode must be checked BEFORE the enable flag, or a deployment
    # with the scheduler already enabled would start it anyway.
    assert source.index("recovery_mode.is_recovery_mode()") < source.index("SCHEDULER_ENABLED"), (
        "recovery mode must outrank ORACLE_SCHEDULER_ENABLED"
    )


def test_a_guarded_provider_method_actually_refuses_at_runtime(monkeypatch):
    """The AST tests prove the call is written. This proves it fires."""
    monkeypatch.setenv(recovery_mode.ENV_VAR, "1")

    import asyncio

    from voice_provider import PlivoVoiceProvider

    with pytest.raises(RecoveryModeBlocked):
        asyncio.run(
            PlivoVoiceProvider().place_call(
                to_number="+15551234567",
                from_number="+15559876543",
                answer_url="https://example.invalid/answer",
            )
        )
