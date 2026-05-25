"""
Oracle — Authentication Router
JWT-based agent auth with in-memory session registry.

SECRET_KEY is hardcoded here as a placeholder.
In production, load from environment: os.environ["ORACLE_SECRET_KEY"]
"""

import os
import time
from typing import Optional

import jwt
from fastapi import APIRouter, HTTPException, Header, status
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# PRODUCTION NOTE: replace with a securely generated 64-byte hex string and
# load from os.environ["ORACLE_SECRET_KEY"]. Never commit a real key.
SECRET_KEY = "ORACLE_DEV_PLACEHOLDER_KEY_replace_in_production_64bytes_abcdef1234"
ALGORITHM = "HS256"
TOKEN_TTL_SECONDS = 86_400  # 24 hours

# ---------------------------------------------------------------------------
# Demo credential store
# In production this would be a hashed-password database lookup.
# ---------------------------------------------------------------------------

DEMO_CREDENTIALS: dict[str, str] = {
    "oracle_agent": "nexus_access_2026",
    "analyst_01":   "scanner_ready",
    "ydn":          "sypher_core",
}

# ---------------------------------------------------------------------------
# In-memory session registry
# Maps agent_id → {token, issued_at, payload}
# Capped at MAX_SESSIONS concurrent entries.
# ---------------------------------------------------------------------------

MAX_SESSIONS = 100

_session_registry: dict[str, dict] = {}


def _prune_expired_sessions() -> None:
    """Remove any sessions whose token has already expired."""
    now = time.time()
    expired = [
        agent_id
        for agent_id, entry in _session_registry.items()
        if entry["issued_at"] + TOKEN_TTL_SECONDS < now
    ]
    for agent_id in expired:
        del _session_registry[agent_id]


def _register_session(agent_id: str, token: str) -> None:
    _prune_expired_sessions()
    if len(_session_registry) >= MAX_SESSIONS:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Session registry at capacity. Try again shortly.",
        )
    _session_registry[agent_id] = {
        "token": token,
        "issued_at": time.time(),
        "agent_id": agent_id,
    }


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class LoginRequest(BaseModel):
    agent_id: str
    passphrase: str


class LoginResponse(BaseModel):
    token: str
    agent_id: str
    expires_in: int  # seconds


class VerifyResponse(BaseModel):
    agent_id: str
    issued_at: float
    expires_at: float


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=LoginResponse)
def login(body: LoginRequest) -> LoginResponse:
    """
    Validate agent credentials and return a signed JWT.

    Accepts:  { "agent_id": str, "passphrase": str }
    Returns:  { "token": str, "agent_id": str, "expires_in": int }
    """
    expected = DEMO_CREDENTIALS.get(body.agent_id)
    if expected is None or expected != body.passphrase:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid agent credentials.",
        )

    now = time.time()
    payload = {
        "sub": body.agent_id,
        "iat": now,
        "exp": now + TOKEN_TTL_SECONDS,
    }
    token = jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)

    # jwt.encode returns str in PyJWT >= 2.x, bytes in < 2.x
    if isinstance(token, bytes):
        token = token.decode("utf-8")

    _register_session(body.agent_id, token)

    return LoginResponse(
        token=token,
        agent_id=body.agent_id,
        expires_in=TOKEN_TTL_SECONDS,
    )


@router.post("/verify", response_model=VerifyResponse)
def verify(authorization: Optional[str] = Header(default=None)) -> VerifyResponse:
    """
    Decode and validate a Bearer JWT.

    Expects:  Authorization: Bearer <token>
    Returns:  { "agent_id": str, "issued_at": float, "expires_at": float }
              or 401 on invalid / expired token.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization header.",
        )

    raw_token = authorization.removeprefix("Bearer ").strip()

    try:
        payload = jwt.decode(raw_token, SECRET_KEY, algorithms=[ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired.",
        )
    except jwt.InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {exc}",
        )

    return VerifyResponse(
        agent_id=payload["sub"],
        issued_at=payload["iat"],
        expires_at=payload["exp"],
    )
