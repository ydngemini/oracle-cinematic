/**
 * avatarFacts — the adapters from app state to NeohAnimationEvents.
 *
 * These are the only producers of animation events (animationEvent.js). Each
 * one reads a fact the backend or the browser already reported and stamps it
 * with the source that can actually know it:
 *
 *   chat turn pending/streaming     → thinking   (chat_turn)
 *   chat turn completed + a receipt → success    (action_receipt)
 *   chat turn completed, no receipt → idle       — an answer is not an action
 *   chat turn failed                → error      (chat_turn, reason from error_code)
 *   command analyzing               → thinking   (command_request)
 *   command authorizing / running   → acting     (command_request)
 *   command awaiting approval       → needs_attention (approval_pending)
 *   command completed WITH receipt  → success    (action_receipt)
 *   command completed, analysis only, or queued → idle
 *   command failed                  → error      (command_request)
 *   command cancelled               → idle, reason "cancelled" (user_cancel)
 *   speech recogniser capturing     → listening  (browser_mic)
 *   audio element playing           → speaking   (audio_playback) — no producer today
 *   socket not online               → disconnected (channel)
 *
 * Timestamps are what make "stale" decidable: a chat message carries the
 * local time its last frame arrived (`observed_at`, stamped by the socket
 * handler) or, for history, the server's `updated_at`; a command status
 * carries the time it was set (`at`, stamped by AssistantContext). A fact
 * with no timestamp cannot be shown to be current and is refused.
 */

import { actionTypeFor, SUCCESS_HOLD_MS } from './avatarModel';
import {
  CANCEL_HOLD_MS,
  createAnimationMachine,
  ERROR_HOLD_MS,
  IDLE_VIEW,
  ingestEvents,
  resolveAnimation,
  streamKey,
  THINKING_VALID_MS,
} from './animationEvent';

/** Only the newest turns can still be live; older ones are history. */
const RECENT_TURNS = 20;

const FOREVER = Infinity;

/** Map a backend chat error code to a reason the surface can name. */
export function chatErrorReason(code) {
  const value = String(code || '').toUpperCase();
  // Not sent today: the backend folds its 90 s turn deadline into
  // AI_RESPONSE_UNAVAILABLE (ai_chat_agent.py), so a provider timeout reads
  // as "unavailable" until it sends a distinct code.
  if (value.includes('TIMEOUT')) return 'timeout';
  if (value === 'AI_RESPONSE_INTERRUPTED') return 'interrupted';
  if (value === 'AI_RESPONSE_UNAVAILABLE') return 'unavailable';
  if (value.includes('REJECT') || value.includes('INVALID')) return 'rejected';
  return 'failed';
}

/** An applied, not-undone action with a ledger id is a receipt. */
export function isAppliedReceipt(receipt) {
  if (!receipt || typeof receipt !== 'object' || !receipt.action_id) return false;
  if (receipt.status === 'undone') return false;
  return receipt.status === 'applied' || receipt.ok === true;
}

function messageTime(message) {
  if (typeof message.observed_at === 'number') return message.observed_at;
  const parsed = Date.parse(message.updated_at || message.created_at || '');
  return Number.isFinite(parsed) ? parsed : Number.NaN;
}

function chatEvents(messages) {
  const turns = (messages || []).filter((m) => m?.role === 'assistant' && (m.request_id || m.id));
  return turns.slice(-RECENT_TURNS).map((message) => {
    const at = messageTime(message);
    const base = {
      origin: 'chat', sessionId: String(message.request_id || message.id), at,
    };
    const id = (stage) => `chat:${base.sessionId}:${stage}:${at}`;
    switch (message.status) {
      case 'pending':
      case 'streaming':
        return {
          ...base, id: id('thinking'), stage: 'thinking', source: 'chat_turn',
          validForMs: THINKING_VALID_MS, transient: false,
        };
      case 'failed':
        return {
          ...base, id: id('error'), stage: 'error', source: 'chat_turn',
          validForMs: ERROR_HOLD_MS, transient: true, reason: chatErrorReason(message.error_code),
        };
      case 'completed':
        if ((message.actions || []).some(isAppliedReceipt)) {
          return {
            ...base, id: id('success'), stage: 'success', source: 'action_receipt',
            validForMs: SUCCESS_HOLD_MS, transient: true,
          };
        }
        return { ...base, id: id('idle'), stage: 'idle', source: 'chat_turn', validForMs: FOREVER, transient: false };
      default:
        return { ...base, id: id('idle'), stage: 'idle', source: 'chat_turn', validForMs: FOREVER, transient: false };
    }
  });
}

