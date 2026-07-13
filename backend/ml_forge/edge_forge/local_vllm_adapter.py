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
import time
from typing import Any, Dict, List, Optional

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
        fallback_models: Optional[List[str]] = None,
    ):
        if not 0.10 <= gpu_memory_utilization <= 0.95:
            raise ValueError("gpu_memory_utilization must be between 0.10 and 0.95")
        try:
            from vllm import LLM, SamplingParams
            from vllm.lora.request import LoRARequest
        except ImportError as exc:
            raise RuntimeError(
                "vLLM is unavailable; run EdgeUnderwriter inside the GPU inference image"
            ) from exc
        self._sampling_params_class = SamplingParams
        self._lora_request_class = LoRARequest
        self.base_model = base_model
        self.max_lora_rank = max_lora_rank
        # enable_lora keeps the base weights resident while LoRA deltas swap in.
        load_errors: list[str] = []
        self.llm = None
        for candidate in [base_model, *(fallback_models or [])]:
            try:
                self.llm = LLM(
                    model=candidate,
                    enable_lora=True,
                    max_loras=max_loras,
                    max_lora_rank=max_lora_rank,
                    max_cpu_loras=max_loras * 2,
                    gpu_memory_utilization=gpu_memory_utilization,
                )
                self.base_model = candidate
                break
            except Exception as exc:  # noqa: BLE001 - explicit configured fallback
                load_errors.append(f"{candidate}: {type(exc).__name__}")
        if self.llm is None:
            raise RuntimeError("No configured base model could load: " + "; ".join(load_errors))
        self.tokenizer = self.llm.get_tokenizer()
        self.adapter_root = adapter_root

        # Registry of known state adapters and a monotonic id counter vLLM needs.
        self._adapter_paths: Dict[str, str] = {}
        self._lora_ids: Dict[str, int] = {}
        self._next_lora_id = 1

        # Currently mounted state adapter.
        self._active_state: Optional[str] = None
        self._active_request: Optional[Any] = None
        self._adapter_metadata: Dict[str, dict] = {}
        self._telemetry = {
            "base_model": self.base_model,
            "swaps": 0,
            "batches": 0,
            "records": 0,
            "parse_failures": 0,
            "last_latency_ms": None,
        }

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

    def register_adapter(
        self,
        state_code: str,
        adapter_path: str,
        *,
        model_version: Optional[str] = None,
    ):
        """Register an adapter after base-model/rank compatibility checks."""
        state_code = state_code.upper()
        if not os.path.exists(adapter_path):
            raise FileNotFoundError(f"Adapter path missing: {adapter_path}")
        config_path = os.path.join(adapter_path, "adapter_config.json")
        if not os.path.isfile(config_path):
            raise ValueError(f"adapter_config.json missing under {adapter_path}")
        with open(config_path, "r", encoding="utf-8") as handle:
            config = json.load(handle)
        expected_base = str(config.get("base_model_name_or_path") or "")
        if expected_base and expected_base != self.base_model:
            # Repositories may use an alias for the same base; require an
            # explicit compatible_base_models declaration to permit it.
            compatible = set(config.get("compatible_base_models") or [])
            if self.base_model not in compatible:
                raise ValueError(
                    f"adapter base {expected_base!r} is incompatible with resident base {self.base_model!r}"
                )
        rank = int(config.get("r") or config.get("lora_rank") or 0)
        if rank <= 0 or rank > self.max_lora_rank:
            raise ValueError(
                f"adapter rank {rank} exceeds configured max_lora_rank {self.max_lora_rank}"
            )
        self._adapter_paths[state_code] = adapter_path
        self._adapter_metadata[state_code] = {
            "model_version": model_version or config.get("model_version") or "unversioned",
            "rank": rank,
            "base_model": expected_base or self.base_model,
        }
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

        self._active_request = self._lora_request_class(
            lora_name=f"{state_code.lower()}_underwriter",
            lora_int_id=self._lora_ids[state_code],
            lora_path=self._adapter_paths[state_code],
        )
        self._active_state = state_code
        self._telemetry["swaps"] += 1
        self._telemetry["active_adapter"] = state_code
        self._telemetry["active_model_version"] = self._adapter_metadata[state_code]["model_version"]
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
        sampling = self._sampling_params_class(
            temperature=temperature,
            top_p=0.9,
            max_tokens=max_tokens,
            guided_decoding=_guided_json(UNDERWRITE_JSON_SCHEMA),
        )

        started = time.monotonic()
        outputs = self.llm.generate(
            prompts,
            sampling,
            lora_request=self._active_request,
        )

        results = []
        for prop, out in zip(properties, outputs):
            raw = out.outputs[0].text if out.outputs else ""
            parsed = _safe_json(raw)
            if parsed is None:
                self._telemetry["parse_failures"] += 1
            results.append(
                {
                    "state": self._active_state,
                    "model_version": self._adapter_metadata.get(self._active_state or "", {}).get("model_version"),
                    "input": prop,
                    "raw": raw,
                    "parsed": parsed,
                }
            )
        self._telemetry["batches"] += 1
        self._telemetry["records"] += len(properties)
        self._telemetry["last_latency_ms"] = round((time.monotonic() - started) * 1000, 2)
        return results

    def canary_evaluate(self, state_code: str, cases: List[dict]) -> dict:
        """Run fixed cases and require parseable outputs plus expected verdicts."""
        if not cases:
            raise ValueError("at least one canary case is required")
        inputs = [dict(case.get("input") or {}) for case in cases]
        outputs = self.hot_swap_state_lora(state_code).underwrite_batch(inputs)
        passed = 0
        details = []
        for case, output in zip(cases, outputs):
            parsed = output.get("parsed")
            expected = case.get("expected_verdict")
            ok = parsed is not None and (expected is None or parsed.get("verdict") == expected)
            passed += int(ok)
            details.append({"case_id": case.get("case_id"), "passed": ok})
        return {
            "passed": passed == len(cases),
            "pass_rate": round(passed / len(cases), 4),
            "cases": details,
            "model_version": self._adapter_metadata[state_code.upper()]["model_version"],
        }

    def telemetry(self) -> dict:
        return dict(self._telemetry)


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
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    required = set(UNDERWRITE_JSON_SCHEMA["required"])
    if set(parsed) != required:
        return None
    if parsed.get("mao_formula") != "0.70 * ARV - rehab":
        return None
    if parsed.get("verdict") not in {"Proceed", "Reject"}:
        return None
    for key in ("arv_estimate", "rehab_estimate", "mao"):
        if not isinstance(parsed.get(key), int) or parsed[key] < 0:
            return None
    if not isinstance(parsed.get("rationale"), str):
        return None
    return parsed


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
