"""Call transcripts must not be written to the container log stream.

Transcripts carry their own retention policy — see
platform_policy.PUBLIC_PROPERTY_DATA_POLICY's `call_transcripts_days`, which the
product publishes as a commitment. The Twilio speech webhook printed the first
100 characters of what a caller said straight to stdout, so that content lived
in the ECS log group under the log group's retention and nobody's transcript
policy, in plaintext, outside every deletion path the platform models.

Debugging a silent call leg needs to know whether speech arrived, not what was
said, so the length is logged instead.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
import re

import commands_api

BACKEND = pathlib.Path(commands_api.__file__).resolve().parent

# Names that hold caller-spoken or caller-written content.
_TRANSCRIPT_NAMES = re.compile(r"\b(speech_text|transcript|speech|utterance)\b")


def test_no_transcript_text_is_logged_or_printed():
    source = pathlib.Path(commands_api.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            child.parent = parent

    def counted(node) -> bool:
        """Inside len(...) — a length, which is the point of the fix."""
        current = getattr(node, "parent", None)
        while current is not None:
            if isinstance(current, ast.Call) and ast.unparse(current.func) == "len":
                return True
            current = getattr(current, "parent", None)
        return False

    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = ast.unparse(node.func)
        if target != "print" and not target.startswith(("logger.", "log.")):
            continue
        for arg in node.args:
            # Only VARIABLES carry content. A format string may say the word
            # "speech" — "speech_chars=%d" does — without ever holding any.
            for name in ast.walk(arg):
                if not isinstance(name, ast.Name):
                    continue
                if _TRANSCRIPT_NAMES.fullmatch(name.id) and not counted(name):
                    offenders.append(f"{target} line {node.lineno}: {name.id}")

    assert offenders == [], (
        "transcript content reached a log sink:\n  " + "\n  ".join(offenders)
    )


def test_request_handlers_use_the_logger_not_print():
    """print() bypasses log level, formatting and every handler configured."""
    source = pathlib.Path(commands_api.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    prints = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and ast.unparse(node.func) == "print"
    ]

    assert prints == [], f"print() in a request module at lines {prints}"


def test_the_speech_webhook_still_reports_whether_speech_arrived():
    """The diagnostic must survive the redaction, or it will be added back."""
    source = inspect.getsource(commands_api)
    assert "speech_chars=%d" in source
