"""
Pytest bootstrap for the Oracle backend test suite.

Two responsibilities, both before any test module is imported:

  1. Force ORACLE_ENV=dev. auth.py fails fast at import when ORACLE_SECRET_KEY is
     unset outside development (a deliberate prod safety gate). The test suite
     runs without secrets, so we declare the dev posture here rather than make
     every CI invocation remember to export it.
  2. Put backend/ on sys.path so `import auth` / `import tenancy` resolve whether
     pytest is launched from the repo root or backend/.
"""

import os
import sys

import pytest

# Never read the developer's real .env (server.py honours this). It holds live
# SMTP, Stripe and carrier credentials; with it loaded, a test that created an
# invitation really emailed it (bounce from x@y.test, 2026-10-01).
os.environ["ORACLE_SKIP_DOTENV"] = "1"
os.environ.setdefault("ORACLE_ENV", "dev")
os.environ.setdefault("ORACLE_SECRET_KEY", "test-only-secret-key-with-at-least-32-bytes")

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)


@pytest.fixture
def granted_feed_lock(monkeypatch):
    """Grant the per-feed sync lock without a database.

    `RESOListingsFeed.sync_once` and `BridgeListingsFeed.sync_once` take a
    session-scoped advisory lock on a real pooled connection (see
    mls_feed_lock) before doing anything. Tests that exercise paging, cursor
    arithmetic or resume logic against a stub transport have no pool, and the
    lock is not what they are asserting about.

    Deliberately NOT autouse. An always-on stub would disable the lock for a
    future test that meant to exercise it, and that test would pass while
    proving nothing — which is exactly how this lock came to be written,
    correct, and called by nothing at all for its whole first life.
    tests/test_mls_feed_lock.py covers the real thing.
    """
    from contextlib import asynccontextmanager

    import mls_feed_lock

    @asynccontextmanager
    async def _granted(mls_id, *, on_busy="skip"):
        yield True

    monkeypatch.setattr(mls_feed_lock, "feed_sync_session_lock", _granted)


class _NoNetworkSMTP:
    """Any test that reaches a real SMTP connection is a bug: it would send mail."""

    def __init__(self, *args, **kwargs):
        raise AssertionError(
            "a test tried to open a real SMTP connection — mock smtp_mailer.send"
        )


@pytest.fixture(autouse=True)
def _no_real_email(monkeypatch):
    import smtplib

    monkeypatch.setattr(smtplib, "SMTP", _NoNetworkSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", _NoNetworkSMTP)
    for key in ("ORACLE_SMTP_HOST", "ORACLE_SMTP_USERNAME", "ORACLE_SMTP_PASSWORD"):
        monkeypatch.delenv(key, raising=False)


class _NoSemanticModel:
    """The semantic-frame parser's gateway in tests: no model at all.

    ORACLE_SEMANTIC_FRAME_ENABLED is ON by default, so every chat turn a test
    drives would otherwise reach llm_gateway — and a CI box with a provider key
    in its environment would make a real model call. This answers exactly as a
    deployment with no structured-output provider does. Tests that want a frame
    set `semantic_turn.GATEWAY` to their own fake.
    """

    calls = 0

    async def complete(self, *_args, **_kwargs):
        import llm_gateway

        type(self).calls += 1
        raise llm_gateway.LLMUnavailable("tests: no semantic-frame model")


@pytest.fixture(autouse=True)
def _no_real_semantic_model(monkeypatch):
    import semantic_turn

    monkeypatch.setattr(semantic_turn, "GATEWAY", _NoSemanticModel())
