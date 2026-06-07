"""
Edge Forge — local vLLM inference adapter with hot-swappable state LoRA lobes.

EdgeUnderwriter holds ONE resident copy of the base Llama-3-8B model in GPU
memory and dynamically attaches the correct per-state LoRA adapter
(trained by train_lora.py) before underwriting a batch of properties. This
keeps the 8B base weights hot while swapping only the cheap LoRA deltas as
deals cross state lines (DE -> PA -> NJ ...).

Usage:
    uw = EdgeUnderwriter()
    uw.register_adapter("DE", "/path/to/adapters/de")
    results = uw.hot_swap_state_lora("DE").underwrite_batch(properties)

Requires:
    pip install vllm
"""

import json
import os
from typing import Dict, List, Optional

from vllm import LLM, SamplingParams
from vllm.lora.request import LoRARequest

BASE_MODEL = "unsloth/llama-3-8b-Instruct-bnb-4bit"

_FORGE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ADAPTER_ROOT = os.path.join(_FORGE_DIR, "adapters")

# The inference contract — MUST match the adapter's training target in
# train_lora.py (_underwrite). The model is a math engine, not a lawyer: it
# predicts ARV, applies the 70% rule minus rehab for the MAO, and verdicts the
# deal. Any legal/contract/notarization language here would feed the adapter a
# prompt outside its training distribution and produce garbage.
SYSTEM_PROMPT = (
    "You are NexaSwarm's Senior Underwriter for U.S. real estate wholesale "
    "deals. Given raw property data and distress signals, you predict the "
    "After Repair Value (ARV), estimate rehab cost, and apply the 70% rule — "
    "MAO = 0.70 * ARV - rehab — to decide whether the deal clears.\n\n"
    "Respond with STRICTLY valid JSON and NOTHING else. No prose, no markdown "
    "fences, no legal language. The object must contain EXACTLY these keys:\n"
    "  arv_estimate    (integer)  — predicted After Repair Value in whole dollars\n"
    "  rehab_estimate  (integer)  — estimated rehab cost in whole dollars\n"
    "  mao_formula     (string)   — literally '0.70 * ARV - rehab'\n"
    "  mao             (integer)  — Maximum Allowable Offer in whole dollars\n"
    "  verdict         (string)   — exactly 'Proceed' or 'Reject'\n"
    "  rationale       (string)   — one-sentence justification of the verdict"
)

# JSON Schema handed to vLLM's guided decoder so the grammar physically cannot
# emit anything but these six keys (closed object, all required).
UNDERWRITE_JSON_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "arv_estimate",
        "rehab_estimate",
        "mao_formula",
        "mao",
        "verdict",
        "rationale",
    ],
    "properties": {
        "arv_estimate": {"type": "integer"},
        "rehab_estimate": {"type": "integer"},
        "mao_formula": {"type": "string", "enum": ["0.70 * ARV - rehab"]},
        "mao": {"type": "integer"},
        "verdict": {"type": "string", "enum": ["Proceed", "Reject"]},
        "rationale": {"type": "string"},
    },
}


