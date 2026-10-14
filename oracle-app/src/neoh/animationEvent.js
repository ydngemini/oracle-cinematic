/**
 * animationEvent — the only way anything may move Neoh.
 *
 * Every state the mascot shows has to be traceable to a real event: a chat
 * turn the backend accepted, a receipt for an applied action, the browser's
 * own microphone, the socket's own connection state. This module defines the
 * envelope those events travel in (`NeohAnimationEvent`), rejects anything
 * that does not fit it, and runs the statechart that turns the surviving
 * events into one displayed state.
 *
 * What it refuses, on purpose:
 *   · a stage asserted by a source that cannot know it (success without an
 *     action receipt, speaking without audio playback, listening without the
 *     microphone);
 *   · an event that has expired, or claims to come from the future;
 *   · any coordinate, pose or animation parameter. Behaviour comes from
 *     product state; a model response can never position a limb;
 *   · unknown fields and unknown stages — an unknown event changes nothing,
 *     so with nothing else live the mascot rests at idle.
 *
 * "Verified source" means the event was built by one of the app's own
 * adapters (avatarFacts.js) from state the backend or the browser reported.
 * It is an allow-list, not a signature: nothing here can authenticate a
 * producer, and nothing outside those adapters should construct events.
 *
 * Pure: no React, no clock. Callers pass `now`.
 */

import { ACTION_TYPES, PRECEDENCE, SUCCESS_HOLD_MS } from './avatarModel';

// ── Timing ───────────────────────────────────────────────────────────────

/** How long a failure face holds before settling (the text stays). */
export const ERROR_HOLD_MS = 10_000;
/** How long a cancellation is remembered as the reason the mascot is idle. */
export const CANCEL_HOLD_MS = SUCCESS_HOLD_MS;
/**
 * The longest an in-flight request may claim "thinking" without new backend
 * activity. The backend ends a chat turn at ORACLE_AI_TURN_DEADLINE_SECONDS
 * (90 s by default, ai_chat_agent.py); past this ceiling the request is no
 * longer known to be in flight, so the mascot stops claiming it.
 */
export const THINKING_VALID_MS = 120_000;
/** Clock skew tolerated on server timestamps before an event is refused. */
export const MAX_FUTURE_SKEW_MS = 120_000;
/**
 * Hysteresis: an activity state (listening, thinking, acting, speaking) is
 * shown only once it has been the winner for this long. A state that ends is
 * dropped immediately — the delay can only under-claim, never hold a state
 * past its truth. Alerts and outcomes are never delayed.
 */
export const ENTER_MS = 120;
/** Ended streams are remembered this long so a late, older event is refused. */
export const TOMBSTONE_MS = 10 * 60_000;
/** Hard cap on remembered streams, so an event storm cannot grow memory. */
export const MAX_STREAMS = 64;

// ── Vocabulary ───────────────────────────────────────────────────────────

/** The nine product states, plus `cancelled`, which displays as idle. */
export const EVENT_STAGES = Object.freeze([...PRECEDENCE, 'cancelled']);

export const EVENT_ORIGINS = Object.freeze(['chat', 'command', 'voice', 'audio', 'channel', 'surface']);

export const EVENT_SOURCES = Object.freeze([
  'chat_turn',        // an AI chat turn's own frames (accepted/start/delta/complete/error)
  'ask_request',      // the deterministic ask path's HTTP request, in flight
  'command_request',  // a Personal AI command parse/execute request, in flight
  'action_receipt',   // the backend confirmed an applied action
  'approval_pending', // a staged action is waiting for the person
  'browser_mic',      // the browser speech recogniser is capturing
  'audio_playback',   // an audio element is actually playing Neoh's voice
  'channel',          // the socket's connection state
  'user_cancel',      // the person cancelled
]);

/**
 * Which producer may assert which stage. Anything else is rejected.
 *
 * `audio_playback` has no producer today: no browser surface plays Neoh's
 * voice (T15.14), so `speaking` is unreachable rather than guessed.
 */
