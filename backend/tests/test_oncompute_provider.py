"""OnComputeProvider — the Ocean-node C2D reconstruction path.

What these tests pin is the *transport contract*, because it is deliberately
different from RunPod's and the differences are all things the node forced:

- inputs go through the node's persistent storage, not presigned URLs — C2D
  containers run with networking disabled, so nothing inside the job can fetch;
- the compute env id is resolved at submit time — env ids derive from node
  state and rotate on restart, so a pinned id goes stale silently;
- the result comes back through getComputeResult and may be either a bare file
  or a tar archive, and the provider must handle both without guessing from
  Content-Type headers the node does not reliably send.
"""

from __future__ import annotations

import asyncio
import io
import json
import struct
import tarfile
from pathlib import Path

import pytest

import reconstruction_providers as rp


def _splat_bytes(rows: int = 4) -> bytes:
    row = struct.pack("<3f3f", 0, 0, 0, 0.1, 0.1, 0.1) + bytes((200, 200, 200, 255, 255, 128, 128, 128))
    assert len(row) == 32
    return row * rows


def _images(tmp_path: Path, count: int = rp.MIN_CAPTURE_IMAGES) -> list[Path]:
    out = []
    for i in range(count):
        p = tmp_path / f"img_{i}.jpg"
        p.write_bytes(b"\xff\xd8\xff" + bytes(64))
        out.append(p)
    return out


class _Response:
    def __init__(self, *, status=200, body=None, content=None):
        self.status_code = status
        self._body = body
        self.content = content if content is not None else json.dumps(body).encode()

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"status {self.status_code}")


class _Session:
    """Scripted node. Records every request for the contract assertions."""

    def __init__(self, *, result_content: bytes, statuses=None):
        self.requests: list[tuple[str, str, dict]] = []
        self.uploads: list[str] = []
        self._statuses = list(statuses or [{"status": 70, "statusText": "Job finished"}])
        self._result_content = result_content

    def get(self, url, headers=None, params=None, timeout=None):
        self.requests.append(("GET", url, {"params": params or {}, "headers": headers or {}}))
        if url.endswith("/api/services/nonce"):
            self._nonce = getattr(self, "_nonce", 0) + 1
            return _Response(body={"nonce": self._nonce - 1})
        if url.endswith("/computeEnvironments"):
            return _Response(body=[
                {   # access-restricted env — must be skipped
                    "id": "env-restricted",
                    "free": {"access": {"addresses": ["0xsomeone"]}},
                },
                {   # open env — must be chosen
                    "id": "env-open",
                    "free": {"access": {"addresses": []}},
                },
            ])
        if url.endswith("/api/services/compute"):
            row = self._statuses.pop(0) if len(self._statuses) > 1 else self._statuses[0]
            return _Response(body=[row])
        if url.endswith("/computeResult"):
            return _Response(content=self._result_content, body=None)
        raise AssertionError(f"unexpected GET {url}")

    def post(self, url, headers=None, json=None, data=None, params=None, timeout=None):
        self.requests.append(
            ("POST", url, {"json": json, "headers": headers or {}, "params": params or {}})
        )
        if url.endswith("/persistentStorage/buckets"):
            return _Response(body={"bucketId": "bucket-1"})
        if "/persistentStorage/buckets/bucket-1/files/" in url:
            self.uploads.append(url.rsplit("/", 1)[1])
            return _Response(body={"name": url.rsplit("/", 1)[1], "size": len(data or b"")})
        if url.endswith("/freeCompute"):
            return _Response(body=[{"jobId": "job-42", "status": 0, "statusText": "Job started"}])
        raise AssertionError(f"unexpected POST {url}")


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("ONCOMPUTE_NODE_URL", "https://node.test")
    monkeypatch.setenv("ONCOMPUTE_AUTH_TOKEN", "jwt-token")
    monkeypatch.delenv("ONCOMPUTE_ENV_ID", raising=False)
    # Both credential vars must be absent so this fixture reliably means
    # "JWT mode" regardless of what's ambient in the real process
    # environment — Oracle/.env sets ONCOMPUTE_PRIVATE_KEY_FILE for the
    # operator-key path, and without clearing it here that leaks into every
    # test using this fixture and trips the provider's own "not both" guard.
    monkeypatch.delenv("ONCOMPUTE_PRIVATE_KEY_FILE", raising=False)
    monkeypatch.delenv("ONCOMPUTE_PRIVATE_KEY", raising=False)
    monkeypatch.setattr(rp, "MIN_RUNPOD_TIMEOUT", 1)


