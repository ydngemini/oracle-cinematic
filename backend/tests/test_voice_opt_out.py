"""Opt-out detection on a voice transcript, in both directions.

This matcher has two ways to be wrong and both are expensive:

  * miss a real opt-out — a TCPA violation, since inbound "stop" is valid by
    "any reasonable method" and nothing downstream re-asks;
  * fire on ordinary conversation — permanently marks a genuine inbound seller
    do-not-contact, discards the answers they just gave, and no path in
    inbound_voice ever clears the flag.

Scanning the joined transcript for bare keywords fails the second way; matching
only whole-utterance keywords fails the first. Clause scope is what separates
"I'm not interested, stop." from "stop by the house on Tuesday".
"""

from __future__ import annotations

import pytest

from inbound_voice import _requested_opt_out


def _caller(*turns: str):
    return [{"role": "caller", "text": t} for t in turns]


@pytest.mark.parametrize(
    "utterance",
    [
        "stop",
        "Stop.",
        "I'm not interested, stop.",
        "I said stop",
        "Please stop!",
        "No — stop.",
        "Yeah, cancel.",
        "unsubscribe",
        "opt-out",
        "Take me off your list.",
        "Do not call me again.",
        "Lose my number.",
        "Quit calling me.",
        "Never contact me again please.",
    ],
)
def test_real_opt_outs_are_honoured(utterance):
    assert _requested_opt_out(_caller(utterance)) is True, utterance


@pytest.mark.parametrize(
    "utterance",
    [
        # The exact false positives that motivated narrowing the matcher.
        "I need to sell before the end of the summer.",
        "I want to cancel my listing with my current agent.",
        # `stop` and `remove` as ordinary verbs with objects.
        "Can you stop by the house on Tuesday?",
        "We had to stop the renovation halfway through.",
        "They want us to remove the old shed before closing.",
        "The quit claim deed is with my attorney.",
        # Seller answering questions — the whole point of the call.
        "Three bedrooms, two baths, and the roof is about ten years old.",
        "My end goal is to close by September.",
    ],
)
def test_ordinary_conversation_is_not_an_opt_out(utterance):
    assert _requested_opt_out(_caller(utterance)) is False, utterance


def test_only_the_caller_side_counts():
    """The AI reads the disclosure script; it must not opt the caller out."""
    transcript = [
        {"role": "assistant", "text": "If you'd like me to stop, just say stop."},
        {"role": "caller", "text": "That's fine, go ahead."},
    ]
    assert _requested_opt_out(transcript) is False


def test_opt_out_later_in_the_call_still_counts():
    transcript = _caller(
        "Sure, it's a three bedroom on Maple.",
        "Actually, stop.",
    )
    assert _requested_opt_out(transcript) is True


def test_empty_transcript_is_not_an_opt_out():
    assert _requested_opt_out([]) is False
