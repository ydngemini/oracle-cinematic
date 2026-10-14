/**
 * Chat turns keep the local time their last frame arrived (`observed_at`),
 * including across the history re-fetch that follows every completed turn.
 * Neoh's mascot uses it to tell a live turn from one left in history.
 */
import { describe, expect, it } from 'vitest';

import { ACTIONS, oracleReducer } from './oracleReducer';

const base = { aiChatMessages: [], aiChatRevision: 0, aiChatConnection: 'online' };
const run = (state, ...actions) => actions.reduce((s, a) => oracleReducer(s, a), state);

describe('oracleReducer chat arrival stamps', () => {
  it('stamps each frame and keeps the newest', () => {
    const state = run(
      base,
      { type: ACTIONS.AI_CHAT_START, payload: { request_id: 'r', message_id: 'm', observed_at: 100 } },
      { type: ACTIONS.AI_CHAT_DELTA, payload: { request_id: 'r', message_id: 'm', delta: 'Hi', observed_at: 200 } },
    );
    expect(state.aiChatMessages[0]).toMatchObject({ status: 'streaming', observed_at: 200 });
    const done = run(state, { type: ACTIONS.AI_CHAT_COMPLETE, payload: { request_id: 'r', message_id: 'm', observed_at: 300 } });
    expect(done.aiChatMessages[0]).toMatchObject({ status: 'completed', observed_at: 300 });
  });

  it('does not invent a stamp for a frame that has none', () => {
    const state = run(base, { type: ACTIONS.AI_CHAT_START, payload: { request_id: 'r', message_id: 'm' } });
    expect('observed_at' in state.aiChatMessages[0]).toBe(false);
  });

  it('carries the stamp onto the saved row when history replaces the live one', () => {
    const live = run(base, { type: ACTIONS.AI_CHAT_COMPLETE, payload: { request_id: 'r', message_id: 'm', observed_at: 300 } });
    const hydrated = run(live, {
      type: ACTIONS.AI_CHAT_HYDRATE,
      payload: [{ id: 'm', request_id: 'r', role: 'assistant', status: 'completed', updated_at: '2026-10-14T00:00:00Z' }],
    });
    expect(hydrated.aiChatMessages).toHaveLength(1);
    expect(hydrated.aiChatMessages[0]).toMatchObject({ id: 'm', observed_at: 300, updated_at: '2026-10-14T00:00:00Z' });
  });

  it('records a rejection as a failed turn with a code and a stamp', () => {
    const sent = run(base, {
      type: ACTIONS.AI_CHAT_SEND_LOCAL,
      payload: {
        user: { id: 'u', request_id: 'r', role: 'user', status: 'completed', local: true },
        assistant: { id: 'a', request_id: 'r', role: 'assistant', status: 'pending', local: true, observed_at: 50 },
      },
    });
    const rejected = run(sent, { type: ACTIONS.AI_CHAT_REJECTED, payload: { request_id: 'r', message: 'No', observed_at: 60 } });
    expect(rejected.aiChatMessages[1]).toMatchObject({ status: 'failed', error_code: 'AI_CHAT_REJECTED', observed_at: 60 });
  });
});
