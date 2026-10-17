/**
 * The adapters are the only producers of animation events. Every event they
 * build from fresh facts must pass validation, and each stage must come from
 * the source that can know it.
 */
import { describe, expect, it } from 'vitest';

import { validateAnimationEvent } from './animationEvent';
import { chatErrorReason, createAvatarStore, eventsFromFacts, isAppliedReceipt } from './avatarFacts';

const T0 = 1_760_000_000_000;
const stamp = { at: T0, seq: 1 };
const stamps = Object.fromEntries(
  ['connection', 'failed', 'micActive', 'speaking', 'asking', 'actionName', 'attention'].map((k) => [k, stamp]),
);
const turn = (status, extra = {}) => ({ id: 'm', request_id: 'r', role: 'assistant', status, observed_at: T0, ...extra });

const stagesFor = (facts) => eventsFromFacts(facts, stamps)
  .map((e) => ({ e, v: validateAnimationEvent(e, T0) }))
  .map(({ e, v }) => {
    expect(v.ok, `${e.id}: ${v.reason}`).toBe(true);
    return `${e.origin}:${e.stage}:${e.source}`;
  });

describe('eventsFromFacts', () => {
  it('emits a valid ending for every quiet source', () => {
    const stages = stagesFor({});
    expect(stages.every((s) => s.includes(':idle:'))).toBe(true);
  });

  it('stamps each live fact with the source that can know it', () => {
    const stages = stagesFor({
      connection: 'offline',
      failed: true,
      micActive: true,
      speaking: true,
      asking: true,
      actionName: 'place_call',
      attention: true,
      messages: [turn('pending'), turn('failed', { request_id: 'r2', error_code: 'AI_RESPONSE_UNAVAILABLE' })],
      commandStatus: { state: 'completed', receipt: true, at: T0, seq: 1 },
    });
    expect(stages).toEqual(expect.arrayContaining([
      'channel:disconnected:channel',
      'surface:error:channel',
      'voice:listening:browser_mic',
      'audio:speaking:audio_playback',
      'surface:thinking:ask_request',
      'surface:acting:command_request',
      'surface:needs_attention:approval_pending',
      'chat:thinking:chat_turn',
      'chat:error:chat_turn',
      'command:success:action_receipt',
    ]));
  });

  it('maps every command status the app sets', () => {
    const map = {
      analyzing: 'thinking', authorizing: 'acting', running: 'acting', executing: 'acting',
      awaiting_approval: 'needs_attention', needs_approval: 'needs_attention',
      completed: 'idle', queued: 'idle', failed: 'error', error: 'error', cancelled: 'cancelled', idle: 'idle',
      something_new: 'idle',
    };
    for (const [state, stage] of Object.entries(map)) {
      const command = eventsFromFacts({ commandStatus: { state, at: T0, seq: 1 } }, stamps).at(-1);
      expect(command.stage, state).toBe(stage);
      expect(validateAnimationEvent(command, T0).ok, state).toBe(true);
    }
  });

  it('only looks at assistant turns', () => {
    const events = eventsFromFacts({ messages: [{ role: 'user', request_id: 'r', status: 'pending', observed_at: T0 }] }, stamps);
    expect(events.some((e) => e.origin === 'chat')).toBe(false);
  });
});

describe('receipts and reasons', () => {
  it('recognises the live and the saved receipt shapes, and refuses undone or read-only ones', () => {
    expect(isAppliedReceipt({ ok: true, action_id: 'a' })).toBe(true);
    expect(isAppliedReceipt({ status: 'applied', action_id: 'a' })).toBe(true);
    expect(isAppliedReceipt({ status: 'undone', ok: true, action_id: 'a' })).toBe(false);
    expect(isAppliedReceipt({ ok: true })).toBe(false);
    expect(isAppliedReceipt(null)).toBe(false);
  });

  it('names backend error codes', () => {
    expect(chatErrorReason('AI_RESPONSE_INTERRUPTED')).toBe('interrupted');
    expect(chatErrorReason('AI_RESPONSE_UNAVAILABLE')).toBe('unavailable');
    expect(chatErrorReason('AI_RESPONSE_TIMEOUT')).toBe('timeout');
    expect(chatErrorReason('AI_CHAT_REJECTED')).toBe('rejected');
    expect(chatErrorReason(undefined)).toBe('failed');
  });

  it('names a budget stop as a budget, and an unreadable budget as unavailable (T18.07)', () => {
    expect(chatErrorReason('AI_BUDGET_EXHAUSTED')).toBe('budget');
    expect(chatErrorReason('AI_BUDGET_INSUFFICIENT')).toBe('budget');
    expect(chatErrorReason('AI_SPEND_PAUSED')).toBe('budget');
    expect(chatErrorReason('AI_SPEND_HALTED')).toBe('budget');
    expect(chatErrorReason('AI_BUDGET_UNAVAILABLE')).toBe('unavailable');
    // A refused turn is a valid error event, with its reason.
    const [event] = eventsFromFacts({ messages: [turn('failed', { error_code: 'AI_BUDGET_EXHAUSTED' })] }, stamps)
      .filter((e) => e.origin === 'chat');
    expect(event.stage).toBe('error');
    expect(event.reason).toBe('budget');
    expect(validateAnimationEvent(event, T0).ok).toBe(true);
  });
});

describe('createAvatarStore', () => {
  it('sets one timer for the next change and notifies only when the view changes', () => {
    let now = T0;
    const timers = [];
    const store = createAvatarStore({
      now: () => now,
      setTimer: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
      clearTimer: () => {},
    });
    let notified = 0;
    store.subscribe(() => { notified += 1; });
    store.observe({ commandStatus: { state: 'completed', receipt: true, at: T0, seq: 1 } });
    expect(store.getView().state).toBe('success');
    expect(timers.at(-1).ms).toBe(1_400);
    const before = notified;
    store.observe({ commandStatus: { state: 'completed', receipt: true, at: T0, seq: 1 } });
    expect(notified).toBe(before); // same facts, same view, no render
    now = T0 + 1_400;
    timers.at(-1).fn();
    expect(store.getView().state).toBe('idle');
    store.dispose();
  });

  it('reports what it refused, for debugging', () => {
    const refused = [];
    const store = createAvatarStore({ now: () => T0, setTimer: () => 0, clearTimer: () => {}, onRejected: (r) => refused.push(...r) });
    store.observe({ commandStatus: { state: 'completed', receipt: true } });
    expect(refused.some((r) => r.reason === 'missing_at')).toBe(true);
  });
});
