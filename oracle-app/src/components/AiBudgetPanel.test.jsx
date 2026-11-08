// @vitest-environment jsdom
/**
 * The brokerage's AI budget, where its owner can see and change it (T18.07).
 *
 * Pins: an agent (403) sees nothing; an unreadable budget says so and never
 * reads as "not limited"; no cap is shown as no cap; the state is named; a
 * save sends all three values, with an empty cap meaning no cap; and the
 * panel says what the number counts and what it does not.
 */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const api = vi.hoisted(() => ({ crmGet: vi.fn(), crmPut: vi.fn() }));
vi.mock('../state/useCrmApi', () => api);

const { AiBudgetPanel } = await import('./AiBudgetPanel');

const PATH = '/api/billing/budgets/ai-tokens';
const UNLIMITED = {
  channel: 'ai_tokens', unit: 'tokens',
  period: { start: '2026-10-01T00:00:00+00:00', end: '2026-11-01T00:00:00+00:00', basis: 'calendar month, UTC' },
  used: { prompt: 120000, completion: 30000, total: 150000 },
  reserved: 0, cap: null, warn_pct: 80, paused: false, platform_halted: false, state: 'unlimited',
  counts: 'Hosted model calls made by Neoh chat turns.',
  not_counted: ['local-model calls', 'phone-call voice replies'],
  evidence_status: 'observed',
};

function failure(status) {
  return Object.assign(new Error('nope'), { status, isUnavailable: status === 503 });
}

beforeEach(() => {
  api.crmGet.mockReset();
  api.crmPut.mockReset();
  api.crmGet.mockResolvedValue(UNLIMITED);
});
afterEach(cleanup);

