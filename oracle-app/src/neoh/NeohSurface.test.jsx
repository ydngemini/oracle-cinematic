// @vitest-environment jsdom
/**
 * The floating Neoh bar: same composer contract as the Neoh tab.
 *
 * It used to make the whole surface a live region while thinking and put the
 * interim transcript in a second one, so a screen reader re-read the reply as
 * it streamed and every partial word as it was heard. It printed raw channel
 * state ("Channel offline."). And it showed a bare record name with an X.
 */

import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { crmGet, crmPost } from '../state/useCrmApi';
import { resetNeohChannelCache } from './useNeohChannel';
import { resetCommandReceiptCache } from './useCommandReceipts';
import { NeohSurface } from './NeohSurface';
import { NeohCorner } from './NeohCorner';
import { getSharedAvatar, resetAvatarPresence } from './avatarPresence';
// Warm the lazily-loaded open half so findBy* is not racing a cold transform.
import './NeohSurfacePanel';

vi.mock('../state/useCrmApi', () => ({ crmGet: vi.fn(), crmPost: vi.fn() }));
vi.mock('../state', () => ({
  ACTIONS: { AI_CHAT_HYDRATE: 'AI_CHAT_HYDRATE', AI_CHAT_SEND_LOCAL: 'AI_CHAT_SEND_LOCAL' },
  useOracleState: () => globalThis.__oracle.state,
  useOracleDispatch: () => globalThis.__oracle.dispatchValue,
}));
vi.mock('../components/AssistantContext', () => ({
  useAssistant: () => globalThis.__assistant,
}));
vi.mock('./motion', () => ({
  useMotionPolicy: () => ({ reduced: true, layout: false, transition: { duration: 0 } }),
}));

class FakeRecognition {
  constructor() { FakeRecognition.last = this; }
  start() { this.onstart?.(); }
  stop() { this.onend?.(); }
  abort() {}
}

beforeEach(() => {
  vi.clearAllMocks();
  resetNeohChannelCache();
  resetCommandReceiptCache();
  globalThis.__oracle = {
    state: { aiChatMessages: [], aiChatRevision: 0, aiChatConnection: 'offline' },
    dispatchValue: { dispatch: vi.fn(), wsRef: { current: { readyState: 1, send: vi.fn() } } },
  };
  globalThis.__assistant = {
    open: true,
    setOpen: vi.fn(),
    record: { type: 'client', id: 'cccccccc-2222-4222-8222-222222222222', label: 'Sarah Johnson' },
    clearRecord: vi.fn(),
    commandRequest: null,
    clearCommandRequest: vi.fn(),
    requestCommand: vi.fn(),
    commandStatus: { state: 'idle' },
  };
  window.SpeechRecognition = FakeRecognition;
  crmGet.mockImplementation((path) => {
    if (path === '/api/ai/chat/status') return Promise.resolve({ enabled: true });
    if (path.startsWith('/api/ai/chat/messages')) return Promise.resolve({ messages: [] });
    return Promise.resolve({});
  });
  crmPost.mockResolvedValue({ fallthrough: true });
});

afterEach(() => {
  cleanup();
  resetAvatarPresence();
  delete window.SpeechRecognition;
});

