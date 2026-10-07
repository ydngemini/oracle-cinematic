#!/usr/bin/env python3
"""Create or update the ElevenLabs Agents agent that voices Neoh's Plivo calls.

    python scripts/elevenlabs-agent.py create --env staging   # prints the new agent_id
    python scripts/elevenlabs-agent.py update --env staging --agent-id <id>
    python scripts/elevenlabs-agent.py show   --agent-id <id>

Reads ELEVENLABS_API_KEY from the environment (or the repo .env); never prints
it. The configuration is the contract backend/elevenlabs_realtime.py relies on:

* asr + tts audio in ulaw_8000 — Plivo's stream format, relayed byte-for-byte;
* auth enabled — a conversation needs a signed URL, which only the backend
  (holding the key) can mint;
* per-call overrides of the system prompt and first message allowed — each
  approved call carries its own briefing;
* voice recording OFF and one-day retention — the call audio is not kept by
  ElevenLabs beyond what the conversation needs;
* a 5-minute ceiling per conversation;
* two client tools, record_note_consent and save_note, which the backend
  answers over the call's WebSocket — notes exist only after the callee says
  yes, and reach the agent as a note on the client;
* TTS model eleven_flash_v2 — ElevenLabs refuses English agents on any model
  but turbo/flash v2 ("English Agents must use turbo or flash v2").

The base instructions are the same rules the Qwen bridge uses
(qwen_omni_realtime._system_instructions): disclosure already played, never deny
being automated, no invented facts or binding offers, honour opt-outs at once.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "backend"))
API = os.getenv("ELEVENLABS_API_BASE", "").strip().rstrip("/") or "https://api.elevenlabs.io"
DEFAULT_VOICE = "cjVigY5qzO86Huf0OWal"   # an ElevenLabs premade voice (no voice-slot cost)


def _api_key() -> str:
    key = os.getenv("ELEVENLABS_API_KEY", "").strip()
    if not key and (REPO / ".env").exists():
        for line in (REPO / ".env").read_text().splitlines():
            if line.startswith("ELEVENLABS_API_KEY="):
                key = line.split("=", 1)[1].strip().strip('"').strip("'")
    if not key:
        sys.exit("ELEVENLABS_API_KEY is not set")
    return key


def tool_configs() -> list[dict]:
    from elevenlabs_realtime import TOOL_NOTE_CONSENT, TOOL_SAVE_NOTE

    return [
        {"type": "client", "name": TOOL_NOTE_CONSENT, "expects_response": True,
         "response_timeout_secs": 10,
         "description": "Record the person's answer when asked whether you may take notes for "
                        "their agent. Call it once, right after they answer.",
         "parameters": {"type": "object", "required": ["granted"], "properties": {
             "granted": {"type": "boolean",
                         "description": "true only if they clearly agreed to notes"}}}},
        {"type": "client", "name": TOOL_SAVE_NOTE, "expects_response": True,
         "response_timeout_secs": 10,
         "description": "Save one short factual note for their agent (an interest, a "
                        "preference, a question, or a good time). Only after they agreed to notes.",
         "parameters": {"type": "object", "required": ["note"], "properties": {
             "note": {"type": "string",
                      "description": "one fact in a short sentence, e.g. 'Prefers afternoon showings'"}}}},
    ]


def ensure_tools() -> list[str]:
    """Create or update the client tools by name; returns their ids."""
    existing = {(t.get("tool_config") or {}).get("name"): t.get("id")
                for t in _call("GET", "/v1/convai/tools").get("tools") or []}
    ids = []
    for cfg in tool_configs():
        tool_id = existing.get(cfg["name"])
        if tool_id:
            _call("PATCH", f"/v1/convai/tools/{tool_id}", {"tool_config": cfg})
        else:
            tool_id = _call("POST", "/v1/convai/tools", {"tool_config": cfg}).get("id")
        ids.append(tool_id)
    return ids


def agent_config(env: str, tool_ids: list[str] | None = None) -> dict:
    os.environ.setdefault("ORACLE_SKIP_DOTENV", "1")
    from qwen_omni_realtime import _system_instructions

    voice = os.getenv("ORACLE_ELEVENLABS_VOICE_ID", "").strip() or DEFAULT_VOICE
    return {
        "name": f"Neoh calls ({env})",
        "conversation_config": {
            "agent": {
                "first_message": "",
                "language": "en",
                "prompt": {"prompt": _system_instructions(), "llm": os.getenv(
                    "ELEVENLABS_AGENT_LLM", "gemini-2.0-flash"), "temperature": 0.3,
                    "tool_ids": list(tool_ids or [])},
            },
            "asr": {"user_input_audio_format": "ulaw_8000"},
            "tts": {"agent_output_audio_format": "ulaw_8000",
                    "model_id": "eleven_flash_v2", "voice_id": voice},
            "conversation": {"max_duration_seconds": 300},
        },
        "platform_settings": {
            "auth": {"enable_auth": True},
            "overrides": {"conversation_config_override": {
                "agent": {"prompt": {"prompt": True}, "first_message": True}}},
            "privacy": {"record_voice": False, "retention_days": 1},
        },
    }


def _call(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method, headers={
        "xi-api-key": _api_key(), "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        sys.exit(f"ElevenLabs {method} {path} -> {exc.code}: {exc.read()[:600].decode('utf-8', 'replace')}")


def _summary(agent: dict) -> dict:
    conv = agent.get("conversation_config") or {}
    plat = agent.get("platform_settings") or {}
    return {
        "agent_id": agent.get("agent_id"), "name": agent.get("name"),
        "asr_format": (conv.get("asr") or {}).get("user_input_audio_format"),
        "tts_format": (conv.get("tts") or {}).get("agent_output_audio_format"),
        "llm": ((conv.get("agent") or {}).get("prompt") or {}).get("llm"),
        "tool_ids": ((conv.get("agent") or {}).get("prompt") or {}).get("tool_ids"),
        "auth": (plat.get("auth") or {}).get("enable_auth"),
        "record_voice": (plat.get("privacy") or {}).get("record_voice"),
        "retention_days": (plat.get("privacy") or {}).get("retention_days"),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["create", "update", "show"])
    ap.add_argument("--env", default="staging", choices=["staging", "production"])
    ap.add_argument("--agent-id", default="")
    args = ap.parse_args(argv)
    if args.action == "create":
        created = _call("POST", "/v1/convai/agents/create", agent_config(args.env, ensure_tools()))
        agent_id = created.get("agent_id")
        print(json.dumps(_summary(_call("GET", f"/v1/convai/agents/{agent_id}")), indent=1))
        return 0
    if not args.agent_id:
        sys.exit("--agent-id is required")
    if args.action == "update":
        _call("PATCH", f"/v1/convai/agents/{args.agent_id}", agent_config(args.env, ensure_tools()))
    print(json.dumps(_summary(_call("GET", f"/v1/convai/agents/{args.agent_id}")), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