export const STAGE_SOURCES = Object.freeze({
  idle: EVENT_SOURCES,
  listening: Object.freeze(['browser_mic']),
  thinking: Object.freeze(['chat_turn', 'ask_request', 'command_request']),
  speaking: Object.freeze(['audio_playback']),
  acting: Object.freeze(['command_request']),
  success: Object.freeze(['action_receipt']),
  needs_attention: Object.freeze(['approval_pending']),
  error: Object.freeze(['chat_turn', 'ask_request', 'command_request', 'channel']),
  disconnected: Object.freeze(['channel']),
  cancelled: Object.freeze(['user_cancel']),
});

export const EVENT_REASONS = Object.freeze([
  'timeout', 'unavailable', 'interrupted', 'rejected', 'failed', 'send_refused',
  'approval', 'cancelled',
]);

const URGENCIES = Object.freeze(['low', 'normal', 'high']);

/** Stages that may only be transient, with the longest window each may claim. */
const TRANSIENT_CEILING = Object.freeze({
  success: SUCCESS_HOLD_MS,
  error: ERROR_HOLD_MS,
  cancelled: CANCEL_HOLD_MS,
});
/** In-flight stages must carry a finite staleness ceiling. */
const IN_FLIGHT_CEILING = Object.freeze({ thinking: THINKING_VALID_MS, acting: THINKING_VALID_MS });

const ACTIVITY = new Set(['listening', 'thinking', 'acting', 'speaking']);
const TERMINAL = new Set(['idle', 'success', 'error', 'cancelled']);

/** Field names that would let a producer drive geometry. Named so the
 *  rejection says why, not just "unknown field". */
const COORDINATE_KEY = /^(x|y|z|w|pos|position|rotation|rot|quat|quaternion|euler|pose|transform|matrix|bones?|joints?|gaze|gazex|gazey|yaw|pitch|roll|offset|scale|keyframes?|blend|blendshapes?|morph|morphs|visemes?|coordinates?|coords?)$/i;

const MAX_TEXT = 200;
const isText = (v) => typeof v === 'string' && v.length > 0 && v.length <= MAX_TEXT;
const isInt = (v, lo, hi) => Number.isInteger(v) && v >= lo && v <= hi;

/** Optional fields: name → value check. */
const OPTIONAL = Object.freeze({
  actionId: isText,
  sessionId: isText,
  priority: (v) => isInt(v, 0, 9),
  seq: (v) => isInt(v, 0, Number.MAX_SAFE_INTEGER),
  amplitude: (v) => typeof v === 'number' && Number.isFinite(v) && v >= 0 && v <= 1,
  urgency: (v) => URGENCIES.includes(v),
  actionType: (v) => ACTION_TYPES.includes(v),
  reason: (v) => EVENT_REASONS.includes(v),
});
const REQUIRED = Object.freeze(['id', 'origin', 'stage', 'source', 'at', 'validForMs', 'transient']);
const KNOWN = new Set([...REQUIRED, ...Object.keys(OPTIONAL)]);

/** Fields that only make sense on some stages. */
const STAGE_ONLY = Object.freeze({
  amplitude: new Set(['listening', 'speaking']),
  urgency: new Set(['needs_attention', 'error']),
  actionType: new Set(['acting']),
});

const reject = (reason) => ({ ok: false, reason });

/**
 * Validate one raw event.
 *
 * @param {unknown} raw
 * @param {number} now  ms since the epoch
 * @returns {{ok: true, event: object} | {ok: false, reason: string}}
 */