class EdgeUnderwriter:
    """Single resident 8B base model with hot-swappable per-state LoRA adapters."""

    def __init__(
        self,
        base_model: str = BASE_MODEL,
        adapter_root: str = DEFAULT_ADAPTER_ROOT,
        max_loras: int = 4,
        max_lora_rank: int = 16,
        gpu_memory_utilization: float = 0.90,
    ):
        # enable_lora keeps the base weights resident while LoRA deltas swap in.
        self.llm = LLM(
            model=base_model,
            enable_lora=True,
            max_loras=max_loras,
            max_lora_rank=max_lora_rank,
            max_cpu_loras=max_loras * 2,
            gpu_memory_utilization=gpu_memory_utilization,
        )
        self.tokenizer = self.llm.get_tokenizer()
        self.adapter_root = adapter_root

        # Registry of known state adapters and a monotonic id counter vLLM needs.
        self._adapter_paths: Dict[str, str] = {}
        self._lora_ids: Dict[str, int] = {}
        self._next_lora_id = 1

        # Currently mounted state adapter.
        self._active_state: Optional[str] = None
        self._active_request: Optional[LoRARequest] = None

        self._discover_adapters()

    # ------------------------------------------------------------------ #
    # Adapter registry
    # ------------------------------------------------------------------ #
    def _discover_adapters(self):
        """Auto-register any per-state adapter dirs under adapter_root."""
        if not os.path.isdir(self.adapter_root):
            return
        for name in os.listdir(self.adapter_root):
            path = os.path.join(self.adapter_root, name)
            if os.path.isdir(path) and os.path.exists(
                os.path.join(path, "adapter_config.json")
            ):
                self.register_adapter(name.upper(), path)

    def register_adapter(self, state_code: str, adapter_path: str):
        """Register a trained LoRA adapter directory for a state code."""
        state_code = state_code.upper()
        if not os.path.exists(adapter_path):
            raise FileNotFoundError(f"Adapter path missing: {adapter_path}")
        self._adapter_paths[state_code] = adapter_path
        if state_code not in self._lora_ids:
            self._lora_ids[state_code] = self._next_lora_id
            self._next_lora_id += 1

    # ------------------------------------------------------------------ #
    # Hot swap
    # ------------------------------------------------------------------ #
    def hot_swap_state_lora(self, state_code: str) -> "EdgeUnderwriter":
        """Mount the LoRA adapter for `state_code` ahead of a batch.

        vLLM loads/caches the adapter on first reference and keeps it in the
        LoRA cache (up to max_loras) — subsequent swaps back to a warm state
        are free. Returns self so calls can be chained.
        """
        state_code = state_code.upper()
        if state_code not in self._adapter_paths:
            raise KeyError(
                f"No adapter registered for state '{state_code}'. "
                f"Known: {sorted(self._adapter_paths)}"
            )

        if state_code == self._active_state:
            return self  # already mounted

        self._active_request = LoRARequest(
            lora_name=f"{state_code.lower()}_underwriter",
            lora_int_id=self._lora_ids[state_code],
            lora_path=self._adapter_paths[state_code],
        )
        self._active_state = state_code
        return self

    @property
    def active_state(self) -> Optional[str]:
        return self._active_state

    # ------------------------------------------------------------------ #
    # Inference
    # ------------------------------------------------------------------ #
    def _build_prompt(self, property_record: dict) -> str:
        user_turn = (
            "Underwrite this wholesale deal. Predict ARV, estimate rehab, apply "
            "the 70% rule for the MAO, and return a Proceed/Reject verdict as "
            "strict JSON with exactly: arv_estimate, rehab_estimate, mao_formula, "
            "mao, verdict, rationale.\n\n"
            + json.dumps(property_record, default=str, indent=2)
        )
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_turn},
        ]
        return self.tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True
        )

    def underwrite_batch(
        self,
        properties: List[dict],
        max_tokens: int = 1024,
        temperature: float = 0.2,
    ) -> List[dict]:
        """Run inference over a batch of property records with the active adapter.

        Must call hot_swap_state_lora() first so the right state lobe is mounted.
        """
        if self._active_request is None:
            raise RuntimeError(
                "No state adapter mounted — call hot_swap_state_lora(state) first."
            )

        prompts = [self._build_prompt(p) for p in properties]
        sampling = SamplingParams(
            temperature=temperature,
            top_p=0.9,
            max_tokens=max_tokens,
            guided_decoding=_guided_json(UNDERWRITE_JSON_SCHEMA),
        )

        outputs = self.llm.generate(
            prompts,
            sampling,
            lora_request=self._active_request,
        )

        results = []
        for prop, out in zip(properties, outputs):
            raw = out.outputs[0].text if out.outputs else ""
            results.append(
                {
                    "state": self._active_state,
                    "input": prop,
                    "raw": raw,
                    "parsed": _safe_json(raw),
                }
            )
        return results


def _guided_json(schema: dict):
    """Build vLLM's guided-decoding params that constrain output to `schema`.

    Wrapped so a vLLM build without structured-output support degrades to plain
    sampling (the SYSTEM_PROMPT + _safe_json still enforce the contract softly)
    instead of crashing the engine."""
    try:
        from vllm.sampling_params import GuidedDecodingParams
        return GuidedDecodingParams(json=schema)
    except Exception:
        return None


def _safe_json(text: str) -> Optional[dict]:
    """Best-effort parse of the model's JSON output."""
    text = text.strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None


if __name__ == "__main__":
    # Smoke-test wiring (requires a GPU + a trained DE adapter under ./adapters/de).
    sample = {
        "property": {
            "address": "100 Main St, Dover, DE 19901",
            "county": "Kent",
            "sqft": 1800,
            "year_built": 1985,
        },
        "deal_terms": {"purchase_price": 180000, "earnest_money": 4000},
        "distress_signals": ["Aging mechanicals", "Inspection contingency active"],
    }
    underwriter = EdgeUnderwriter()
    out = underwriter.hot_swap_state_lora("DE").underwrite_batch([sample])
    print(json.dumps(out, indent=2, default=str))
