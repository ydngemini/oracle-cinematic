// @vitest-environment jsdom
/**
 * The Neoh tab is the conversation — and its composer is never taken away.
 *
 * Pins the Mission 3 contract: the tab renders the thread and a persistent
 * [field][mic][send] composer (not the old AI hub); the composer stays usable
 * while Neoh thinks, while the microphone listens, after a mic error and
 * while the channel reconnects; the context chip says what Neoh is reading
 * and comes off in one tap; interim voice captions are visible but never a
 * live region; a finished reply is announced once; and staged outreach shows
 * the command's real, durable state.
 */

import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { crmGet, crmPost } from '../state/useCrmApi';
import { resetNeohChannelCache } from './useNeohChannel';
import { resetCommandReceiptCache } from './useCommandReceipts';
import { NeohConversation } from './NeohConversation';

vi.mock('../state/useCrmApi', () => ({ crmGet: vi.fn(), crmPost: vi.fn() }));
vi.mock('../state', () => ({
  ACTIONS: {
    AI_CHAT_HYDRATE: 'AI_CHAT_HYDRATE',
    AI_CHAT_SEND_LOCAL: 'AI_CHAT_SEND_LOCAL',
    AI_CHAT_ACTION_UNDONE: 'AI_CHAT_ACTION_UNDONE',
  },
  useOracleState: () => globalThis.__oracle.state,
  useOracleDispatch: () => globalThis.__oracle.dispatchValue,
}));
vi.mock('../components/AssistantContext', () => ({
  useAssistant: () => globalThis.__assistant,
}));
vi.mock('./motion', () => ({
  useMotionPolicy: () => ({ reduced: true, layout: false, transition: { duration: 0 } }),
}));

const ASSISTANT_ID = 'aaaaaaaa-1111-4111-8111-111111111111';
const CLIENT_ID = 'cccccccc-2222-4222-8222-222222222222';

class FakeRecognition {
  constructor() { FakeRecognition.last = this; }
  start() { this.onstart?.(); }
  stop() { this.onend?.(); }
  abort() {}
}

function setState(overrides = {}) {
  globalThis.__oracle.state = {
    aiChatMessages: [],
    aiChatRevision: 0,
    aiChatConnection: 'online',
    ...overrides,
  };
}

function renderTab(props = {}) {
  return render(<NeohConversation onNavigate={vi.fn()} onOpenEntity={vi.fn()} {...props} />);
}

beforeEach(() => {
  vi.clearAllMocks();
  resetNeohChannelCache();
  resetCommandReceiptCache();
  const socket = { readyState: 1, send: vi.fn() };
  globalThis.__oracle = {
    state: null,
    dispatchValue: { dispatch: vi.fn(), wsRef: { current: socket } },
    socket,
  };
  setState();
  globalThis.__assistant = {
    record: null,
    clearRecord: vi.fn(),
    registerRecord: vi.fn(),
    commandRequest: null,
    clearCommandRequest: vi.fn(),
    commandStatus: { state: 'idle' },
  };
  vi.stubGlobal('matchMedia', (query) => ({
    matches: false, media: query, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {},
  }));
  window.matchMedia = globalThis.matchMedia;
  crmGet.mockImplementation((path) => {
    if (path === '/api/ai/chat/status') return Promise.resolve({ enabled: true });
    if (path.startsWith('/api/ai/chat/messages')) return Promise.resolve({ messages: [] });
    if (path.startsWith('/api/search/recent')) {
      return Promise.resolve({ results: [{ kind: 'people', id: CLIENT_ID, label: 'Sarah Chen', href: `/p/${CLIENT_ID}` }] });
    }
    if (path.startsWith('/api/commands')) return Promise.resolve({ commands: [] });
    return Promise.resolve({});
  });
  crmPost.mockResolvedValue({ fallthrough: true });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  delete window.SpeechRecognition;
});