export function validateAnimationEvent(raw, now) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return reject('not_object');
  for (const key of Object.keys(raw)) {
    if (COORDINATE_KEY.test(key)) return reject('coordinates');
    if (!KNOWN.has(key)) return reject('unknown_field');
    const value = raw[key];
    if (value !== null && typeof value === 'object') return reject('bad_field');
  }
  for (const key of REQUIRED) {
    if (raw[key] === undefined || raw[key] === null) return reject(`missing_${key}`);
  }
  const { id, origin, stage, source, at, validForMs, transient } = raw;
  if (!isText(id)) return reject('bad_id');
  if (!EVENT_ORIGINS.includes(origin)) return reject('bad_origin');
  if (!EVENT_STAGES.includes(stage)) return reject('bad_stage');
  if (!EVENT_SOURCES.includes(source)) return reject('unverified_source');
  if (!STAGE_SOURCES[stage].includes(source)) return reject('unverified_source');
  if (typeof at !== 'number' || !Number.isFinite(at) || at < 0) return reject('bad_timestamp');
  if (typeof transient !== 'boolean') return reject('bad_field');
  if (typeof validForMs !== 'number' || Number.isNaN(validForMs) || validForMs <= 0) return reject('bad_window');
  if (validForMs === Infinity && transient) return reject('bad_window');

  const ceiling = TRANSIENT_CEILING[stage];
  if (ceiling !== undefined && (!transient || validForMs > ceiling)) return reject('bad_window');
  const inFlight = IN_FLIGHT_CEILING[stage];
  if (inFlight !== undefined && validForMs > inFlight) return reject('bad_window');

  for (const [key, check] of Object.entries(OPTIONAL)) {
    if (raw[key] === undefined || raw[key] === null) continue;
    if (!check(raw[key])) return reject('bad_field');
    if (STAGE_ONLY[key] && !STAGE_ONLY[key].has(stage)) return reject('bad_field');
  }

  if (at > now + MAX_FUTURE_SKEW_MS) return reject('future');
  if (now >= at + validForMs) return reject('expired');

  const event = {};
  for (const key of KNOWN) if (raw[key] !== undefined && raw[key] !== null) event[key] = raw[key];
  return { ok: true, event: Object.freeze(event) };
}

/** The activity an event belongs to. Later events for a stream replace earlier ones. */
export function streamKey(event) {
  return `${event.origin}:${event.actionId ?? event.sessionId ?? event.id}`;
}

/**
 * Whether `incoming` supersedes `stored` for the same stream. Newer wins; on
 * an exact tie a higher `seq` wins, then an ending beats an activity (a turn
 * that completed in the same millisecond it streamed has completed).
 */
function supersedes(incoming, stored) {
  if (incoming.at !== stored.at) return incoming.at > stored.at;
  const a = incoming.seq ?? 0;
  const b = stored.seq ?? 0;
  if (a !== b) return a > b;
  return TERMINAL.has(incoming.stage) && !TERMINAL.has(stored.stage);
}

/** An idle event is an ending; anything else is live inside its window. */
const live = (event, now) => event.stage !== 'idle' && now < event.at + event.validForMs;

/** Precedence rank: lower is stronger. `cancelled` sits just above idle. */
const RANK = Object.freeze(Object.fromEntries(
  [...PRECEDENCE.slice(0, -1), 'cancelled', 'idle'].map((stage, index) => [stage, index]),
));
const displayStage = (stage) => (stage === 'cancelled' ? 'idle' : stage);

export const IDLE_VIEW = Object.freeze({
  state: 'idle', actionType: 'generic', reason: null, urgency: null, amplitude: 0, eventId: null,
});

export function createAnimationMachine() {
  return Object.freeze({ streams: Object.freeze({}), candidate: 'idle', candidateSince: 0, displayed: 'idle' });
}

/**
 * Feed events into the machine.
 *
 * @returns {{machine: object, rejected: Array<{id: string|null, reason: string}>}}
 */