function commandEvent(status) {
  if (!status) return null;
  const state = String(status.state || '').toLowerCase();
  const at = status.at;
  const seq = Number.isInteger(status.seq) ? status.seq : undefined;
  const base = { origin: 'command', sessionId: 'command', at, seq };
  const id = (stage) => `command:${stage}:${at}:${seq ?? 0}`;
  const idle = (source = 'command_request') => ({
    ...base, id: id('idle'), stage: 'idle', source, validForMs: FOREVER, transient: false,
  });
  switch (state) {
    case 'analyzing':
      return {
        ...base, id: id('thinking'), stage: 'thinking', source: 'command_request',
        validForMs: THINKING_VALID_MS, transient: false,
      };
    case 'authorizing':
    case 'running':
    case 'executing':
      return {
        ...base, id: id('acting'), stage: 'acting', source: 'command_request',
        validForMs: THINKING_VALID_MS, transient: false, actionType: actionTypeFor(status.detail || ''),
      };
    case 'awaiting_approval':
    case 'needs_approval':
      return {
        ...base, id: id('needs_attention'), stage: 'needs_attention', source: 'approval_pending',
        validForMs: FOREVER, transient: false, reason: 'approval',
      };
    case 'completed':
    case 'succeeded':
      // "Analysis complete" is not an action. Only a status that carries the
      // execute call's receipt may celebrate.
      if (status.receipt !== true) return idle();
      return {
        ...base, id: id('success'), stage: 'success', source: 'action_receipt',
        validForMs: SUCCESS_HOLD_MS, transient: true,
      };
    case 'failed':
    case 'error':
      return {
        ...base, id: id('error'), stage: 'error', source: 'command_request',
        validForMs: ERROR_HOLD_MS, transient: true, reason: 'failed',
      };
    case 'cancelled':
      return {
        ...base, id: id('cancelled'), stage: 'cancelled', source: 'user_cancel',
        validForMs: CANCEL_HOLD_MS, transient: true, reason: 'cancelled',
      };
    default:
      return idle();
  }
}

/**
 * A boolean fact the caller holds as live state (the mic is open, audio is
 * playing). `stamp` is when it last changed, from the store's clock.
 */
function flagEvent({ key, origin, active, stage, source, stamp, window = FOREVER, transient = false, extra = {} }) {
  const at = stamp?.at ?? Number.NaN;
  const seq = stamp?.seq;
  const on = Boolean(active);
  return {
    id: `${origin}:${key}:${on ? stage : 'idle'}:${at}:${seq ?? 0}`,
    origin, sessionId: key, at, seq,
    stage: on ? stage : 'idle',
    source,
    validForMs: on ? window : FOREVER,
    transient: on ? transient : false,
    ...(on ? extra : {}),
  };
}

/**
 * Every event the current facts imply. Pure.
 *
 * @param {object} facts  the useNeohAvatarState inputs
 * @param {Record<string, {at: number, seq: number}>} stamps when each flag last changed
 */
export function eventsFromFacts(facts = {}, stamps = {}) {
  const events = [
    flagEvent({
      key: 'connection', origin: 'channel', active: (facts.connection ?? 'online') !== 'online',
      stage: 'disconnected', source: 'channel', stamp: stamps.connection,
    }),
    flagEvent({
      key: 'send', origin: 'surface', active: facts.failed, stage: 'error', source: 'channel',
      stamp: stamps.failed, window: ERROR_HOLD_MS, transient: true, extra: { reason: 'send_refused' },
    }),
    flagEvent({
      key: 'mic', origin: 'voice', active: facts.micActive, stage: 'listening', source: 'browser_mic',
      stamp: stamps.micActive,
    }),
    flagEvent({
      key: 'playback', origin: 'audio', active: facts.speaking, stage: 'speaking', source: 'audio_playback',
      stamp: stamps.speaking,
    }),
    flagEvent({
      key: 'ask', origin: 'surface', active: facts.asking, stage: 'thinking', source: 'ask_request',
      stamp: stamps.asking, window: THINKING_VALID_MS,
    }),
    flagEvent({
      key: 'action', origin: 'surface', active: facts.actionName, stage: 'acting', source: 'command_request',
      stamp: stamps.actionName, window: THINKING_VALID_MS,
      extra: { actionType: actionTypeFor(facts.actionName || '') },
    }),
    flagEvent({
      key: 'attention', origin: 'surface', active: facts.attention, stage: 'needs_attention',
      source: 'approval_pending', stamp: stamps.attention, extra: { reason: 'approval' },
    }),
    ...chatEvents(facts.messages),
  ];
  const command = commandEvent(facts.commandStatus);
  if (command) events.push(command);
  return events;
}

