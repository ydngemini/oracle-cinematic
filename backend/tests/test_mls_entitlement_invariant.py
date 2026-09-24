"""Every reader of the shared listings table must narrow to entitled feeds.

`oracle_mls_listings` is shared across tenants on purpose — it is a cache, not
tenant-owned data, and it deliberately carries no `tenant_id`, because putting
one there would need a row-level RLS policy over a `regexp_replace` expression
index, which Postgres silently refuses to use under FORCE RLS. Visibility is
therefore enforced per FEED, in the query, by every reader.

Which means a reader that forgets is not a degraded experience. It is one
brokerage's paid board data served to a tenant that never licensed it, and it
looks exactly like a working feature from the outside.

This has now been found three times by review and zero times by the suite:
first four routes in `routes_mls.py`, then four more in the same file, then the
AI tool surface, the portfolio anchor and the lead-dossier overlay. Review
catches instances; only an invariant catches the next one. A new reader added
tomorrow fails here until it either narrows or is listed below with a reason.
"""

from __future__ import annotations

import pathlib
import re

BACKEND = pathlib.Path(__file__).resolve().parent.parent

TABLE = "oracle_mls_listings"

#: Any one of these means the module has applied the feed-visibility rule.
NARROWING_MARKERS = (
    "visible_feed_predicate",   # the SQL rule, for fragments and raw queries
    "visible_feeds",            # the Python rule, returns the allowed feed ids
    "visible_feeds_with",
    "narrow_to_entitled",
    "entitled_feed_ids",
)

#: Modules that touch the table without narrowing, each with the reason it is
#: correct. A bare filename is not enough — the reason is the review.
EXEMPT: dict[str, str] = {
    "data_integrations/mls_sink.py":
        "Ingest. Writes rows and assigns their licence; narrowing a write to "
        "what the writer may READ would be nonsense.",
    "data_integrations/listings_feed.py":
        "Ingest (RESO). Same as the sink — it is the source of the "
        "classification that readers later filter on.",
    "data_integrations/bridge_listings_feed.py":
        "Ingest (Bridge). Same as above.",
    "mls_licensing.py":
        "Defines the classification itself. Narrowing here would be circular.",
    "mls_health.py":
        "Defines the narrowing rule. It is the authority, not a consumer.",
}


def _modules_touching_the_table() -> dict[str, str]:
    found: dict[str, str] = {}
    for path in BACKEND.rglob("*.py"):
        rel = path.relative_to(BACKEND).as_posix()
        if rel.startswith(("tests/", "venv/", "ml_forge/")) or "/venv/" in rel:
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if TABLE in source:
            found[rel] = source
    return found


def test_every_listings_reader_narrows_to_entitled_feeds():
    offenders = []
    for rel, source in _modules_touching_the_table().items():
        if rel in EXEMPT:
            continue
        if not any(marker in source for marker in NARROWING_MARKERS):
            offenders.append(rel)

    assert not offenders, (
        "These modules read " + TABLE + " without narrowing to the tenant's "
        "entitled feeds, which serves licensed board data to tenants that did "
        "not license it:\n  " + "\n  ".join(sorted(offenders)) + "\n\n"
        "Fix by applying mls_health.visible_feed_predicate() in the WHERE "
        "clause, or by adding the module to EXEMPT in this file WITH the "
        "reason it is safe."
    )


def test_the_exempt_list_has_not_gone_stale():
    """An exemption for a module that no longer touches the table is a stale
    permission slip — it would silently cover a future file of the same name."""
    touching = _modules_touching_the_table()
    stale = sorted(set(EXEMPT) - set(touching))
    assert not stale, f"EXEMPT lists modules that no longer read {TABLE}: {stale}"


def test_the_sql_and_python_rules_agree_on_the_decisive_terms():
    """The rule is written twice — once in `_partition` for callers holding
    rows, once as SQL for fragments that cannot take a bind parameter. Two
    copies drift, so pin the three terms that carry the decision.
    """
    from mls_health import _partition, visible_feed_predicate
    from mls_licensing import DEVELOPER, LICENSED
    import inspect

    sql = visible_feed_predicate("m")
    python_rule = inspect.getsource(_partition)

    # Both must key off LICENSED, not a hand-typed string.
    assert LICENSED in sql
    assert "LICENSED" in python_rule
    # Unclassified defaults to unlicensed in both.
    assert DEVELOPER in sql
    # And the SQL reads the tenant from the session, never from an argument,
    # so no caller can widen it by passing someone else's tenant.
    assert "app_current_tenant()" in sql
    assert "$1" not in sql and "%s" not in sql


def test_python_rule_hides_licensed_feeds_without_entitlement():
    """The behaviour itself, on the Python side: the three cases that matter."""
    from mls_health import _partition
    from mls_licensing import DEVELOPER, LICENSED

    rows = [
        {"mls_id": "actris", "license_classification": DEVELOPER},
        {"mls_id": "paid_board", "license_classification": LICENSED},
        {"mls_id": "other_board", "license_classification": LICENSED},
    ]

    allowed, _ = _partition(rows, entitled={"paid_board"})

    assert "actris" in allowed, "unlicensed reference data stays visible to all"
    assert "paid_board" in allowed, "a licensed feed is visible once entitled"
    assert "other_board" not in allowed, "a licensed feed is hidden without entitlement"


def test_the_service_has_exactly_one_health_vocabulary():
    """`/api/mls/regions/{id}` used to compute its own health from
    `sync_lag_minutes` and `errors_last_24h`. Every sink writes both as a
    literal 0 and nothing else writes them at all, so the branch could only
    ever land on "healthy" — a feed that had not synced in a year reported
    healthy, and the feed LIST said something different about the same feed.

    One question, one answer.
    """
    import inspect

    from state_compliance import routes_mls

    source = inspect.getsource(routes_mls.get_mls_sync_status)
    # Comments explain the old vocabulary on purpose; only code counts.
    code = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("#")
    )

    assert "compute_health" in code, "health must come from the shared authority"
    for invented in ('health = "healthy"', 'health = "offline"', 'health = "degraded"'):
        assert invented not in code, (
            f"`{invented}` is a second health vocabulary; use mls_health's"
        )
    assert 'row.get("sync_lag_minutes")' not in code, (
        "that column is a literal 0 everywhere it is written — deriving lag "
        "from it means never reporting lag"
    )
