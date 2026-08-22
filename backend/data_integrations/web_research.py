"""
data_integrations/web_research.py — keyless web search for the AI agent.

The chat agent's `web_search` tool has always been gated on TAVILY_API_KEY,
which is unset here and on most deployments. The gate is honest — hiding a tool
that cannot work beats letting the model call it and improvise — but the effect
is that the agent has **no** web research capability at all, while its system
prompt forbids it from claiming otherwise.

This is the keyless fallback: DuckDuckGo's Instant Answer endpoint plus
Wikipedia's public search API. Neither needs a key, an account, or an agreement.
Together they cover the "what is this / who is this / summarise this" questions
the agent actually asks; neither is a general web index and this module does not
pretend otherwise.

Deliberately a DataSource subclass rather than a bare urllib call, so it
inherits the rate limiter, retry policy and mandatory IntegrationCache that
every other external source here uses. A hand-rolled HTTP client is how the
duplicate FEMA clients happened.
"""

from __future__ import annotations

import json
import urllib.parse
from typing import Any, Optional

from .base import DataSource, RateLimiter, RetryConfig
from .cache import IntegrationCache

# Both endpoints are public and unauthenticated. They are also courtesy
# endpoints, so the rate limiter below is deliberately conservative.
_DDG_URL = "https://api.duckduckgo.com/"
_WIKI_URL = "https://en.wikipedia.org/w/api.php"

# Search results go stale, but not within a conversation. An hour keeps a
# multi-turn exchange consistent without serving yesterday's answer.
_TTL = 3600


class WebResearchSource(DataSource):
    """Keyless web lookup. Returns None when nothing usable came back."""

    source_name = "web_research"

    def __init__(self, cache: Optional[IntegrationCache] = None):
        super().__init__(
            rate_limiter=RateLimiter(min_interval=1.0, jitter=0.3),
            retry_config=RetryConfig(max_attempts=2, base_backoff=2.0),
            cache=cache,
        )

    def _cache_ttl(self) -> int:
        return _TTL

    async def fetch(self, *, query: str) -> Optional[dict]:
        ddg = await self._instant_answer(query)
        wiki = await self._wikipedia(query)
        if not ddg and not wiki:
            return None
        return {"instant_answer": ddg, "wikipedia": wiki}

    async def _instant_answer(self, query: str) -> Optional[dict]:
        params = urllib.parse.urlencode({
            "q": query, "format": "json", "no_html": 1, "skip_disambig": 1,
        })
        try:
            return await self._get_json(f"{_DDG_URL}?{params}", timeout=12)
        except Exception as exc:  # noqa: BLE001 — one source failing is not fatal
            self._log.info("DuckDuckGo instant answer failed for %r: %s", query[:80], exc)
            return None

    async def _wikipedia(self, query: str) -> Optional[dict]:
        params = urllib.parse.urlencode({
            "action": "query", "list": "search", "srsearch": query,
            "srlimit": 5, "format": "json",
        })
        try:
            return await self._get_json(f"{_WIKI_URL}?{params}", timeout=12)
        except Exception as exc:  # noqa: BLE001
            self._log.info("Wikipedia search failed for %r: %s", query[:80], exc)
            return None

    def normalize(self, raw: dict) -> dict:
        """Flatten both sources into titled snippets with their origin attached.

        Every snippet keeps its source so the agent can attribute what it says,
        and so a reader can tell an encyclopaedia summary from a search blurb.
        """
        results: list[dict[str, Any]] = []

        answer = ""
        instant = raw.get("instant_answer") or {}
        if isinstance(instant, dict):
            answer = str(instant.get("AbstractText") or "").strip()
            if answer:
                results.append({
                    "title": str(instant.get("Heading") or "Summary"),
                    "snippet": answer,
                    "url": str(instant.get("AbstractURL") or ""),
                    "source": str(instant.get("AbstractSource") or "DuckDuckGo"),
                })
            for topic in (instant.get("RelatedTopics") or [])[:5]:
                if not isinstance(topic, dict):
                    continue
                text = str(topic.get("Text") or "").strip()
                if text:
                    results.append({
                        "title": text.split(" - ")[0][:120],
                        "snippet": text,
                        "url": str((topic.get("FirstURL") or "")),
                        "source": "DuckDuckGo",
                    })

        wiki = raw.get("wikipedia") or {}
        for hit in (((wiki.get("query") or {}).get("search")) or [])[:5]:
            if not isinstance(hit, dict):
                continue
            title = str(hit.get("title") or "").strip()
            # The API returns HTML search-match markup in the snippet.
            snippet = (
                str(hit.get("snippet") or "")
                .replace('<span class="searchmatch">', "")
                .replace("</span>", "")
                .replace("&quot;", '"')
                .strip()
            )
            if title:
                results.append({
                    "title": title,
                    "snippet": snippet,
                    "url": f"https://en.wikipedia.org/wiki/{urllib.parse.quote(title.replace(' ', '_'))}",
                    "source": "Wikipedia",
                })

        return {"answer": answer, "results": results, "provider": "keyless"}

    async def search(self, query: str) -> Optional[dict]:
        return await self.get(f"web_research:{query.strip().lower()[:200]}", query=query)


def format_for_agent(payload: Optional[dict]) -> str:
    """Render a result set as the plain text the tool loop passes to a model.

    Raises on an empty payload rather than returning a bland "no results"
    string: the agent must be able to tell "the web says nothing about this"
    from "the search did not happen", and only an exception carries that
    distinction through the tool loop.
    """
    if not payload or not payload.get("results"):
        raise RuntimeError("Web search returned no usable results.")

    lines: list[str] = []
    if payload.get("answer"):
        lines.append(str(payload["answer"]).strip())
    for item in payload["results"][:8]:
        title = item.get("title") or "Untitled"
        snippet = (item.get("snippet") or "")[:300]
        source = item.get("source") or "web"
        url = item.get("url") or ""
        lines.append(f"- [{source}] {title}: {snippet}{f' ({url})' if url else ''}")
    return "\n\n".join(lines)
