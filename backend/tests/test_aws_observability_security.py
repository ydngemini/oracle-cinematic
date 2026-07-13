import asyncio
import json

import pytest
from botocore.exceptions import ClientError
from fastapi import HTTPException
from fastapi import WebSocketDisconnect
from pydantic import ValidationError

import aws_observability as obs
from tenancy import Role, TenantContext


ADMIN = TenantContext(
    agent_id="platform-admin",
    tenant_id="11111111-1111-1111-1111-111111111111",
    role=Role.PLATFORM_ADMIN,
)
AGENT = TenantContext(
    agent_id="agent",
    tenant_id="11111111-1111-1111-1111-111111111111",
    role=Role.AGENT,
)


def test_scale_request_caps_desired_capacity():
    assert obs.ScaleRequest(desired_capacity=0).desired_capacity == 0
    with pytest.raises(ValidationError):
        obs.ScaleRequest(desired_capacity=obs._MAX_ASG_DESIRED_CAPACITY + 1)


def test_scale_operation_requires_admin_and_valid_group_name():
    request = obs.ScaleRequest(desired_capacity=1)

    with pytest.raises(HTTPException) as forbidden:
        asyncio.run(obs.api_scale_asg("valid-group", request, ctx=AGENT))
    assert forbidden.value.status_code == 403

    with pytest.raises(HTTPException) as invalid:
        asyncio.run(obs.api_scale_asg("invalid/group", request, ctx=ADMIN))
    assert invalid.value.status_code == 422


def test_aws_operation_error_is_not_exposed(monkeypatch):
    class FailingAsg:
        def set_desired_capacity(self, **_kwargs):
            raise ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "internal role details"}},
                "SetDesiredCapacity",
            )

    class InlineLoop:
        async def run_in_executor(self, _executor, callback):
            return callback()

    monkeypatch.setattr(obs, "_get_autoscaling_client", lambda: FailingAsg())
    monkeypatch.setattr(obs.asyncio, "get_event_loop", lambda: InlineLoop())
    response = asyncio.run(
        obs.api_scale_asg(
            "valid-group",
            obs.ScaleRequest(desired_capacity=1),
            ctx=ADMIN,
        )
    )

    assert response.status_code == 502
    assert json.loads(response.body) == {"error": "AWS scaling operation failed"}


def test_copilot_rejects_oversized_context_before_model_call():
    obs._copilot_timestamps.clear()
    body = obs.CopilotRequest(message="status", context={"data": "x" * 40000})

    response = asyncio.run(obs.api_copilot_query(body, ctx=ADMIN))

    assert response.status_code == 413
    assert json.loads(response.body) == {"error": "context too large"}


def test_rds_memory_percentage_is_bounded_and_honest():
    gib = 1024 ** 3
    assert obs._rds_memory_pct_used("db.t3.micro", 0.5 * gib) == 50.0
    assert obs._rds_memory_pct_used("db.unknown", 0.5 * gib) is None
    assert obs._rds_memory_pct_used("db.t3.micro", 2 * gib) == 0.0


class FakeWebSocket:
    def __init__(self, messages):
        self.headers = {
            "sec-websocket-protocol": "oracle.jwt, test.jwt.token",
        }
        self.messages = list(messages)
        self.sent = []
        self.accepted_protocol = None
        self.closed_code = None

    async def accept(self, *, subprotocol):
        self.accepted_protocol = subprotocol

    async def send_text(self, value):
        self.sent.append(json.loads(value))

    async def receive_text(self):
        if not self.messages:
            raise WebSocketDisconnect()
        return self.messages.pop(0)

    async def close(self, *, code):
        self.closed_code = code


def test_websocket_throttles_repeated_snapshot_requests(monkeypatch):
    calls = []

    async def snapshot():
        calls.append(True)
        return {"timestamp": "now"}

    monkeypatch.setattr(
        obs,
        "decode_token",
        lambda _token: {
            "tenant_id": ADMIN.tenant_id,
            "role": "platform_admin",
        },
    )
    monkeypatch.setattr(obs, "get_full_infrastructure_snapshot", snapshot)
    monkeypatch.setattr(obs.time, "monotonic", lambda: 100.0)
    ws = FakeWebSocket(
        [
            json.dumps({"type": "REQUEST_SNAPSHOT"}),
            json.dumps({"type": "REQUEST_SNAPSHOT"}),
        ]
    )

    asyncio.run(obs.observability_websocket(ws))

    assert ws.accepted_protocol == "oracle.jwt"
    assert len(calls) == 1
    assert {message["type"] for message in ws.sent} >= {
        "AWS_CONNECTED",
        "AWS_INFRASTRUCTURE_SNAPSHOT",
        "AWS_RATE_LIMITED",
    }


def test_websocket_rejects_oversized_commands(monkeypatch):
    monkeypatch.setattr(
        obs,
        "decode_token",
        lambda _token: {
            "tenant_id": ADMIN.tenant_id,
            "role": "platform_admin",
        },
    )
    ws = FakeWebSocket(["x" * (obs._WS_MAX_MESSAGE_BYTES + 1)])

    asyncio.run(obs.observability_websocket(ws))

    assert ws.closed_code == 1009
