"""The 503 a route raises when the database or the sealing layer under it fails.

The handlers that use this catch RuntimeError around tenant_tx. What reaches
them is DatabaseUnavailable (the pool is not up, exhausted or unreachable), a
CryptoError or a SealError/NoteSealError, and those messages name
configuration: the missing master key, the pool's state, the table whose text
would not decrypt. The browser gets one fixed sentence, plus what was not
saved where the route says so.

The log gets the route, the exception type and, for those four app-authored
types only, their text: it names configuration, ids and tables, never row
content. Any other exception, and every cause (an asyncpg error can quote the
value it rejected), is logged by type.

The routes keep their own 503 rather than the global DatabaseUnavailable
handler: that one answers "Your work is saved", which a route reporting
"note not persisted" must not claim.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import HTTPException, status

logger = logging.getLogger("oracle.memory_core")

# Matched by class name along the MRO so this module imports neither crypto
# nor the sealing modules (both import the database layer).
_APP_AUTHORED = frozenset({"DatabaseUnavailable", "SealError", "NoteSealError", "CryptoError"})


def _describe(exc: BaseException) -> str:
    if {cls.__name__ for cls in type(exc).__mro__} & _APP_AUTHORED:
        return f"{type(exc).__name__}: {exc}"
    return type(exc).__name__


def _route(exc: BaseException) -> str:
    # The first traceback entry is the frame that caught it: the route.
    tb = exc.__traceback__
    return tb.tb_frame.f_code.co_name if tb is not None else "?"


def memory_core_offline(exc: BaseException, outcome: Optional[str] = None) -> HTTPException:
    """`raise memory_core_offline(exc, "note not persisted") from exc`."""
    cause = exc.__cause__ if exc.__cause__ is not None else (
        None if exc.__suppress_context__ else exc.__context__)
    logger.error(
        "Memory Core offline in %s%s: %s%s",
        _route(exc),
        f" ({outcome})" if outcome else "",
        _describe(exc),
        f" caused by {type(cause).__name__}" if cause is not None else "",
    )
    detail = f"Memory Core offline — {outcome}." if outcome else "Memory Core offline."
    return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail)
