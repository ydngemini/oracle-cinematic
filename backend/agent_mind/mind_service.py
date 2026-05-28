"""
Agent Mind Service — per-agent memory + inner monologue via local llama-server.

Each agent (SCOUT, ANALYST, CLOSER, LEGAL) maintains its own rolling memory window
and generates a continuous inner monologue stream. The monologue is streamed token-by-token
over WebSocket as AGENT_THOUGHT messages for the Walker speech bubble to live-replace.

Connects to the running llama-server at localhost:8090 (Llama 3.2 1B Instruct).
"""

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Optional

import aiohttp

LLAMA_SERVER_URL = "http://127.0.0.1:8090"
COMPLETION_ENDPOINT = f"{LLAMA_SERVER_URL}/completion"
HEALTH_ENDPOINT = f"{LLAMA_SERVER_URL}/health"

MAX_MEMORY_ENTRIES = 24
MONOLOGUE_MAX_TOKENS = 80
MONOLOGUE_TEMPERATURE = 0.7


@dataclass
class MemoryEntry:
    timestamp: float
    role: str
    content: str
    importance: float = 0.5

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "role": self.role,
            "content": self.content,
            "importance": self.importance,
        }


@dataclass
class AgentMind:
    agent_id: str
    persona: str
    memory: list[MemoryEntry] = field(default_factory=list)
    current_thought: str = ""
    thought_count: int = 0

    def remember(self, content: str, role: str = "observation", importance: float = 0.5):
        entry = MemoryEntry(
            timestamp=time.time(),
            role=role,
            content=content,
            importance=importance,
        )
        self.memory.append(entry)
        if len(self.memory) > MAX_MEMORY_ENTRIES:
            self.memory.sort(key=lambda e: e.importance, reverse=True)
            self.memory = self.memory[:MAX_MEMORY_ENTRIES]

    def build_context_window(self) -> str:
        recent = sorted(self.memory, key=lambda e: e.timestamp)[-12:]
        memory_text = "\n".join(
            f"[{e.role}] {e.content}" for e in recent
        )
        return (
            f"<|begin_of_text|><|start_header_id|>system<|end_header_id|>\n\n"
            f"You are {self.agent_id}, an AI agent in Oracle (a real estate command platform). "
            f"{self.persona}\n"
            f"Your recent memory:\n{memory_text}\n\n"
            f"Express your current inner thought in ONE concise sentence. "
            f"Think about what you should do next based on your observations. "
            f"Be specific and operational, not generic."
            f"<|eot_id|><|start_header_id|>user<|end_header_id|>\n\n"
            f"What are you thinking right now?"
            f"<|eot_id|><|start_header_id|>assistant<|end_header_id|>\n\n"
        )


AGENT_PERSONAS = {
    "SCOUT": (
        "You scan public records, county assessor data, and court filings to find "
        "distressed property signals. You hunt for motivated sellers — divorce, probate, "
        "tax liens, pre-foreclosure."
    ),
    "ANALYST": (
        "You run Comparative Market Analysis on properties. You evaluate comps, calculate "
        "price-per-sqft, estimate ARV (After Repair Value), and determine acquisition ceilings."
    ),
    "CLOSER": (
        "You negotiate with property owners. You craft persuasive scripts, handle objections, "
        "and structure wholesale assignment offers that protect the buyer's spread."
    ),
    "LEGAL": (
        "You draft and review contracts, assignment agreements, and disclosure documents. "
        "You ensure Delaware statutory compliance and protect against legal exposure."
    ),
}


class MindService:
    def __init__(self):
        self.minds: dict[str, AgentMind] = {}
        self._session: Optional[aiohttp.ClientSession] = None
        self._healthy = False

    async def start(self):
        self._session = aiohttp.ClientSession()
        await self._check_health()

    async def stop(self):
        if self._session:
            await self._session.close()

    async def _check_health(self):
        try:
            async with self._session.get(HEALTH_ENDPOINT, timeout=aiohttp.ClientTimeout(total=3)) as resp:
                self._healthy = resp.status == 200
        except Exception:
            self._healthy = False

    def get_or_create_mind(self, agent_id: str) -> AgentMind:
        if agent_id not in self.minds:
            persona = AGENT_PERSONAS.get(agent_id, "You are a general-purpose AI agent.")
            self.minds[agent_id] = AgentMind(agent_id=agent_id, persona=persona)
        return self.minds[agent_id]

    def observe(self, agent_id: str, content: str, importance: float = 0.5):
        mind = self.get_or_create_mind(agent_id)
        mind.remember(content, role="observation", importance=importance)

    def record_action(self, agent_id: str, action: str, importance: float = 0.6):
        mind = self.get_or_create_mind(agent_id)
        mind.remember(action, role="action", importance=importance)

    def record_result(self, agent_id: str, result: str, importance: float = 0.7):
        mind = self.get_or_create_mind(agent_id)
        mind.remember(result, role="result", importance=importance)

    async def generate_monologue(self, agent_id: str) -> Optional[str]:
        if not self._healthy:
            await self._check_health()
            if not self._healthy:
                return None

        mind = self.get_or_create_mind(agent_id)
        prompt = mind.build_context_window()

        payload = {
            "prompt": prompt,
            "n_predict": MONOLOGUE_MAX_TOKENS,
            "temperature": MONOLOGUE_TEMPERATURE,
            "top_p": 0.9,
            "stop": ["\n", "<|eot_id|>", "</s>"],
            "stream": False,
        }

        try:
            async with self._session.post(
                COMPLETION_ENDPOINT,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                thought = data.get("content", "").strip()
                if thought:
                    mind.current_thought = thought
                    mind.thought_count += 1
                    mind.remember(thought, role="thought", importance=0.3)
                return thought
        except Exception:
            return None

    async def stream_monologue(self, agent_id: str):
        """Yields tokens as they arrive from llama-server streaming mode."""
        if not self._healthy:
            await self._check_health()
            if not self._healthy:
                return

        mind = self.get_or_create_mind(agent_id)
        prompt = mind.build_context_window()

        payload = {
            "prompt": prompt,
            "n_predict": MONOLOGUE_MAX_TOKENS,
            "temperature": MONOLOGUE_TEMPERATURE,
            "top_p": 0.9,
            "stop": ["\n", "<|eot_id|>", "</s>"],
            "stream": True,
        }

        full_text = ""
        try:
            async with self._session.post(
                COMPLETION_ENDPOINT,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status != 200:
                    return
                async for line in resp.content:
                    decoded = line.decode("utf-8").strip()
                    if not decoded or not decoded.startswith("data: "):
                        continue
                    json_str = decoded[6:]
                    if json_str == "[DONE]":
                        break
                    try:
                        chunk = json.loads(json_str)
                        token = chunk.get("content", "")
                        if token:
                            full_text += token
                            yield token
                        if chunk.get("stop"):
                            break
                    except json.JSONDecodeError:
                        continue

            if full_text.strip():
                mind.current_thought = full_text.strip()
                mind.thought_count += 1
                mind.remember(full_text.strip(), role="thought", importance=0.3)
        except Exception:
            return

    def get_state(self, agent_id: str) -> dict:
        mind = self.get_or_create_mind(agent_id)
        return {
            "agent_id": agent_id,
            "current_thought": mind.current_thought,
            "thought_count": mind.thought_count,
            "memory_size": len(mind.memory),
            "recent_memory": [e.to_dict() for e in mind.memory[-5:]],
        }
