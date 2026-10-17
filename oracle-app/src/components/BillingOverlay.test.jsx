// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import overlaySource from './BillingOverlay.jsx?raw';
import { afterEach, describe, expect, it, vi } from 'vitest';

const api = vi.hoisted(() => ({
  apiGet: vi.fn(),
  apiPost: vi.fn(),
  ApiError: class ApiError extends Error {
    constructor(message, status) { super(message); this.status = status; }
  },
}));
vi.mock('../lib/apiClient', () => api);
const sub = vi.hoisted(() => ({ status: 'none', openPortal: null }));
vi.mock('../state/useSubscription', () => ({
  useSubscription: () => ({
    active: false, status: sub.status, loading: false, error: null,
    refresh: vi.fn(), openPortal: sub.openPortal, tenantId: 't1',
  }),
}));

const { BillingOverlay } = await import('./BillingOverlay');
const { formatPlanPrice } = await import('../lib/planPrice');

afterEach(() => { cleanup(); vi.clearAllMocks(); sub.status = 'none'; sub.openPortal = null; });

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
    sub.status = status;
    sub.openPortal = vi.fn().mockResolvedValue({ ok: true });
    api.apiGet.mockResolvedValueOnce({ name: 'Neoh', source: 'unavailable', reason: 'lookup_failed' });
    render(<BillingOverlay />);
    const portal = await screen.findByRole('button', { name: 'Manage billing' });
    expect(screen.getByText(/payment isn.t settled/)).toBeTruthy();
    fireEvent.click(portal);
    expect(sub.openPortal).toHaveBeenCalledTimes(1);
  });

  it('turns a refused checkout (409) into the billing portal instead of a dead end', async () => {
    sub.status = 'none';
    sub.openPortal = vi.fn().mockResolvedValue({ ok: true });
    api.apiGet.mockResolvedValueOnce({ name: 'Neoh', source: 'unavailable', reason: 'lookup_failed' });
    api.apiPost.mockRejectedValueOnce(new api.ApiError(
      'This brokerage already has a plan whose payment is not settled. Open Manage billing to update the card or review the invoice.',
      409,
    ));
    render(<BillingOverlay />);
    expect(screen.queryByRole('button', { name: 'Manage billing' })).toBeNull();
    fireEvent.click(await screen.findByRole('button', { name: 'Start Neoh' }));
    const portal = await screen.findByRole('button', { name: 'Manage billing' });
    expect(screen.getByRole('alert').textContent).toMatch(/Open Manage billing/);
    fireEvent.click(portal);
    expect(sub.openPortal).toHaveBeenCalledTimes(1);
  });

  it('does not offer the portal to a brokerage with no plan at all', async () => {
    api.apiGet.mockResolvedValueOnce({ name: 'Neoh', source: 'unavailable', reason: 'lookup_failed' });
    render(<BillingOverlay />);
    await screen.findByRole('button', { name: 'Start Neoh' });
    expect(screen.queryByRole('button', { name: 'Manage billing' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Reactivate via Portal' })).toBeNull();
  });
});
