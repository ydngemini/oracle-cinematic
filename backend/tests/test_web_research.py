"""The agent must either search the web or say it could not.

`web_search` was gated on TAVILY_API_KEY, which is unset on most deployments —
so the tool was hidden and the agent had no web access at all, while its system
prompt forbade it from claiming sources it does not have. A keyless provider now
sits behind the same seam.

The load-bearing property is the failure mode: a search that cannot run must
raise, never return a bland "no results". The latter reads to a model as a
finding about the world and licenses it to fill the gap from memory.
"""

from __future__ import annotations

import asyncio

import pytest

from data_integrations.web_research import format_for_agent


def test_results_carry_their_source_so_the_agent_can_attribute_them():
    payload = {
        "answer": "Dover is the capital of Delaware.",
        "results": [
            {"title": "Dover, Delaware", "snippet": "Capital city.",
             "url": "https://en.wikipedia.org/wiki/Dover,_Delaware", "source": "Wikipedia"},
            {"title": "Dover", "snippet": "County seat of Kent County.",
             "url": "", "source": "DuckDuckGo"},
        ],
    }

    rendered = format_for_agent(payload)

    assert "Dover is the capital of Delaware." in rendered
    assert "[Wikipedia]" in rendered
    assert "[DuckDuckGo]" in rendered
    assert "en.wikipedia.org" in rendered


def test_an_empty_result_set_raises_rather_than_reporting_nothing_found():
    """"No results" is a claim about the world; an exception is a claim about us."""
    with pytest.raises(RuntimeError):
        format_for_agent({"answer": "", "results": []})

    with pytest.raises(RuntimeError):
        format_for_agent(None)


def test_normalisation_strips_wikipedia_search_markup():
    from data_integrations.web_research import WebResearchSource

    source = WebResearchSource.__new__(WebResearchSource)
    out = source.normalize({
        "instant_answer": None,
        "wikipedia": {"query": {"search": [
            {"title": "Kent County", "snippet": 'A <span class="searchmatch">county</span> in &quot;Delaware&quot;.'},
        ]}},
    })

    snippet = out["results"][0]["snippet"]
    assert "searchmatch" not in snippet
    assert "<span" not in snippet
    assert '"Delaware"' in snippet
    assert out["results"][0]["url"].endswith("/wiki/Kent_County")


def test_normalisation_survives_a_source_returning_nothing():
    """One upstream failing must not lose the other's results."""
    from data_integrations.web_research import WebResearchSource

    source = WebResearchSource.__new__(WebResearchSource)
    out = source.normalize({
        "instant_answer": {"AbstractText": "An answer.", "Heading": "Topic",
                           "AbstractURL": "https://example.test", "AbstractSource": "Example"},
        "wikipedia": None,
    })

    assert out["answer"] == "An answer."
    assert len(out["results"]) == 1
    assert out["results"][0]["source"] == "Example"


def test_the_tool_is_offered_even_without_a_tavily_key(monkeypatch):
    """The gate used to hide it, leaving the agent with no web access at all."""
    import ai_chat_agent

    monkeypatch.setattr(ai_chat_agent, "TAVILY_API_KEY", "")

    assert ai_chat_agent._tool_is_enabled("web_search") is True


def test_web_search_falls_back_to_the_keyless_provider(monkeypatch):
    import ai_chat_agent

    monkeypatch.setattr(ai_chat_agent, "TAVILY_API_KEY", "")

    async def fake_keyless(query):
        return f"keyless answer for {query}"

    monkeypatch.setattr(ai_chat_agent, "_keyless_web_search", fake_keyless)

    assert asyncio.run(ai_chat_agent._web_search("dover delaware")) == "keyless answer for dover delaware"


def test_a_tavily_failure_falls_back_rather_than_leaving_the_agent_blind(monkeypatch):
    import ai_chat_agent

    monkeypatch.setattr(ai_chat_agent, "TAVILY_API_KEY", "tvly-something")

    async def fake_keyless(query):
        return "keyless rescue"

    monkeypatch.setattr(ai_chat_agent, "_keyless_web_search", fake_keyless)

    def explode(*_a, **_k):
        raise OSError("tavily unreachable")

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", explode)

    assert asyncio.run(ai_chat_agent._web_search("anything at all")) == "keyless rescue"


def test_a_too_short_query_is_still_rejected(monkeypatch):
    """Validation must run before either provider is consulted."""
    import ai_chat_agent

    monkeypatch.setattr(ai_chat_agent, "TAVILY_API_KEY", "")

    with pytest.raises(ValueError):
        asyncio.run(ai_chat_agent._web_search("ab"))


# ---------------------------------------------------------------------------
# Harvester retention — the other half of "research has no citations"
# ---------------------------------------------------------------------------

def test_every_base_harvester_resolves_a_source_key():
    """`_retain_raw` used to require an explicitly-set SOURCE_KEY.

    Only 6 of 60 harvesters set one, so retention silently returned for the
    rest — which is why `source_records` was nearly empty and every
    /api/intelligence route 422'd for want of a citable observation.
    """
    from harvesters.base import BaseHarvester
    from harvesters.firehose import REGISTRY

    missing = []
    for state, cls in REGISTRY.items():
        if not issubclass(cls, BaseHarvester):
            continue  # see the test below
        instance = cls.__new__(cls)
        instance.STATE = getattr(cls, "STATE", state)
        key = type(instance).source_key.fget(instance)
        if not key or key.endswith(":??"):
            missing.append(state)

    assert missing == [], f"harvesters with no resolvable source key: {missing}"


