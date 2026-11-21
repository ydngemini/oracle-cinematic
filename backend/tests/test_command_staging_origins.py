"""Every pipeline that stages an outreach command is pinned with its origin
(external audit 26.5; review of PR #170).

The approver of a one-to-one text may declare it informational; a campaign
text (a mission, a sales sequence, the AI sales agent, speed-to-lead) stays
marketing. commands_api._campaign_origin recognises a campaign by the
idempotency key it was staged with (no edit can change it), by created_by and
by the context. This test finds every production call to ``create_command`` /
``stage_command`` by AST, reads the key each one stages with, and pins the
table: a new campaign pipeline cannot silently default to one-to-one, because
it has to be added here with a prefix _campaign_origin recognises.
"""
from __future__ import annotations

import ast
from pathlib import Path

import commands_api

BACKEND = Path(__file__).resolve().parents[1]
STAGERS = {"create_command", "stage_command"}

# (file, enclosing function, key prefix, origin). "client" = a key the person
# chose (a human request); one-to-one rows have origin None.
EXPECTED = sorted([
    ("ai_tools_gated.py", "_stage", "ai:", None),                       # an agent's own request in chat
    ("commands_api.py", "create_command", "client", None),              # POST /api/commands (a person)
    ("commands_api.py", "execute_personal_command", "client|execute:", None),
    ("commands_api.py", "parse_personal_command", "client|parse:", None),
    ("missions/executor.py", "_stage", "mission:", "a mission"),
    ("sales_api.py", "_smart_plan_dispatch", "smart-plan:", "a sales sequence"),
    ("sales_api.py", "run_agent_action", "sales-agent:", "the AI sales agent"),
    ("speed_to_lead.py", "_attempt_first_response", "speed-to-lead:", "speed-to-lead"),
])


def _prefix(node: ast.AST) -> str:
    """The literal start of an idempotency key expression, up to its first ':'."""
    if isinstance(node, ast.JoinedStr) and node.values and isinstance(node.values[0], ast.Constant):
        text = str(node.values[0].value)
        return text[:text.index(":") + 1] if ":" in text else text
    if isinstance(node, ast.BoolOp):            # body.idempotency_key or f"parse:{...}"
        return "|".join("client" if not isinstance(v, ast.JoinedStr) else _prefix(v) for v in node.values)
    return "client"                              # a value the caller supplied


def _key_of(call: ast.Call, function: ast.AST) -> ast.AST:
    keywords = {k.arg: k.value for k in call.keywords}
    if "idempotency_key" in keywords:
        return keywords["idempotency_key"]
    body = call.args[0]
    if isinstance(body, ast.Name):              # create_command(create_body, ctx)
        body = next(n.value for n in ast.walk(function)
                    if isinstance(n, ast.Assign) and any(getattr(t, "id", None) == body.id for t in n.targets))
    assert isinstance(body, ast.Call) and getattr(body.func, "id", None) == "CommandCreate", ast.unparse(call)
    return {k.arg: k.value for k in body.keywords}["idempotency_key"]


def _stagers():
    found = []
    for path in sorted(BACKEND.rglob("*.py")):
        rel = path.relative_to(BACKEND)
        if "tests" in rel.parts or rel.parts[0] in {"venv", ".venv", "node_modules"}:
            continue
        source = path.read_text()
        if not any(name in source for name in STAGERS):
            continue
        for function in ast.walk(ast.parse(source)):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(function):
                if not isinstance(node, ast.Call):
                    continue
                name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                inner = next((f for f in ast.walk(function) if f is not function
                              and isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))
                              and node in ast.walk(f)), None)
                if name in STAGERS and inner is None:
                    found.append((rel.as_posix(), function.name, _prefix(_key_of(node, function))))
    return found


def test_every_command_staging_pipeline_is_pinned_with_its_origin():
    found = _stagers()
    table = sorted((file, function, prefix, _origin(prefix)) for file, function, prefix in found)
    assert table == EXPECTED, table


def _origin(prefix: str):
    if "client" in prefix:
        return None
    return commands_api._campaign_origin({"idempotency_key": prefix + "x", "created_by": "agent@x.test",
                                          "draft": {"content": {}, "context": {}}})


def test_the_campaign_prefixes_are_the_pipelines_prefixes():
    pinned = {prefix for _f, _fn, prefix, origin in EXPECTED if origin}
    assert pinned == {p for p, _o in commands_api._CAMPAIGN_KEY_PREFIXES}
