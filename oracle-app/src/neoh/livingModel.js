/**
 * livingModel — a person is not a row that looks the same forever.
 *
 * A contact renders differently dormant, engaged, on a call, just off a call,
 * under contract, or just closed — because those are different situations for
 * the agent, and the object should say so before any words do.
 *
 * The rule that keeps this honest: the STATE is a fact, not a mood. Every
 * state is derived from something recorded — an interaction, a transaction, a
 * call — by ONE function on the server (`backend/living_state.py`). This file
 * never derives dormant/engaged/under-contract from raw facts; it would be a
 * second implementation that drifts. It does exactly two things the server
 * cannot:
 *
 *   1. overlays the one fact only the browser knows first — that the agent's
 *      own softphone is ringing or connected right now (`callPresence.js`);
 *   2. turns a state into words and a tone for the card.
 *
 * The vocabulary and thresholds below are asserted equal to the Python module
 * by `livingModel.test.js`, the same way the primitive registry is.
 */

/** Closed vocabulary, in ascending precedence. Keep in step with living_state.py. */
export const LIVING_STATES = Object.freeze([
  'dormant', 'quiet', 'engaged', 'closed', 'under_contract', 'after_call', 'calling',
]);

/** Stated priors, not fitted. Keep in step with living_state.py. */
export const ENGAGED_DAYS = 7;
export const DORMANT_DAYS = 45;
export const AFTER_CALL_MINUTES = 30;
export const CLOSED_RECENT_DAYS = 30;

const MS_MIN = 60_000;
const MS_DAY = 86_400_000;

/** The softphone states that mean "a call is happening". */
export const ACTIVE_CALL_STATES = Object.freeze(['connecting', 'ringing', 'connected']);

/**
 * Compose the server's living state with the browser's call presence.
 * `presence` is `{ state, startedAt, endedAt }` from callPresence for THIS
 * person, or null. Returns a new object; never mutates the server payload.
 */
export function composeLiving(server, presence, now = Date.now()) {
  const base = server && typeof server === 'object' ? server : null;
  if (presence && ACTIVE_CALL_STATES.includes(presence.state)) {
    return { ...(base || {}), state: 'calling', since: presence.startedAt || null, local: true };
  }
  if (presence?.endedAt) {
    const ended = Date.parse(presence.endedAt);
    if (Number.isFinite(ended) && now - ended < AFTER_CALL_MINUTES * MS_MIN) {
      return { ...(base || {}), state: 'after_call', since: presence.endedAt, local: true };
    }
  }
  if (!base || !LIVING_STATES.includes(base.state)) return null;
  return { ...base, local: false };
}

/** Tone drives the CSS; it is deliberately coarser than the state. */
export const LIVING_TONE = Object.freeze({
  dormant: 'dim',
  quiet: 'neutral',
  engaged: 'live',
  closed: 'done',
  under_contract: 'deal',
  after_call: 'call',
  calling: 'call',
});

export function livingLabel(state) {
  switch (state) {
    case 'calling': return 'On a call';
    case 'after_call': return 'Just spoke';
    case 'under_contract': return 'Under contract';
    case 'closed': return 'Closed';
    case 'engaged': return 'Active';
    case 'quiet': return 'Quiet';
    case 'dormant': return 'Dormant';
    default: return '';
  }
}

/**
 * One sentence for the card. Always derived from a recorded time or count —
 * never "very active" or "cooling off", which would be adjectives pretending
 * to be facts.
 */
export function livingLine(living, now = Date.now()) {
  if (!living) return '';
  const { state } = living;
  if (state === 'calling') {
    const dur = duration(living.since, now);
    return dur ? `On a call · ${dur}` : 'On a call';
  }
  if (state === 'after_call') return `Call ended ${ago(living.since, now)}`;
  if (state === 'under_contract') {
    const closing = living.transaction?.closing_deadline;
    return closing ? `Under contract · closing ${dateWord(closing)}` : 'Under contract';
  }
  if (state === 'closed') return `Closed ${ago(living.transaction?.closed_at || living.since, now)}`;
  if (state === 'engaged') {
    const n = Number(living.signals_7d) || 0;
    const head = n > 0 ? `${n} signal${n === 1 ? '' : 's'} this week` : 'Active this week';
    return living.last_activity_at ? `${head} · last ${ago(living.last_activity_at, now)}` : head;
  }
  if (state === 'quiet') return `Last activity ${ago(living.last_activity_at, now)}`;
  if (state === 'dormant') {
    return living.last_activity_at ? `Last heard from ${ago(living.last_activity_at, now)}` : 'Never heard from';
  }
  return '';
}

/* ── time words ─────────────────────────────────────────────────────────── */

export function ago(iso, now = Date.now()) {
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return 'a while ago';
  const ms = Math.max(0, now - t);
  if (ms < MS_MIN) return 'just now';
  if (ms < 60 * MS_MIN) return `${Math.round(ms / MS_MIN)} min ago`;
  if (ms < MS_DAY) return `${Math.round(ms / (60 * MS_MIN))} h ago`;
  const days = Math.round(ms / MS_DAY);
  if (days === 1) return 'yesterday';
  if (days < 14) return `${days} days ago`;
  if (days < 60) return `${Math.round(days / 7)} weeks ago`;
  if (days < 365) return `${Math.round(days / 30)} months ago`;
  return `${Math.round(days / 365)} years ago`;
}

export function duration(iso, now = Date.now()) {
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return '';
  const s = Math.max(0, Math.floor((now - t) / 1000));
  const m = Math.floor(s / 60);
  return `${m}:${String(s % 60).padStart(2, '0')}`;
}

export function dateWord(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}
