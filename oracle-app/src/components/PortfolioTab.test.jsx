// @vitest-environment jsdom
/**
 * A broker owner confirms or dismisses a title finding from the portfolio's
 * flag list (T11.03, F11.04). Until this, nothing wrote a finding's review, so
 * every matched finding read "Not verified" to the homeowner and none could be
 * dismissed. The decision needs a reason; an agent sees the state, not the
 * buttons, because the server answers them 403. The reviewer sees the record
 * and the submitter's match claim, because confirming a possible match leaves
 * the homeowner's "Possible match" label in place.
 */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const crmGet = vi.fn();
const crmPost = vi.fn();
vi.mock('../state/useCrmApi', () => ({
  crmGet: (...a) => crmGet(...a),
  crmPost: (...a) => crmPost(...a),
}));
vi.mock('./AssistantContext', () => ({
  useAssistant: () => ({ requestCommand: vi.fn(), setOpen: vi.fn() }),
}));

const { default: PortfolioTab } = await import('./PortfolioTab');

const TITLE_FLAG = {
  type: 'TITLE',
  property_key: 'DE-NCC-0142',
  label: 'lien',
  review_status: 'required',
  finding_id: 'f-1',
  match_status: 'matched',
  record_id: '2024-001234',
  amount: 12500,
  recorded_at: '2024-03-01',
  released_at: null,
};
const ZONING_FLAG = { type: 'ZONING', property_key: 'DE-NCC-0142', label: 'R-2', review_status: 'required' };
const summary = (flags) => ({
  metrics: {},
  ghosting_clients: [],
  activity_pulse: [],
  milestone_breakdown: {},
  intelligence_flags: flags,
});
const SUMMARY = summary([TITLE_FLAG, ZONING_FLAG]);
// The reload after a decision: the server lists only findings awaiting review.
const DECIDED_SUMMARY = summary([ZONING_FLAG]);
const REASON = 'Judgment names a different owner at another address';

beforeEach(() => {
  crmGet.mockReset();
  crmPost.mockReset();
  crmGet.mockResolvedValue(SUMMARY);
});
afterEach(() => {
  cleanup();
  sessionStorage.clear();
  vi.restoreAllMocks();
});

