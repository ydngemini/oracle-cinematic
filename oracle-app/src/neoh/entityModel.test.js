import { describe, expect, it } from 'vitest';

import { MIN_READ_CONFIDENCE, dealRead, entityTitle, personRead } from './entityModel';

describe('personRead', () => {
  it('uses the latent sentence, the top state, and the first dispute question', () => {
    const read = personRead({
      journey: 'buyer',
      latent: { summary: 'Says browsing, acts ready.', confidence: 0.6 },
      state_distribution: [{ state: 'active_search', probability: 0.42 }],
      disputes: [{ question: 'Is the budget 400k or 550k?' }, { question: 'second' }],
    });
    expect(read.sentence).toBe('Says browsing, acts ready.');
    expect(read.confidence).toBe(0.6);
    expect(read.forming).toBe(false);
    expect(read.meta).toBe('active search · 42%');
    expect(read.question).toBe('Is the budget 400k or 550k?');
    expect(read.disputes).toBe(2);
  });

  it('marks a low-confidence read as still forming rather than a verdict', () => {
    const read = personRead({ latent: { summary: 'Too early to say.', confidence: 0.2 }, disputes: [] });
    expect(read.forming).toBe(true);
    expect(MIN_READ_CONFIDENCE).toBeGreaterThan(0.2);
  });

  it('returns null when there is nothing to say', () => {
    expect(personRead(null)).toBeNull();
    expect(personRead({ latent: {}, state_distribution: [] })).toBeNull();
  });
});

describe('dealRead', () => {
  const soon = new Date(Date.now() + 2 * 86_400_000).toISOString();
  const late = new Date(Date.now() - 3 * 86_400_000).toISOString();

  it('names the earliest open milestone and counts the rest', () => {
    const read = dealRead({ status: 'under_contract' }, [
      { title: 'Appraisal', due_at: soon, completed_at: null },
      { title: 'Inspection', due_at: late, completed_at: null },
      { title: 'Offer accepted', due_at: late, completed_at: late },
    ]);
    expect(read.sentence).toBe('Waiting on Inspection, due 3 days ago.');
    expect(read.meta).toBe('2 of 3 open');
    expect(read.overdue).toBe(true);
  });

  it('distinguishes no milestones from all done', () => {
    expect(dealRead({ status: 'active' }, []).forming).toBe(true);
    expect(dealRead({ status: 'active' }, []).question).toMatch(/next step/);
    const done = dealRead({ status: 'active' }, [{ title: 'x', completed_at: late }]);
    expect(done.sentence).toBe('Every milestone is done.');
    expect(done.forming).toBe(false);
  });

  it('says closed and lost plainly, with the lost reason when recorded', () => {
    expect(dealRead({ status: 'closed' }, []).sentence).toBe('Closed.');
    expect(dealRead({ status: 'lost', lost_reason_code: 'competing_offer' }, []).sentence)
      .toBe('Lost (competing offer).');
  });
});

describe('entityTitle', () => {
  it('falls back to the kind name until data arrives', () => {
    expect(entityTitle('person', null)).toBe('Person');
    expect(entityTitle('person', { full_name: 'Sarah Chen' })).toBe('Sarah Chen');
    expect(entityTitle('deal', { property_address: '12 Main' })).toBe('12 Main');
  });
});
