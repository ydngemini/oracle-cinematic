"""AI32-05: one provider-independent postcondition. A claim in the final reply
(staged, sent, changed) must be backed by this turn's receipts, whichever
provider (gateway, Fireworks, Foundry, Bedrock, local) wrote it."""

from __future__ import annotations

import asyncio

import pytest

import ai_chat_agent as aca

STAGED = {"staged_effects": [{"id": "c1", "action_type": "draft_sms"}]}
APPLIED = [{"ok": True, "action_id": "a1"}]


@pytest.mark.parametrize("text,actions,bundle,expected", [
    ("Your text to Sam is staged for your approval.", [], {}, [aca.UNBACKED_CLAIM_NOTE]),
    ("Your text to Sam is staged for your approval.", [], STAGED, []),
    ("I've sent the text to Sam.", [], STAGED, [aca.UNSENT_CLAIM_NOTE]),
    ("The email has just been sent.", [], {}, [aca.UNSENT_CLAIM_NOTE]),
    ("I’ve sent the text to Sam.", [], {}, [aca.UNSENT_CLAIM_NOTE]),
    ("I’ve updated Sam’s stage to negotiating.", [], {}, [aca.UNCHANGED_CLAIM_NOTE]),
    ("Your text is awaiting your approval.", [], {}, [aca.UNBACKED_CLAIM_NOTE]),
    # History and honest negations are not claims (review r1).
    ("The offer was sent on Tuesday.", [], {}, []),
    ("Sam has been sent the disclosure by his lender.", [], {}, []),
    ("Your last text to Sam was sent Nov 12.", [], {}, []),
    ("The seller disclosure was delivered on 10/3.", [], {}, []),
    ("The inspection was booked for Saturday.", [], {}, []),
    ("Two counter-offers were sent last week.", [], {}, []),
    ("Nothing was sent yet.", [], {}, []),
    ("No message has been sent.", [], {}, []),
    ("I haven't sent anything.", [], {}, []),
    ("As I called out above, the comps are thin.", [], {}, []),
    ("It will be sent once you approve it.", [], STAGED, []),
    ("I've updated Sam's stage to negotiating.", [], {}, [aca.UNCHANGED_CLAIM_NOTE]),
    ("I've updated Sam's stage to negotiating.", APPLIED, {}, []),
    ("Sam's pipeline stage is negotiating and his budget is $400k.", [], {}, []),
    ("I've added more detail below about the comps.", [], {}, []),
])
def test_claims_are_checked_against_the_turns_receipts(text, actions, bundle, expected):
    out = aca._verify_claims(text, actions, bundle)
    assert out.startswith(text)
    for note in (aca.UNBACKED_CLAIM_NOTE, aca.UNSENT_CLAIM_NOTE, aca.UNCHANGED_CLAIM_NOTE):
        assert (note in out) == (note in expected), (text, note)


def test_a_note_already_added_by_the_local_loop_is_not_repeated():
    text = f"Staged for your approval.\n\n{aca.UNBACKED_CLAIM_NOTE}"
    assert aca._verify_claims(text, [], {}) == text


@pytest.mark.parametrize("provider_text", [
    "Done — I texted Sam the showing time.",          # e.g. a Foundry reply
    "I booked your call with Sam for 3pm.",           # e.g. a Bedrock reply
])
def test_every_provider_reply_passes_the_postcondition_before_anyone_sees_it(monkeypatch, provider_text):
    seen = {}

    async def bundle(_ctx, _aid):
        return {"assistant": {"status": "pending"}, "messages": [{"content": "text Sam"}]}

    async def generate(_ctx, _bundle, _aid):
        return provider_text, [], "azure-foundry:any"

    async def chunks(_ctx, _aid, _rid, content):
        seen["content"] = content

    async def nothing(*_a, **_kw):
        return True

    class Sessions:
        def __init__(self, _ctx):
            pass

        async def record_interaction(self, *_a):
            return None

    monkeypatch.setattr(aca, "load_response_bundle", bundle)
    monkeypatch.setattr(aca, "_generate", generate)
    monkeypatch.setattr(aca, "_broadcast_chunks", chunks)
    monkeypatch.setattr(aca, "update_assistant", nothing)
    monkeypatch.setattr(aca.ws_hub, "broadcast_user", nothing)
    monkeypatch.setattr(aca, "release_concurrency", nothing)
    monkeypatch.setattr(aca, "SessionManager", Sessions)
    asyncio.run(aca.handle_ai_chat_job({"tenant_id": "00000000-0000-0000-0000-0000000000aa", "user_id": "a",
                                        "assistant_id": "11111111-1111-1111-1111-111111111111",
                                        "request_id": "r"}, reporter=None))
    assert seen["content"].startswith(provider_text) and aca.UNSENT_CLAIM_NOTE in seen["content"]


def test_the_interrupted_reply_passes_unchanged():
    """AI32-03's own reply ('nothing was sent') must not trip the check."""
    bundle = {"staged_effects": [{"id": "c1", "action_type": "draft_sms"}]}
    reply = aca._interrupted_reply(bundle, [])
    assert aca._verify_claims(reply, [], bundle) == reply