/** Facts whose change time the store tracks itself. */
const FLAGS = Object.freeze(['connection', 'failed', 'micActive', 'speaking', 'asking', 'actionName', 'attention']);

function hookView(view, facts) {
  const voice = view.state === 'listening' || view.state === 'speaking';
  const level = Math.min(1, Math.max(0, Number(facts.audioLevel) || 0));
  return {
    state: view.state,
    actionType: view.actionType,
    reason: view.reason,
    audioLevel: voice ? level : 0,
    attentionLevel: view.state === 'needs_attention' ? 1 : 0,
  };
}

const sameView = (a, b) => a.state === b.state && a.actionType === b.actionType
  && a.reason === b.reason && a.audioLevel === b.audioLevel && a.attentionLevel === b.attentionLevel;

/**
 * A small external store per avatar: holds the statechart, re-resolves on
 * new facts and on ONE timer for the next expiry, and notifies only when the
 * shown view changes. React reads it with useSyncExternalStore.
 */
export function createAvatarStore({
  now = () => Date.now(),
  setTimer = (fn, ms) => window.setTimeout(fn, ms),
  clearTimer = (handle) => window.clearTimeout(handle),
  onRejected = null,
} = {}) {
  let machine = createAnimationMachine();
  let facts = {};
  let view = hookView(IDLE_VIEW, {});
  let timer = 0;
  let seq = 0;
  const stamps = {};
  const listeners = new Set();

  function schedule(nextAt, t) {
    if (timer) clearTimer(timer);
    timer = 0;
    if (nextAt === null) return;
    const delay = Math.min(2 ** 31 - 1, Math.max(0, nextAt - t));
    timer = setTimer(() => { timer = 0; recompute(); }, delay);
  }

  function recompute() {
    const t = now();
    const events = eventsFromFacts(facts, stamps);
    // A stream the facts no longer mention (a turn cleared from the thread)
    // has ended — say so rather than letting it linger until expiry.
    const present = new Set(events.map(streamKey));
    for (const [key, stored] of Object.entries(machine.streams)) {
      if (present.has(key) || stored.stage === 'idle') continue;
      events.push({
        id: `${key}:gone:${t}`, origin: stored.origin, sessionId: stored.sessionId, actionId: stored.actionId,
        stage: 'idle', source: stored.source, at: Math.max(t, stored.at), seq: (stored.seq ?? 0) + 1,
        validForMs: FOREVER, transient: false,
      });
    }
    const ingested = ingestEvents(machine, events, t);
    if (onRejected && ingested.rejected.length) onRejected(ingested.rejected);
    const resolved = resolveAnimation(ingested.machine, t);
    machine = resolved.machine;
    const next = hookView(resolved.view, facts);
    schedule(resolved.nextAt, t);
    if (!sameView(next, view)) {
      view = next;
      for (const listener of listeners) listener();
    }
  }

  return {
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    getView: () => view,
    /** New facts from the caller. Stamps every flag that changed. */
    observe(nextFacts = {}) {
      const t = now();
      for (const key of FLAGS) {
        // A changed action name is a new action, so it keeps its string.
        const value = key === 'connection' ? (nextFacts.connection ?? 'online') : (nextFacts[key] || false);
        const previous = stamps[key];
        if (!previous || previous.value !== value) stamps[key] = { at: t, seq: ++seq, value };
      }
      facts = nextFacts;
      recompute();
    },
    dispose() {
      if (timer) clearTimer(timer);
      timer = 0;
    },
  };
}
