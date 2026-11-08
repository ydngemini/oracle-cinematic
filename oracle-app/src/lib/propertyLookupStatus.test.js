import { describe, expect, it } from 'vitest';
import { degradedLabel, propertyLookupView } from './propertyLookupStatus';

const withStatus = (entry, extra = {}) => ({
  property_candidates: [],
  source_status: { public_property_records: entry },
  ...extra,
});

describe('propertyLookupView (audit A02)', () => {
  it('a timed-out lookup is never shown as "no matches"', () => {
    const view = propertyLookupView(withStatus({
      status: 'timed_out', last_checked_at: '2026-10-01T09:00:00+00:00',
    }));
    expect(view.failed).toBe(true);
    expect(view.tone).toBe('warn');
    expect(view.text).toMatch(/timed out/);
    expect(view.text).toMatch(/not a "no matches"/);
    expect(view.text).toMatch(/2026-10-01/);
    expect(view.text).not.toMatch(/no exact owner-name match/);
  });

  it('an unavailable lookup says so and admits no successful check', () => {
    const view = propertyLookupView(withStatus({ status: 'unavailable', last_checked_at: null }));
    expect(view.failed).toBe(true);
    expect(view.text).toMatch(/unavailable/);
    expect(view.text).toMatch(/No successful check is on record/);
  });

  it('a checked lookup with no rows is a verified zero', () => {
    const view = propertyLookupView(withStatus({
      status: 'checked', matched: 0, checked_at: '2026-10-13T12:00:00+00:00',
    }));
    expect(view.failed).toBe(false);
    expect(view.text).toBe('Public records checked 2026-10-13: no exact owner-name match.');
  });

  it('no recorded status is "not checked yet", never a zero', () => {
    const view = propertyLookupView({ property_candidates: [], source_status: {} });
    expect(view.tone).toBe('muted');
    expect(view.text).toMatch(/not been checked/);
  });

  it('says nothing when candidates are shown or the lookup does not apply', () => {
    expect(propertyLookupView(withStatus({ status: 'checked' }, {
      property_candidates: [{ public_record_id: 'r1' }],
    }))).toBeNull();
    expect(propertyLookupView(withStatus({ status: 'not_applicable' }))).toBeNull();
  });
});

describe('degradedLabel', () => {
  it('names the model fallback, the lookup failure, or neither', () => {
    expect(degradedLabel({ last_error_code: 'MODEL_UNAVAILABLE_OR_INVALID' })).toBe('Rules only');
    expect(degradedLabel(withStatus({ status: 'timed_out' }, {
      last_error_code: 'PROPERTY_LOOKUP_TIMED_OUT',
    }))).toBe('Lookup failed');
    expect(degradedLabel({})).toBe('Partial');
  });

  it('names a pass the AI budget stopped, not a model failure or a bare "Partial"', () => {
    for (const code of ['AI_BUDGET_EXHAUSTED', 'AI_BUDGET_INSUFFICIENT', 'AI_SPEND_PAUSED', 'AI_SPEND_HALTED']) {
      expect(degradedLabel({ last_error_code: code })).toBe('Rules only: AI budget');
    }
  });
});
