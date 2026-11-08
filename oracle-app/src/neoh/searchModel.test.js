import { describe, expect, it } from 'vitest';

import {
  MIN_QUERY,
  SEARCH_KINDS,
  degradedMessage,
  emptyMessage,
  failedKinds,
  groupHits,
  isSearchKind,
} from './searchModel';

const hit = (kind, id) => ({ kind, id, label: id, sublabel: null, href: `/${kind}/${id}`, score: 0.5 });

describe('kinds', () => {
  it('searches exactly the four kinds the API defaults to', () => {
    expect(SEARCH_KINDS.map((k) => k.id)).toEqual(['people', 'properties', 'deals', 'conversations']);
    expect(isSearchKind('deals')).toBe(true);
    expect(isSearchKind('opportunities')).toBe(false);
    expect(isSearchKind('records')).toBe(false);
  });
});

describe('groupHits', () => {
  it('groups in chip order and drops empty groups', () => {
    const groups = groupHits([hit('deals', 'd1'), hit('people', 'p1'), hit('deals', 'd2')]);
    expect(groups.map((g) => g.kind)).toEqual(['people', 'deals']);
    expect(groups[1].hits.map((h) => h.id)).toEqual(['d1', 'd2']);
  });

  it('puts the selected kind first so the chip and the list agree', () => {
    const groups = groupHits([hit('people', 'p'), hit('deals', 'd')], 'deals');
    expect(groups.map((g) => g.kind)).toEqual(['deals', 'people']);
  });

  it('keeps an unknown kind rather than losing its hits', () => {
    const groups = groupHits([hit('records', 'r'), hit('mystery', 'm')]);
    expect(groups.map((g) => g.kind)).toEqual(['records', 'mystery']);
    expect(groups[0].label).toBe('Public records');
  });

  it('survives an empty or missing list', () => {
    expect(groupHits(null)).toEqual([]);
    expect(groupHits([])).toEqual([]);
  });
});

describe('empty and degraded messages', () => {
  it('says nothing below the minimum query', () => {
    expect(emptyMessage('s', { degraded: [] })).toBeNull();
    expect(MIN_QUERY).toBe(2);
  });

  it('distinguishes no-match from a failed leg', () => {
    expect(emptyMessage('sarah', { degraded: [] })).toBe('Nothing matches “sarah”.');
    expect(emptyMessage('sarah', { degraded: ['deals'] })).toMatch(/deals could not be searched/);
    expect(emptyMessage('sarah', { degraded: ['deals'] })).toMatch(/may be incomplete/);
  });

  it('shows the degraded banner even when there are results', () => {
    expect(degradedMessage({ degraded: [] })).toBeNull();
    expect(degradedMessage({ degraded: ['records', 'deals'] }))
      .toBe('Public records, deals could not be searched just now. Results below are from the other kinds.');
  });
});

describe('failed legs come from the per-leg C6 status (T06.06)', () => {
  const at = '2026-10-16T00:00:00Z';
  const response = {
    counts: { people: 0, deals: 0, conversations: 0, properties: 0 },
    degraded: [],
    source_status: {
      people: { status: 'checked', matched: 0, checked_at: at, elapsed_ms: 12 },
      deals: { status: 'timed_out', reason: 'upstream_timeout', elapsed_ms: 3000 },
      conversations: { status: 'unavailable', reason: 'upstream_error', elapsed_ms: 40 },
    },
  };

  it('a timed-out or unavailable leg is failed; a verified zero is not', () => {
    expect(failedKinds(response)).toEqual(['deals', 'conversations', 'properties']);
  });

  it('a leg with no status at all is failed, never a verified miss', () => {
    expect(failedKinds(response)).toContain('properties');
    expect(emptyMessage('sarah', response)).toMatch(/properties could not be searched/);
  });

  it('a too-short query (not_applicable) is not failed', () => {
    expect(failedKinds({ counts: { people: 0 }, source_status: { people: { status: 'not_applicable' } } }))
      .toEqual([]);
  });

  it('still honours the bare list from an older API or a failed request', () => {
    expect(failedKinds({ degraded: ['people', 'deals'] })).toEqual(['people', 'deals']);
    expect(failedKinds(undefined)).toEqual([]);
  });
});