def test_states_outside_the_base_harvester_cannot_retain_observations():
    """Maryland is on a separate `PropertyHarvester` ABC with no retention path.

    Recorded rather than asserted-away: giving BaseHarvester a default
    source_key unblocks retention for 50 of the 51 registered states, and MD
    still cannot produce a citable observation because its base class has no
    `_retain_raw` at all. Anything relying on MD citations needs that port
    first. If this list changes, the assumption behind it has changed too.
    """
    from harvesters.base import BaseHarvester
    from harvesters.firehose import REGISTRY

    outside = sorted(
        state for state, cls in REGISTRY.items() if not issubclass(cls, BaseHarvester)
    )

    assert outside == ["MD"], (
        f"the set of states outside BaseHarvester changed: {outside}. "
        "Each one is a state that cannot produce source_records."
    )


def test_licence_terms_are_declared_rather_than_hardcoded():
    """The upsert used to write 'municipal-open-data', property-level-allowed,
    outreach-not-allowed for every source regardless of what it actually was.

    Those values are cited by /api/intelligence as the basis for using the data,
    so an unstated licence is a claim nobody checked.
    """
    from harvesters.base import BaseHarvester

    assert BaseHarvester.LICENSE_NAME == "municipal-open-data"
    assert BaseHarvester.PROPERTY_LEVEL_ALLOWED is True
    # Public does not mean contactable — this default is the conservative one.
    assert BaseHarvester.OUTREACH_USE_ALLOWED is False


def test_a_harvester_can_state_its_own_terms():
    from harvesters.base import BaseHarvester

    class Licensed(BaseHarvester):
        STATE = "ZZ"
        LICENSE_NAME = "vendor-agreement"
        OUTREACH_USE_ALLOWED = True

        async def fetch_raw(self, *a, **k):  # pragma: no cover — never called
            return []

        def map_record(self, row):  # pragma: no cover
            return row

    assert Licensed.LICENSE_NAME == "vendor-agreement"
    assert Licensed.OUTREACH_USE_ALLOWED is True
    assert Licensed.PROPERTY_LEVEL_ALLOWED is True, "unstated fields keep the safe default"


# ---------------------------------------------------------------------------
# On-demand retention — the half that actually populates source_records
# ---------------------------------------------------------------------------

def test_on_demand_retention_does_not_advance_the_harvest_cursor(monkeypatch):
    """A targeted lookup must not make the bulk pass think it covered ground.

    `_retain_raw` writes the cursor into harvest_sources as part of the same
    transaction. If an on-demand lookup did that too, the next firehose run
    would resume from a parcel it never actually swept.
    """
    from contextlib import asynccontextmanager

    from harvesters.base import BaseHarvester

    class Fake(BaseHarvester):
        STATE = "ZZ"

        async def fetch_raw(self, *a, **k):  # pragma: no cover
            return []

        def map_record(self, row):  # pragma: no cover
            return row

    harvester = Fake.__new__(Fake)
    harvester.STATE = "ZZ"
    harvester.tenant_id = "11111111-1111-1111-1111-111111111111"
    harvester.agent_id = "test"
    harvester.metrics = {"raw_retained": 0}
    harvester._cursor_start = None

    statements: list[str] = []

    class _Conn:
        async def fetchrow(self, query, *_a):
            statements.append(query)
            return {"id": "lic-1", "retention_days": 730}

        async def execute(self, query, *_a):
            statements.append(query)

    @asynccontextmanager
    async def _tx(_ctx):
        yield _Conn()

    monkeypatch.setattr("db.connection.tenant_tx", _tx)

    written = asyncio.run(harvester.retain_observations([{"id": "row-1", "a": 1}]))

    assert written == 1
    assert any("source_records" in q for q in statements), "the observation must be stored"
    assert not any("harvest_sources" in q for q in statements), (
        "an on-demand lookup must not write a harvest cursor"
    )


def test_on_demand_retention_ignores_the_bulk_flag(monkeypatch):
    """RETAIN_RAW governs the bulk path only.

    It is off for the parcel firehoses on purpose — raw JSON for every parcel in
    51 states is unbounded storage for data nobody has looked at. Retention for
    a property somebody just opened is a different decision.
    """
    import ast
    import inspect
    import textwrap

    from harvesters.base import BaseHarvester

    def _reads_retain_raw(func) -> bool:
        """True when the function's *code* consults RETAIN_RAW.

        Parsed rather than grepped: the docstrings deliberately explain the
        bulk/on-demand distinction and mention the flag by name, so a substring
        search cannot tell an explanation from a branch.
        """
        tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "RETAIN_RAW":
                return True
        return False

    assert BaseHarvester.RETAIN_RAW is False
    assert _reads_retain_raw(BaseHarvester._retain_raw), "the bulk path gates on it"
    assert not _reads_retain_raw(BaseHarvester.retain_observations)
    assert not _reads_retain_raw(BaseHarvester._persist_observations)