describe('Title finding review', () => {
  it('a broker owner dismisses one finding with a reason, and the outcome outlives the reload', async () => {
    sessionStorage.setItem('oracle_role', 'broker_owner');
    crmGet.mockResolvedValueOnce(SUMMARY).mockResolvedValue(DECIDED_SUMMARY);
    crmPost.mockResolvedValue({ id: 'f-1', review_status: 'dismissed' });
    render(<PortfolioTab />);
    await screen.findByText('Awaiting review');
    // One finding, one pair of actions: zoning flags are not title findings.
    expect(screen.getAllByRole('button', { name: 'Confirm' })).toHaveLength(1);

    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }));
    expect(screen.getByText(/will no longer list it/)).toBeTruthy();
    const submit = screen.getByRole('button', { name: 'Dismiss finding' });
    const reason = screen.getByLabelText(/Why this record does not apply/);
    fireEvent.change(reason, { target: { value: 'nope' } });
    expect(submit.disabled).toBe(true);
    fireEvent.change(reason, { target: { value: `  ${REASON}  ` } });
    expect(submit.disabled).toBe(false);
    fireEvent.click(submit);

    await waitFor(() => expect(crmPost).toHaveBeenCalledWith(
      '/api/intelligence/title/findings/f-1/review',
      { decision: 'dismissed', reason: REASON },
    ));
    await waitFor(() => expect(crmGet).toHaveBeenCalledTimes(2));
    // The reloaded list no longer has the finding; the outcome is still said.
    await waitFor(() => expect(screen.queryByText('Awaiting review')).toBeNull());
    expect(screen.getByText(/lien on DE-NCC-0142/).closest('[role="status"]').textContent).toContain('Dismissed');
    expect(screen.queryByRole('button', { name: 'Confirm' })).toBeNull();
  });

  it('the reviewer sees the record and the match claim they decide on', async () => {
    sessionStorage.setItem('oracle_role', 'agent');
    render(<PortfolioTab />);
    const evidence = await screen.findByText(/Record 2024-001234/);
    expect(evidence.textContent).toBe('Matched (not verified) · Record 2024-001234 · $12,500 · Recorded 2024-03-01');
  });

  it('confirming a matched record says the homeowner will see it without the "Not verified" label', async () => {
    sessionStorage.setItem('oracle_role', 'platform_admin');
    render(<PortfolioTab />);
    fireEvent.click(await screen.findByRole('button', { name: 'Confirm' }));
    expect(screen.getByLabelText(/Why this record belongs to this property/)).toBeTruthy();
    expect(screen.getByText(/without the .Not verified. label/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(screen.getByRole('button', { name: 'Confirm' })).toBeTruthy();
    expect(crmPost).not.toHaveBeenCalled();
  });

  it.each(['possible_match', 'unresolved'])(
    'confirming a %s says the homeowner still sees "Possible match"',
    async (matchStatus) => {
      sessionStorage.setItem('oracle_role', 'broker_owner');
      crmGet.mockResolvedValue(summary([{ ...TITLE_FLAG, match_status: matchStatus }]));
      render(<PortfolioTab />);
      expect((await screen.findByText(/Record 2024-001234/)).textContent).toMatch(/^(Possible|Unresolved) match · /);
      fireEvent.click(screen.getByRole('button', { name: 'Confirm' }));
      expect(screen.getByText(/keeps showing it as a .Possible match./)).toBeTruthy();
      expect(screen.queryByText(/without the .Not verified. label/)).toBeNull();
    },
  );

  it('an agent sees the review state but no decision buttons', async () => {
    sessionStorage.setItem('oracle_role', 'agent');
    render(<PortfolioTab />);
    await screen.findByText('Awaiting review');
    expect(screen.queryByRole('button', { name: 'Confirm' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Dismiss' })).toBeNull();
  });

  it('a finding someone else already decided says so, and the sentence outlives the reload', async () => {
    sessionStorage.setItem('oracle_role', 'broker_owner');
    crmGet.mockResolvedValueOnce(SUMMARY).mockResolvedValue(DECIDED_SUMMARY);
    const message = 'This title finding is already confirmed; only a finding awaiting review can be decided.';
    crmPost.mockRejectedValue(Object.assign(new Error(message), { status: 409 }));
    render(<PortfolioTab />);
    fireEvent.click(await screen.findByRole('button', { name: 'Dismiss' }));
    fireEvent.change(screen.getByLabelText(/Why this record does not apply/), { target: { value: REASON } });
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss finding' }));

    await waitFor(() => expect(crmGet).toHaveBeenCalledTimes(2));
    // The reload dropped the decided finding's row and its form...
    await waitFor(() => expect(screen.queryByLabelText(/Why this record does not apply/)).toBeNull());
    expect(screen.queryByText('Awaiting review')).toBeNull();
    // ...and the server's sentence is still on screen until the user closes it.
    const [alert] = screen.getAllByRole('alert');
    expect(alert.textContent).toContain(message);
    expect(alert.textContent).toContain('lien on DE-NCC-0142');
    fireEvent.click(screen.getByRole('button', { name: 'Close' }));
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('a failure that is not a conflict stays in the open form', async () => {
    sessionStorage.setItem('oracle_role', 'broker_owner');
    crmPost.mockRejectedValue(Object.assign(new Error('Service unavailable'), { status: 503 }));
    render(<PortfolioTab />);
    fireEvent.click(await screen.findByRole('button', { name: 'Dismiss' }));
    fireEvent.change(screen.getByLabelText(/Why this record does not apply/), { target: { value: REASON } });
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss finding' }));

    await screen.findByRole('alert');
    expect(crmGet).toHaveBeenCalledTimes(1);
    expect(screen.getByLabelText(/Why this record does not apply/)).toBeTruthy();
  });
});