describe('AiBudgetPanel', () => {
  it('renders nothing for an agent', async () => {
    api.crmGet.mockRejectedValue(failure(403));
    const { container } = render(<AiBudgetPanel />);
    await waitFor(() => expect(api.crmGet).toHaveBeenCalledWith(PATH));
    await waitFor(() => expect(container.innerHTML).toBe(''));
  });

  it('says an unreadable budget cannot be read, and never that it is not limited', async () => {
    api.crmGet.mockRejectedValue(failure(503));
    render(<AiBudgetPanel />);
    expect(await screen.findByText(/can.t be read right now/)).toBeTruthy();
    expect(screen.queryByText(/not limited/i)).toBeNull();
    expect(screen.queryByText(/No cap/)).toBeNull();
    expect(screen.queryByRole('button', { name: 'Save budget' })).toBeNull();
    // The panel cannot know what admission decides: no claim about Neoh's answers.
    expect(screen.queryByText(/Neoh holds its answers/)).toBeNull();
  });

  it.each([[401], [404], [500], [undefined]])('a %s failure is a load error, not an unreadable budget', async (status) => {
    api.crmGet.mockRejectedValue(failure(status));
    render(<AiBudgetPanel />);
    expect(await screen.findByText(/didn.t load/)).toBeTruthy();
    expect(screen.queryByText(/can.t be read right now/)).toBeNull();
    expect(screen.queryByText(/Neoh holds/)).toBeNull();
  });

  it('names too little left for a request, with the estimate it is judged by', async () => {
    api.crmGet.mockResolvedValue({
      ...UNLIMITED, cap: 160000, state: 'insufficient', smallest_request_estimate: 13684,
    });
    render(<AiBudgetPanel />);
    expect(await screen.findByText('Too little left for a request')).toBeTruthy();
    expect(screen.getByText(/smallest Neoh request holds \(about 13,684 tokens, an estimate\)/)).toBeTruthy();
  });

  it('a committed save whose usage cannot be read says saved, and shows no usage numbers', async () => {
    api.crmPut.mockResolvedValue({
      channel: 'ai_tokens', unit: 'tokens', saved: true, cap: 500000, warn_pct: 80, paused: true,
      used: null, reserved: null, state: 'unknown', counts: UNLIMITED.counts,
      not_counted: UNLIMITED.not_counted, evidence_status: 'unavailable',
    });
    render(<AiBudgetPanel />);
    fireEvent.change(await screen.findByLabelText(/Monthly cap/), { target: { value: '500000' } });
    fireEvent.click(screen.getByLabelText(/Pause Neoh/));
    fireEvent.click(screen.getByRole('button', { name: 'Save budget' }));
    expect(await screen.findByText(/Saved\. This month.s usage can.t be read right now/)).toBeTruthy();
    expect(screen.queryByText(/Nothing was changed/)).toBeNull();
    expect(screen.queryByText(/so far: 0 tokens/)).toBeNull();
    expect(screen.getByText(/Cap: 500,000 tokens a month/)).toBeTruthy();
    expect(screen.getByLabelText(/Pause Neoh/).checked).toBe(true);
  });

  it('shows no cap as no cap, the month so far, and what is and is not counted', async () => {
    render(<AiBudgetPanel />);
    expect(await screen.findByText('No cap set — not limited')).toBeTruthy();
    expect(screen.getByText(/150,000 tokens/)).toBeTruthy();
    expect(screen.getByText(/120,000 prompt · 30,000 completion/)).toBeTruthy();
    expect(screen.getByText(/Hosted model calls made by Neoh chat turns/)).toBeTruthy();
    expect(screen.getByText(/local-model calls, phone-call voice replies/)).toBeTruthy();
    expect(screen.getByText(/Tokens, not money/)).toBeTruthy();
  });

  it('names the state: near the cap, and stopped at it', async () => {
    api.crmGet.mockResolvedValue({ ...UNLIMITED, cap: 160000, state: 'warning' });
    const view = render(<AiBudgetPanel />);
    expect(await screen.findByText('Near the cap')).toBeTruthy();
    expect(screen.getByText(/160,000 tokens a month/)).toBeTruthy();
    view.unmount();
    api.crmGet.mockResolvedValue({ ...UNLIMITED, cap: 150000, state: 'exhausted' });
    render(<AiBudgetPanel />);
    expect(await screen.findByText('Cap reached — Neoh is stopped')).toBeTruthy();
  });

  it('saves all three values, and an empty cap is no cap', async () => {
    api.crmPut.mockResolvedValue({ ...UNLIMITED, cap: 500000, warn_pct: 70, paused: true, state: 'paused' });
    render(<AiBudgetPanel />);
    fireEvent.change(await screen.findByLabelText(/Monthly cap/), { target: { value: '500000' } });
    fireEvent.change(screen.getByLabelText(/Warn at/), { target: { value: '70' } });
    fireEvent.click(screen.getByLabelText(/Pause Neoh/));
    fireEvent.click(screen.getByRole('button', { name: 'Save budget' }));
    await waitFor(() => expect(api.crmPut).toHaveBeenCalledWith(PATH, {
      monthly_cap_tokens: 500000, warn_pct: 70, paused: true,
    }));
    expect(await screen.findByText('Paused')).toBeTruthy();

    fireEvent.change(screen.getByLabelText(/Monthly cap/), { target: { value: '' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save budget' }));
    await waitFor(() => expect(api.crmPut).toHaveBeenLastCalledWith(PATH, {
      monthly_cap_tokens: null, warn_pct: 70, paused: true,
    }));
  });

  it.each([[401], [422], [429]])('a %s refusal of the save says nothing was changed', async (status) => {
    api.crmPut.mockRejectedValue(failure(status));
    render(<AiBudgetPanel />);
    fireEvent.change(await screen.findByLabelText(/Monthly cap/), { target: { value: '500000' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save budget' }));
    expect(await screen.findByText(/Nothing was changed/)).toBeTruthy();
    expect(api.crmGet).toHaveBeenCalledTimes(1);
  });

  it('a 403 save says only an owner can change it', async () => {
    api.crmPut.mockRejectedValue(failure(403));
    render(<AiBudgetPanel />);
    fireEvent.click(await screen.findByRole('button', { name: 'Save budget' }));
    expect(await screen.findByText(/Only a broker owner can change the AI budget/)).toBeTruthy();
  });

  // The server may have committed before the answer was lost (a dropped
  // connection, or an edge 502/504 in front of a committed write): the panel
  // cannot know, so it never says nothing changed, and re-reads what is in force.
  it.each([[0], [500], [502], [503], [504], [undefined]])(
    'a %s save failure says the outcome is unknown and re-reads the budget', async (status) => {
      api.crmPut.mockRejectedValue(failure(status));
      api.crmGet
        .mockResolvedValueOnce(UNLIMITED)
        .mockResolvedValueOnce({ ...UNLIMITED, cap: 500000, state: 'ok' });
      render(<AiBudgetPanel />);
      fireEvent.change(await screen.findByLabelText(/Monthly cap/), { target: { value: '500000' } });
      fireEvent.click(screen.getByRole('button', { name: 'Save budget' }));
      expect(await screen.findByText(/couldn.t confirm whether the budget was saved/i)).toBeTruthy();
      expect(screen.queryByText(/Nothing was changed/)).toBeNull();
      await waitFor(() => expect(api.crmGet).toHaveBeenCalledTimes(2));
      expect(await screen.findByText(/Cap: 500,000 tokens a month/)).toBeTruthy();
      expect(screen.getByText(/shown here is what is in force now/)).toBeTruthy();
    });

  it('an unknown save outcome whose re-read also fails says so, and claims nothing', async () => {
    api.crmPut.mockRejectedValue(failure(504));
    api.crmGet.mockResolvedValueOnce(UNLIMITED).mockRejectedValueOnce(failure(503));
    render(<AiBudgetPanel />);
    fireEvent.click(await screen.findByRole('button', { name: 'Save budget' }));
    expect(await screen.findByText(/couldn.t confirm whether the budget was saved/i)).toBeTruthy();
    expect(await screen.findByText(/Reload the page to see what is in force/)).toBeTruthy();
    expect(screen.queryByText(/Nothing was changed/)).toBeNull();
  });

  it('refuses a cap that is not a whole number of tokens without sending it', async () => {
    render(<AiBudgetPanel />);
    fireEvent.change(await screen.findByLabelText(/Monthly cap/), { target: { value: '12.5' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save budget' }));
    expect(await screen.findByRole('alert')).toBeTruthy();
    expect(api.crmPut).not.toHaveBeenCalled();
  });
});
