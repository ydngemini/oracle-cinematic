"""One switch that stops a restored copy of Neoh from touching the real world.

The problem this solves
───────────────────────
A restore is only useful if you can start the application against it and check
that the data is right. But a restored Neoh is a *complete* Neoh: it has the
same Telnyx key, the same Plivo credentials, the same Stripe key and the same
SMTP settings as production, because they came out of the same environment. The
moment its scheduler ticks, it will text real clients, place real calls, charge
real cards and send real email — from data that may be hours old, about deals
that may already have closed.

That is the actual disaster. Not the outage: the recovery.

Why one switch and not twenty
──────────────────────────────
This codebase already has around twenty individual feature flags
(`ORACLE_MISSIONS_ENABLED`, `ORACLE_SCHEDULER_ENABLED`, and so on). An operator
restoring a database at 3 a.m. during an incident cannot be asked to remember
all of them, and forgetting one is indistinguishable from remembering all of
them right up until a customer's phone rings. Safety that depends on recalling
a list is not safety.

`ORACLE_RECOVERY_MODE=1` blocks every outbound side effect at once.

Where the guard goes, and why not at the factory
─────────────────────────────────────────────────
The obvious choke point looks like `get_messaging_provider()` /
`get_voice_provider()` — if you cannot obtain a provider you cannot send. But
`messaging_api.py` constructs `TelnyxMessagingProvider()` directly, so a factory
guard would have had a hole in it from the first day, and the hole would be
invisible: everything would look guarded.

So the guard sits on the egress methods themselves, and
`tests/test_recovery_mode.py` asserts by AST that every method in
`MUST_BE_GUARDED` on every concrete provider calls it. A new provider, or a new
method on an existing one, fails that test until it is either guarded or
explicitly classified as read-only.

Fail closed
───────────
An unset variable means normal operation, because the common case is production
and production must work. But anything that is set and is not recognisably
"off" counts as ON. A typo like `ORACLE_RECOVERY_MODE=ture` protects the
customer instead of exposing them — the wrong guess in this direction costs an
operator a confusing error, and in the other direction costs a client a phone
call from a system that should not have been talking to anyone.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

log = logging.getLogger("oracle.recovery_mode")

ENV_VAR = "ORACLE_RECOVERY_MODE"

#: Only these mean "off". Everything else that is set means ON — see "Fail
#: closed" above.
_OFF_VALUES = {"", "0", "false", "no", "off"}

#: Egress methods that must call `guard()`. Named here rather than inferred,
#: because the distinction is about consequence, not about signature: a method
#: that reads a brand's status is safe from a restored copy, and one that
#: registers a brand is not.
MUST_BE_GUARDED: frozenset[str] = frozenset({
    # Reaches a customer directly.
    "send_message",
    "place_call",
    "transfer_call",
    # Mutates state at the provider. Harmless-looking, but a restored copy
    # disconnecting a hosted number or re-pointing a webhook breaks the
    # PRODUCTION number it still shares.
    "abort_call",
    "provision_forwarding_number",
    "configure_number_webhook",
    "begin_hosted_messaging",
    "disconnect_hosted_number",
    "start_ownership_verification",
    "complete_ownership_verification",
    "upload_hosted_documents",
    "create_messaging_profile",
    "create_brand",
    "submit_campaign",
    "assign_number_to_campaign",
    "verify_caller_id_start",
    "verify_caller_id_complete",
})


class RecoveryModeBlocked(RuntimeError):
    """A side effect was refused because this instance is in recovery mode.

    A distinct type on purpose: a caller that treats every provider failure as
    "retry later" would otherwise queue the call up to be made the moment
    recovery mode is lifted, which is the same customer contact one restart
    later.
    """

    def __init__(self, action: str, detail: Optional[str] = None):
        self.action = action
        self.detail = detail
        message = (
            f"Refused to {action}: this instance is running in recovery mode "
            f"({ENV_VAR} is set). It is a restored or non-production copy and "
            f"must not contact customers or mutate provider state."
        )
        if detail:
            message += f" ({detail})"
        super().__init__(message)


def is_recovery_mode() -> bool:
    """True when this instance must not cause any outward side effect.

    Read from the environment on every call rather than cached at import: an
    operator who realises mid-incident that they are on the wrong instance can
    set it and restart a worker without reasoning about import order, and a
    test can set it without fighting module state.
    """
    raw = os.getenv(ENV_VAR)
    if raw is None:
        return False
    return raw.strip().lower() not in _OFF_VALUES


def guard(action: str, *, detail: Optional[str] = None) -> None:
    """Refuse `action` when in recovery mode. A no-op in normal operation.

    `action` is a human phrase completing "Refused to ..." — it ends up in an
    operator's log during an incident, so "send an SMS to +1555…" beats
    "send_message".
    """
    if not is_recovery_mode():
        return
    log.warning("RECOVERY MODE: refused to %s", action)
    raise RecoveryModeBlocked(action, detail)


def describe() -> dict:
    """Machine-readable state, for /health and the recovery-mode banner.

    The UI needs to say plainly which instance someone is looking at. An
    operator comparing a restored copy against production with two identical
    browser tabs open is exactly how the wrong one gets acted on.
    """
    active = is_recovery_mode()
    return {
        "recovery_mode": active,
        "outbound_side_effects": "blocked" if active else "enabled",
        "blocks": sorted(MUST_BE_GUARDED) if active else [],
        "env_var": ENV_VAR,
    }
