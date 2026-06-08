"""
Qwen Voice Agent — manages the live AI Closer audio stream and intercepts
WHISPER_INSTRUCT payloads to dynamically inject human operator instructions
into the active conversation.

Architecture:
  - Maintains a Qwen2-Audio inference session via llama.cpp
  - Audio stream runs as a continuous generation loop
  - When WHISPER_INSTRUCT arrives over WebSocket, the instruction is injected
    as a system-level directive mid-stream, steering the conversation without
    breaking the natural flow

The agent weaves operator instructions seamlessly:
  Operator whispers "Offer a 10-day close" →
  Qwen naturally pivots: "...and we can actually close in as little as 10 days
  if that timeline works for your situation."
"""

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("oracle.qwen_voice_agent")

MODEL_PATH = os.environ.get(
    "QWEN_VOICE_MODEL",
    "/media/ydn/SYPHER_CORE/models/qwen2-audio-closer.gguf"
)
LLAMA_CPP_PATH = os.environ.get(
    "LLAMA_CPP_PATH",
    "/media/ydn/SYPHER_CORE/Untitled Folder/llama.cpp"
)
N_GPU_LAYERS = int(os.environ.get("VOICE_GPU_LAYERS", "35"))
CTX_SIZE = 8192
GENERATION_TEMP = 0.7


@dataclass
class ConversationTurn:
    role: str
    content: str
    timestamp: float = field(default_factory=time.time)


@dataclass
class WhisperInstruction:
    instruction: str
    priority: str = "normal"
    timestamp: float = field(default_factory=time.time)
    applied: bool = False


class QwenVoiceAgent:
    def __init__(self, websocket=None):
        self.websocket = websocket
        self._running = False
        self._conversation: list[ConversationTurn] = []
        self._pending_whispers: list[WhisperInstruction] = []
        self._lock = asyncio.Lock()
        self._active_generation = False
        self._system_prompt = self._build_system_prompt()

    def _build_system_prompt(self) -> str:
        return (
            "You are a real estate acquisition specialist having a live phone conversation "
            "with a homeowner. You are empathetic, professional, and solution-oriented. "
            "Your goal is to understand their situation and present a fair cash offer. "
            "Speak naturally and conversationally. Never mention that you are an AI. "
            "When you receive bracketed instructions like [OPERATOR: ...], seamlessly "
            "incorporate that guidance into your next response without acknowledging the "
            "instruction itself. The homeowner cannot hear the operator instructions."
        )

    async def handle_whisper_instruct(self, payload: dict):
        instruction = payload.get("instruction", "")
        if not instruction:
            return

        whisper = WhisperInstruction(
            instruction=instruction,
            priority=payload.get("priority", "normal"),
            timestamp=payload.get("timestamp", time.time()),
        )

        async with self._lock:
            self._pending_whispers.append(whisper)

        logger.info(f"Whisper queued: {instruction[:80]}")
        await self._emit_status(f"WHISPER RECEIVED — injecting: {instruction[:50]}...")

        if not self._active_generation:
            await self._trigger_generation()

    async def handle_homeowner_speech(self, transcript: str):
        async with self._lock:
            self._conversation.append(ConversationTurn(
                role="homeowner",
                content=transcript,
            ))

        await self._trigger_generation()

    async def _trigger_generation(self):
        if self._active_generation:
            return

        self._active_generation = True
        try:
            await self._generate_response()
        finally:
            self._active_generation = False

    async def _generate_response(self):
        async with self._lock:
            pending = [w for w in self._pending_whispers if not w.applied]
            for w in pending:
                w.applied = True
            conversation_snapshot = list(self._conversation)
            # Prune applied whispers: keep only the last 10 so the list doesn't grow unbounded
            applied = [w for w in self._pending_whispers if w.applied]
            unapplied = [w for w in self._pending_whispers if not w.applied]
            self._pending_whispers = applied[-10:] + unapplied

        prompt = self._build_prompt(conversation_snapshot, pending)
        response = await self._run_inference(prompt)

        if response:
            async with self._lock:
                self._conversation.append(ConversationTurn(
                    role="closer",
                    content=response,
                ))

            await self._emit_transcript("AI CLOSER", response)

    def _build_prompt(
        self, conversation: list[ConversationTurn], whispers: list[WhisperInstruction]
    ) -> str:
        lines = [f"<|im_start|>system\n{self._system_prompt}<|im_end|>"]

        for turn in conversation[-12:]:
            role = "user" if turn.role == "homeowner" else "assistant"
            lines.append(f"<|im_start|>{role}\n{turn.content}<|im_end|>")

        if whispers:
            injection_parts = [w.instruction for w in whispers]
            injection = "; ".join(injection_parts)
            lines.append(
                f"<|im_start|>system\n[OPERATOR: {injection}]<|im_end|>"
            )

        lines.append("<|im_start|>assistant\n")
        return "\n".join(lines)

    async def _run_inference(self, prompt: str) -> Optional[str]:
        llama_bin = Path(LLAMA_CPP_PATH) / "build" / "bin" / "llama-cli"
        model_path = Path(MODEL_PATH)

        if not model_path.exists():
            logger.warning(f"Voice model not found at {model_path}")
            return self._fallback_response()

        if not llama_bin.exists():
            logger.warning(f"llama-cli not found at {llama_bin}")
            return self._fallback_response()

        cmd = [
            str(llama_bin),
            "-m", str(model_path),
            "-p", prompt,
            "-n", "256",
            "-c", str(CTX_SIZE),
            "-ngl", str(N_GPU_LAYERS),
            "--temp", str(GENERATION_TEMP),
            "--repeat-penalty", "1.15",
            "--top-p", "0.9",
            "--no-display-prompt",
            "-e",
        ]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)

            if proc.returncode != 0:
                logger.error(f"Voice inference failed: {stderr.decode()[:300]}")
                return self._fallback_response()

            output = stdout.decode().strip()
            end_token = output.find("<|im_end|>")
            if end_token > 0:
                output = output[:end_token]

            return output.strip() if output.strip() else None

        except asyncio.TimeoutError:
            logger.error("Voice inference timed out")
            proc.kill()
            return self._fallback_response()
        except FileNotFoundError:
            return self._fallback_response()

    def _fallback_response(self) -> str:
        pending = [w for w in self._pending_whispers if w.applied]
        if pending:
            last = pending[-1].instruction
            return f"I completely understand. Let me tell you — {last.lower()}."
        return "I hear you. Tell me more about your situation."

    async def _emit_status(self, message: str):
        logger.info(message)
        if self.websocket:
            try:
                await self.websocket.send_text(json.dumps({
                    "type": "STATUS_UPDATE",
                    "agent": message,
                }))
            except Exception:
                pass

    async def _emit_transcript(self, agent: str, text: str):
        if self.websocket:
            try:
                await self.websocket.send_text(json.dumps({
                    "type": "TRANSCRIPT_LINE",
                    "agent": agent,
                    "text": text,
                    "timestamp": int(time.time() * 1000),
                }))
            except Exception:
                pass
