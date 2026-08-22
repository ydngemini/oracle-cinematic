"""JSON-safe serialisation for database rows leaving the tool surface.

Split out of ``ai_chat_store`` so that ``ai_tools_read`` can use the same two
functions without importing the module that dispatches to it. The cycle was
survivable only because both names happened to be defined near the top of
``ai_chat_store``; moving a definition would have broken imports at a distance.
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any


def json_value(value: Any, default: Any = None) -> Any:
    """Decode a jsonb column that asyncpg handed back as text."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return default
    return default if value is None else value


def clean(value: Any) -> Any:
    """Recursively make a row JSON-encodable.

    Datetimes, UUIDs and Decimals become strings rather than raising in the
    encoder. Decimal in particular must not become a float: a price that
    round-trips through binary floating point stops being the stored value.
    """
    if isinstance(value, (datetime, date, uuid.UUID, Decimal)):
        return str(value)
    if isinstance(value, dict):
        return {key: clean(item) for key, item in value.items()}
    if isinstance(value, list):
        return [clean(item) for item in value]
    return value
