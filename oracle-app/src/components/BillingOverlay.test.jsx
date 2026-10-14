// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react';
import overlaySource from './BillingOverlay.jsx?raw';
import { afterEach, describe, expect, it, vi } from 'vitest';

const api = vi.hoisted(() => ({ apiGet: vi.fn(), apiPost: vi.fn() }));
vi.mock('../lib/apiClient', () => ({ ...api, ApiError: class ApiError extends Error {} }));
vi.mock('../state/useSubscription', () => ({
  useSubscription: () => ({
    active: false, status: 'none', loading: false, error: null,
    refresh: vi.fn(), openPortal: vi.fn(), tenantId: 't1',
  }),
}));

const { BillingOverlay } = await import('./BillingOverlay');
const { formatPlanPrice } = await import('../lib/planPrice');

afterEach(() => { cleanup(); vi.clearAllMocks(); });

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