describe('NeohSurface', () => {
  it('speaks product language and shows the context in words', async () => {
    render(<NeohSurface />);
    expect(await screen.findByText('Talking about Sarah Johnson')).toBeTruthy();
    expect(screen.getByText('Neoh is reconnecting. Your work is saved.')).toBeTruthy();
    expect(screen.queryByText(/Channel/)).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Stop talking about Sarah Johnson' }));
    expect(globalThis.__assistant.clearRecord).toHaveBeenCalled();
  });

  it('is not a live region while thinking, and its captions are not either', async () => {
    globalThis.__oracle.state.aiChatMessages = [
      { id: 'a1', request_id: 'r1', role: 'assistant', content: 'Look', status: 'streaming' },
    ];
    render(<NeohSurface />);
    const dialog = await screen.findByRole('dialog', { name: 'Neoh' });
    expect(dialog.getAttribute('aria-live')).toBeNull();
    // Still typeable while thinking.
    expect(screen.getByRole('textbox', { name: 'Message Neoh' }).disabled).toBe(false);

    fireEvent.click(screen.getByRole('button', { name: 'Talk to Neoh' }));
    act(() => {
      FakeRecognition.last.onresult?.({
        resultIndex: 0,
        results: [Object.assign([{ transcript: 'show me' }], { isFinal: false })],
      });
    });
    expect(screen.getByText('show me').closest('[aria-live]')).toBeNull();
    expect(document.querySelectorAll('[aria-live]').length).toBe(1);
  });

  it('hands its draft to the full conversation when expanded', async () => {
    const onExpand = vi.fn();
    render(<NeohSurface onExpand={onExpand} />);
    fireEvent.change(await screen.findByRole('textbox', { name: 'Message Neoh' }), { target: { value: 'half a thought' } });
    fireEvent.click(screen.getByRole('button', { name: 'Open the full conversation' }));
    expect(globalThis.__assistant.requestCommand).toHaveBeenCalledWith({ rawText: 'half a thought', surface: 'conversation' });
    expect(onExpand).toHaveBeenCalled();
  });

  it('leaves a request addressed to the conversation for the Neoh tab', async () => {
    globalThis.__assistant.commandRequest = { rawText: 'for the tab', surface: 'conversation' };
    render(<NeohSurface />);
    await screen.findByRole('textbox', { name: 'Message Neoh' });
    await new Promise((resolve) => setTimeout(resolve, 30));
    expect(globalThis.__assistant.clearCommandRequest).not.toHaveBeenCalled();
    expect(screen.getByRole('textbox', { name: 'Message Neoh' }).value).toBe('');
  });

  // F15.09: the pill and the Home corner are two mascots on one screen. The
  // corner shows what the surface resolved — never a state of its own.
  it('shares its resolved state with the Home corner, so the two never disagree', async () => {
    window.matchMedia = (query) => ({ matches: false, media: query, addEventListener() {}, removeEventListener() {} });
    globalThis.__assistant.open = false;
    globalThis.__oracle.state = {
      ...globalThis.__oracle.state,
      aiChatConnection: 'online',
      aiChatMessages: [{
        id: 'local-assistant-r1', request_id: 'r1', role: 'assistant', status: 'pending',
        content: '', created_at: new Date().toISOString(), observed_at: Date.now(), local: true,
      }],
    };
    const { container } = render(<><NeohSurface /><NeohCorner /></>);
    await screen.findByRole('button', { name: /Press slash or command K/ });
    await act(() => new Promise((resolve) => { setTimeout(resolve, 200); }));
    const states = [...container.querySelectorAll('[data-state][data-variant]')].map((n) => n.getAttribute('data-state'));
    expect(states).toEqual(['thinking', 'thinking']);
    expect(getSharedAvatar().state).toBe('thinking');
  });

  it('keeps the corner at rest when Neoh is switched off for the workspace', async () => {
    window.matchMedia = (query) => ({ matches: false, media: query, addEventListener() {}, removeEventListener() {} });
    crmGet.mockImplementation((path) => (path === '/api/ai/chat/status'
      ? Promise.resolve({ enabled: false }) : Promise.resolve({})));
    globalThis.__oracle.state = { ...globalThis.__oracle.state, aiChatConnection: 'offline' };
    const { container } = render(<><NeohSurface /><NeohCorner /></>);
    await act(() => new Promise((resolve) => { setTimeout(resolve, 50); }));
    expect(container.querySelector('[data-state]').getAttribute('data-state')).toBe('idle');
    expect(getSharedAvatar().state).toBe('idle');
  });
});
