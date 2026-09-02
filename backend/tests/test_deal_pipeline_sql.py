"""The DEAL_PIPELINE frame's SQL, and why it silently never worked.

`push_deal_pipeline` builds its query by concatenating a triple-quoted WHERE
clause onto a SELECT. One interval literal in it was written

    updated_at) < now()-interval '45 days))

with no closing quote — five lines below an identical branch that had one. The
unterminated literal swallowed `))\\n AND ($5=` and ended at the next quote in
the file, leaving a bare `all` where the parser expected an operator:

    DEAL_PIPELINE  syntax error at or near "all"

Every WebSocket connect pushes this frame, so the acquisition pipeline — the
main CRM view over 8.47M leads — had never rendered. Nothing caught it because
the SQL is assembled from strings at runtime: it is not parsed at import, no
type checker sees inside it, and the frame swallows its own error into a
payload field rather than raising.

These tests are cheap and catch the whole class rather than the one instance.
"""

import ast
import pathlib

BACKEND = pathlib.Path(__file__).parent.parent


#: A statement, not prose. Anchored at the start because the thing being
#: guarded is a query literal — matching "FROM" anywhere also matches every
#: docstring containing an apostrophe ("the Responses API's shape"), which is
#: balanced-quote noise rather than SQL.
_SQL_START = ("SELECT ", "INSERT INTO", "UPDATE ", "DELETE FROM", "WITH ")


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """id() of every Constant that is a docstring, so prose is excluded."""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                if isinstance(body[0].value.value, str):
                    out.add(id(body[0].value))
    return out


def _sql_literals(path: pathlib.Path) -> list[tuple[int, str]]:
    """Query literals in the module: not docstrings, and starting with a verb."""
    tree = ast.parse(path.read_text())
    docstrings = _docstring_nodes(tree)
    found = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        if id(node) in docstrings:
            continue
        stripped = node.value.strip().upper()
        if stripped.startswith(_SQL_START):
            found.append((node.lineno, node.value))
    return found


def _unbalanced(sql: str) -> bool:
    """Does this query contain an unterminated string literal?

    Two things are stripped first, because an apostrophe in either is harmless
    and counting it produces false alarms rather than findings:

      * `--` line comments — a real one in state_market_projection reads
        "a row's as_of_date", and that apostrophe is not a quote.
      * doubled '' — SQL's own escape, which is already balanced.
    """
    without_comments = "\n".join(line.split("--", 1)[0] for line in sql.splitlines())
    return without_comments.replace("''", "").count("'") % 2 == 1


def test_sql_string_literals_have_balanced_quotes():
    """An odd number of quotes in a SQL fragment is an unterminated literal.

    This is the exact defect: the parser does not stop at the typo, it keeps
    consuming until the next quote, so the error surfaces somewhere unrelated
    ("near 'all'") and reads like a logic bug rather than a missing character.
    """
    offenders = []
    for path in sorted(BACKEND.glob("*.py")):
        for lineno, text in _sql_literals(path):
            if _unbalanced(text):
                offenders.append(f"{path.name}:{lineno}")
    assert offenders == [], (
        "these SQL literals have an odd number of single quotes, so one string "
        f"is unterminated and will consume the SQL that follows it: {offenders}"
    )


def test_the_deal_pipeline_where_clause_is_parseable_sql():
    """Pinned specifically, because this frame is on the connect path.

    Asserted structurally rather than by executing: the query needs a database
    and eight bound parameters, and the failure being guarded against is a
    syntax error, which is visible without either.
    """
    server = BACKEND / "server.py"
    src = server.read_text()
    start = src.index('    where = """')
    fragment = src[start + len('    where = """'):src.index('"""', start + 15)]

    assert fragment.replace("''", "").count("'") % 2 == 0, (
        "unbalanced quotes in the DEAL_PIPELINE WHERE clause"
    )
    assert fragment.count("(") == fragment.count(")"), "unbalanced parentheses"
    # Both freshness branches compare against the same window; one having lost
    # its closing quote is what made them differ.
    assert fragment.count("interval '45 days'") == 2, (
        "both the fresh and verify branches must carry a complete interval literal"
    )


def test_the_pipeline_total_does_not_count_the_leads_table():
    """`SELECT count(*) FROM leads` is 8.47M rows and past command_timeout.

    Fixing the syntax error above only exposed this one underneath. The
    unfiltered total now reads lead_pipeline_counts — the trigger-maintained
    rollup migration 0038 added, which ai_tools_read already treats as the
    authority — and a filtered total is bounded by a cap instead of by the
    table. Reporting "1000+" is honest; timing out is not.
    """
    src = (BACKEND / "server.py").read_text()
    start = src.index("async def push_deal_pipeline(")
    body = src[start:src.index("\nasync def ", start + 10)]

    assert "lead_pipeline_counts" in body, "the unfiltered total must use the rollup"
    assert "_PIPELINE_COUNT_CAP" in body, "a filtered total must be capped"
    assert '"SELECT count(*) FROM leads " + where' not in body, (
        "counting the leads table directly is what took this frame past the "
        "pool's 30s command_timeout"
    )
    assert "total_is_exact" in body, (
        "a capped total must say it is capped rather than present a floor as a fact"
    )
