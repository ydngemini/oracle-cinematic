"""The message a mission sends, and what it refuses to send.

This is the last thing standing between a plan and a real person's phone, so
the tests are mostly about refusal. A message under the agent's licence that
states a wrong price is the worst single failure this feature can produce, and
it is also the cheapest to detect — so it is detected, not merely discouraged
in a prompt.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import sys
import types

import pytest

from missions import drafter
from tenancy import Role, TenantContext

CTX = TenantContext(
    agent_id="a@t.test",
    tenant_id="11111111-1111-1111-1111-111111111111",
    role=Role.AGENT,
)


class _Gateway:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls: list[dict] = []

    async def complete(self, prompt, **kwargs):
        self.calls.append({"prompt": prompt, **kwargs})
        return self.answers.pop(0)


@pytest.fixture(autouse=True)
def _restore_gateway():
    real = sys.modules.get("llm_gateway")
    yield
    if real is not None:
        sys.modules["llm_gateway"] = real
    else:
        sys.modules.pop("llm_gateway", None)


def _run(gateway, **kwargs):
    fake = types.ModuleType("llm_gateway")
    fake.complete = gateway.complete
    sys.modules["llm_gateway"] = fake
    args = {
        "channel": "sms", "recipient_name": "Sarah Chen",
        "objective": "Win three listings in Newark", "intent": "no contact in 40 days",
        **kwargs,
    }
    return asyncio.run(drafter.draft_message(CTX, **args))


def _draft(body, subject=""):
    return json.dumps({"subject": subject, "body": body})


class TestRefusals:
    def test_it_refuses_a_draft_that_states_a_price(self):
        """The prompt forbids it. The prompt is not a guarantee, so this is
        checked after the fact — a wrong number under the agent's licence is
        the most damaging thing this can produce."""
        gateway = _Gateway(
            _draft("Hi Sarah, homes like yours are fetching $840,000 right now."),
            _draft("Hi Sarah, it has been a while — worth a quick chat about your options?"),
        )
        out = _run(gateway)
        assert "840" not in out.body
        assert len(gateway.calls) == 2
        assert "monetary figure" in gateway.calls[1]["prompt"]

    @pytest.mark.parametrize("body", [
        "Worth about £600,000 now.",
        "Around 450000 dollars in this market.",
        "Roughly 900k for a home like yours.",
    ])
    def test_money_is_caught_in_several_shapes(self, body):
        assert drafter._refuse(drafter.Draft(body=body), "sms", drafter.SMS_MAX)

    def test_a_plain_sentence_is_not_mistaken_for_a_price(self):
        """A false positive here silently blocks good messages, so the check
        must not fire on ordinary numbers."""
        for body in ("Free on the 3rd or 4th?", "I have 2 buyers looking in Newark."):
            assert drafter._refuse(drafter.Draft(body=body), "sms", drafter.SMS_MAX) is None

    def test_an_unfilled_placeholder_is_refused(self):
        assert "placeholder" in drafter._refuse(
            drafter.Draft(body="Hi {name}, quick question."), "sms", drafter.SMS_MAX)

    def test_an_email_without_a_subject_is_refused(self):
        assert "subject" in drafter._refuse(
            drafter.Draft(body="Hello there."), "email", drafter.EMAIL_BODY_MAX)

    def test_a_text_longer_than_one_segment_is_refused(self):
        long_body = "x" * (drafter.SMS_MAX + 1)
        assert "limit for sms" in drafter._refuse(
            drafter.Draft(body=long_body), "sms", drafter.SMS_MAX)


class TestFailure:
    def test_two_unusable_drafts_raise_rather_than_send_something(self):
        """There is no placeholder body. A message the system could not write
        is an action it must not take."""
        gateway = _Gateway(_draft("It is worth $1,000,000."), _draft("Still $999,000."))
        with pytest.raises(drafter.DraftUnavailable):
            _run(gateway)

    def test_an_unreachable_model_is_a_refusal_not_a_crash(self):
        class _Dead:
            async def complete(self, *_a, **_k):
                raise RuntimeError("litellm is not installed")

        with pytest.raises(drafter.DraftUnavailable) as caught:
            _run(_Dead())
        assert "no model could write the message" in str(caught.value)


class TestContainment:
    def test_it_demands_structured_output(self):
        gateway = _Gateway(_draft("Hi Sarah, worth a quick chat?"))
        _run(gateway)
        assert gateway.calls[0]["response_format"] is drafter.DRAFT_SCHEMA

    def test_the_model_is_not_shown_the_property_record(self):
        """Anything it is shown, it may repeat. A message that states a fact
        about someone's home is a message that can state a WRONG one."""
        gateway = _Gateway(_draft("Hi Sarah, worth a quick chat?"))
        _run(gateway)
        prompt = gateway.calls[0]["prompt"]
        for field in ("bedrooms", "sqft", "square", "price", "valuation", "parcel"):
            assert field not in prompt.lower(), field

    def test_the_prompt_forbids_inventing_specifics(self):
        assert "No prices" in drafter.SYSTEM
        assert "cannot be un-sent" in drafter.SYSTEM

    def test_a_text_never_carries_a_subject(self):
        gateway = _Gateway(_draft("Hi Sarah, worth a quick chat?", subject="Your home"))
        assert _run(gateway).subject == ""

    def test_this_module_sends_nothing(self):
        """Checked against the CODE, not the prose. The module docstring names
        stage_command and guard_outreach precisely to say they happen
        elsewhere, and a check that cannot tell those apart would force the
        explanation out of the file to stay green."""
        source = inspect.getsource(drafter)
        body = source.split('"""', 2)[2] if source.count('"""') >= 2 else source
        for forbidden in ("stage_command", "release_command", "guard_outreach", "enqueue_job"):
            assert forbidden not in body, forbidden
