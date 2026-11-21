// @vitest-environment jsdom
import { cleanup, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const ctx = vi.hoisted(() => ({ dispatch: vi.fn(), wsRef: { current: null } }));
vi.mock('./OracleContext', () => ({
  ACTIONS: { FEED_EVENT: 'FEED_EVENT', AI_CHAT_CONNECTION: 'AI_CHAT_CONNECTION' },
  useOracleDispatch: () => ctx,
}));

const sockets = [];
class FakeWebSocket {
  static OPEN = 1;
  static CONNECTING = 0;
  constructor(url) { this.url = url; this.readyState = 0; sockets.push(this); }
  send() {}
  close() {}
}

const { useOracleWebSocket } = await import('./useOracleWebSocket');

beforeEach(() => {
  vi.useFakeTimers();
  vi.stubGlobal('WebSocket', FakeWebSocket);
  sockets.length = 0;
  ctx.dispatch.mockReset();
  ctx.wsRef.current = null;
});
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); });

describe('useOracleWebSocket — FIREHOSE_METADATA', () => {
  it('turns a mirrored call frame into a metadata-only Live Feed entry', () => {
    renderHook(() => useOracleWebSocket());
    vi.advanceTimersByTime(150);
    expect(sockets).toHaveLength(1);
    sockets[0].onmessage({ data: JSON.stringify({
      type: 'FIREHOSE_METADATA', version: 1, frame_type: 'NEGOTIATION_TELEMETRY',
      source_tenant: '11111111-2222-4333-8444-555555555555', content_withheld: true,
      mirrored_at: '2026-11-21T10:00:00+00:00', consent_recorded: true,
      // A content key a frame should never carry: still not copied.
      transcript: { text: 'Pat Owner: 251357' },
    }) });
    const feeds = ctx.dispatch.mock.calls.map(([a]) => a).filter((a) => a.type === 'FEED_EVENT');
    expect(feeds).toHaveLength(1);
    const { id, ...entry } = feeds[0].payload;
    expect(typeof id).toBe('string');
    expect(entry).toEqual({
      kind: 'firehose_metadata', frameType: 'NEGOTIATION_TELEMETRY',
      tenant: '11111111-2222-4333-8444-555555555555', flags: ['consent recorded'],
      timestamp: Date.parse('2026-11-21T10:00:00+00:00'),
    });
  });
});
