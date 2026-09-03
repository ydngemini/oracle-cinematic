/**
 * entityModel — the pure half of the entity sheet.
 *
 * Neoh's read is one sentence with a confidence and, when the graph holds two
 * things that cannot both be true, the one question that resolves it. These
 * functions turn service responses into that shape so the sheet renders the
 * same way for a person, a property and a deal — and so the wording is pinned
 * by tests, not by whoever last touched the JSX.
 */

/** Below this the read is shown as "still forming", never as a verdict. */
export const MIN_READ_CONFIDENCE = 0.35;

const clamp01 = (n) => (Number.isFinite(n) ? Math.min(1, Math.max(0, n)) : 0);

/**
 * A person's read from `GET /api/clients/{id}/intent`. Latent intent is the
 * reconciliation of what they said and what they did; its summary is already
 * a sentence. Disputes ride along because "these two things cannot both be
 * true" is the most useful thing the sheet can say.
 */
export function personRead(intent) {
  if (!intent || typeof intent !== 'object') return null;
  const latent = intent.latent || {};
  const confidence = clamp01(Number(latent.confidence));
  const disputes = Array.isArray(intent.disputes) ? intent.disputes : [];
  const top = Array.isArray(intent.state_distribution) ? intent.state_distribution[0] : null;
  const sentence = latent.summary
    || (top ? `Most likely ${humanState(top.state)}.` : null);
  if (!sentence) return null;
  return {
    sentence,
    confidence,
    forming: confidence < MIN_READ_CONFIDENCE,
    meta: top && Number.isFinite(top.probability)
      ? `${humanState(top.state)} · ${Math.round(top.probability * 100)}%`
      : null,
    question: disputes[0]?.question || null,
    disputes: disputes.length,
    journey: intent.journey || null,
  };
}

/**
 * A deal's read is computed, not modelled: the earliest milestone that is not
 * done is what is holding it up. No milestones is a different sentence from
 * all-done — the first is a gap in the record, the second is a fact.
 */
export function dealRead(transaction, milestones) {
  const list = Array.isArray(milestones) ? milestones : [];
  const status = transaction?.status || null;
  if (status === 'closed') return { sentence: 'Closed.', confidence: 1, forming: false, meta: null, question: null };
  if (status === 'lost') {
    const why = transaction?.lost_reason_code ? ` (${humanState(transaction.lost_reason_code)})` : '';
    return { sentence: `Lost${why}.`, confidence: 1, forming: false, meta: null, question: null };
  }
  const open = list
    .filter((m) => !m.completed_at && m.status !== 'completed' && m.status !== 'skipped')
    .sort((a, b) => (a.due_at || '9999').localeCompare(b.due_at || '9999'));
  if (list.length === 0) {
    return {
      sentence: 'No milestones recorded yet, so Neoh cannot say what is next.',
      confidence: 0, forming: true, meta: null, question: 'What is the next step on this deal?',
    };
  }
  if (open.length === 0) {
    return { sentence: 'Every milestone is done.', confidence: 1, forming: false, meta: `${list.length} complete`, question: null };
  }
  const next = open[0];
  const title = (next.title || humanState(next.milestone_type) || 'Next milestone').trim();
  const due = next.due_at ? dueWord(next.due_at) : null;
  return {
    sentence: due ? `Waiting on ${title}, due ${due}.` : `Waiting on ${title}.`,
    confidence: 1,
    forming: false,
    meta: `${open.length} of ${list.length} open`,
    question: null,
    overdue: Boolean(next.due_at) && new Date(next.due_at).getTime() < Date.now(),
  };
}

/** The sheet title for each kind, from whatever has loaded so far. */
export function entityTitle(kind, data) {
  if (kind === 'person') return data?.full_name || data?.client_name || 'Person';
  if (kind === 'property') return data?.address || data?.property_address || 'Property';
  if (kind === 'deal') return data?.property_address || data?.address || 'Deal';
  return 'Record';
}

export function humanState(value) {
  if (!value) return '';
  return String(value).replace(/_/g, ' ');
}

function dueWord(iso, now = new Date()) {
  const due = new Date(iso);
  if (Number.isNaN(due.getTime())) return null;
  const days = Math.round((due.getTime() - now.getTime()) / 86_400_000);
  if (days === 0) return 'today';
  if (days === 1) return 'tomorrow';
  if (days === -1) return 'yesterday';
  if (days < 0) return `${-days} days ago`;
  if (days < 7) return `in ${days} days`;
  return due.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}
