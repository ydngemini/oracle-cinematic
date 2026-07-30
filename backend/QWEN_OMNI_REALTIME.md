# Qwen Omni realtime call engine

The ACS call pipeline can route live phone audio through Alibaba Model Studio's
`qwen3.5-omni-flash-realtime` model.

## Call flow

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
acs_resource_id            = "/subscriptions/.../providers/Microsoft.Communication/CommunicationServices/..."
qwen_realtime_workspace_id = "your-workspace-id"
qwen_realtime_region       = "intl"
qwen_realtime_model        = "qwen3.5-omni-flash-realtime"
qwen_realtime_voice        = "Ethan"
```

The feature is disabled by default. Enabling it without
`DASHSCOPE_API_KEY`, `DASHSCOPE_WORKSPACE_ID`, or
`ORACLE_ACS_RESOURCE_ID` makes production startup fail closed.

For a custom Alibaba gateway, set `DASHSCOPE_REALTIME_URL` to the complete
WebSocket base URL. The model query parameter is added automatically.

## Operations

Relevant logs use `oracle.qwen_omni_realtime` and `oracle.acs_inbound`.
Do not log or expose the DashScope API key. The ACS media endpoint validates
the signed JWT in the WebSocket `Authorization` header against Microsoft's
OIDC signing keys, issuer, expiry, and the exact ACS resource ID audience.
Reusable query-string secrets are not accepted by the media endpoint.

Useful checks:

```bash
pytest -q tests/test_qwen_omni_realtime.py tests/test_acs_call_handler.py
terraform -chdir=infra/terraform validate
```
