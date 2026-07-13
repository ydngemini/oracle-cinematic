import importlib.util
from pathlib import Path

import pytest
import requests


_HANDLER_PATH = (
    Path(__file__).resolve().parents[2]
    / "infra"
    / "reconstruction-runpod"
    / "handler.py"
)
_SPEC = importlib.util.spec_from_file_location("runpod_worker_handler", _HANDLER_PATH)
assert _SPEC and _SPEC.loader
handler_module = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(handler_module)


def _signed_url(host="bucket.s3.us-east-1.amazonaws.com", path="object"):
    return (
        f"https://{host}/{path}"
        "?X-Amz-Algorithm=AWS4-HMAC-SHA256"
        "&X-Amz-Credential=test"
        "&X-Amz-Expires=3600"
        "&X-Amz-Signature=abc123"
    )


def test_presigned_url_validation_is_s3_only():
    url = _signed_url()
    assert handler_module._validate_presigned_s3_url(url, "image") == url

    with pytest.raises(ValueError, match="target S3"):
        handler_module._validate_presigned_s3_url(
            _signed_url("metadata.example.com"), "image"
        )

    with pytest.raises(ValueError, match="Signature V4"):
        handler_module._validate_presigned_s3_url(
            "https://bucket.s3.us-east-1.amazonaws.com/object", "image"
        )


def test_iterations_are_bounded():
    assert handler_module._iterations("7000") == 7000
    with pytest.raises(ValueError, match="between"):
        handler_module._iterations(999)
    with pytest.raises(ValueError, match="between"):
        handler_module._iterations(30001)


def test_selftest_can_return_small_inline_splat(monkeypatch):
    monkeypatch.setattr(handler_module, "_progress", lambda _job, _message: None)

    result = handler_module.handler(
        {"input": {"selftest": True, "return_splat_b64": True}}
    )

    assert result["selftest"] is True
    assert result["bytes"] > 0
    assert result["gaussians"] == result["bytes"] // 32
    assert result["splat_b64"]


def test_worker_rejects_direct_s3_and_inline_production_output():
    direct = handler_module.handler(
        {
            "input": {
                "input_s3": "s3://bucket/input",
                "output_s3": "s3://bucket/output",
            }
        }
    )
    assert direct == {"error": "direct S3 mode is disabled; use presigned URLs"}

    inline = handler_module.handler(
        {"input": {"image_urls": [_signed_url()], "return_splat_b64": True}}
    )
    assert inline == {"error": "return_splat_b64 is restricted to selftest jobs"}


def test_request_errors_do_not_leak_presigned_urls():
    error = requests.RequestException(
        "failed https://bucket.s3.amazonaws.com/key?X-Amz-Signature=secret"
    )
    assert handler_module._safe_error(error) == "object transfer failed"
