/**
 * missionModel — the pure half of the mission builder.
 *
 * The consent sentences live here, in one place, because they are the record.
 * When an agent ticks "let Neoh send texts", the sentence beside that box is
 * what gets written to the database verbatim and what an audit reads a year
 * later. Generating it server-side from a template would mean the stored
 * record and the words on screen could drift; composing it here and sending it
 * with the request means they cannot.
 */

/** What a mission can be aimed at. */
export const OBJECTIVES = Object.freeze([
  { id: 'listings_won', label: 'Win listings' },
  { id: 'buyers_converted', label: 'Convert buyers' },
  { id: 'appointments_set', label: 'Set appointments' },
  { id: 'database_reactivated', label: 'Reactivate the database' },
  { id: 'sphere_touched', label: 'Stay in touch with my sphere' },
  { id: 'deals_saved', label: 'Save deals at risk' },
]);

export const CHANNELS = Object.freeze([
  { id: 'sms', label: 'Text' },
  { id: 'email', label: 'Email' },
  { id: 'voice', label: 'Call' },
  { id: 'task', label: 'Task for me' },
]);

/** Channels that can carry an autopilot grant. A task reaches nobody. */
export const AUTOPILOT_CHANNELS = Object.freeze(['sms', 'email', 'voice']);

/**
 * The sentence for each channel. First person, present tense, and specific
 * about the two things that actually matter: it goes under the agent's own
 * licence, and it cannot be recalled.
 */
export const CONSENT_SENTENCES = Object.freeze({
  sms: 'I authorise Neoh to send text messages for this mission without '
    + 'showing me each one first. They are sent under my licence, they cannot '
    + 'be un-sent, and I can revoke this by pausing the mission.',
  email: 'I authorise Neoh to send emails for this mission without showing me '
    + 'each one first. They are sent under my licence, they cannot be '
    + 'un-sent, and I can revoke this by pausing the mission.',
  voice: 'I authorise Neoh to place AI voice calls for this mission without '
    + 'showing me each one first. They are placed under my licence, they '
    + 'cannot be un-made, and I can revoke this by pausing the mission.',
});

/**
 * The caveat that must sit next to the calls checkbox.
 *
 * This is not a disclaimer. An agent who ticks "calls" reasonably expects
 * calls to happen, and mostly they will not: an AI voice call needs express
 * WRITTEN consent from that specific contact under FCC 24-17, which almost
 * nobody in a normal database has given. Saying so here prevents a mission
 * that looks live and does nothing, which is the version of this that erodes
 * trust fastest.
 */
export const VOICE_CAVEAT =
  'Even with this on, Neoh will only call contacts who have given express '
  + 'written consent to AI voice calls. Everyone else is skipped with the '
  + 'reason recorded — so expect this to reach far fewer people than texts.';

/** Compose exactly what will be stored. Order is stable so it is diffable. */
export function consentText(autoChannels) {
  const chosen = AUTOPILOT_CHANNELS.filter((c) => (autoChannels || []).includes(c));
  if (chosen.length === 0) return '';
  return chosen.map((c) => CONSENT_SENTENCES[c]).join('\n\n');
}

/** Whether the form can be submitted, and if not, the one thing to fix. */
export function validate(draft) {
  const objective = (draft.objectiveText || '').trim();
  if (!objective) return { ok: false, problem: 'Say what you want to happen.' };
  if (!(draft.allowedChannels || []).length) {
    return { ok: false, problem: 'Choose at least one way for Neoh to reach people.' };
  }
  const stray = (draft.autoChannels || []).filter(
    (c) => !(draft.allowedChannels || []).includes(c));
  if (stray.length) {
    return {
      ok: false,
      problem: `Autopilot is on for ${stray.join(', ')}, which this mission is not allowed to use.`,
    };
  }
  return { ok: true, problem: null };
}

/** The request body. `consent_text` is composed here, never server-side. */
export function toRequest(draft) {
  const auto = AUTOPILOT_CHANNELS.filter((c) => (draft.autoChannels || []).includes(c));
  return {
    objective_kind: draft.objectiveKind,
    objective_text: (draft.objectiveText || '').trim(),
    target_count: draft.targetCount ? Number(draft.targetCount) : null,
    deadline: draft.deadline || null,
    budget_cents: Math.round(Number(draft.budgetDollars || 0) * 100),
    allowed_channels: (draft.allowedChannels || []).slice().sort(),
    auto_channels: auto.slice().sort(),
    consent_text: consentText(auto) || null,
  };
}

/** How a mission's state reads in one line. */
export function statusLine(mission) {
  if (!mission) return '';
  const auto = (mission.auto_channels || []).length;
  const where = mission.mode === 'live' ? 'live' : 'shadow — recording, not sending';
  if (mission.status === 'paused') return 'Paused.';
  if (mission.status === 'draft') return 'Draft — not started.';
  if (mission.status === 'simulated') return 'Simulated. Not launched.';
  return auto
    ? `${where}, sending ${mission.auto_channels.join(' and ')} on its own.`
    : `${where}, everything waits for your approval.`;
}
