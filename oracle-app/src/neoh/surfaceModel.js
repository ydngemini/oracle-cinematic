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
  return record?.label ? `Ask about ${record.label}` : 'Ask Neoh anything about your work';
}
