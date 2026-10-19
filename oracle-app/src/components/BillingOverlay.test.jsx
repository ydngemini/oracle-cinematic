// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import overlaySource from './BillingOverlay.jsx?raw';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const api = vi.hoisted(() => ({
  apiGet: vi.fn(),
  apiPost: vi.fn(),
  ApiError: class ApiError extends Error {
    constructor(message, status) { super(message); this.status = status; }
  },
}));
vi.mock('../lib/apiClient', () => api);
// What /billing/status said; each test sets the state it is about.
const sub = vi.hoisted(() => ({ current: null }));
vi.mock('../state/useSubscription', () => ({
  useSubscription: () => sub.current,
}));

const { BillingOverlay } = await import('./BillingOverlay');
const { formatPlanPrice } = await import('../lib/planPrice');

function subscription(status, active = false, { everEntitled = false, openPortal = vi.fn() } = {}) {
  sub.current = {
    active, status, everEntitled, loading: false, error: null,
    refresh: vi.fn(), openPortal, tenantId: 't1',
  };
  return sub.current;
}

beforeEach(() => {
  subscription('none');
  window.sessionStorage.setItem('oracle_role', 'broker_owner');
});
afterEach(() => { cleanup(); vi.clearAllMocks(); window.sessionStorage.clear(); });

