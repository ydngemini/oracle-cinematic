"""Bounded Azure Foundry supervisor for public-data collection jobs.

The model is a planner, not a Python or network executor. It may order known
dataset jobs and choose conservative retry/concurrency settings. Code retains
authority over every URL, parser, publisher allowlist, byte limit, timeout,
database write, and scheduled job.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import asdict, dataclass
from functools import lru_cache
from typing import Any, Iterable

from market_research_agent import (
    DatasetSpec,
    NationwideMarketResearchAgent,
    _default_specs,
)

logger = logging.getLogger("oracle.scraper_supervisor")

_PLAN_FIELDS = frozenset(
    {
        "dataset_order",
        "max_concurrency",
        "attempts_per_source",
        "retry_backoff_seconds",
        "reason",
    }
)
_PLANNER_INSTRUCTIONS = """You plan one legal, bounded public housing-data refresh.
Return exactly one JSON object and no markdown.

You may:
- order the provided dataset keys;
- choose max_concurrency from 1 through 4;
- choose attempts_per_source from 1 through 3;
- choose retry_backoff_seconds from 0 through 30.

You may not invent, remove, or rename datasets. You may not provide URLs,
Python paths, modules, code, shell commands, credentials, browser automation,
consumer listing-site scraping, or database instructions. All listed datasets
must be included exactly once. Prefer comprehensive coverage over speed."""


@dataclass(frozen=True)
class ScraperPlan:
    dataset_order: tuple[str, ...]
    max_concurrency: int
    attempts_per_source: int
    retry_backoff_seconds: float
    reason: str
    planner: str

    def public_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["dataset_order"] = list(self.dataset_order)
        return data


def _supervisor_enabled() -> bool:
    return os.getenv("ORACLE_MARKET_AI_SUPERVISOR_ENABLED", "0") == "1"


def _supervisor_model() -> str:
    return (
        os.getenv("ORACLE_MARKET_AI_SUPERVISOR_MODEL", "").strip()
        or "gpt-oss-120b"
    )


def _configured_concurrency() -> int:
    try:
        value = int(os.getenv("ORACLE_MARKET_DATA_CONCURRENCY", "2"))
    except ValueError:
        value = 2
    return max(1, min(4, value))


def _deterministic_plan(
    specs: Iterable[DatasetSpec],
    *,
    planner: str = "deterministic",
    reason: str = "Comprehensive allowlisted refresh.",
) -> ScraperPlan:
    return ScraperPlan(
        dataset_order=tuple(spec.key for spec in specs),
        max_concurrency=_configured_concurrency(),
        attempts_per_source=2,
        retry_backoff_seconds=2.0,
        reason=reason[:240],
        planner=planner,
    )


def _parse_model_plan(text: str, specs: Iterable[DatasetSpec]) -> ScraperPlan:
    """Parse a strict plan and reject any attempt to expand model authority."""
    spec_list = list(specs)
    known_keys = tuple(spec.key for spec in spec_list)
    known_set = set(known_keys)
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("planner response was not strict JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("planner response must be a JSON object")
    unknown_fields = set(payload) - _PLAN_FIELDS
    if unknown_fields:
        raise ValueError("planner response included forbidden fields")

    dataset_order = payload.get("dataset_order")
    if (
        not isinstance(dataset_order, list)
        or not dataset_order
        or not all(isinstance(key, str) for key in dataset_order)
    ):
        raise ValueError("dataset_order must be a non-empty string list")
    if len(dataset_order) != len(set(dataset_order)):
        raise ValueError("dataset_order contains duplicates")
    if set(dataset_order) != known_set:
        raise ValueError("dataset_order must contain every allowlisted dataset exactly once")

    try:
        concurrency = int(payload.get("max_concurrency", 2))
        attempts = int(payload.get("attempts_per_source", 2))
        backoff = float(payload.get("retry_backoff_seconds", 2))
    except (TypeError, ValueError) as exc:
        raise ValueError("planner execution settings must be numeric") from exc
    if not 1 <= concurrency <= 4:
        raise ValueError("max_concurrency is outside the safe range")
    if not 1 <= attempts <= 3:
        raise ValueError("attempts_per_source is outside the safe range")
    if not 0 <= backoff <= 30:
        raise ValueError("retry_backoff_seconds is outside the safe range")

    reason = payload.get("reason", "")
    if not isinstance(reason, str):
        raise ValueError("reason must be a string")
    return ScraperPlan(
        dataset_order=tuple(dataset_order),
        max_concurrency=concurrency,
        attempts_per_source=attempts,
        retry_backoff_seconds=backoff,
        reason=(reason.strip() or "Azure Foundry selected a bounded refresh plan.")[:240],
        planner=f"azure-foundry:{_supervisor_model()}",
    )


def _planner_prompt(specs: Iterable[DatasetSpec]) -> str:
    datasets = [
        {"key": spec.key, "publisher": spec.attribution}
        for spec in specs
    ]
    return json.dumps(
        {
            "objective": (
                "Refresh redistributable aggregate housing research with the "
                "strongest available 50-state plus DC coverage."
            ),
            "datasets": datasets,
            "source_count": len(datasets),
        },
        separators=(",", ":"),
    )


def _response_text_config(specs: Iterable[DatasetSpec]) -> dict[str, Any]:
    dataset_keys = [spec.key for spec in specs]
    return {
        "format": {
            "type": "json_schema",
            "name": "neoh_scraper_plan",
            "strict": True,
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "dataset_order",
                    "max_concurrency",
                    "attempts_per_source",
                    "retry_backoff_seconds",
                    "reason",
                ],
                "properties": {
                    "dataset_order": {
                        "type": "array",
                        "items": {"type": "string", "enum": dataset_keys},
                        "minItems": len(dataset_keys),
                        "maxItems": len(dataset_keys),
                    },
                    "max_concurrency": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 4,
                    },
                    "attempts_per_source": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 3,
                    },
                    "retry_backoff_seconds": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 30,
                    },
                    "reason": {"type": "string", "maxLength": 240},
                },
            },
        }
    }


@lru_cache(maxsize=1)
def _foundry_openai_client():
    endpoint = os.getenv("ORACLE_FOUNDRY_PROJECT_ENDPOINT", "").rstrip("/")
    if not endpoint:
        raise RuntimeError("Foundry project endpoint is not configured")
    from azure.ai.projects import AIProjectClient
    from azure.identity import DefaultAzureCredential

    credential = DefaultAzureCredential(
        exclude_interactive_browser_credential=True,
        managed_identity_client_id=os.getenv("AZURE_CLIENT_ID") or None,
    )
    project = AIProjectClient(endpoint=endpoint, credential=credential)
    return project.get_openai_client()


def _request_model_plan(specs: Iterable[DatasetSpec]) -> str:
    spec_list = list(specs)
    response = _foundry_openai_client().responses.create(
        model=_supervisor_model(),
        input=[{"role": "user", "content": _planner_prompt(spec_list)}],
        instructions=_PLANNER_INSTRUCTIONS,
        max_output_tokens=1000,
        reasoning={"effort": "low"},
        text=_response_text_config(spec_list),
        store=False,
    )
    return str(response.output_text or "").strip()


async def build_scraper_plan(specs: Iterable[DatasetSpec]) -> ScraperPlan:
    spec_list = list(specs)
    if not _supervisor_enabled():
        return _deterministic_plan(spec_list)
    timeout_seconds = max(
        5,
        min(
            60,
            int(os.getenv("ORACLE_MARKET_AI_SUPERVISOR_TIMEOUT_SECONDS", "20")),
        ),
    )
    try:
        text = await asyncio.wait_for(
            asyncio.to_thread(_request_model_plan, spec_list),
            timeout=timeout_seconds,
        )
        return _parse_model_plan(text, spec_list)
    except Exception as exc:  # noqa: BLE001 - deterministic operation must survive AI outage
        logger.warning("Foundry scraper plan rejected; using safe fallback: %s", exc)
        return _deterministic_plan(
            spec_list,
            planner="deterministic-fallback",
            reason=f"Foundry plan unavailable or invalid ({type(exc).__name__}).",
        )


async def run_supervised_market_research_sync() -> dict[str, Any]:
    specs = _default_specs()
    plan = await build_scraper_plan(specs)
    by_key = {spec.key: spec for spec in specs}
    ordered_specs = [by_key[key] for key in plan.dataset_order]
    result = await NationwideMarketResearchAgent(
        ordered_specs,
        concurrency=plan.max_concurrency,
        attempts_per_source=plan.attempts_per_source,
        retry_backoff_seconds=plan.retry_backoff_seconds,
    ).sync_once()
    result["supervisor"] = plan.public_dict()
    result["execution_policy"] = {
        "arbitrary_python": False,
        "arbitrary_urls": False,
        "publisher_allowlist_enforced": True,
        "consumer_listing_scraping": False,
    }
    return result
