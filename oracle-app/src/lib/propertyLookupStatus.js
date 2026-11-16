/**
 * What the client steward's public-record property lookup actually did.
 *
 * The backend records one status per source (CONTRACT C6: checked,
 * timed_out, unavailable, not_applicable) in `automation.source_status`
 * (migration 0135). An empty candidate list only means "no matches" when the
 * source was `checked`; after a timeout or an error it means "not checked",
 * and the drawer must say so (audit A02, ledger L11-02).
 */

export const PROPERTY_SOURCE = 'public_property_records';

function shortDate(iso) {
  if (typeof iso !== 'string' || iso.length < 10) return null;
  return iso.slice(0, 10);
}

/**
 * @returns {{ tone: 'ok'|'warn'|'muted', failed: boolean, text: string } | null}
 *   null when there is nothing worth saying (candidates are shown, or the
 *   lookup does not apply to this client).
 */
export function propertyLookupView(automation) {
  const candidates = Array.isArray(automation?.property_candidates) ? automation.property_candidates : [];
  const entry = automation?.source_status?.[PROPERTY_SOURCE];
  const status = entry && typeof entry === 'object' ? entry.status : undefined;
  const last = shortDate(entry?.last_checked_at);

  if (status === 'timed_out' || status === 'unavailable') {
    return {
      tone: 'warn',
      failed: true,
      text: `Public-record lookup ${status === 'timed_out' ? 'timed out' : 'was unavailable'} on the last pass, `
        + 'so property candidates were not checked. This is not a "no matches" result; it retries on the next pass.'
        + (last ? ` Last successful check: ${last}.` : ' No successful check is on record.'),
    };
  }
  if (candidates.length > 0 || status === 'not_applicable') return null;
  if (status === 'checked') {
    const when = shortDate(entry?.checked_at) || last;
    return {
      tone: 'ok',
      failed: false,
      text: `Public records checked${when ? ` ${when}` : ''}: no exact owner-name match.`,
    };
  }
  // No status recorded (not reconciled yet, or a pass from before 0135).
  return {
    tone: 'muted',
    failed: false,
    text: 'Public records have not been checked for this client yet.',
  };
}

// The producer (client_ai_automation._property_candidates) stores "exact owner
// name" but compares the name lower-cased with everything outside a-z and 0-9
// removed, so the drawer says what the comparison is and that nobody has
// confirmed it (F11.04). The stored words are unchanged: Neoh's renderer keys
// on them.
const OWNER_BASIS = 'owner name, ignoring case, spaces, punctuation and letters outside a-z';
const BASIS_TEXT = {
  'exact owner name': OWNER_BASIS,
  'exact owner name and target ZIP': `${OWNER_BASIS}, in the target ZIP`,
};

/** "Possible match: ..." for a review-only candidate's stored match_basis. */
export function candidateBasisView(basis) {
  const recorded = typeof basis === 'string' ? basis.trim() : '';
  if (!recorded) return 'Possible match';
  return `Possible match: ${BASIS_TEXT[recorded] || recorded}`;
}

// The brokerage's AI budget (or the platform switch) stopped the AI step; the
// pass reconciled recorded facts only and retries once there is budget.
const AI_BUDGET_CODES = new Set([
  'AI_BUDGET_EXHAUSTED', 'AI_BUDGET_INSUFFICIENT', 'AI_SPEND_PAUSED', 'AI_SPEND_HALTED',
]);

/** The header label for a degraded pass names what degraded it. */
export function degradedLabel(automation) {
  if (automation?.last_error_code === 'MODEL_UNAVAILABLE_OR_INVALID') return 'Rules only';
  if (AI_BUDGET_CODES.has(automation?.last_error_code)) return 'Rules only: AI budget';
  if (propertyLookupView(automation)?.failed) return 'Lookup failed';
  return 'Partial';
}
