"""Can the PREVIOUS release run against the schema this release leaves behind?

That is the only question that decides whether an application-only rollback is
safe, and it is not the same as "did the migration work". A migration can apply
perfectly and still make rollback impossible — dropping a column the old code
still SELECTs, or adding a NOT NULL the old code never writes.

So the classification is deliberately about the OLD app against the NEW schema,
not the other way round:

    ADDITIVE      the old app keeps working. Roll the app back and LEAVE the
                  migration in place. This is the preferred pattern and should
                  be the common case.

    DESTRUCTIVE   the old app would break. An application-only rollback is
                  UNSAFE and must not be offered as if it were routine —
                  recovery is a corrective forward migration, a PITR restore,
                  or an emergency compatibility patch.

    UNKNOWN       this module could not tell. Treated as DESTRUCTIVE at every
                  call site, because the cost of being wrong is asymmetric: a
                  needless manual review costs minutes, and a rollback onto an
                  incompatible schema costs the database.

Deliberately conservative and deliberately syntactic. It reads DDL; it cannot
know whether the old code actually touched a dropped column. Guessing in the
permissive direction would turn "we checked" into a false reassurance at the
exact moment someone is acting on it under pressure.

Expand / migrate / contract exists precisely so this answer is usually
ADDITIVE: add the new shape, backfill, move reads and writes over, and only
drop the old shape in a LATER release, once no deployable version still
references it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

ADDITIVE = "additive"
DESTRUCTIVE = "destructive"
UNKNOWN = "unknown"


@dataclass
class Verdict:
    """One migration's effect on the previous release's ability to run."""

    filename: str
    classification: str
    reasons: list[str] = field(default_factory=list)

    @property
    def rollback_safe(self) -> bool:
        return self.classification == ADDITIVE


def _strip_noise(sql: str) -> str:
    """Remove comments and string literals before pattern matching.

    A migration that merely *mentions* `DROP COLUMN` in a comment explaining
    why it is NOT dropping one would otherwise be classified destructive, and a
    classifier that cries wolf gets overridden by hand until it is useless.
    """
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    sql = "\n".join(
        line for line in sql.splitlines() if not line.lstrip().startswith("--")
    )
    sql = re.sub(r"--[^\n]*", " ", sql)
    # Dollar-quoted bodies (functions, DO blocks) are kept: they contain real
    # DDL. Ordinary string literals are not.
    sql = re.sub(r"'(?:[^']|'')*'", "''", sql)
    return sql


#: Each pattern is a way the PREVIOUS release stops working. The message says
#: what breaks, not what the statement is — an operator reading this at 3am
#: needs the consequence, not a restatement of the SQL.
_BREAKING = (
    (r"\bDROP\s+TABLE\b",
     "drops a table the previous release may still read"),
    (r"\bALTER\s+TABLE\b[^;]*?\bDROP\s+(?:COLUMN\b|(?!CONSTRAINT|DEFAULT|NOT\s+NULL)\w)",
     "drops a column the previous release may still SELECT"),
    (r"\bDROP\s+(?:MATERIALIZED\s+)?VIEW\b",
     "drops a view the previous release may still query"),
    (r"\bDROP\s+FUNCTION\b",
     "drops a function the previous release may still call"),
    (r"\bDROP\s+TYPE\b",
     "drops a type the previous release may still reference"),
    (r"\bRENAME\s+(?:TO|COLUMN)\b",
     "renames an object; the previous release refers to the old name"),
    (r"\bALTER\s+COLUMN\b[^;]*?\bTYPE\b",
     "changes a column's type; the previous release may write the old one"),
    (r"\bALTER\s+COLUMN\b[^;]*?\bSET\s+NOT\s+NULL\b",
     "makes a column NOT NULL; the previous release may insert without it"),
    (r"\bADD\s+COLUMN\b(?:(?!DEFAULT|;)[\s\S])*?\bNOT\s+NULL\b(?![\s\S]{0,80}?DEFAULT)",
     "adds a NOT NULL column with no default; the previous release's INSERTs omit it"),
    (r"\bDROP\s+POLICY\b",
     "drops an RLS policy; the previous release relies on the isolation it provided"),
    (r"\bREVOKE\b",
     "revokes a privilege the previous release may depend on"),
    (r"\bTRUNCATE\b",
     "destroys data outright"),
    (r"\bDELETE\s+FROM\b",
     "deletes rows; the previous release may expect them"),
)

#: Statements that are always safe for a previous release. Used only to decide
#: whether a migration said anything at all — never to override a break.
_ADDITIVE_SHAPES = (
    r"\bCREATE\s+TABLE\b",
    r"\bADD\s+COLUMN\b",
    r"\bCREATE\s+(?:UNIQUE\s+)?INDEX\b",
    r"\bCREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\b",
    r"\bCREATE\s+POLICY\b",
    r"\bCREATE\s+TRIGGER\b",
    r"\bINSERT\s+INTO\b",
    r"\bADD\s+CONSTRAINT\b",
    r"\bGRANT\b",
    r"\bCOMMENT\s+ON\b",
    r"\bALTER\s+TABLE\b[^;]*?\bENABLE\s+ROW\s+LEVEL\s+SECURITY\b",
    r"\bCREATE\s+EXTENSION\b",
)


#: Object kinds that this codebase routinely DROPs only in order to recreate
#: them in the same migration, because `CREATE ... IF NOT EXISTS` does not exist
#: for them. `DROP POLICY IF EXISTS listings_read` followed by
#: `CREATE POLICY listings_read` is a REPLACE, and calling it destructive makes
#: the classifier wrong about half this repository's migrations — at which
#: point an operator learns to override it, which is worse than not having it.
_REPLACEABLE = ("POLICY", "TRIGGER", "INDEX", "FUNCTION", "VIEW")


def _drop_recreate_pairs(body: str) -> set[tuple[str, str]]:
    """(kind, name) pairs this migration drops AND then recreates."""
    dropped: set[tuple[str, str]] = set()
    created: set[tuple[str, str]] = set()

    for kind in _REPLACEABLE:
        for m in re.finditer(
            rf"\bDROP\s+{kind}\s+(?:CONCURRENTLY\s+)?(?:IF\s+EXISTS\s+)?([A-Za-z0-9_.\"]+)",
            body, re.I,
        ):
            dropped.add((kind, m.group(1).strip('"').lower()))
        for m in re.finditer(
            rf"\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:UNIQUE\s+)?(?:MATERIALIZED\s+)?"
            rf"{kind}\s+(?:CONCURRENTLY\s+)?(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z0-9_.\"]+)",
            body, re.I,
        ):
            created.add((kind, m.group(1).strip('"').lower()))
    return dropped & created


def _objects_created_here(body: str) -> set[str]:
    """Tables this migration creates, lowercased and unqualified."""
    return {
        m.group(1).strip('"').split(".")[-1].lower()
        for m in re.finditer(
            r"\bCREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z0-9_.\"]+)",
            body, re.I,
        )
    }


def _only_revokes_from_public(body: str) -> bool:
    """Every REVOKE in this migration targets PUBLIC.

    `REVOKE ... FROM PUBLIC` is the least-privilege hardening 0003 established:
    it takes away the implicit grant everyone has, and the application keeps
    working because it holds an explicit grant. A REVOKE from the app's own
    role is a different statement with a different consequence, and only that
    one breaks a previous release.
    """
    revokes = re.findall(r"\bREVOKE\b[\s\S]*?;", body, re.I)
    if not revokes:
        return False

    created_here = _objects_created_here(body)

    def harmless(statement: str) -> bool:
        if re.search(r"\bFROM\s+PUBLIC\b", statement, re.I):
            return True
        # A REVOKE on an object this same migration CREATED cannot break the
        # previous release: it never knew the object existed. 0110 revokes on
        # mls_feed_entitlements, a table it creates twelve lines earlier.
        target = re.search(r"\bON\s+(?:TABLE\s+)?([A-Za-z0-9_.\"]+)", statement, re.I)
        if target:
            name = target.group(1).strip('"').split(".")[-1].lower()
            if name in created_here:
                return True
        return False

    return all(harmless(r) for r in revokes)


def classify_sql(filename: str, sql: str) -> Verdict:
    """Classify one migration by what it does to the previous release."""
    body = _strip_noise(sql)
    replaced = _drop_recreate_pairs(body)
    revokes_are_hardening = _only_revokes_from_public(body)

    reasons: list[str] = []
    for pattern, message in _BREAKING:
        if not re.search(pattern, body, re.I):
            continue
        # A DROP whose object is recreated in the same migration is a replace.
        kind = next(
            (k for k in _REPLACEABLE if f"DROP\\s+{k}" in pattern), None
        )
        if kind is not None:
            dropped_here = {
                n for (k, n) in
                {(k, n) for (k, n) in _drop_recreate_pairs(body)} if k == kind
            }
            all_dropped = {
                m.group(1).strip('"').lower()
                for m in re.finditer(
                    rf"\bDROP\s+{kind}\s+(?:CONCURRENTLY\s+)?(?:IF\s+EXISTS\s+)?([A-Za-z0-9_.\"]+)",
                    body, re.I,
                )
            }
            if all_dropped and all_dropped <= dropped_here:
                continue  # every one of them comes straight back
        if "REVOKE" in pattern and revokes_are_hardening:
            continue
        reasons.append(message)
    if reasons:
        return Verdict(filename, DESTRUCTIVE, reasons)

    if any(re.search(p, body, re.I) for p in _ADDITIVE_SHAPES):
        return Verdict(filename, ADDITIVE, ["only additive statements"])

    # Said nothing this module recognises. Not "safe" — unrecognised.
    return Verdict(
        filename, UNKNOWN,
        ["no recognised DDL; classify by hand before relying on a rollback"],
    )


def classify_files(paths: Iterable) -> list[Verdict]:
    verdicts = []
    for path in paths:
        try:
            sql = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:  # noqa: PERF203
            verdicts.append(Verdict(path.name, UNKNOWN, [f"unreadable: {exc}"]))
            continue
        verdicts.append(classify_sql(path.name, sql))
    return verdicts


def rollback_is_safe(verdicts: list[Verdict]) -> tuple[bool, list[str]]:
    """Is an application-only rollback safe across ALL of these migrations?

    One destructive migration in the set is enough. Rolling back past a column
    drop is unsafe whatever the other twenty migrations did, and a summary that
    averaged them would be worse than no summary.
    """
    blocking = [v for v in verdicts if v.classification != ADDITIVE]
    if not blocking:
        return True, []
    return False, [
        f"{v.filename}: {v.classification} — {'; '.join(v.reasons)}"
        for v in blocking
    ]