describe('NeohConversation', () => {
  it('renders the conversation with a persistent composer, not the old AI hub', async () => {
    renderTab();
    expect(await screen.findByRole('heading', { name: 'Neoh' })).toBeTruthy();
    expect(screen.getByRole('textbox', { name: 'Message Neoh' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Send' })).toBeTruthy();
    // The microphone is part of the composer's shape even where the browser
    // cannot do speech: disabled, and saying why.
    expect(screen.getByRole('button', { name: /Voice input isn't available/ })).toBeTruthy();
    expect(screen.queryByText('Our AI')).toBeNull();
    expect(screen.queryByText('Neoh tools')).toBeNull();
    expect(screen.queryByRole('tablist')).toBeNull();
  });

  it('offers a real person as the first thing to ask about, staged with visible context', async () => {
    renderTab();
    fireEvent.click(await screen.findByRole('button', { name: 'Ask about Sarah Chen' }));
    expect(globalThis.__assistant.registerRecord).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'client', id: CLIENT_ID, label: 'Sarah Chen' }), 'neoh-starter',
    );
    expect(screen.getByRole('textbox', { name: 'Message Neoh' }).value).toMatch(/Sarah Chen/);
    // Staged, never sent on its own.
    expect(globalThis.__oracle.socket.send).not.toHaveBeenCalled();
  });

  it('keeps the composer usable while thinking, listening, after a mic error and while reconnecting', async () => {
    window.SpeechRecognition = FakeRecognition;
    setState({
      aiChatConnection: 'offline',
      aiChatMessages: [
        { id: 'u1', request_id: 'r1', role: 'user', content: 'Who is hot?', status: 'completed' },
        { id: 'a1', request_id: 'r1', role: 'assistant', content: '', status: 'pending' },
      ],
    });
    renderTab();
    const field = await screen.findByRole('textbox', { name: 'Message Neoh' });

    // Thinking + disconnected: the field is still a field.
    expect(field.disabled).toBe(false);
    expect(screen.getByText('Neoh is reconnecting. Your work is saved.')).toBeTruthy();
    expect(screen.queryByText(/Channel offline/)).toBeNull();
    fireEvent.change(field, { target: { value: 'and the Smiths?' } });
    expect(screen.getByRole('button', { name: 'Send' }).disabled).toBe(false);

    // Listening: still typeable, the mic says it is listening.
    fireEvent.click(screen.getByRole('button', { name: 'Talk to Neoh' }));
    expect(screen.getByRole('button', { name: 'Stop listening' }).getAttribute('aria-pressed')).toBe('true');
    expect(screen.getByRole('textbox', { name: 'Message Neoh' }).disabled).toBe(false);

    // Mic error: reported in words, composer intact.
    act(() => { FakeRecognition.last.onerror?.({ error: 'no-speech' }); });
    expect(screen.getByText("Neoh didn't hear anything.")).toBeTruthy();
    expect(screen.getByRole('textbox', { name: 'Message Neoh' })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Send' })).toBeTruthy();
  });

  it('a spoken turn while Neoh is still answering lands in the field instead of interrupting', async () => {
    window.SpeechRecognition = FakeRecognition;
    setState({
      aiChatMessages: [{ id: 'a1', request_id: 'r1', role: 'assistant', content: '', status: 'streaming' }],
    });
    renderTab();
    fireEvent.click(await screen.findByRole('button', { name: 'Talk to Neoh' }));
    act(() => {
      FakeRecognition.last.onresult?.({
        resultIndex: 0,
        results: [Object.assign([{ transcript: 'text sarah' }], { isFinal: true })],
      });
    });
    expect(screen.getByRole('textbox', { name: 'Message Neoh' }).value).toBe('text sarah');
    expect(globalThis.__oracle.socket.send).not.toHaveBeenCalled();
  });

  it('shows what Neoh is reading as a small chip that comes off in one tap', async () => {
    globalThis.__assistant.record = { type: 'client', id: CLIENT_ID, label: 'Sarah Johnson' };
    renderTab();
    expect(await screen.findByText('Talking about Sarah Johnson')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Stop talking about Sarah Johnson' }));
    expect(globalThis.__assistant.clearRecord).toHaveBeenCalled();
  });

  it('keeps interim voice captions visible but out of every live region', async () => {
    window.SpeechRecognition = FakeRecognition;
    renderTab();
    fireEvent.click(await screen.findByRole('button', { name: 'Talk to Neoh' }));
    act(() => {
      FakeRecognition.last.onresult?.({
        resultIndex: 0,
        results: [Object.assign([{ transcript: 'call sar' }], { isFinal: false })],
      });
    });
    const caption = screen.getByText('call sar');
    expect(caption.closest('[aria-live]')).toBeNull();
    expect(caption.getAttribute('aria-hidden')).toBe('true');
    // The only live region on the page is the one-shot reply announcer.
    const live = document.querySelectorAll('[aria-live]');
    expect(live.length).toBe(1);
    expect(live[0].textContent).toBe('');
  });

  it('announces a finished reply once, and never the streaming text', async () => {
    setState({
      aiChatMessages: [{ id: ASSISTANT_ID, request_id: 'r1', role: 'assistant', content: 'Here is', status: 'streaming' }],
    });
    const view = renderTab();
    await screen.findByRole('heading', { name: 'Neoh' });
    const live = () => document.querySelector('[aria-live]');
    expect(live().textContent).toBe('');

    setState({
      aiChatRevision: 1,
      aiChatMessages: [{ id: ASSISTANT_ID, request_id: 'r1', role: 'assistant', content: 'Here is the summary.', status: 'completed' }],
    });
    view.rerender(<NeohConversation onNavigate={vi.fn()} onOpenEntity={vi.fn()} />);
    await waitFor(() => expect(live().textContent).toBe('Neoh: Here is the summary.'));
  });

  it('sends with Enter, with the record as the context the backend accepts', async () => {
    globalThis.__assistant.record = { type: 'property', id: CLIENT_ID, label: '12 Main St' };
    renderTab();
    const field = await screen.findByRole('textbox', { name: 'Message Neoh' });
    fireEvent.change(field, { target: { value: 'What is it worth?' } });
    fireEvent.keyDown(field, { key: 'Enter' });
    await waitFor(() => expect(globalThis.__oracle.socket.send).toHaveBeenCalled());
    const frame = JSON.parse(globalThis.__oracle.socket.send.mock.calls[0][0]);
    expect(frame.type).toBe('AI_CHAT_SEND');
    expect(frame.context).toEqual({ type: 'lead', id: CLIENT_ID });
    expect(field.value).toBe('');
  });

  it('Shift+Enter is a newline, not a send', async () => {
    renderTab();
    const field = await screen.findByRole('textbox', { name: 'Message Neoh' });
    fireEvent.change(field, { target: { value: 'line one' } });
    fireEvent.keyDown(field, { key: 'Enter', shiftKey: true });
    expect(crmPost).not.toHaveBeenCalled();
    expect(field.value).toBe('line one');
  });

  it('shows the durable state of outreach Neoh staged, joined to the turn that staged it', async () => {
    setState({
      aiChatMessages: [
        { id: 'u1', request_id: 'r1', role: 'user', content: 'Text Sarah', status: 'completed' },
        { id: ASSISTANT_ID, request_id: 'r1', role: 'assistant', content: 'Drafted and queued.', status: 'completed', actions: [] },
      ],
    });
    crmGet.mockImplementation((path) => {
      if (path === '/api/ai/chat/status') return Promise.resolve({ enabled: true });
      if (path.startsWith('/api/ai/chat/messages')) return Promise.resolve({ messages: [] });
      if (path.startsWith('/api/commands')) {
        return Promise.resolve({
          commands: [
            {
              id: 'cmd-1', command_type: 'SMS', state: 'reconciliation_required',
              target: { phone: '+15555550100' }, created_at: '2026-10-03T10:00:00Z',
              idempotency_key: `ai:${ASSISTANT_ID}:draft_sms:${CLIENT_ID}`,
            },
            {
              id: 'cmd-2', command_type: 'EMAIL', state: 'succeeded',
              target: { email: 's@example.test' }, created_at: '2026-10-03T10:01:00Z',
              idempotency_key: `ai:${ASSISTANT_ID}:draft_email:${CLIENT_ID}`,
            },
            // Not from this conversation: never attached.
            { id: 'cmd-3', command_type: 'CALL', state: 'succeeded', target: {}, idempotency_key: 'assistant:abc' },
          ],
        });
      }
      return Promise.resolve({});
    });
    const onNavigate = vi.fn();
    renderTab({ onNavigate });
    expect(await screen.findByText('Text needs review')).toBeTruthy();
    expect(screen.getByText(/couldn't confirm whether it went out/)).toBeTruthy();
    expect(screen.getByText('Email sent')).toBeTruthy();
    expect(screen.queryByText('Call placed')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Review' }));
    expect(onNavigate).toHaveBeenCalledWith('automations');
  });

  it('says Neoh is unavailable in product language when the conversation is switched off', async () => {
    crmGet.mockImplementation((path) => (
      path === '/api/ai/chat/status' ? Promise.resolve({ enabled: false }) : Promise.resolve({})
    ));
    renderTab();
    expect(await screen.findByText(/isn.t available here right now/)).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Go to Work' })).toBeTruthy();
  });
});
