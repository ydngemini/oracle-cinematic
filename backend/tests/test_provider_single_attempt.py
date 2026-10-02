"""Side-effecting provider requests are sent ONCE per attempt — no hidden SDK
re-POSTs that the command ledger cannot see (resilience 2026-10-02)."""

from __future__ import annotations

import pytest


class _Resp:
    status_code = 500
    url = "https://api.plivo.com/v1/Account/x/Call/"
    content = b'{"error":"server"}'
    text = '{"error":"server"}'
    headers = {"Content-Type": "application/json"}

    def json(self):
        return {"error": "server"}


def test_plivo_call_create_is_not_reposted_to_fallback_hosts():
    import plivo

    from voice_provider import _single_attempt

    client = _single_attempt(plivo.RestClient("MAXXXXXXXXXXXXXXXXXX", "token"))
    sent = []

    def fake_send(req, **kw):
        sent.append(req.url)
        return _Resp()

    client.session.send = fake_send
    with pytest.raises(Exception):
        client.calls.create(from_="+13025550100", to_="+13025550101",
                            answer_url="https://example.test/a", answer_method="POST")
    assert len(sent) == 1, sent


def test_plivo_without_the_pin_would_repost():
    """Documents the SDK behaviour the pin defends against."""
    import plivo

    client = plivo.RestClient("MAXXXXXXXXXXXXXXXXXX", "token")
    sent = []
    client.session.send = lambda req, **kw: (sent.append(req.url), _Resp())[1]
    with pytest.raises(Exception):
        client.calls.create(from_="+13025550100", to_="+13025550101",
                            answer_url="https://example.test/a", answer_method="POST")
    assert len(sent) == 3


def test_telnyx_client_never_retries_on_its_own():
    from messaging_provider import TelnyxMessagingProvider

    client = TelnyxMessagingProvider._client({"api_key": "KEYtest"})
    assert client.max_retries == 0


def test_twilio_client_has_a_timeout():
    from command_providers import _twilio_client

    client = _twilio_client("AC" + "0" * 32, "token", "", "")
    assert client.http_client.timeout and client.http_client.timeout <= 30


@pytest.mark.parametrize("status,uncertain", [(500, True), (503, True), (429, True), (408, True),
                                               (400, False), (404, False), (422, False)])
def test_twilio_status_classification(status, uncertain):
    from command_providers import ProviderRejectedError, ProviderRequestError, twilio_failure

    class Exc(Exception):
        code = 21211

    e = Exc()
    e.status = status
    out = twilio_failure(e, "SMS request")
    assert isinstance(out, ProviderRequestError if uncertain else ProviderRejectedError)
