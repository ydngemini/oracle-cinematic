// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

const store = vi.hoisted(() => ({
  state: { transcriptLog: [], negotiationTelemetry: null, aiChatConnection: 'online' },
  dispatch: vi.fn(),
  ws: { current: null },
}));
vi.mock('../state', () => ({
  ACTIONS: { APPEND_TRANSCRIPT: 'APPEND_TRANSCRIPT', NEGOTIATION_TELEMETRY: 'NEGOTIATION_TELEMETRY' },
  useOracleState: () => store.state,
  useOracleDispatch: () => ({ dispatch: store.dispatch, wsRef: store.ws }),
}));
vi.mock('../state/useCrmApi', () => ({ crmGet: vi.fn(() => Promise.resolve({ events: [] })) }));
const { LiveTranscript } = await import('./LiveTranscript');

afterEach(() => { cleanup(); store.dispatch.mockReset(); });

function typeNote(text) {
  fireEvent.change(screen.getByLabelText('Private note to Neoh'), { target: { value: text } });
  fireEvent.click(screen.getByRole('button', { name: 'Send' }));
}

describe('LiveTranscript', () => {
  it('speaks product language — no AI Closer, ORCL link codes, or caps verdicts', () => {
    store.state = {
      transcriptLog: [{ id: '1', agent: 'CLIENT', text: 'We need 250.' }],
      negotiationTelemetry: { threshold: 'red', mao: 231000 },
      aiChatConnection: 'online',
    };
    const { container } = render(<LiveTranscript />);
    expect(container.textContent).not.toMatch(/AI Closer|ORCL|VOICE_LINK|OVER MAO|JARVIS/);
    expect(screen.getByText('Client')).toBeTruthy();
    expect(screen.getByText('Offer is over your maximum')).toBeTruthy();
    expect(screen.getByText('Live call updates connected')).toBeTruthy();
    // The call log never announces every line to a screen reader.
    expect(screen.getByRole('log').getAttribute('aria-live')).toBe('off');
  });

  it('does not log a note as sent when the live connection is down', () => {
    store.state = { transcriptLog: [], negotiationTelemetry: null, aiChatConnection: 'offline' };
    store.ws.current = null;
    render(<LiveTranscript />);
    typeNote('Ask about the roof');
    expect(store.dispatch).not.toHaveBeenCalled();
    expect(screen.getByRole('alert').textContent).toMatch(/Couldn't send your note/);
  });

  it('sends and logs the note when connected', () => {
    store.state = { transcriptLog: [], negotiationTelemetry: null, aiChatConnection: 'online' };
    const send = vi.fn();
    store.ws.current = { readyState: 1, send };
    globalThis.WebSocket = globalThis.WebSocket || { OPEN: 1 };
    render(<LiveTranscript />);
    typeNote('Ask about the roof');
    expect(send).toHaveBeenCalledTimes(1);
    expect(store.dispatch).toHaveBeenCalledWith(expect.objectContaining({ type: 'APPEND_TRANSCRIPT' }));
  });
});
