"""The 503 a route raises when the database or the sealing layer under it fails.

The handlers that use this catch RuntimeError around tenant_tx. What reaches
them is the pool refusing to start, a CryptoError or a SealError, and each of
those messages names configuration: the environment variable that is missing,
the database user, the TLS setting, the table whose text would not decrypt.
The browser gets one fixed sentence (and what was not saved, where the route
says so). The log gets the exception's type and the type of its cause, never
its text, as operator_api._db_unavailable already does.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import HTTPException, status

logger = logging.getLogger("oracle.memory_core")


def memory_core_offline(exc: BaseException, outcome: Optional[str] = None) -> HTTPException:
    """`raise memory_core_offline(exc, "note not persisted") from exc`."""
    cause = exc.__cause__ or exc.__context__
    logger.error(
        "Memory Core offline%s: %s%s",
        f" ({outcome})" if outcome else "",
        type(exc).__name__,
        f" caused by {type(cause).__name__}" if cause is not None else "",
    )
    detail = f"Memory Core offline — {outcome}." if outcome else "Memory Core offline."
    return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail)
