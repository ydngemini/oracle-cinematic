"""The backend must boot with no AWS environment and no AWS SDK loaded.

This is the deployment guarantee for the Azure move: every remaining AWS
integration is opt-in behind a variable that defaults to its Azure counterpart,
and every AWS import is lazy. If someone adds a module-scope `import boto3` (as
aws_observability, ml_forge.bedrock_client and contract_vault all had), this
fails and says so before it reaches a container.

Runs in a subprocess because the assertion is about what a *fresh* interpreter
loads — by the time this test executes, the parent has already imported half the
application.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent

# Anything that would let boto3 find real credentials or a region.
_AWS_ENV = (
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_PROFILE",
    "BEDROCK_REGION",
)


def _boot(**extra_env) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _AWS_ENV}
    env["PYTHONPATH"] = str(BACKEND_DIR)
    env.update(extra_env)

    script = textwrap.dedent(
        """
        import json, sys
        import server
        print("RESULT" + json.dumps({
            "routes": len(server.app.routes),
            "boto3": "boto3" in sys.modules,
            "botocore": "botocore" in sys.modules,
            "google": any(
                m == "google" or m.startswith(("google.", "googleapiclient"))
                for m in sys.modules
            ),
            "onelogin": "onelogin" in sys.modules,
            "aws_routes": sum(
                1 for r in server.app.routes
                if getattr(r, "path", "").startswith("/api/aws")
            ),
        }))
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if proc.returncode != 0:
        pytest.fail(f"backend failed to import without AWS env:\n{proc.stderr[-3000:]}")
    line = next(l for l in proc.stdout.splitlines() if l.startswith("RESULT"))
    import json

    return json.loads(line[len("RESULT"):])


def test_boots_without_any_aws_environment():
    result = _boot()

    assert result["routes"] > 300, "the app mounted almost nothing — check the import"


def test_no_aws_sdk_is_loaded_at_startup():
    """A module-scope AWS import costs every deployment the SDK load, to serve
    code paths an Azure deployment never reaches."""
    result = _boot()

    assert result["boto3"] is False, "something imports boto3 at module scope"
    assert result["botocore"] is False, "something imports botocore at module scope"


def test_no_google_or_saml_libraries_are_loaded_at_startup():
    """Identity and email are deliberately free of third-party dependencies.

    Login is `auth.py` against Neoh's own database and mail goes out over plain
    SMTP, so no Google client and no SAML toolkit should exist on the startup
    path. The only sanctioned Google use left is the calendar provider, whose
    imports are lazy."""
    result = _boot()

    assert result["google"] is False, "something imports a Google client at module scope"
    assert result["onelogin"] is False, "something imports the SAML toolkit at module scope"


def test_aws_observability_is_not_mounted_by_default():
    """Its routes all require AWS credentials, so mounting them on Azure would
    advertise endpoints that can only fail."""
    assert _boot()["aws_routes"] == 0


def test_aws_observability_still_mounts_when_explicitly_enabled():
    result = _boot(AWS_OBSERVABILITY_ENABLED="1", AWS_REGION="us-east-1")

    assert result["aws_routes"] > 0
    # Opting in is exactly when paying for the SDK is correct.
    assert result["boto3"] is True
