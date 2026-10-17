"""Seat limits (prompt 18, T18.04; ledger L18-05; migration 0161).

The database enforces the ceiling (trigger users_seat_limit raises SQLSTATE
NSEAT); this module only turns that refusal into the sentence a person sees,
so every route that can make a member active says the same thing.
"""

from __future__ import annotations

from fastapi import HTTPException, status

SEAT_LIMIT_SQLSTATE = "NSEAT"

SEAT_LIMIT_MESSAGE = (
    "This brokerage is using every seat on its plan. "
    "The broker can suspend a member to free a seat, or change the plan."
)


def is_seat_limit(exc: BaseException) -> bool:
    return getattr(exc, "sqlstate", None) == SEAT_LIMIT_SQLSTATE


def seat_limit_http(exc: BaseException) -> HTTPException:
    return HTTPException(status.HTTP_409_CONFLICT, SEAT_LIMIT_MESSAGE)
