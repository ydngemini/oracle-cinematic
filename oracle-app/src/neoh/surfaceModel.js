/**
 * surfaceModel — which shape the Neoh surface takes, and what it says at rest.
 *
 * One object, five shapes:
 *   rest     — a pill above the deck; the label says what Neoh is looking at
 *   input    — a wide bar with the cursor in it
 *   thinking — the bar, holding, while a reply is pending
 *   result   — a panel: the conversation, receipts, undo
 *   yielded  — collapsed out of the way while a sheet has the screen
 *
 * The shape is a pure function of what is true, so the component never holds
 * a "mode" it could get wrong; it derives one every render.
 */

export const STATES = Object.freeze(['rest', 'input', 'thinking', 'result', 'yielded']);

const LIVE = new Set(['pending', 'streaming']);

export function isBusy(messages) {
  return (messages || []).some((m) => LIVE.has(m?.status));
}

/**
 * @param {object} facts
 * @param {boolean} facts.open        the person asked for Neoh (⌘K, tap, a command request)
 * @param {boolean} facts.entityOpen  a record sheet has the screen
 * @param {Array}   facts.messages    the conversation, newest last
 * @param {boolean} facts.showResult  the person has opened the conversation panel
 */
export function surfaceState({ open, entityOpen, messages, showResult }) {
  if (entityOpen && !open) return 'yielded';
  if (!open) return 'rest';
  if (isBusy(messages)) return 'thinking';
  if (showResult && (messages || []).length > 0) return 'result';
  return 'input';
}

/** The pill's label: what Neoh is looking at, or an invitation. */
export function restLabel({ record, messages, busy }) {
  if (busy) return 'Neoh is working…';
  if (record?.label) return `Ask about ${record.label}`;
  const last = (messages || []).slice().reverse().find((m) => m?.role === 'assistant' && m?.status === 'completed');
  if (last) return 'Neoh answered';
  return 'Ask Neoh';
}

/** Placeholder for the bar: the record narrows it, nothing else does. */
export function inputPlaceholder(record) {
  return record?.label ? `Ask about ${record.label}` : 'Ask Neoh anything…';
}

/** The context chip. Said in full, so there is never a mystery about what
 *  Neoh is reading alongside the question. */
export function contextLabel(record) {
  return record?.label ? `Talking about ${record.label}` : '';
}

/**
 * The connection, in product language. The raw state ("offline",
 * "Channel offline.") is plumbing; what a person needs is whether their work
 * is safe and whether they have to do anything.
 */
export function connectionMessage(connection) {
  if (!connection || connection === 'online') return '';
  return 'Neoh is reconnecting. Your work is saved.';
}

/**
 * The microphone's label and availability, from the speech hook's state.
 * The button is always rendered — the composer's shape is
 * [field] [mic] [send] everywhere — so an unsupported browser gets a
 * disabled button that says why, not a missing one.
 */
export function micControl({ supported, state }) {
  if (!supported) return { label: "Voice input isn't available in this browser", disabled: true, pressed: false };
  if (state === 'listening') return { label: 'Stop listening', disabled: false, pressed: true };
  if (state === 'requesting') return { label: 'Waiting for microphone permission', disabled: false, pressed: false };
  if (state === 'error') return { label: 'Try the microphone again', disabled: false, pressed: false };
  return { label: 'Talk to Neoh', disabled: false, pressed: false };
}

/** One line under Neoh's name on the conversation page. */
export function presenceLine({ connection, busy, listening }) {
  if (connection && connection !== 'online') return 'Reconnecting…';
  if (listening) return 'Listening…';
  if (busy) return 'Thinking…';
  return 'Ready when you are';
}
