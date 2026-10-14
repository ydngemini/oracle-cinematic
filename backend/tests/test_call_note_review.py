"""AI call notes: who gets the live frame, how the mode is read, and that the
AI never reads a proposed note (D-2026-10-12-03; prompt 09 T09.12, T09.14)."""

from __future__ import annotations

import inspect

import call_note_settings
import commands_api


def test_the_frame_goes_to_the_requester_and_the_assignee_once_each():
    assert commands_api.call_note_recipients("agent-a", "agent-b") == ["agent-a", "agent-b"]
    assert commands_api.call_note_recipients("Agent-A", "agent-a") == ["Agent-A"], "case-insensitive"
    assert commands_api.call_note_recipients("agent-a", None) == ["agent-a"]
    assert commands_api.call_note_recipients("", "  ") == []
    assert commands_api.call_note_recipients(commands_api.AI_CALL_NOTE_AUTHOR, "agent-b") == ["agent-b"]


def test_delivery_never_broadcasts_note_text_tenant_wide():
    src = inspect.getsource(commands_api.deliver_call_notes)
    # The requester and the assignee, among the members who may still read the
    # client (L03-08: broadcast_record re-checks the record, 0145).
    assert "ws_hub.broadcast_record(" in src and "only=call_note_recipients(" in src
    assert "ws_hub.broadcast(" not in src, "CALL_NOTES carries customer content (CONTRACT C3.6)"


def test_review_is_the_default_and_unknown_modes_fall_back_to_it():
    assert call_note_settings.DEFAULT_CALL_NOTE_MODE == "review"
    assert call_note_settings.normalize_mode("direct") == "direct"
    for junk in (None, "", "auto", 1):
        assert call_note_settings.normalize_mode(junk) == "review"


def test_the_ai_never_reads_a_proposed_note():
    import ai_chat_store
    import client_ai_automation

    for module in (ai_chat_store, client_ai_automation):
        src = inspect.getsource(module)
        for chunk in src.split("FROM client_notes")[1:]:
            where = chunk[:300]
            assert "review_state='approved'" in where, f"{module.__name__} reads client_notes without the review filter"


def test_migration_defaults_existing_notes_to_approved():
    from pathlib import Path

    sql = (Path(__file__).resolve().parents[1] / "db/migrations/0130_client_note_review.sql").read_text()
    assert "DEFAULT 'approved'" in sql and "CHECK (review_state IN ('proposed', 'approved'))" in sql