def _run(monkeypatch, session, tmp_path):
    provider = rp.OnComputeProvider()
    monkeypatch.setattr("requests.Session", lambda: session)
    monkeypatch.setattr("time.sleep", lambda _s: None)
    return asyncio.run(provider.reconstruct(_images(tmp_path), tmp_path))


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------

def test_unconfigured_reports_what_to_set(monkeypatch):
    monkeypatch.delenv("ONCOMPUTE_NODE_URL", raising=False)
    monkeypatch.delenv("ONCOMPUTE_AUTH_TOKEN", raising=False)
    ready, reason = rp.OnComputeProvider().available()
    assert not ready
    assert "ONCOMPUTE_NODE_URL" in reason
    # Both credential modes must be named — an operator reading this decides
    # between a dashboard token and a key file from the message alone.
    assert "ONCOMPUTE_AUTH_TOKEN" in reason and "ONCOMPUTE_PRIVATE_KEY_FILE" in reason


def test_a_non_http_node_url_is_refused(monkeypatch, configured):
    monkeypatch.setenv("ONCOMPUTE_NODE_URL", "ftp://node.test")
    ready, reason = rp.OnComputeProvider().available()
    assert not ready and "http" in reason


def test_registered_and_produces_captured():
    assert rp._PROVIDERS["oncompute"] is rp.OnComputeProvider
    assert rp.OnComputeProvider.produces == "captured", (
        "OnCompute runs the real COLMAP→splatfacto pipeline; its output is a "
        "capture, and the tier resolver may promote it"
    )


# ---------------------------------------------------------------------------
# The happy path, and the transport contract inside it
# ---------------------------------------------------------------------------

def test_full_flow_uploads_stages_and_downloads(monkeypatch, configured, tmp_path):
    session = _Session(result_content=_splat_bytes())
    out = _run(monkeypatch, session, tmp_path)

    assert out.name == "model.splat"
    assert out.stat().st_size % 32 == 0

    # Every image was staged into node persistent storage, none skipped.
    assert len(session.uploads) == rp.MIN_CAPTURE_IMAGES

    submit = next(r for r in session.requests if r[1].endswith("/freeCompute"))
    body = submit[2]["json"]
    # The rotating-env rule: resolved at submit, restricted env skipped.
    assert body["environment"] == "env-open"
    # Networking is disabled in the job: every dataset must be a
    # nodePersistentStorage mount, never a URL the container would fetch.
    kinds = {d["fileObject"]["type"] for d in body["datasets"]}
    assert kinds == {"nodePersistentStorage"}
    assert len(body["datasets"]) == rp.MIN_CAPTURE_IMAGES
    # The driver runs the image's own pipeline.sh against the mounts.
    raw = body["algorithm"]["meta"]["rawcode"]
    assert "/data/persistentStorage" in raw
    assert "pipeline.sh" in raw
    assert body["algorithm"]["meta"]["container"]["image"] == "ghcr.io/ydngemini/neoh-recon-runpod"


