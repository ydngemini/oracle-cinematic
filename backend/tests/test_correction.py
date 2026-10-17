"""A correction withdraws what the previous turn staged (F05.04, L05-07)."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

import pytest

import correction
from tenancy import Role, TenantContext

CTX = TenantContext(agent_id="avery@b.test", tenant_id="11111111-1111-1111-1111-111111111111", role=Role.AGENT)


@pytest.mark.parametrize("text", [
    "Actually, the other Sarah", "actually text Luis instead", "No, I meant Sarah Chen",
    "Sorry, I meant the Dover listing", "the other one", "No, not her — Sarah Lee",
    "Not her, the buyer", "Wait, no — call her tomorrow",
    "En realidad, la otra Sarah", "No, quise decir Sarah Lee", "la otra", "Mejor a la otra Sarah",
    "no, el otro",
])
def test_corrections_are_recognised(text):
    assert correction.is_correction(text)


@pytest.mark.parametrize("text", [
    "Text Sarah about the open house", "What did Sarah actually say?", "Send the other listing to Sarah",
    "Is there another Sarah?", "Not now", "Call the buyer", "Mejor precio para la casa?", "",
])
def test_ordinary_requests_are_not_corrections(text):
    assert not correction.is_correction(text)


def _bundle(text, prior_assistant="a1"):
    return {"messages": [
        {"role": "user", "id": "u1", "content": "Text Sarah about the open house"},
        {"role": "assistant", "id": prior_assistant, "content": "Staged a text to Sarah Chen."},
        {"role": "user", "id": "u2", "content": text},
    ]}


def _wire(monkeypatch, receipts, commands):
    import commands_api
    import sealed_text
    from db import connection

    class _Conn:
        async def fetch(self, sql, *args):
            assert args[1] == "a1", "only the previous assistant turn is read"
            return [{"tool_name": "draft_sms", "result": json.dumps(r), "result_ciphertext": None,
                     "result_ciphertext_tenant_id": CTX.tenant_id} for r in receipts]

    @asynccontextmanager
    async def tx(ctx):
        yield _Conn()

    async def open_rows(conn, rows, field):
        return rows

    async def get(ctx, command_id, **kw):
        return commands.get(command_id)

    withdrawn = []

    async def withdraw(ctx, row, *, reason):
        withdrawn.append((row["id"], reason))
        return {}

    monkeypatch.setattr(connection, "tenant_tx", tx)
    monkeypatch.setattr(sealed_text, "open_rows", open_rows)
    monkeypatch.setattr(commands_api, "_get_command", get)
    monkeypatch.setattr(commands_api, "withdraw_command", withdraw)
    return withdrawn


def test_a_correction_withdraws_only_this_agents_still_staged_requests(monkeypatch):
    commands = {
        "c-staged": {"id": "c-staged", "state": "awaiting_approval", "created_by": "Avery@b.test"},
        "c-sent": {"id": "c-sent", "state": "succeeded", "created_by": "avery@b.test"},
        "c-other": {"id": "c-other", "state": "awaiting_approval", "created_by": "morgan@b.test"},
        "c-reused": {"id": "c-reused", "state": "awaiting_approval", "created_by": "avery@b.test"},
    }
    receipts = [{"ok": True, "command_id": "c-staged"}, {"ok": True, "command_id": "c-sent"},
                {"ok": True, "command_id": "c-other"},
                {"ok": True, "command_id": "c-reused", "reused_existing_request": True},
                {"ok": False, "error": "refused"}]
    withdrawn = _wire(monkeypatch, receipts, commands)
    out = asyncio.run(correction.withdraw_superseded(CTX, _bundle("Actually, the other Sarah")))
    assert out == ["c-staged"]
    assert withdrawn == [("c-staged", correction.REASON)]
    assert "1 request staged" in correction.prompt_note(out) and "was withdrawn" in correction.prompt_note(out)


def test_no_correction_no_withdrawal(monkeypatch):
    withdrawn = _wire(monkeypatch, [{"ok": True, "command_id": "c"}],
                      {"c": {"id": "c", "state": "awaiting_approval", "created_by": "avery@b.test"}})
    assert asyncio.run(correction.withdraw_superseded(CTX, _bundle("Also text Luis"))) == []
    assert withdrawn == [] and correction.prompt_note([]) == ""


def test_a_failure_never_breaks_the_turn(monkeypatch):
    import commands_api

    _wire(monkeypatch, [{"ok": True, "command_id": "c"}], {})

    async def boom(ctx, command_id, **kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(commands_api, "_get_command", boom)
    assert asyncio.run(correction.withdraw_superseded(CTX, _bundle("actually, not her"))) == []
