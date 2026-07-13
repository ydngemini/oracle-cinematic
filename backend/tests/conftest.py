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

os.environ.setdefault("ORACLE_ENV", "dev")
os.environ.setdefault("ORACLE_SECRET_KEY", "test-only-secret-key-with-at-least-32-bytes")

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)