def test_the_pinned_env_id_wins_when_set(monkeypatch, configured, tmp_path):
    monkeypatch.setenv("ONCOMPUTE_ENV_ID", "env-pinned")
    session = _Session(result_content=_splat_bytes())
    _run(monkeypatch, session, tmp_path)

    submit = next(r for r in session.requests if r[1].endswith("/freeCompute"))
    assert submit[2]["json"]["environment"] == "env-pinned"
    assert not any(r[1].endswith("/computeEnvironments") for r in session.requests), (
        "a pinned env must not trigger discovery"
    )


def test_a_tar_result_is_unpacked_to_the_splat(monkeypatch, configured, tmp_path):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        payload = _splat_bytes(8)
        info = tarfile.TarInfo("outputs/model.splat")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
        junk = tarfile.TarInfo("outputs/algorithm.log")
        junk.size = 3
        archive.addfile(junk, io.BytesIO(b"ok\n"))

    session = _Session(result_content=buffer.getvalue())
    out = _run(monkeypatch, session, tmp_path)
    assert out.read_bytes() == _splat_bytes(8)


def test_a_tar_without_a_splat_is_an_error_not_an_empty_file(monkeypatch, configured, tmp_path):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        info = tarfile.TarInfo("outputs/algorithm.log")
        info.size = 5
        archive.addfile(info, io.BytesIO(b"boom\n"))

    session = _Session(result_content=buffer.getvalue())
    with pytest.raises(rp.ProviderError, match="no model.splat"):
        _run(monkeypatch, session, tmp_path)


# ---------------------------------------------------------------------------
# Failure honesty
# ---------------------------------------------------------------------------

def test_a_failed_job_surfaces_the_node_status_text(monkeypatch, configured, tmp_path):
    session = _Session(
        result_content=b"",
        statuses=[{"status": 31, "statusText": "Algorithm failed"}],
    )
    with pytest.raises(rp.ProviderError, match="[Aa]lgorithm failed"):
        _run(monkeypatch, session, tmp_path)


def test_a_misaligned_artifact_is_refused(monkeypatch, configured, tmp_path):
    session = _Session(result_content=b"not-a-splat")
    with pytest.raises(rp.ProviderError, match="invalid .splat"):
        _run(monkeypatch, session, tmp_path)


def test_too_few_images_fail_before_any_network_io(monkeypatch, configured, tmp_path):
    provider = rp.OnComputeProvider()

    def _explode():
        raise AssertionError("no session may be created for an invalid capture")

    monkeypatch.setattr("requests.Session", _explode)
    lone = _images(tmp_path, count=1)
    with pytest.raises(rp.ProviderError):
        asyncio.run(provider.reconstruct(lone, tmp_path))


# ---------------------------------------------------------------------------
# Operator-key mode — the path that exists because token minting is broken
# ---------------------------------------------------------------------------

# A fixed throwaway key for tests. Never funded, never used off-tests.
TEST_KEY = "0x" + "7" * 64


