"""Every send path says why it sends (external audit 26.5; rules chosen
conservatively under the owner's 2026-11-20 instruction, revisable by the owner).

``guard_outreach`` takes a ``purpose`` ('informational' | 'marketing'): a
marketing text needs documented written consent, an informational one prior
express consent. Its default is the strict MARKETING, so a forgotten argument
fails closed — but every call site is classified on purpose, with a one-line
reason in the code. The one human-declared purpose is the approved one-to-one
text (commands_api SMS: what the approver declared for that approval,
tests/test_sms_declared_purpose.py). This test finds every production call to
``guard_outreach`` / ``enforce_outreach`` by AST and pins the table, so a new
call site (or a quiet reclassification) has to come through here.
"""

from __future__ import annotations

import ast
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
GATES = {"guard_outreach", "enforce_outreach"}

# (file, enclosing function, gate, purpose expression). Where the content is
# not classified anywhere, the rule is: strict (MARKETING).
EXPECTED = [
    ("commands_api.py", "_execute_command_job", "guard_outreach", "Purpose.MARKETING"),  # approved email
    ("commands_api.py", "_execute_command_job", "guard_outreach", "sms_purpose"),  # approved text: declared
    ("commands_api.py", "_execute_command_job", "guard_outreach", "Purpose.MARKETING"),  # approved AI call
    ("crm.py", "send_message", "enforce_outreach", "Purpose.MARKETING"),
    ("email_outbox.py", "deliver", "guard_outreach", "outreach_compliance.Purpose.MARKETING"),
    ("missions/policy.py", "evaluate_action", "guard_outreach", "Purpose.MARKETING"),
    ("outreach_compliance.py", "check_outreach", "guard_outreach", "body.purpose"),
    ("sales_api.py", "_preview", "guard_outreach", "Purpose.MARKETING"),
    ("speed_to_lead.py", "_speed_to_lead_job", "guard_outreach", "Purpose.MARKETING"),
    ("telephony_api.py", "prepare_agent_call", "guard_outreach", "Purpose.MARKETING"),
    ("telephony_api.py", "twilio_agent_outbound", "guard_outreach", "Purpose.MARKETING"),
]
EXPECTED_COUNT = 12  # the 11 above (commands_api three times) + the forwarder below
# enforce_outreach forwards its caller's kwargs (purpose included) unchanged.
FORWARDERS = {("outreach_compliance.py", "enforce_outreach")}


def _production_files():
    for path in sorted(BACKEND.rglob("*.py")):
        rel = path.relative_to(BACKEND)
        if "tests" in rel.parts or rel.parts[0] in {"venv", ".venv", "node_modules"} or "site-packages" in rel.parts:
            continue
        yield rel, path


def _visit(node, file: str, lines: list[str], function: str, found: list) -> None:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        function = node.name
    if isinstance(node, ast.Call):
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name in GATES:
            found.append((file, function, name, node, "\n".join(lines[node.lineno - 1:node.end_lineno])))
    for child in ast.iter_child_nodes(node):
        _visit(child, file, lines, function, found)


def _gate_calls():
    found: list = []
    for rel, path in _production_files():
        source = path.read_text()
        if any(g in source for g in GATES):
            _visit(ast.parse(source), rel.as_posix(), source.splitlines(), "<module>", found)
    return found


def test_every_production_gate_call_names_its_purpose_with_a_reason():
    calls = _gate_calls()
    assert len(calls) == EXPECTED_COUNT, [(f, fn, ln.lineno) for f, fn, _g, ln, _s in calls]
    table = []
    for file, function, gate, node, text in calls:
        if (file, function) in FORWARDERS:
            assert any(k.arg is None for k in node.keywords), f"{file}:{node.lineno} must forward **kwargs"
            continue
        purpose = next((k.value for k in node.keywords if k.arg == "purpose"), None)
        assert purpose is not None, f"{file}:{node.lineno} {gate} does not say why it sends (26.5)"
        assert "# Purpose:" in text, f"{file}:{node.lineno} classifies without a one-line reason"
        table.append((file, function, gate, ast.unparse(purpose)))
    assert sorted(table) == sorted(EXPECTED)


def test_the_gate_defaults_to_the_strict_purpose():
    import inspect

    import outreach_compliance as oc

    for fn in (oc.guard_outreach, oc.evaluate):
        assert inspect.signature(fn).parameters["purpose"].default is oc.Purpose.MARKETING
    assert oc.CheckRequest.model_fields["purpose"].default is oc.Purpose.MARKETING
