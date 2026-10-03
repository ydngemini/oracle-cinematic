/**
 * actionLabels — what Neoh did, said the way a person would say it.
 *
 * The backend speaks in tool names (`update_listing`, `add_client_note`) and
 * command states (`awaiting_approval`, `reconciliation_required`). Both were
 * printed as-is: Home's "Neoh handled" list read "update listing", and a
 * receipt said "Record updated" whatever had happened. This is the one table
 * that turns them into product language. It never invents an outcome — every
 * line maps a state the backend actually reported.
 */

// [noun, past-tense verb]. The pair, rather than a sentence, so one entry can
// say both "Note added" (a receipt) and "3 notes added" (a count).
const TOOL_PHRASES = Object.freeze({
  update_client: ['client', 'updated'],
  add_client_note: ['note', 'added'],
  set_client_stage: ['client stage', 'updated'],
  add_client_tag: ['tag', 'added'],
  score_client_lead: ['lead', 'scored'],
  assign_client: ['client', 'assigned'],
  archive_client: ['client', 'archived'],
  create_client: ['client', 'added'],
  create_deal_note: ['deal note', 'added'],
  update_listing: ['listing', 'updated'],
  move_deal_stage: ['deal', 'moved to a new stage'],
  draft_contract: ['contract', 'drafted'],
  generate_contract: ['contract', 'generated'],
  generate_assignment_agreement: ['assignment agreement', 'generated'],
  draft_email: ['email', 'drafted'],
  draft_sms: ['text', 'drafted'],
  call_contact: ['call', 'prepared'],
  schedule_event: ['calendar event', 'drafted'],
  generate_tour_link: ['tour link', 'created'],
  share_property_tour: ['tour', 'shared'],
  record_consent: ['consent record', 'saved'],
  add_consent: ['consent record', 'saved'],
  set_consent: ['consent record', 'updated'],
  delete_suppression: ['opt-out', 'removed'],
});

const RECORD_NOUNS = Object.freeze({
  client: 'Client', lead: 'Deal', listing: 'Listing', contract: 'Contract',
});

function sentence(text) {
  const s = String(text || '').trim();
  return s ? s[0].toUpperCase() + s.slice(1) : s;
}

function humanize(name) {
  return String(name || '').replace(/_/g, ' ').trim();
}

/** "Note added". For a receipt; falls back to the record it touched. */
export function receiptTitle(action) {
  const phrase = TOOL_PHRASES[action?.action_type];
  if (phrase) return sentence(`${phrase[0]} ${phrase[1]}`);
  const noun = RECORD_NOUNS[action?.record_type];
  if (noun) return `${noun} updated`;
  return 'Record updated';
}

/** "3 notes added" without the number: the count is rendered on its own. */
export function handledPhrase(tool, count = 1) {
  const phrase = TOOL_PHRASES[tool];
  if (!phrase) return humanize(tool);
  const [noun, verb] = phrase;
  return `${count === 1 ? noun : `${noun}s`} ${verb}`;
}

// ── Command receipts: outreach Neoh staged, and what became of it ─────────

const KIND = Object.freeze({
  SMS: 'Text', EMAIL: 'Email', CALL: 'Call', CALENDAR: 'Calendar event',
});

const DONE = Object.freeze({
  SMS: 'Text sent', EMAIL: 'Email sent', CALL: 'Call placed', CALENDAR: 'Added to calendar',
});

const DOING = Object.freeze({
  SMS: 'Sending text…', EMAIL: 'Sending email…', CALL: 'Placing call…', CALENDAR: 'Adding to calendar…',
});

/** Terminal states: nothing more will happen without a person. */
export const TERMINAL_COMMAND_STATES = Object.freeze(new Set([
  'succeeded', 'failed', 'cancelled', 'reconciliation_required',
]));

/**
 * A receipt for one row of command_executions.
 *
 * `tone` is never the only signal — every receipt carries its outcome in
 * words — and `review` marks the ones that are waiting on the person.
 * `reconciliation_required` is the state the provider drills produce when a
 * send may or may not have happened; it must read as "check this", never as
 * a clean success or a clean failure.
 */
export function commandReceipt(command) {
  const type = command?.command_type;
  const kind = KIND[type] || 'Request';
  const target = command?.target || {};
  const to = target.phone || target.email || '';
  const toLine = to ? `To ${to}. ` : '';
  switch (command?.state) {
    case 'draft':
    case 'awaiting_approval':
      return { tone: 'pending', title: `${kind} waiting for your approval`, detail: `${toLine}Nothing has been sent.`, review: true };
    case 'approved':
    case 'queued':
      return { tone: 'pending', title: `${kind} approved`, detail: `${toLine}Queued to go out.`, review: false };
    case 'executing':
      return { tone: 'pending', title: DOING[type] || 'In progress…', detail: toLine.trim(), review: false };
    case 'succeeded':
      return { tone: 'done', title: DONE[type] || 'Done', detail: toLine.trim(), review: false };
    case 'failed':
      return { tone: 'failed', title: `${kind} didn't go through`, detail: `${toLine}You can try again.`, review: false };
    case 'cancelled':
      return { tone: 'muted', title: `${kind} cancelled`, detail: `${toLine}Nothing was sent.`, review: false };
    case 'reconciliation_required':
      return {
        tone: 'review',
        title: `${kind} needs review`,
        detail: `${toLine}Neoh couldn't confirm whether it went out. Check before trying again.`,
        review: true,
      };
    default:
      return null;
  }
}

// Staged by the chat agent with idempotency key ai:{message_id}:{tool}:{anchor}
// (ai_tools_gated._stage). That key is the only link from a command back to
// the turn that produced it, and it is durable — so it is what joins them.
const AI_KEY = /^ai:([0-9a-f-]{36}):/i;

/** The assistant message a command was staged by, or null. */
export function commandMessageId(command) {
  const match = AI_KEY.exec(String(command?.idempotency_key || ''));
  return match ? match[1].toLowerCase() : null;
}

/** Map of assistant message id → its commands, oldest first. */
export function receiptsByMessage(commands) {
  const map = new Map();
  for (const command of commands || []) {
    const id = commandMessageId(command);
    if (!id) continue;
    if (!map.has(id)) map.set(id, []);
    map.get(id).push(command);
  }
  for (const list of map.values()) {
    list.sort((a, b) => String(a.created_at || '').localeCompare(String(b.created_at || '')));
  }
  return map;
}