@pytest.fixture
def key_configured(monkeypatch, tmp_path):
    key_file = tmp_path / "operator.key"
    key_file.write_text(TEST_KEY)
    monkeypatch.setenv("ONCOMPUTE_NODE_URL", "https://node.test")
    monkeypatch.setenv("ONCOMPUTE_PRIVATE_KEY_FILE", str(key_file))
    monkeypatch.delenv("ONCOMPUTE_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("ONCOMPUTE_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("ONCOMPUTE_ENV_ID", raising=False)
    monkeypatch.setattr(rp, "MIN_RUNPOD_TIMEOUT", 1)


def test_key_mode_is_available_without_a_token(key_configured):
    ready, reason = rp.OnComputeProvider().available()
    assert ready, reason


def test_both_credentials_at_once_is_refused(monkeypatch, key_configured):
    """Two modes mean two consumer identities; jobs and buckets belong to a
    consumer, so ambiguity here is a config bug and not a preference."""
    monkeypatch.setenv("ONCOMPUTE_AUTH_TOKEN", "jwt")
    ready, reason = rp.OnComputeProvider().available()
    assert not ready and "not both" in reason


def test_signed_flow_carries_credentials_on_every_authenticated_call(
    monkeypatch, key_configured, tmp_path
):
    session = _Session(result_content=_splat_bytes())
    out = _run(monkeypatch, session, tmp_path)
    assert out.stat().st_size % 32 == 0

    # Bucket create carries auth in the BODY; uploads/status/result as params —
    # the same split the node's own HTTP routes implement.
    create = next(r for r in session.requests if r[1].endswith("/persistentStorage/buckets"))
    assert {"consumerAddress", "nonce", "signature"} <= set(create[2]["json"])

    submit = next(r for r in session.requests if r[1].endswith("/freeCompute"))
    assert {"consumerAddress", "nonce", "signature"} <= set(submit[2]["json"])

    status = next(r for r in session.requests if r[1].endswith("/api/services/compute"))
    assert {"consumerAddress", "nonce", "signature"} <= set(status[2]["params"])

    result = next(r for r in session.requests if r[1].endswith("/computeResult"))
    assert {"consumerAddress", "nonce", "signature"} <= set(result[2]["params"])

    # No Authorization header in key mode — the signature IS the identity.
    assert "Authorization" not in submit[2]["headers"]


def test_the_signature_recovers_to_the_operator_over_the_node_message(
    monkeypatch, key_configured, tmp_path
):
    """The contract that makes or breaks this mode, proven by recovery.

    The node verifies personal_sign(keccak256(addr + nonce + command)) with the
    PROTOCOL_COMMANDS constant. If the message drifts — wrong command string,
    missing nonce increment — recovery yields a different address and the node
    answers 401. This asserts the exact bytes, not just that fields exist.
    """
    from eth_account import Account
    from eth_account.messages import encode_defunct
    from eth_utils import keccak

    session = _Session(result_content=_splat_bytes())
    _run(monkeypatch, session, tmp_path)

    expected = Account.from_key(TEST_KEY).address
    submit = next(r for r in session.requests if r[1].endswith("/freeCompute"))
    auth = submit[2]["json"]

    digest = keccak(text=f"{auth['consumerAddress']}{auth['nonce']}freeStartCompute")
    recovered = Account.recover_message(
        encode_defunct(primitive=digest), signature=auth["signature"]
    )
    assert recovered == expected == auth["consumerAddress"]


def test_each_signed_call_uses_a_fresh_incremented_nonce(
    monkeypatch, key_configured, tmp_path
):
    """Nonces are per-consumer and strictly increasing; a reused nonce is
    rejected by the node. Every signature must ask for the current nonce and
    sign current+1."""
    session = _Session(result_content=_splat_bytes())
    _run(monkeypatch, session, tmp_path)

    nonce_calls = [r for r in session.requests if r[1].endswith("/api/services/nonce")]
    signed_calls = []
    for method, url, extra in session.requests:
        payload = extra.get("json") if isinstance(extra.get("json"), dict) else None
        params = extra.get("params") or {}
        for source in (payload, params):
            if source and "nonce" in source:
                signed_calls.append(source["nonce"])
    assert len(nonce_calls) == len(signed_calls), (
        "every signature must fetch the live nonce; caching one gets the "
        "second call rejected"
    )
    assert signed_calls == sorted(signed_calls, key=int), "nonces must not go backwards"
    assert len(set(signed_calls)) == len(signed_calls), "a nonce was reused"


def test_missing_eth_account_degrades_to_unavailable(monkeypatch, key_configured):
    """A deployment without the dependency must refuse capture honestly at
    available(), not explode mid-reconstruction."""
    import builtins

    real_import = builtins.__import__

    def _no_eth(name, *args, **kwargs):
        if name.startswith("eth_account"):
            raise ImportError("No module named 'eth_account'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_eth)
    ready, reason = rp.OnComputeProvider().available()
    assert not ready
    assert "eth-account" in reason