describe('BillingOverlay plan price', () => {
  it('shows the price checkout actually bills, from the backend', async () => {
    api.apiGet.mockResolvedValueOnce({ name: 'Neoh Brokerage', amount_cents: 199900, currency: 'usd', interval: 'month', source: 'stripe' });
    render(<BillingOverlay />);
    expect(await screen.findByText('$1,999')).toBeTruthy();
    expect(screen.getByText('per month')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Start Neoh Brokerage' })).toBeTruthy();
    expect(api.apiGet).toHaveBeenCalledWith('/billing/plan');
  });

  it('never guesses a price it cannot read', async () => {
    api.apiGet.mockResolvedValueOnce({ name: 'Neoh', source: 'unavailable' });
    render(<BillingOverlay />);
    expect(await screen.findByText('Price shown at secure checkout')).toBeTruthy();
    expect(document.body.textContent).not.toMatch(/\$\d/);
  });

  it('says billing is not set up, and does not offer checkout, when the deployment has no price', async () => {
    api.apiGet.mockResolvedValueOnce({ name: 'Neoh', source: 'unavailable', reason: 'not_configured' });
    render(<BillingOverlay />);
    expect(await screen.findByText("Billing isn't set up on this deployment yet")).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Start Neoh' }).disabled).toBe(true);
    expect(document.body.textContent).not.toMatch(/temporarily/i);
  });

  it('keeps checkout available when only the price lookup failed', async () => {
    api.apiGet.mockResolvedValueOnce({ name: 'Neoh', source: 'unavailable', reason: 'lookup_failed' });
    render(<BillingOverlay />);
    expect(await screen.findByText('Price shown at secure checkout')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Start Neoh' }).disabled).toBe(false);
  });

  it('formats intervals and odd cents', () => {
    expect(formatPlanPrice({ source: 'stripe', amount_cents: 29950, currency: 'usd', interval: 'year', interval_count: 1 }))
      .toEqual({ amount: '$299.50', period: 'per year' });
    expect(formatPlanPrice({ source: 'unavailable' })).toBeNull();
  });

  it('holds no price, seat or solo-plan literal in the component', () => {
    expect(overlaySource).not.toMatch(/\$\d|Solo Premium|Named User/);
  });
});

describe('BillingOverlay way forward for a plan that is not paid up', () => {
  // Checkout refuses a second plan while one may still bill (T18.05), and the
  // overlay is modal with no close path: the billing portal must be on it.
  it.each(['unpaid', 'incomplete', 'paused'])('offers the billing portal when the plan is %s', async (status) => {
    const { openPortal } = subscription(status, false, { openPortal: vi.fn().mockResolvedValue({ ok: true }) });
    api.apiGet.mockResolvedValueOnce({ name: 'Neoh', source: 'unavailable', reason: 'lookup_failed' });
    render(<BillingOverlay />);
    const portal = await screen.findByRole('button', { name: 'Manage billing' });
    expect(screen.getByText(/payment isn.t settled|first payment hasn.t settled/)).toBeTruthy();
    fireEvent.click(portal);
    expect(openPortal).toHaveBeenCalledTimes(1);
  });

  it('turns a refused checkout (409) into the billing portal instead of a dead end', async () => {
    const { openPortal } = subscription('none', false, { openPortal: vi.fn().mockResolvedValue({ ok: true }) });
    api.apiGet.mockResolvedValueOnce({ name: 'Neoh', source: 'unavailable', reason: 'lookup_failed' });
    api.apiPost.mockRejectedValueOnce(new api.ApiError(
      'This brokerage already has a plan whose payment is not settled. Open Manage billing to check the payment or change the payment method.',
      409,
    ));
    render(<BillingOverlay />);
    expect(screen.queryByRole('button', { name: 'Manage billing' })).toBeNull();
    fireEvent.click(await screen.findByRole('button', { name: 'Start Neoh' }));
    const portal = await screen.findByRole('button', { name: 'Manage billing' });
    expect(screen.getByRole('alert').textContent).toMatch(/Open Manage billing/);
    fireEvent.click(portal);
    expect(openPortal).toHaveBeenCalledTimes(1);
  });

  // 'incomplete' is a first payment Stripe has not settled: a bank debit can
  // take days, so the card is not necessarily the problem.
  it('does not tell an owner whose first payment is processing to fix the card', async () => {
    subscription('incomplete');
    api.apiGet.mockResolvedValueOnce({ name: 'Neoh', source: 'unavailable', reason: 'lookup_failed' });
    render(<BillingOverlay />);
    await screen.findByRole('button', { name: 'Manage billing' });
    expect(screen.getByText(/first payment hasn.t settled/)).toBeTruthy();
    expect(document.body.textContent).not.toMatch(/update the card/);
  });

  // A 409 can also mean the status this page read is stale (a plan went
  // active elsewhere): re-read it, so an entitled brokerage is let in.
  it('re-reads the subscription after a refused checkout', async () => {
    const state = subscription('none', false, { openPortal: vi.fn().mockResolvedValue({ ok: true }) });
    api.apiGet.mockResolvedValueOnce({ name: 'Neoh', source: 'unavailable', reason: 'lookup_failed' });
    api.apiPost.mockRejectedValueOnce(new api.ApiError('This brokerage already has an active plan.', 409));
    render(<BillingOverlay />);
    fireEvent.click(await screen.findByRole('button', { name: 'Start Neoh' }));
    await screen.findByRole('alert');
    expect(state.refresh).toHaveBeenCalledTimes(1);
  });

  it('does not offer the portal to a brokerage with no plan at all', async () => {
    api.apiGet.mockResolvedValueOnce({ name: 'Neoh', source: 'unavailable', reason: 'lookup_failed' });
    render(<BillingOverlay />);
    await screen.findByRole('button', { name: 'Start Neoh' });
    expect(screen.queryByRole('button', { name: 'Manage billing' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Reactivate via Portal' })).toBeNull();
  });
});

// AC18.07 / F18.07: a failed or ended payment never locks a brokerage out of
// its own records. A lapsed subscription can still read and export; the
// server gate (billing.require_active_subscription) keeps refusing the
// billable outbound actions. A brokerage that never paid stays at checkout.
describe('BillingOverlay when a paid subscription lapses', () => {
  beforeEach(() => {
    api.apiGet.mockResolvedValue({ name: 'Neoh', source: 'unavailable' });
  });

  it.each(['canceled', 'unpaid', 'paused'])('%s: the owner can export, or continue to the records', async (status) => {
    subscription(status);
    const onLockedChange = vi.fn();
    const onOpenAccountData = vi.fn();
    render(<BillingOverlay onLockedChange={onLockedChange} onOpenAccountData={onOpenAccountData} />);
    const dialog = screen.getByRole('dialog');
    expect(dialog.textContent).toMatch(/records are still here/i);
    expect(dialog.textContent).toMatch(/email, texts and calls/i);

    fireEvent.click(screen.getByRole('button', { name: 'Export your data' }));
    expect(onOpenAccountData).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(onLockedChange).toHaveBeenLastCalledWith(false);
    // Not modal any more: nothing over the shell, the page scrolls again, and
    // a quiet notice keeps the state and the way back in view.
    expect(document.body.style.overflow).toBe('');
    const notice = screen.getByRole('region', { name: 'Subscription inactive' });
    expect(notice.textContent).toMatch(/can.t be sent/i);
  });

  // email_outbox.deliver fails a queued email for good, and the command
  // worker dead-letters a queued send: nothing waits for reactivation. The
  // copy must not promise that it does.
  it('says sends due while inactive are cancelled, not paused', () => {
    subscription('canceled');
    render(<BillingOverlay onOpenAccountData={vi.fn()} />);
    const dialog = screen.getByRole('dialog');
    expect(dialog.textContent).not.toMatch(/paused/i);
    expect(dialog.textContent).toMatch(/cancelled, not held/i);
    expect(dialog.textContent).toMatch(/send it again/i);

    fireEvent.click(screen.getByRole('button', { name: 'Continue to your records' }));
    const notice = screen.getByRole('region', { name: 'Subscription inactive' });
    expect(notice.textContent).not.toMatch(/paused/i);
    expect(notice.textContent).toMatch(/cancelled, not held/i);
  });

  // A mission's due send is never released while inactive: it stays a draft
  // awaiting approval (commands_api.release_command refuses, the mission
  // leaves it staged). Sending it again by hand and then approving the draft
  // would reach the client twice, so the copy names the exception.
  it('says a draft awaiting approval stays a draft, so it is not sent twice', () => {
    subscription('canceled');
    render(<BillingOverlay />);
    expect(screen.getByRole('dialog').textContent).toMatch(/awaiting approval stays a draft/i);
    fireEvent.click(screen.getByRole('button', { name: 'Continue to your records' }));
    expect(screen.getByRole('region', { name: 'Subscription inactive' }).textContent)
      .toMatch(/awaiting approval stays a draft/i);
  });

  // A paid brokerage that checks out again by bank debit: the new
  // subscription is 'incomplete' while the debit settles and
  // 'incomplete_expired' if it fails. It has records; it is lapsed.
  it.each(['incomplete', 'incomplete_expired'])('%s after a paid subscription: lapsed, not never-paid', (status) => {
    subscription(status, false, { everEntitled: true });
    const onOpenAccountData = vi.fn();
    render(<BillingOverlay onOpenAccountData={onOpenAccountData} />);
    expect(screen.getByRole('dialog').textContent).toMatch(/records are still here/i);
    fireEvent.click(screen.getByRole('button', { name: 'Export your data' }));
    expect(onOpenAccountData).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(screen.getByRole('region', { name: 'Subscription inactive' })).toBeTruthy();
  });

  it('continuing to the records unlocks the shell, and the notice reopens the options', async () => {
    subscription('canceled');
    const onLockedChange = vi.fn();
    render(<BillingOverlay onLockedChange={onLockedChange} />);
    expect(onLockedChange).toHaveBeenLastCalledWith(true);

    fireEvent.click(screen.getByRole('button', { name: 'Continue to your records' }));
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(onLockedChange).toHaveBeenLastCalledWith(false);

    fireEvent.click(screen.getByRole('button', { name: 'Reactivate' }));
    expect(screen.getByRole('dialog')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Reactivate via Portal' })).toBeTruthy();
  });

  it('an agent can continue but is told the owner exports', () => {
    window.sessionStorage.setItem('oracle_role', 'agent');
    subscription('canceled');
    render(<BillingOverlay />);
    expect(screen.queryByRole('button', { name: 'Export your data' })).toBeNull();
    expect(screen.getByRole('dialog').textContent).toMatch(/owner can export/i);
    expect(screen.getByRole('button', { name: 'Continue to your records' })).toBeTruthy();
  });

  it.each(['none', 'incomplete', 'incomplete_expired'])('%s (never paid): stays at checkout', (status) => {
    subscription(status);
    render(<BillingOverlay />);
    expect(screen.getByRole('dialog').getAttribute('aria-modal')).toBe('true');
    expect(screen.queryByRole('button', { name: 'Continue to your records' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Export your data' })).toBeNull();
  });

  it('past_due (Stripe still retrying) is entitled: nothing covers the workspace', () => {
    subscription('past_due', true);
    const onLockedChange = vi.fn();
    render(<BillingOverlay onLockedChange={onLockedChange} />);
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(onLockedChange).toHaveBeenLastCalledWith(false);
  });
});
