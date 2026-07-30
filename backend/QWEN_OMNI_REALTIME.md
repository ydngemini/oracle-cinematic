# Qwen Omni realtime call engine

The ACS and Twilio call pipelines can route live phone audio through Alibaba
Model Studio's `qwen3.5-omni-flash-realtime` model.

## ACS call flow

1. ACS connects the inbound or outbound call.
2. ACS TextSource plays the mandatory AI/recording disclosure.
3. Only after PlayCompleted, ACS opens a bidirectional 16 kHz PCM WebSocket to
   `/api/commands/media/acs`. The backend validates the signed ACS bearer JWT
   before accepting the socket and binds its call-connection ID to live Redis
   state.
4. The backend forwards caller PCM to Qwen using
   `input_audio_buffer.append`.
5. Qwen returns 24 kHz PCM through `response.audio.delta`. The bridge resamples
   it to 16 kHz and immediately streams it back into the call.
6. Qwen semantic VAD handles turns and interruption. If the caller speaks over
   the model, queued ACS audio is stopped and the active Qwen response is
   cancelled.
7. If Qwen disconnects or errors, the handler stops media streaming and resumes
   the existing ACS speech-recognition, AI-chat, and TextSource fallback.

No call audio is written to disk by the bridge.

## Twilio Media Streams call flow

1. The approval worker checks outreach consent, suppression, calling hours, and
   frequency limits before using Twilio's Calls API.
2. The returned Call SID is bound to tenant-scoped Redis state. If that state
   cannot be written, the worker terminates the unmanaged call.
3. Twilio requests signed TwiML from `/api/commands/webhooks/twilio`. The
   response plays the mandatory AI/recording disclosure before opening
   `<Connect><Stream>` to `/api/commands/media/twilio`.
4. The WebSocket endpoint validates Twilio's `X-Twilio-Signature`, then binds
   the `start` frame's Account SID and Call SID to live Redis state and a
   five-minute HMAC token carried in `customParameters`.
5. Caller G.711 mu-law audio is decoded and resampled from 8 kHz to Qwen's
   16 kHz PCM input. Qwen's 24 kHz PCM output is resampled and encoded back to
   Twilio's 8 kHz mu-law format.
6. Caller interruption sends Twilio a `clear` frame and cancels the active Qwen
   response. Completed responses send a `mark` frame.
7. Signed status callbacks update the live-call record and remove Redis state
   after the call reaches a terminal status.

Twilio must own a voice-capable `TWILIO_FROM_NUMBER`. A stale, externally owned,
or merely formatted caller ID is not enough for the Calls API.

## Alibaba configuration

Create an Alibaba Model Studio workspace in either Singapore (`intl`) or
Beijing (`cn`). The API key must belong to the same region as the workspace.

Add this key to the existing AWS Secrets Manager JSON:

```json
{
  "DASHSCOPE_API_KEY": "sk-..."
}
```

Then configure Terraform:

```hcl
qwen_realtime_enabled      = true
twilio_qwen_realtime_enabled = true
acs_resource_id            = "/subscriptions/.../providers/Microsoft.Communication/CommunicationServices/..."
qwen_realtime_workspace_id = "your-workspace-id"
qwen_realtime_region       = "intl"
qwen_realtime_model        = "qwen3.5-omni-flash-realtime"
qwen_realtime_voice        = "Ethan"
```

Both provider bridges are disabled by default. Enabling either without
`DASHSCOPE_API_KEY`, `DASHSCOPE_WORKSPACE_ID`, or
`DASHSCOPE_REALTIME_URL` makes production startup fail closed. The ACS bridge
also requires `ORACLE_ACS_RESOURCE_ID`; the Twilio bridge requires
`TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`, and
`ORACLE_PUBLIC_BASE_URL`.

For a custom Alibaba gateway, set `DASHSCOPE_REALTIME_URL` to the complete
WebSocket base URL. The model query parameter is added automatically.

## Operations

Relevant logs use `oracle.qwen_omni_realtime`, `oracle.acs_inbound`, and
`oracle.twilio_realtime`.
Do not log or expose the DashScope API key. The ACS media endpoint validates
the signed JWT in the WebSocket `Authorization` header against Microsoft's
OIDC signing keys, issuer, expiry, and the exact ACS resource ID audience.
Reusable query-string secrets are not accepted by the media endpoint.
The Twilio media endpoint likewise rejects reusable URL secrets and accepts
only a valid Twilio signature plus a live call-bound token.

Useful checks:

```bash
pytest -q tests/test_qwen_omni_realtime.py tests/test_twilio_qwen_realtime.py tests/test_acs_call_handler.py
terraform -chdir=infra/terraform validate
```
