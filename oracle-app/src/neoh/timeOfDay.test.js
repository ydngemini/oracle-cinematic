import { describe, expect, it } from 'vitest';

import { HOME_ITEM_LIMIT, arrange, imminentItem, modeFor } from './timeOfDay';

/**
 * Home rearranges around the hour but never changes what exists. Every test
 * here pins one of those two halves: the arrangement rules, and the promise
 * that nothing is hidden.
 */

const at = (h, m = 0) => new Date(2026, 8, 2, h, m);
const opp = (id, score, deadline = null) => ({ kind: 'k', subject_id: id, score, deadline });

function briefing(opportunities, horizon = []) {
  return { attention: { opportunities }, horizon };
}

describe('modeFor', () => {
  it('reads the clock when nothing is imminent', () => {
    expect(modeFor(at(8))).toBe('morning');
    expect(modeFor(at(10, 59))).toBe('morning');
    expect(modeFor(at(11))).toBe('default');
    expect(modeFor(at(14))).toBe('default');
    expect(modeFor(at(17))).toBe('evening');
    expect(modeFor(at(22))).toBe('evening');
  });

  it('an imminent dated item overrides the clock at any hour', () => {
    const now = at(21);
    const horizon = [{ key: 'today', items: [opp('a', 0.5, at(21, 45).toISOString())] }];
    expect(modeFor(now, horizon)).toBe('pre_appointment');
  });
});

describe('imminentItem', () => {
  it('ignores items outside the window, in the past, or in later buckets', () => {
    const now = at(9);
    const horizon = [
      { key: 'today', items: [opp('late', 0.9, at(12).toISOString())] },
      { key: 'now', items: [opp('past', 0.9, at(8).toISOString())] },
      { key: 'this_week', items: [opp('week', 0.9, at(9, 30).toISOString())] },
    ];
    expect(imminentItem(now, horizon)).toBeNull();
  });

  it('picks the nearest of several', () => {
    const now = at(9);
    const horizon = [{
      key: 'today',
      items: [opp('b', 0.1, at(10, 15).toISOString()), opp('a', 0.1, at(9, 20).toISOString())],
    }];
    expect(imminentItem(now, horizon).subject_id).toBe('a');
  });

  it('tolerates a missing or unparseable deadline', () => {
    expect(imminentItem(at(9), [{ key: 'now', items: [opp('x', 1), { kind: 'k', deadline: 'soon' }] }])).toBeNull();
  });
});

describe('arrange', () => {
  it('never drops an opportunity — it only orders and counts the rest', () => {
    const five = ['a', 'b', 'c', 'd', 'e'].map((id, i) => opp(id, 1 - i * 0.1));
    const out = arrange(briefing(five), at(14));
    expect(out.items).toHaveLength(HOME_ITEM_LIMIT);
    expect(out.remaining).toBe(five.length - HOME_ITEM_LIMIT);
  });

  it('morning leads with deadlines, then score', () => {
    const items = [opp('score', 0.95), opp('dated', 0.3, at(15).toISOString())];
    const out = arrange(briefing(items), at(8));
    expect(out.mode).toBe('morning');
    expect(out.items[0].subject_id).toBe('dated');
  });

  it('afternoon orders by score alone', () => {
    const items = [opp('dated', 0.3, at(15).toISOString()), opp('score', 0.95)];
    const out = arrange(briefing(items), at(14));
    expect(out.items[0].subject_id).toBe('score');
  });

  it('pre-appointment puts the imminent item first and collapses the rest', () => {
    const lead = opp('lead', 0.2, at(9, 40).toISOString());
    const items = [opp('big', 0.99), lead];
    const horizon = [{ key: 'today', items: [lead] }];
    const out = arrange(briefing(items, horizon), at(9));
    expect(out.mode).toBe('pre_appointment');
    expect(out.items[0].subject_id).toBe('lead');
    expect(out.items).toHaveLength(2);
    expect(out.collapseRest).toBe(true);
  });

  it('evening is a record, not a to-do list', () => {
    const out = arrange(briefing([opp('a', 0.5)]), at(20));
    expect(out.mode).toBe('evening');
    expect(out.handledExpanded).toBe(true);
    expect(out.showDecisions).toBe(false);
    expect(out.items).toHaveLength(1);
  });

  it('survives an empty or missing briefing', () => {
    expect(arrange(null, at(9)).items).toEqual([]);
    expect(arrange({}, at(9)).remaining).toBe(0);
  });
});