export function ingestEvents(machine, raws, now) {
  const streams = { ...machine.streams };
  const rejected = [];
  let changed = false;
  for (const raw of raws || []) {
    const result = validateAnimationEvent(raw, now);
    if (!result.ok) {
      rejected.push({ id: typeof raw?.id === 'string' ? raw.id : null, reason: result.reason });
      continue;
    }
    const event = result.event;
    const key = streamKey(event);
    const stored = streams[key];
    if (stored && !supersedes(event, stored)) {
      if (stored.id !== event.id) rejected.push({ id: event.id, reason: 'out_of_order' });
      continue;
    }
    streams[key] = event;
    changed = true;
  }

  // Forget long-ended streams, then cap the rest (newest kept).
  for (const [key, event] of Object.entries(streams)) {
    const endedAt = event.stage === 'idle' ? event.at : event.at + event.validForMs;
    if (Number.isFinite(endedAt) && now - endedAt > TOMBSTONE_MS) {
      delete streams[key];
      changed = true;
    }
  }
  const keys = Object.keys(streams);
  if (keys.length > MAX_STREAMS) {
    keys.sort((a, b) => streams[b].at - streams[a].at);
    for (const key of keys.slice(MAX_STREAMS)) delete streams[key];
    changed = true;
  }

  return {
    machine: changed ? Object.freeze({ ...machine, streams: Object.freeze(streams) }) : machine,
    rejected,
  };
}

function strongest(events) {
  let best = null;
  for (const event of events) {
    if (!best) { best = event; continue; }
    const r = RANK[event.stage] - RANK[best.stage];
    if (r < 0) { best = event; continue; }
    if (r > 0) continue;
    const p = (event.priority ?? 0) - (best.priority ?? 0);
    if (p > 0 || (p === 0 && supersedes(event, best))) best = event;
  }
  return best;
}

/**
 * Resolve what Neoh shows at `now`.
 *
 * @returns {{machine: object, view: object, nextAt: number|null}}
 *   `nextAt` is when the answer can next change on its own (an expiry or the
 *   end of a hysteresis wait) — the caller sets ONE timer for it. Nothing here
 *   needs a frame loop.
 */
export function resolveAnimation(machine, now) {
  let pool = Object.values(machine.streams).filter((event) => live(event, now));

  // Barge-in: the person speaking over Neoh interrupts it. A listening event
  // newer than every speaking event wins, whatever the precedence says.
  const newest = (stage) => pool.reduce((max, e) => (e.stage === stage && e.at > max ? e.at : max), -Infinity);
  const listeningAt = newest('listening');
  if (listeningAt > newest('speaking') && Number.isFinite(listeningAt)) {
    pool = pool.filter((e) => e.stage !== 'speaking');
  }

  const winner = strongest(pool);
  const candidate = winner ? displayStage(winner.stage) : 'idle';
  const candidateSince = candidate === machine.candidate ? machine.candidateSince : now;
  const settled = !ACTIVITY.has(candidate) || now - candidateSince >= ENTER_MS;

  let displayed = candidate;
  if (!settled) {
    // Keep showing the previous state only while it is still true.
    const stillTrue = machine.displayed === 'idle'
      || pool.some((e) => displayStage(e.stage) === machine.displayed);
    displayed = stillTrue ? machine.displayed : 'idle';
  }

  const backing = displayed === 'idle'
    ? pool.find((e) => e.stage === 'cancelled') || null
    : strongest(pool.filter((e) => displayStage(e.stage) === displayed));

  const view = backing || displayed !== 'idle'
    ? Object.freeze({
      state: displayed,
      actionType: displayed === 'acting' ? (backing?.actionType || 'generic') : 'generic',
      reason: backing?.reason ?? (backing?.stage === 'cancelled' ? 'cancelled' : null),
      urgency: backing?.urgency ?? null,
      amplitude: backing?.amplitude ?? 0,
      eventId: backing?.id ?? null,
    })
    : IDLE_VIEW;

  let nextAt = settled ? Infinity : candidateSince + ENTER_MS;
  for (const event of pool) {
    const end = event.at + event.validForMs;
    if (Number.isFinite(end) && end < nextAt) nextAt = end;
  }

  const next = candidate === machine.candidate && displayed === machine.displayed
    && candidateSince === machine.candidateSince
    ? machine
    : Object.freeze({ ...machine, candidate, candidateSince, displayed });
  return { machine: next, view, nextAt: Number.isFinite(nextAt) ? nextAt : null };
}
