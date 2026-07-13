import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from reconstruction_providers import (
    MAX_CAPTURE_IMAGES,
    ProviderError,
    RunPodProvider,
    UnavailableProvider,
    _validate_remote_output_url,
    _validate_remote_service_url,
    get_provider,
)


def _runpod_env(monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "test-api-key")
    monkeypatch.setenv("RUNPOD_ENDPOINT_ID", "endpoint_123")
    monkeypatch.setenv("RECON_S3_BUCKET", "recon-bucket")
    monkeypatch.setenv("RECON_RUNPOD_TIMEOUT", "60")


def test_unknown_provider_fails_closed(monkeypatch):
    monkeypatch.setenv("RECONSTRUCTION_PROVIDER", "typo-provider")

    provider = get_provider()

    assert isinstance(provider, UnavailableProvider)
    assert provider.available() == (
        False,
        "unknown RECONSTRUCTION_PROVIDER 'typo-provider'",
    )


def test_runpod_availability_validates_endpoint_and_timeout(monkeypatch):
    _runpod_env(monkeypatch)
    provider = RunPodProvider()
    assert provider.available() == (True, "")

    monkeypatch.setenv("RUNPOD_ENDPOINT_ID", "https://not-an-id.example")
    assert provider.available() == (False, "RUNPOD_ENDPOINT_ID has an invalid format")

    monkeypatch.setenv("RUNPOD_ENDPOINT_ID", "endpoint_123")
    monkeypatch.setenv("RECON_RUNPOD_TIMEOUT", "999999")
    assert provider.available()[0] is False


def test_runpod_rejects_unbounded_capture(monkeypatch, tmp_path):
    _runpod_env(monkeypatch)
    provider = RunPodProvider()

    with pytest.raises(ProviderError, match="at least 8"):
        asyncio.run(provider.reconstruct([], tmp_path))

    images = [tmp_path / f"{i}.jpg" for i in range(MAX_CAPTURE_IMAGES + 1)]
    with pytest.raises(ProviderError, match="image limit"):
        asyncio.run(provider.reconstruct(images, tmp_path))


def test_remote_output_url_never_trusts_an_arbitrary_host(monkeypatch):
    service_url = "https://gpu.example.test/jobs"
    assert _validate_remote_output_url(
        "https://gpu.example.test/output/model.splat", service_url
    ).endswith("model.splat")
    assert _validate_remote_output_url(
        "https://bucket.s3.us-east-2.amazonaws.com/model.splat", service_url
    ).endswith("model.splat")

    with pytest.raises(ProviderError, match="untrusted"):
        _validate_remote_output_url(
            "https://attacker.example/output/model.splat", service_url
        )

    monkeypatch.setenv("RECON_REMOTE_OUTPUT_HOSTS", "cdn.vendor.example")
    assert _validate_remote_output_url(
        "https://cdn.vendor.example/model.splat", service_url
    ).endswith("model.splat")


def test_remote_service_url_requires_encrypted_transport():
    assert _validate_remote_service_url("https://gpu.example.test/jobs") == (
        "https://gpu.example.test/jobs"
    )
    assert _validate_remote_service_url("http://127.0.0.1:8080/jobs") == (
        "http://127.0.0.1:8080/jobs"
    )
    with pytest.raises(ProviderError, match="HTTPS"):
        _validate_remote_service_url("http://gpu.example.test/jobs")


class _FakeResponse:
    def __init__(self, payload=None, error=None):
        self.payload = payload or {}
        self.error = error

    def raise_for_status(self):
        if self.error:
            raise self.error

    def json(self):
        return self.payload


class _FakeS3:
    def __init__(self):
        self.upload_keys = []

    def upload_file(self, filename, bucket, key):
        self.upload_keys.append(key)

    def generate_presigned_url(self, operation, Params, ExpiresIn):
        return f"https://recon-bucket.s3.us-east-1.amazonaws.com/{Params['Key']}?signed=1"

    def download_file(self, bucket, key, filename):
        Path(filename).write_bytes(b"\0" * 32)


class _FakeRequests:
    class RequestException(Exception):
        pass

    def __init__(self, *, poll_error=None):
        self.poll_error = poll_error
        self.cancelled = []

    def post(self, url, **kwargs):
        if "/cancel/" in url:
            self.cancelled.append(url)
            return _FakeResponse({"status": "CANCELLED"})
        return _FakeResponse({"id": "job_123"})

    def get(self, url, **kwargs):
        if self.poll_error:
            return _FakeResponse(error=self.poll_error)
        return _FakeResponse({"status": "COMPLETED", "output": {}})


def _install_provider_fakes(monkeypatch, fake_requests):
    fake_s3 = _FakeS3()
    fake_boto3 = SimpleNamespace(client=lambda service, region_name=None: fake_s3)
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)
    monkeypatch.setitem(sys.modules, "requests", fake_requests)
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    return fake_s3


def test_runpod_uses_unique_staging_keys_and_validates_splat(monkeypatch, tmp_path):
    _runpod_env(monkeypatch)
    fake_requests = _FakeRequests()
    fake_s3 = _install_provider_fakes(monkeypatch, fake_requests)
    images = []
    for index in range(8):
        folder = tmp_path / str(index)
        folder.mkdir()
        image = folder / "same-name.jpg"
        image.write_bytes(b"image")
        images.append(image)

    output = RunPodProvider()._run_blocking(images, tmp_path)

    assert output.read_bytes() == b"\0" * 32
    assert len(fake_s3.upload_keys) == len(set(fake_s3.upload_keys)) == 8
    assert fake_requests.cancelled == []


def test_runpod_cancels_job_when_polling_fails(monkeypatch, tmp_path):
    _runpod_env(monkeypatch)
    fake_requests = _FakeRequests()
    fake_requests.poll_error = fake_requests.RequestException("status unavailable")
    _install_provider_fakes(monkeypatch, fake_requests)
    images = []
    for index in range(8):
        image = tmp_path / f"{index}.jpg"
        image.write_bytes(b"image")
        images.append(image)

    with pytest.raises(ProviderError, match="RunPod API request failed"):
        RunPodProvider()._run_blocking(images, tmp_path)

    assert len(fake_requests.cancelled) == 1
