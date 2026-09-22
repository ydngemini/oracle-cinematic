"""commands_api._execute_command_job's CALL branch — Plivo/Twilio provider
dispatch. Structural (source-inspection) tests in the same style as
test_outbound_verified_caller_id.py's, since the function has heavy
DB/compliance dependencies that make full execution tests expensive; the
properties here are about the CODE SHAPE that prevents a duplicate-call bug:
Plivo is a distinct branch (not a try/except fallback layered on Twilio),
so a Plivo failure can never silently retry through Twilio (or vice versa)
and place the same approved call twice.
"""

from __future__ import annotations

import inspect

import commands_api as ca


def _call_branch_source() -> str:
    source = inspect.getsource(ca._execute_command_job)
    start = source.index("elif command_type is CommandType.CALL:")
    end = source.index(
        'else:\n            await reporter.progress(45, "creating approved calendar event")'
    )
    return source[start:end]


def test_provider_is_read_from_the_route_not_a_client_supplied_field():
    branch = _call_branch_source()
    assert "get_telephony_route(ctx)" in branch
    assert 'call_provider = str((call_route or {}).get("provider") or "twilio")' in branch


def test_plivo_and_twilio_are_mutually_exclusive_branches_not_a_fallback_chain():
    """The bug this guards against: `try: place_twilio_call() except
    ProviderConfigurationError: place_plivo_call()` would place the SAME
    approved call twice if Twilio partially succeeded before raising. Plivo
    must be a hard `if/else` on the route's own provider, never inside the
    Twilio branch's except chain."""
    branch = _call_branch_source()
    plivo_idx = branch.index('if call_provider == "plivo":')
    else_idx = branch.index("else:", plivo_idx)
    twilio_idx = branch.index('twilio_raw = await _load_provider_credential(ctx, "twilio")')
    assert plivo_idx < else_idx < twilio_idx, (
        "Plivo dispatch must be an if/else sibling of the Twilio path, not "
        "nested inside its except ProviderConfigurationError fallback chain"
    )
    # The except-fallback chain that exists for Twilio (custom HTTP -> ACS)
    # must not also catch a Plivo failure — Plivo's block has its own
    # try/except that only aborts the call, never falls through to another
    # carrier.
    plivo_block = branch[plivo_idx:else_idx]
    assert "except ProviderConfigurationError" not in plivo_block
    assert "adapter.abort_call" in plivo_block


def test_plivo_branch_sets_submission_started_before_placing_the_call():
    """Same fail-closed discipline as the Twilio branch: submission_started
    must flip to True only once Neoh is actually about to call the carrier,
    so a rejection before that point is recorded as a clean 'failed', not
    'reconciliation_required'."""
    branch = _call_branch_source()
    plivo_idx = branch.index('if call_provider == "plivo":')
    else_idx = branch.index("else:", plivo_idx)
    plivo_block = branch[plivo_idx:else_idx]

    submission_idx = plivo_block.index("submission_started = True")
    place_call_idx = plivo_block.index("adapter.place_call(")
    assert submission_idx < place_call_idx


def test_plivo_branch_never_falls_back_to_an_unverified_caller_id():
    """verified_caller_id is resolved once, above the branch, and reused —
    the Plivo branch must not independently invent a fallback from_number."""
    branch = _call_branch_source()
    plivo_idx = branch.index('if call_provider == "plivo":')
    else_idx = branch.index("else:", plivo_idx)
    plivo_block = branch[plivo_idx:else_idx]
    assert "from_number=verified_caller_id" in plivo_block


def test_plivo_branch_requires_public_base_url_for_the_answer_url():
    branch = _call_branch_source()
    plivo_idx = branch.index('if call_provider == "plivo":')
    else_idx = branch.index("else:", plivo_idx)
    plivo_block = branch[plivo_idx:else_idx]
    assert 'ORACLE_PUBLIC_BASE_URL' in plivo_block
    assert "raise ProviderConfigurationError" in plivo_block
