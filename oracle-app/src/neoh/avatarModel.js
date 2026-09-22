/**
 * avatarModel — what Neoh *is doing*, as one semantic value.
 *
 * The surface already derives its SHAPE from facts (surfaceModel). This
 * derives its FACE from facts the same way: a pure function of what is
 * true, so the avatar can never hold a mood it forgot to clear.
 *
 * These are AI system states, not emotions. There are eight plus an offline
 * one — deliberately few. A status indicator with twenty faces is a virtual
 * pet, and a pet is the wrong thing to put on top of someone's CRM.
 *
 * The model knows nothing about carriers, models or transports. It never
 * learns the words Plivo, Telnyx, Twilio or Qwen — those are infrastructure.
 * It knows call, message, listening, speaking, acting. That is the product.
 */

export const AVATAR_STATES = Object.freeze([
  'idle',
  'listening',
  'thinking',
  'speaking',
  'acting',
  'success',
  'needs_attention',
  'error',
  'disconnected',
]);

export const ACTION_TYPES = Object.freeze([
  'call',
  'message',
  'calendar',
  'property',
  'search',
  'save',
  'share',
  'generic',
]);

/**
 * Precedence, highest first.
 *
 * The order is not arbitrary. Anything the person must act on outranks
 * anything Neoh is merely doing, because the avatar's job in that moment is
 * to be noticed. Below that, the live channel states (speaking, listening)
 * outrank background work, because they are synchronous with the person —
 * a thinking face while Neoh is audibly talking would read as broken.
 * `disconnected` sits above ordinary work but below a real error: it is a
 * true statement about the channel, and pretending to think while the socket
 * is down is exactly the fake activity this file exists to prevent.
 */
const PRECEDENCE = Object.freeze([
  'error',
  'needs_attention',
  'disconnected',
  'speaking',
  'listening',
  'acting',
  'thinking',
  'success',
  'idle',
]);

const LIVE_MESSAGE = new Set(['pending', 'streaming']);

/** How long a success acknowledgement holds before settling back. */
export const SUCCESS_HOLD_MS = 1_400;

/**
 * Map a tool/action name to one of the small set of glyph categories.
 * Unknown actions become `generic` rather than leaking a raw tool name onto
 * the mascot.
 */
export function actionTypeFor(name) {
  const value = String(name || '').toLowerCase();
  if (!value) return 'generic';
  if (/(call|dial|phone|voice)/.test(value)) return 'call';
  if (/(sms|text|message|email|mail|reply)/.test(value)) return 'message';
  if (/(calendar|showing|appointment|schedule|event)/.test(value)) return 'calendar';
  // Share is checked before the property/search nouns on purpose: in
  // `share_property_tour` the thing Neoh is doing is sharing, and the glyph
  // reports the verb, not the object it happens to act on.
  if (/(share|send link|portal)/.test(value)) return 'share';
  if (/(propert|listing|home|house|tour)/.test(value)) return 'property';
  if (/(search|find|lookup|research)/.test(value)) return 'search';
  if (/(save|note|update|create|crm)/.test(value)) return 'save';
  return 'generic';
}

/**
 * Derive the avatar's semantic state from real application facts.
 *
 * Every input is something the app already knows. Nothing here is a timer
 * pretending to be activity — if Neoh looks like it is thinking, a message
 * really is pending.
 *
 * @param {object} facts
 * @param {'online'|'offline'|'connecting'|string} [facts.connection]
 * @param {Array}   [facts.messages]        conversation, newest last
 * @param {boolean} [facts.asking]          the deterministic ask path is in flight
 * @param {boolean} [facts.micActive]       the person is speaking to Neoh
 * @param {boolean} [facts.speaking]        assistant audio is playing
 * @param {boolean} [facts.attention]       something needs the person
 * @param {boolean} [facts.failed]          a genuine failure to surface
 * @param {string}  [facts.actionName]      tool/action currently executing
 * @param {boolean} [facts.succeeded]       a consequential action just confirmed
 * @returns {{state: string, actionType: string}}
 */
export function avatarState(facts = {}) {
  const {
    connection = 'online',
    messages = [],
    asking = false,
    micActive = false,
    speaking = false,
    attention = false,
    failed = false,
    actionName = '',
    succeeded = false,
  } = facts;

  const acting = Boolean(actionName);
  const thinking = asking || (messages || []).some((m) => LIVE_MESSAGE.has(m?.status));

  const candidates = {
    error: Boolean(failed),
    needs_attention: Boolean(attention),
    disconnected: connection !== 'online',
    speaking: Boolean(speaking),
    listening: Boolean(micActive),
    acting,
    thinking,
    success: Boolean(succeeded),
    idle: true,
  };

  const state = PRECEDENCE.find((name) => candidates[name]) || 'idle';
  return { state, actionType: acting ? actionTypeFor(actionName) : 'generic' };
}

/**
 * Smooth an amplitude reading toward a target.
 *
 * Raw amplitude jitters hard enough to make a light look like it is
 * stuttering rather than responding. An exponential follow with a faster
 * attack than release tracks speech onsets without flickering on the gaps
 * between words.
 */
export function smoothLevel(previous, next, { attack = 0.45, release = 0.12 } = {}) {
  const target = Math.min(1, Math.max(0, Number(next) || 0));
  const current = Math.min(1, Math.max(0, Number(previous) || 0));
  const rate = target > current ? attack : release;
  return current + (target - current) * rate;
}
